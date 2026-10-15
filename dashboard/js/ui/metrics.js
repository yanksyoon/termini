/*
 * Resource-utilization charts panel (contract §1.4).
 * Renders chart cards for the selected window; reuses canvases across polls
 * so the projector view never flickers.
 */

import { el, clear } from "./dom.js";
import { TimeSeriesChart } from "../charts.js";
import { buildCharts } from "../core/series.js";
import { scaleForUnit } from "../core/format.js";

export class MetricsPanel {
  constructor(container) {
    this.container = container;
    this.charts = new Map(); // id -> {card, canvas, chart, legend}
    this.lastPayload = null;
    this.includeAll = false;
    this.annotations = []; // decision/threshold marks for the selected node
    this._ro = new ResizeObserver(() => this.redraw());
    this._ro.observe(container);
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
    const title = el("div", { class: "chart-title" }, [
      el("h3", { text: group.title }),
      el("span", { class: "chart-unit", text: scaleForUnit(group.unit).label }),
    ]);
    const caption = el("div", { class: "chart-caption" });
    const card = el("div", { class: "chart-card", dataset: { chart: group.id } }, [title, canvas, legend, caption]);
    const chart = new TimeSeriesChart(canvas);
    return { card, canvas, chart, legend, titleEl: title, captionEl: caption };
  }

  _apply(entry, group) {
    entry.chart.setData(group);
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
    // Fault correlation: state which rule fired here and where the threshold ran,
    // so a viewer reads cause (telemetry) and effect (action) off one chart.
    const markers = group.markers || [];
    const thresholds = group.thresholds || [];
    entry.card.classList.toggle("fault", !!(group.fault && (markers.length || thresholds.length)));
    clear(entry.captionEl);
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

  redraw() {
    for (const [, entry] of this.charts) entry.chart.draw();
  }
}
