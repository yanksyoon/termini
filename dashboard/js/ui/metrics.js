/*
 * Resource-utilization charts panel (contract §1.4).
 * Renders chart cards for the selected window; reuses canvases across polls
 * so the projector view never flickers.
 */

import { el, clear } from "./dom.js";
import { TimeSeriesChart } from "../charts.js";
import { buildCharts, buildSampleView } from "../core/series.js";
import { scaleForUnit, pct, bytes } from "../core/format.js";
import { PROCESSES_MAX } from "../core/health.js";

export class MetricsPanel {
  constructor(container) {
    this.container = container;
    this.charts = new Map(); // id -> {card, canvas, chart, legend}
    this.lastPayload = null;
    this.includeAll = false;
    this.annotations = []; // decision/threshold marks for the selected node
    this.processRows = []; // latest `processes[]` for the selected node (§4.10)
    this._procCard = null;
    this.sample = null; // latest raw telemetry Sample (all groups, nested)
    this._sampleCards = new Map(); // group id -> card
    this._ro = new ResizeObserver(() => this.redraw());
    this._ro.observe(container);
  }

  /**
   * Latest `processes[]` for the selected node (contract §4.10).
   *
   * Process metrics are per-sample, not a time series, so they get their own
   * card rather than a chart line: "what is running inside this node right now"
   * is the question an incident asks, and a chart of the top-10 set over time
   * would answer a different one.
   */
  setProcesses(list) {
    this.processRows = Array.isArray(list) ? list : [];
    this._renderProcesses();
  }

  _renderProcesses() {
    const rows = this.processRows || [];
    if (rows.length === 0) {
      if (this._procCard) {
        this._procCard.remove();
        this._procCard = null;
      }
      return;
    }
    if (!this._procCard) {
      this._procCard = el("div", { class: "chart-card process-card", dataset: { chart: "processes" } });
      this.container.appendChild(this._procCard);
    }
    clear(this._procCard);
    this._procCard.appendChild(
      el("div", { class: "chart-title" }, [
        el("h3", { text: "Top processes" }),
        el("span", { class: "chart-unit", text: `${rows.length} of ${PROCESSES_MAX}` }),
      ]),
    );
    const table = el("div", { class: "process-table" });
    for (const p of rows.slice(0, PROCESSES_MAX)) {
      table.appendChild(
        el("div", { class: "process-row" }, [
          el("span", { class: "ps-name", text: p.name || `pid ${p.pid}`, title: `pid ${p.pid}` }),
          el("span", { class: "ps-state", text: p.state || "" }),
          el("span", { class: "ps-cpu", text: typeof p.cpu_util_pct === "number" ? pct(p.cpu_util_pct) : "—" }),
          el("span", { class: "ps-ram", text: bytes(p.ram_used_bytes) }),
          el("span", { class: "ps-threads", text: typeof p.threads === "number" ? `${p.threads}t` : "" }),
        ]),
      );
    }
    this._procCard.appendChild(table);
  }

  setIncludeAll(v) {
    this.includeAll = !!v;
    if (this.lastPayload) this.update(this.lastPayload);
  }

  /**
   * Annotations for the selected node's charts: `{metric, t, kind, label,
   * threshold:{value,basis,label}}` (raw API units — buildCharts scales them).
   * These are what make cause (telemetry) and effect (action) correlate.
   */
  setAnnotations(list) {
    this.annotations = Array.isArray(list) ? list : [];
    if (this.lastPayload) this.update(this.lastPayload);
  }

  update(payload) {
    this.lastPayload = payload;
    const groups = buildCharts(payload, { includeAll: this.includeAll, annotations: this.annotations });
    const seen = new Set();

    if (groups.length === 0) {
      this._empty(payload);
      return;
    }

    for (const g of groups) {
      seen.add(g.id);
      let entry = this.charts.get(g.id);
      if (!entry) {
        entry = this._create(g);
        this.charts.set(g.id, entry);
        this.container.appendChild(entry.card);
      }
      this._apply(entry, g);
    }

    // drop charts no longer present
    for (const [id, entry] of this.charts) {
      if (!seen.has(id)) {
        entry.chart.destroy();
        entry.card.remove();
        this.charts.delete(id);
      }
    }
    this._clearPlaceholder();
  }

  _empty(payload) {
    for (const [, entry] of this.charts) {
      entry.chart.destroy();
      entry.card.remove();
    }
    this.charts.clear();
    this._clearPlaceholder();
    const hasSeries = payload && Array.isArray(payload.series) && payload.series.length > 0;
    const note = hasSeries
      ? "No data points in this window yet — widen the window or wait for telemetry."
      : "No metrics for this node yet.";
    this.container.appendChild(el("div", { class: "empty", dataset: { placeholder: "1" }, text: note }));
  }

  _clearPlaceholder() {
    const ph = this.container.querySelector('[data-placeholder="1"]');
    if (ph) ph.remove();
  }

  _create(group) {
    const canvas = el("canvas", { class: "chart-canvas" });
    const legend = el("div", { class: "chart-legend" });
    const chip = el("span", { class: `chart-chip ${group.basis === "derived" ? "derived" : "raw"}` });
    const title = el("div", { class: "chart-title" }, [
      el("h3", { text: group.title }),
      chip,
      el("span", { class: "chart-unit", text: scaleForUnit(group.unit).label }),
    ]);
    const caption = el("div", { class: "chart-caption" });
    const card = el("div", { class: "chart-card", dataset: { chart: group.id } }, [title, canvas, legend, caption]);
    const chart = new TimeSeriesChart(canvas);
    return { card, canvas, chart, legend, titleEl: title, captionEl: caption, chipEl: chip };
  }

  _apply(entry, group) {
    entry.chart.setData(group);
    // Derived vs raw, stated in the title/legend: a bucket mean or a `disk[0]`
    // first-element mapping is an AGGREGATE, and a chart that stays silent about
    // that claims the line is the node's raw value. Say which it is.
    clear(entry.legend);
    group.series.forEach((s, i) => {
      const color = entry.chart.colorOf(i);
      entry.legend.appendChild(
        el("span", { class: "li" }, [
          el("span", { class: "sw", style: `background:${color}` }),
          el("span", { text: s.label }),
        ]),
      );
    });
    entry.chipEl.textContent = group.basis === "derived" ? "derived" : "raw";
    entry.chipEl.className = `chart-chip ${group.basis === "derived" ? "derived" : "raw"}`;
    // Fault correlation: state which rule fired here and where the threshold ran,
    // so a viewer reads cause (telemetry) and effect (action) off one chart.
    const markers = group.markers || [];
    const thresholds = group.thresholds || [];
    entry.card.classList.toggle("fault", !!(group.fault && (markers.length || thresholds.length)));
    clear(entry.captionEl);
    entry.captionEl.appendChild(
      el("span", { class: "chart-basis", text: group.aggregateLabel || group.basisLabel || "" }),
    );
    if (markers.length || thresholds.length) {
      const labels = Array.from(new Set(markers.map((m) => m.label))).slice(0, 3).join(", ");
      const bits = [];
      if (labels) bits.push(`decision${markers.length > 1 ? "s" : ""}: ${labels}`);
      if (thresholds.length) bits.push(`threshold: ${thresholds.map((t) => t.label).join(" · ")}`);
      entry.captionEl.appendChild(el("span", { class: "chart-fault", text: bits.join("  |  ") }));
    }
    // make sure the canvas has a size before first draw
    const c = entry.canvas;
    if (c.clientHeight === 0) c.style.height = "150px";
    entry.chart.draw();
  }

  /**
   * The metric-expansion groups (§4.12–§4.14) from the node's LATEST RAW SAMPLE.
   *
   * These are per-sample facts, not time series, so they are cards of values —
   * the same shape the `processes[]` card uses, for the same reason: "what is
   * this node reporting right now" is a different question from "how did it
   * move". A group the node does not report (an unreadable source, or a role
   * that does not have it) renders as NOT REPORTED — never as a flat zero, and
   * never as an empty card. Each value says whether it is raw or derived, and a
   * cumulative counter says so, so a rising count is not read as a rate.
   */
  setSample(sample) {
    this.sample = sample && typeof sample === "object" ? sample : null;
    this._renderSample();
  }

  _renderSample() {
    const view = this.sample ? buildSampleView(this.sample) : [];
    const seen = new Set();
    for (const g of view) {
      seen.add(g.id);
      let card = this._sampleCards.get(g.id);
      if (!card) {
        card = el("div", { class: "chart-card sample-card", dataset: { chart: `sample-${g.id}` } });
        this._sampleCards.set(g.id, card);
        this.container.appendChild(card);
      }
      clear(card);
      card.appendChild(
        el("div", { class: "chart-title" }, [
          el("h3", { text: g.title }),
          el("span", { class: "chart-chip", text: "raw sample" }),
          g.rank ? el("span", { class: "chart-chip", text: `rank ${g.rank}` }) : null,
        ]),
      );
      if (g.reportedCount === 0) {
        // Absent ≠ zero: the whole group is unreadable/not present on this node.
        card.appendChild(el("div", { class: "sample-note nr", text: "not reported — no §4.12–§4.14 fields on this node's latest sample (absent, not zero)" }));
      } else {
        card.appendChild(this._sampleTable(g));
      }
      const bits = [g.basisLabel];
      if (g.rule) bits.push(`leading indicator for ${g.rule}`);
      if (g.entryCount !== null) {
        bits.push(`${g.entryCount} ${g.entryCount === 1 ? "entry" : "entries"}${g.truncated ? ` (${g.truncated} more not shown, cap ${g.maxEntries})` : ""}`);
      }
      if (g.missingCount > 0 && g.reportedCount > 0) bits.push(`${g.missingCount} of ${g.rows.length} values not reported`);
      card.appendChild(el("div", { class: "chart-caption" }, [el("span", { class: "sample-basis", text: bits.join("  |  ") })]));
    }
    for (const [id, card] of this._sampleCards) {
      if (!seen.has(id)) {
        card.remove();
        this._sampleCards.delete(id);
      }
    }
  }

  _sampleTable(g) {
    const table = el("div", { class: "sample-table" });
    for (const r of g.rows) {
      const flags = [];
      if (r.cumulative) flags.push("cumulative");
      if (r.derived) flags.push(`derived: ${r.derivedFrom}`);
      table.appendChild(
        el("div", { class: "sample-row" }, [
          el("span", { class: "sr-where", text: r.entry || "" }),
          el("span", { class: "sr-label", text: r.label }),
          el("span", { class: `sr-value${r.reported ? "" : " nr"}`, text: r.reported ? r.display : "not reported" }),
          el("span", { class: "sr-unit", text: r.reported ? r.unitLabel : "" }),
          el("span", { class: "sr-flags", text: flags.join(" · ") }),
        ]),
      );
    }
    return table;
  }

  redraw() {
    for (const [, entry] of this.charts) entry.chart.draw();
  }
}