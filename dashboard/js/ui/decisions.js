/*
 * Decisions panel — one row per operator decision, live.
 *
 * Answers the operator's actual question in one place: which telemetry crossed
 * the threshold, what was decided, by WHICH decider (model vs deterministic
 * fallback), what action hit the fleet, and what the fleet answered.
 *
 * Data: GET /v1/operator/{decisions,events,audits} (polled), joined client-side
 * by `../core/decisions.js`. Every field shown comes from the response — nothing
 * is inferred or hardcoded.
 */

import { el, clear } from "./dom.js";
import { clock, num, shortId } from "../core/format.js";
import { buildDecisionRows, deciderMeta, sourceMeta, DECISION_SOURCES } from "../core/decisions.js";

const FILTER_LABELS = {
  all: "all",
  model: "AI model",
  fallback: "fallback",
  deterministic: "deterministic",
};

export class DecisionsPanel {
  constructor(dom, handlers = {}) {
    this.dom = dom;
    this.handlers = handlers;
    this.filter = "all";
    this.rows = [];
    this.decider = null; // last egress record seen on the operator log stream
    this._buildFilters();
    this._renderDecider();
    this._renderEmpty("Waiting for operator decisions…");
  }

  _buildFilters() {
    if (!this.dom.filterHost) return;
    clear(this.dom.filterHost);
    for (const key of ["all", ...DECISION_SOURCES]) {
      const chip = el("button", {
        type: "button",
        class: `chip ${key === this.filter ? "on" : ""}`,
        text: FILTER_LABELS[key] || key,
        dataset: { decisionFilter: key },
        "aria-pressed": String(key === this.filter),
      });
      chip.addEventListener("click", () => {
        this.filter = key;
        for (const c of this.dom.filterHost.children) {
          const on = c.dataset.decisionFilter === key;
          c.classList.toggle("on", on);
          c.setAttribute("aria-pressed", String(on));
        }
        this.render();
      });
      this.dom.filterHost.appendChild(chip);
    }
  }

  /**
   * The decider label. Takes the egress record parsed out of the operator log
   * stream (externalModelCall …) so the dashboard states who answered instead of
   * implying local GPU inference.
   */
  setDecider(record) {
    this.decider = record;
    this._renderDecider();
  }

  _renderDecider() {
    const host = this.dom.deciderEl;
    if (!host) return;
    const meta = deciderMeta(this.decider);
    clear(host);
    host.className = `decider decider-${meta.kind}`;
    host.appendChild(el("span", { class: "decider-label", text: `decider: ${meta.label}` }));
    if (meta.fields.length) {
      const fields = meta.fields
        .map(([k, v]) => `${k}=${v}`)
        .join(" · ");
      host.appendChild(el("span", { class: "decider-fields", text: fields }));
    }
    host.appendChild(el("span", { class: "decider-note", text: meta.detail }));
  }

  setNodes(nodes) {
    this.nodes = nodes || [];
  }

  nodeLabel(nodeId) {
    const n = (this.nodes || []).find((x) => x.node_id === nodeId);
    if (!n) return null;
    return { text: n.registration_id || shortId(nodeId), synthetic: !!n.synthetic };
  }

  update({ decisions, events, audits, seriesByNode } = {}) {
    this.rows = buildDecisionRows(
      { decisions, events, audits, seriesByNode },
      { auditWindow: (audits || []).length },
    );
    this.render();
    if (this.dom.updatedEl) {
      this.dom.updatedEl.textContent = `updated ${new Date().toLocaleTimeString("en-GB", { hour12: false })}`;
    }
  }

  /** Rows currently visible after the source filter (used for chart markers). */
  visibleRows() {
    return this.rows.filter((r) => this.filter === "all" || r.source === this.filter);
  }

  /**
   * The controller answered, but not with a decision — say why instead of
   * rendering an empty panel as if nothing had been decided.
   */
  setUnavailable(err) {
    const code = err && err.code ? err.code : "ERROR";
    const msg = err && err.message ? err.message : String(err);
    this.rows = [];
    this.render();
    this._renderEmpty(`Decision feed unavailable (${code}): ${msg}`);
    if (this.dom.countEl) this.dom.countEl.textContent = "0 decisions";
  }

  render() {
    const host = this.dom.list;
    if (!host) return;
    const rows = this.visibleRows();
    clear(host);
    if (this.rows.length === 0) {
      this._renderEmpty("No decisions recorded yet — the operator decides only when telemetry crosses a frozen threshold.");
      this._updateCount(0);
      return;
    }
    if (rows.length === 0) {
      this._renderEmpty(`No ${FILTER_LABELS[this.filter] || this.filter} decisions in the fetched window.`);
      this._updateCount(0);
      return;
    }
    this.dom.emptyEl.hidden = true;
    const frag = document.createDocumentFragment();
    for (const r of rows) frag.appendChild(this._row(r));
    host.appendChild(frag);
    this._updateCount(rows.length);
  }

  _renderEmpty(text) {
    if (this.dom.emptyEl) {
      this.dom.emptyEl.hidden = false;
      this.dom.emptyEl.textContent = text;
    }
  }

  _updateCount(shown) {
    if (this.dom.countEl) {
      const model = this.rows.filter((r) => r.source === "model").length;
      const fb = this.rows.filter((r) => r.source === "fallback").length;
      const withRecord = this.rows.filter((r) => r.incident).length;
      const fromModel = this.rows.filter((r) => r.incidentSource === "model").length;
      const fromController = this.rows.filter((r) => r.incidentSource === "fallback").length;
      const unlabelled = this.rows.filter((r) => r.incident && r.incidentSource === null).length;
      const parts = [
        `${shown} shown / ${this.rows.length} decisions`,
        `model ${model}`,
        `fallback ${fb}`,
        `incident records ${withRecord} (AI ${fromModel} · controller-generated ${fromController})`,
      ];
      // A record with no provenance is a bug; it is counted out loud, not hidden
      // inside the total.
      if (unlabelled) parts.push(`${unlabelled} with no provenance`);
      this.dom.countEl.textContent = parts.join(" · ");
    }
  }

  _row(r) {
    const meta = r.sourceMeta;
    const node = this.nodeLabel(r.nodeId);
    const head = el("div", { class: "decision-head" }, [
      el("span", { class: "decision-time", text: r.decidedAt ? clock(r.decidedAt) : "—" }),
      el("span", {
        class: `badge src-${meta.tone}`,
        text: meta.byModel ? `AI MODEL${r.confidence !== null && r.confidence !== undefined ? ` ${num(r.confidence, 2)}` : ""}` : meta.label.toUpperCase(),
        title: meta.note,
      }),
      el("span", {
        class: "decision-node",
        text: node ? `${node.text}${node.synthetic ? " (demo)" : ""}` : shortId(r.nodeId),
        title: r.nodeId || "",
      }),
      el("span", { class: "decision-rule", text: r.ruleId || "—" }),
      el("span", { class: "decision-arrow", text: "→" }),
      el("span", { class: "decision-action" }, [
        el("strong", { text: r.actionKind || "no action" }),
        r.target ? el("span", { class: "decision-target", text: ` ${r.target}` }) : null,
      ]),
      this._resultBadge(r),
    ]);

    const breach = r.breach
      ? el("div", { class: "decision-breach" }, [
          el("span", { class: "breach-label", text: "breach" }),
          el("span", {
            class: "breach-metric",
            title: `${r.breach.metricLabel || ""} (${r.breach.metric || ""})`,
            text: r.breach.metric || "—",
          }),
          el("span", { class: "breach-metric-label muted", text: r.breach.metricLabel || "" }),
          el("span", { class: "breach-eq", text: "=" }),
          el("span", { class: "breach-value", text: r.breach.valueText }),
          r.breach.threshold ? el("span", { class: "breach-threshold", text: `threshold: ${r.breach.threshold}` }) : null,
          r.breach.firedAt ? el("span", { class: "breach-fired", text: `fired ${clock(r.breach.firedAt)}` }) : null,
        ])
      : el("div", { class: "decision-breach muted", text: "health event not in the fetched window (the decision row is still the API's)" });

    const rationale = el("div", { class: `decision-rationale ${meta.byModel ? "" : "not-model"}` }, [
      el("span", { class: "rat-label", text: meta.byModel ? "model rationale" : meta.key === "fallback" ? "fallback reason" : "playbook rationale" }),
      el("span", {
        class: "rat-text",
        text: meta.byModel
          ? r.rationale || "(the model returned no rationale)"
          : meta.key === "fallback"
            ? `${r.fallbackReason || "unknown"} — ${r.rationale || ""}`.trim()
            : r.rationale || "—",
      }),
    ]);

    const links = el("div", { class: "decision-links" }, [
      el("span", { class: "audit" }, [
        el("span", { class: "muted", text: "audit " }),
        el("button", {
          type: "button",
          class: "linklike",
          text: r.auditId ? shortId(r.auditId, 10, 4) : r.auditMissing ? "not in fetched window" : "none recorded",
          title: r.auditId || "",
          disabled: r.auditId ? null : "disabled",
          onclick: () => this.handlers.onAudit && this.handlers.onAudit(r),
        }),
      ]),
      r.eventId
        ? el("button", {
            type: "button",
            class: "linklike",
            text: "telemetry sample",
            title: `health event ${r.eventId}`,
            onclick: () => this.handlers.onSample && this.handlers.onSample(r),
          })
        : null,
      r.breach && r.breach.metric
        ? el("button", {
            type: "button",
            class: "linklike",
            text: "show on chart",
            title: `plot ${r.breach.metric} for this node`,
            onclick: () => this.handlers.onChart && this.handlers.onChart(r),
          })
        : null,
      el("span", { class: "decision-id muted", text: `decision ${shortId(r.id, 10, 4)}` }),
    ]);

    return el("li", { class: `decision-row src-${meta.tone}`, dataset: { decisionId: r.id, source: r.source } }, [
      head,
      this._incidentCard(r),
      breach,
      rationale,
      links,
    ]);
  }

  /**
   * The §4.2 incident card — the primary surface. Answers the four questions the
   * log stream cannot: what happened (title), why the operator thinks so
   * (explanation), on what evidence (the telemetry field list), and what it did
   * (actions DECIDED) versus what the fleet answered (EXECUTED, from the audit
   * join). Provenance is stated on every card: model output and the §8.1
   * controller-generated fallback are never rendered alike, and a record that
   * arrives with no provenance is shown as unlabelled — a bug, not a style.
   */
  _incidentCard(r) {
    if (!r.incident) {
      // Deterministic mode writes no record at all (§8.1). Say so: an empty card
      // would assert an incident the controller never recorded.
      return el("div", { class: "incident-none", dataset: { incidentSource: "none" } }, [
        el("span", { class: "badge inc-none-badge", text: "incident record" }),
        el("span", { class: "incident-none-text", text: r.noIncidentReason || "no incident record" }),
      ]);
    }

    const inc = r.incident;
    const meta = inc.meta;

    const head = el("div", { class: "incident-head" }, [
      el("span", {
        class: `badge inc-badge inc-${meta.tone}`,
        text: meta.badge,
        title: meta.note,
        dataset: { incidentBadge: meta.key },
      }),
      inc.generated === null
        ? el("span", { class: "incident-prov incident-prov-null", text: "incident_generated: NULL" })
        : el("span", { class: "incident-prov muted", text: `incident_generated: "${inc.generated}"` }),
      el("span", { class: "muted small incident-prov-note", text: meta.label }),
    ]);

    const title = el("h3", {
      class: "incident-title",
      text: inc.title || "(the record carries no title)",
      title: inc.title || "",
    });

    const explanation = el("p", {
      class: "incident-explanation",
      text: inc.explanation || "(the record carries no explanation)",
    });

    const evidence = el("div", { class: "incident-block incident-evidence" }, [
      el("div", {
        class: "inc-label",
        text: `evidence · ${inc.evidence.length} telemetry field${inc.evidence.length === 1 ? "" : "s"} quoted`,
      }),
      inc.evidence.length
        ? el(
            "ul",
            { class: "inc-list inc-evidence-list" },
            inc.evidence.map((e) =>
              el("li", { class: "inc-evidence-row" }, [
                el("code", { class: "inc-field", text: e.field }),
                el("span", { class: "inc-eq", text: "=" }),
                el("span", { class: "inc-value", text: e.valueText }),
                e.seq === undefined ? null : el("span", { class: "inc-seq muted", text: `seq ${e.seq}` }),
                e.ts ? el("span", { class: "inc-ts muted", text: clock(e.ts), title: e.ts }) : null,
              ]),
            ),
          )
        : el("div", { class: "muted small", text: "the record quotes no telemetry fields" }),
    ]);

    const decided = el("div", { class: "incident-block incident-decided" }, [
      el("div", {
        class: "inc-label",
        text: `decided · ${inc.actions.length} action${inc.actions.length === 1 ? "" : "s"} named in the record (a decision, not an outcome)`,
      }),
      inc.actions.length
        ? el(
            "ul",
            { class: "inc-list inc-decided-list" },
            inc.actions.map((a) =>
              el("li", { class: "inc-decided-row" }, [
                el("code", { class: "inc-kind", text: a.action_kind }),
                a.target ? el("span", { class: "inc-target", text: a.target }) : el("span", { class: "muted", text: "(no target in the record)" }),
                a.note ? el("span", { class: "inc-note", text: a.note }) : null,
              ]),
            ),
          )
        : el("div", { class: "muted small", text: "the record names no action" }),
    ]);

    const executed = el("div", { class: `incident-block incident-executed ${r.execution ? "" : "incident-executed-none"}` }, [
      el("div", { class: "inc-label", text: "executed · what the fleet answered (remediation_audits, joined by decision id)" }),
      this._executedList(r),
    ]);

    const cls = `incident-card inc-${meta.tone}`;
    return el("article", { class: cls, dataset: { incidentSource: inc.generated === null ? "null" : inc.generated } }, [
      head,
      title,
      explanation,
      evidence,
      decided,
      executed,
    ]);
  }

  /**
   * The EXECUTED half. Every fact here comes from the audit row the controller
   * wrote when the executor ran (result / detail / endpoint / idempotency key /
   * audit id); when there is no audit row the card says so and claims nothing.
   */
  _executedList(r) {
    const ex = r.execution;
    if (!ex) {
      const why = r.auditMissing
        ? "no audit row for this decision in the fetched window — nothing here claims it ran"
        : "the controller recorded no audit row for this decision";
      return el("ul", { class: "inc-list inc-executed-list" }, [
        el("li", { class: "inc-executed-row muted" }, [
          el("span", { class: "badge exec-none", text: "not executed" }),
          el("span", { text: why }),
        ]),
      ]);
    }
    const bits = [
      el("span", { class: `badge exec-${ex.ok ? "ok" : "err"}`, text: ex.dryRun ? `dry-run · ${ex.result}` : ex.result }),
      ex.endpoint ? el("span", { class: "inc-endpoint", text: hostOf(ex.endpoint), title: ex.endpoint }) : null,
      ex.method || ex.path ? el("code", { class: "inc-call", text: `${ex.method || ""} ${ex.path || ""}`.trim() }) : null,
    ].filter(Boolean);
    const items = [el("li", { class: "inc-executed-row" }, bits)];
    if (ex.idempotencyKey) {
      items.push(el("li", { class: "inc-idem muted", text: `idempotency_key ${ex.idempotencyKey}` }));
    }
    if (ex.detail) items.push(el("li", { class: "inc-detail", text: ex.detail }));
    items.push(
      el("li", { class: "inc-at muted" }, [
        el("span", { text: ex.at ? `executed ${clock(ex.at)}` : "execution time not recorded" }),
        el("span", { text: ` · audit ${r.auditId ? shortId(r.auditId, 10, 4) : "not in the fetched window"}`, title: r.auditId || "" }),
      ]),
    );
    return el("ul", { class: "inc-list inc-executed-list" }, items);
  }

  _resultBadge(r) {
    const ex = r.execution;
    if (!ex) {
      return el("span", { class: "badge exec-none", text: "not executed", title: "no audit row for this decision in the fetched window" });
    }
    return el("span", {
      class: `badge exec-${ex.ok ? "ok" : "err"}`,
      text: ex.dryRun ? "dry-run" : `${ex.result}${ex.endpoint ? ` · ${hostOf(ex.endpoint)}` : ""}`,
      title: `executed ${ex.at || ""} ${ex.method || ""} ${ex.path || ""}`.trim(),
    });
  }
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return url || "";
  }
}
