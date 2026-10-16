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
      // §4.3 thinking logs: counted the same way, and a trace whose provenance
      // contradicts the row is counted out loud rather than left inside the log.
      const withTrace = this.rows.filter((r) => r.trace && r.trace.present).length;
      const traceModel = this.rows.filter((r) => r.trace && r.trace.present && r.trace.decidedBy === "model").length;
      const traceFallback = this.rows.filter((r) => r.trace && r.trace.present && r.trace.decidedBy === "fallback").length;
      const traceBug = this.rows.filter((r) => r.traceBug).length;
      const parts = [
        `${shown} shown / ${this.rows.length} decisions`,
        `model ${model}`,
        `fallback ${fb}`,
        `incident records ${withRecord} (AI ${fromModel} · controller-generated ${fromController})`,
        `thinking logs ${withTrace} (model ${traceModel} · fallback ${traceFallback})`,
      ];
      // A record with no provenance is a bug; it is counted out loud, not hidden
      // inside the total.
      if (unlabelled) parts.push(`${unlabelled} with no provenance`);
      if (traceBug) parts.push(`${traceBug} trace(s) with a provenance problem`);
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
      // A trace with no incident card to sit in (the model answered but returned
      // none of the §4.2 fields) still belongs on the row: the reasoning steps
      // are the only record of that answer. With a card present, the log is
      // rendered INSIDE it (under the explanation) and not a second time here.
      r.incident ? null : this._thinkingLog(r),
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
      // The reasoning that produced the answer, under the explanation and above
      // the evidence: secondary to the conclusion, but present in the card.
      this._thinkingLog(r),
      evidence,
      decided,
      executed,
    ]);
  }

  /**
   * The §4.3 thinking log — the ordered steps behind the answer.
   *
   * Kept SECONDARY on purpose: one collapsed disclosure, so the card's primary
   * surface stays title / explanation / evidence / resolution. What it must
   * never do is hide a problem behind that collapse, so every step that is not
   * a plain `ok`, every step the mapper withheld, a truncated array, a
   * version/provenance contradiction and the raw model reply each get their own
   * line OUTSIDE the disclosure. The log is a record of what the controller
   * wrote — the panel adds no step, reorders nothing silently, and never lets
   * the model's reply read as the action that ran.
   */
  _thinkingLog(r) {
    const t = r.trace;
    if (!t || !t.present) {
      // No trace recorded. Say exactly that: an empty timeline would assert a
      // reasoning record the controller never wrote.
      return el("div", { class: "incident-trace trace-none", dataset: { tracePresent: "false" } }, [
        el("span", { class: "badge trace-none-badge", text: "thinking log" }),
        el("span", { class: "trace-none-text", text: r.noTraceReason || "no decision trace" }),
      ]);
    }

    const meta = t.decidedByMeta;
    const replyState = !t.reply
      ? "no raw reply recorded"
      : t.reply.withheld
        ? "raw reply withheld (C2) — not executed"
        : t.reply.available
          ? "raw reply recorded — not executed"
          : "no raw reply text retained";
    const summaryText = [
      `thinking log · ${t.shownSteps} step${t.shownSteps === 1 ? "" : "s"}`,
      t.decidedBy ? `decided_by "${t.decidedBy}"` : "decided_by MISSING",
      t.version === null ? "trace_version missing" : `trace_version ${t.version}`,
      replyState,
    ].join(" · ");

    const summary = el("summary", { class: "trace-summary" }, [
      el("span", {
        class: `badge trace-badge trace-${meta.tone}`,
        text: meta.badge,
        title: meta.note,
        dataset: { traceDecider: meta.key },
      }),
      el("span", { class: "trace-summary-text", text: summaryText }),
      el("span", { class: "trace-summary-hint muted", text: "expand for the ordered steps" }),
    ]);

    const body = el("div", { class: "trace-body" }, [
      t.steps.length
        ? el(
            "ol",
            { class: "trace-steps" },
            t.steps.map((s, i) => this._traceStep(s, i)),
          )
        : el("div", { class: "muted small", text: "the trace carries no steps" }),
      this._traceReply(r, t),
    ]);

    const alerts = this._traceAlerts(r, t);
    const children = [];
    if (alerts.length) children.push(el("div", { class: "trace-alerts" }, alerts));
    children.push(
      el("details", {
        class: "trace-details",
        dataset: { tracePresent: "true", traceDecider: meta.key },
      }, [summary, body]),
    );
    return el("div", { class: `incident-trace trace-tone-${meta.tone}` }, children);
  }

  /** One step row: index, time, state chip, name, label, and the detail verbatim. */
  _traceStep(s, i) {
    const state = s.state || "unclassified";
    const chipTitle = s.unclassified
      ? "the trace did not classify this step — a §4.3 violation, shown as unclassified rather than as ok"
      : `state=${s.state}`;
    // The reply step's detail is the raw reply up to the 4096-char bound, so it
    // is rendered as pre-formatted text (scrollable) rather than inline code.
    const detailCap = s.step === "reply_received" ? "reply ≤4096 chars" : "label ≤128 / detail ≤512 chars";
    const detail = s.detail
      ? s.step === "reply_received"
        ? el("pre", { class: "trace-detail trace-detail-reply", text: s.detail, title: s.detail })
        : el("code", { class: "trace-detail", text: s.detail, title: s.detail })
      : null;
    return el(
      "li",
      { class: `trace-step trace-state-${state}`, dataset: { traceStep: s.step, traceState: state } },
      [
        el("span", { class: "trace-idx muted", text: `${i + 1}.` }),
        el("span", { class: "trace-ts muted", text: s.ts ? clock(s.ts) : "no ts", title: s.ts || "" }),
        el("span", { class: `badge trace-chip trace-chip-${state}`, text: state, title: chipTitle }),
        el("code", { class: "trace-name", text: s.step }),
        el("span", { class: "trace-label", text: s.label || "(the step carries no label)" }),
        detail,
        s.truncated
          ? el("span", { class: "badge trace-clip", text: "reply cut at the 4096-char bound" })
          : null,
        s.clamped
          ? el("span", { class: "badge trace-clip", text: `clamped by this panel (${detailCap})` })
          : null,
      ],
    );
  }

  /**
   * Everything the collapsed log must still say out loud: the fallback reason
   * and the gate that rejected the model's answer, any step that is not `ok`,
   * anything the mapper withheld, a truncated array, and any provenance
   * contradiction. Returned as always-visible lines.
   */
  _traceAlerts(r, t) {
    const out = [];

    if (t.decidedBy === "fallback") {
      const reason = (t.fallback && t.fallback.reason) || r.fallbackReason || "unknown";
      const gate = t.fallback && t.fallback.rejectingGate;
      out.push(
        el("div", { class: "trace-alert trace-alert-fallback" }, [
          el("span", { class: "badge inc-fallback", text: "fallback" }),
          el("span", {
            text: `the model did not answer — the controller decided: fallback reason ${reason} · ${
              gate
                ? `the gate that rejected the model's answer: ${gate}`
                : "no guardrail gate rejected the model's answer (the model call itself failed)"
            }`,
          }),
        ]),
      );
    }

    // Every step that is not a plain `ok` is stated on the collapsed card. A
    // `fallback`-state step is skipped here only when the fallback alert above
    // already renders it — the same fact is never stated twice, and never left
    // unsaid.
    const notOk = (t.warnings || []).filter(
      (w) => w.state !== "ok" && !(w.state === "fallback" && t.decidedBy === "fallback"),
    );
    if (notOk.length) {
      out.push(
        el("div", { class: "trace-alert trace-alert-state" }, [
          el("span", { class: "badge trace-warn", text: "not ok" }),
          el("span", {
            text: notOk
              .map((w) => `${w.step} (${w.state})${w.label ? `: ${w.label}` : ""}`)
              .join(" · "),
          }),
        ]),
      );
    }

    if (t.truncated) {
      out.push(
        el("div", { class: "trace-alert" }, [
          el("span", { class: "badge trace-warn", text: "truncated" }),
          el("span", { text: `showing first ${t.shownSteps} of ${t.totalSteps} steps (the §4.3 cap is ${t.cap})` }),
        ]),
      );
    }

    if (t.droppedSteps) {
      out.push(
        el("div", { class: "trace-alert" }, [
          el("span", { class: "badge trace-warn", text: "withheld" }),
          el("span", {
            text: `${t.droppedSteps} step${t.droppedSteps === 1 ? "" : "s"} not in the §4.3 step vocabulary — not shown`,
          }),
        ]),
      );
    }

    if (t.unclassifiedSteps) {
      out.push(
        el("div", { class: "trace-alert" }, [
          el("span", { class: "badge trace-bug", text: "unclassified" }),
          el("span", {
            text: `${t.unclassifiedSteps} step${t.unclassifiedSteps === 1 ? "" : "s"} arrived with no valid state — shown unclassified, never as ok`,
          }),
        ]),
      );
    }

    if (t.reply && t.reply.withheld) {
      out.push(
        el("div", { class: "trace-alert trace-alert-reject" }, [
          el("span", { class: "badge trace-bug", text: "reply withheld" }),
          el("span", {
            text: "the model's reply carried material the C2 scanner rejects — the trace holds the controller's marker, not the reply text, and it was NOT executed",
          }),
        ]),
      );
    }

    if (t.reordered) {
      out.push(
        el("div", { class: "trace-alert" }, [
          el("span", { class: "badge trace-warn", text: "reordered" }),
          el("span", { text: "the trace array was not chronological — the steps below are ordered by ts" }),
        ]),
      );
    }

    if (t.versionMismatch) {
      out.push(
        el("div", { class: "trace-alert" }, [
          el("span", { class: "badge trace-bug", text: "version" }),
          el("span", {
            text: `trace_version ${t.version === null ? "missing" : t.version} — this panel renders §4.3 v1; the steps are shown verbatim`,
          }),
        ]),
      );
    }

    if (t.sourceMismatch) {
      out.push(
        el("div", { class: "trace-alert trace-alert-bug" }, [
          el("span", { class: "badge trace-bug", text: "provenance" }),
          el("span", {
            text: `decided_by "${t.decidedBy}" contradicts decision_source "${r.source}" — the row and its trace disagree about who decided (§4.3)`,
          }),
        ]),
      );
    }

    return out;
  }

  /**
   * The raw model reply, in its own block, labelled `model output — not
   * executed`. It is deliberately NOT styled like the executed block: it is
   * what the model said, not what the fleet did, and the two must never be
   * joined into one claim.
   */
  _traceReply(r, t) {
    const rep = t.reply;
    if (!rep) {
      return el("div", { class: "trace-reply trace-reply-none" }, [
        el("div", { class: "inc-label", text: "model output — not executed" }),
        el("div", {
          class: "muted small",
          text: "no `reply_received` step in this trace — the raw model reply was not recorded here",
        }),
      ]);
    }
    const chips = [
      el("span", {
        class: `badge trace-chip trace-chip-${rep.state || "unclassified"}`,
        text: rep.state || "unclassified",
      }),
      el("span", { class: "badge trace-noexec", text: "NOT EXECUTED" }),
      rep.at ? el("span", { class: "muted small", text: `received ${clock(rep.at)}` }) : null,
      rep.truncated
        ? el("span", { class: "badge trace-clip", text: "reply truncated at the 4096-char bound" })
        : null,
    ];
    const lines = [
      el("div", {
        class: "inc-label",
        text: rep.withheld
          ? "model output — not executed · text WITHHELD by the C2 guardrail"
          : rep.available
            ? "model output — not executed (raw reply, `reply_received` step)"
            : "model output — not executed (the trace retained no reply text)",
      }),
      el("div", { class: "trace-reply-chips" }, chips),
    ];
    if (rep.available) {
      lines.push(el("pre", { class: "trace-reply-text", text: rep.text || "(the reply text was empty)" }));
    } else {
      lines.push(
        el("div", {
          class: "muted small",
          text: "the trace recorded that no raw reply text was retained (state skipped) — there is nothing to show",
        }),
      );
    }
    const notes = [];
    if (rep.withheld) {
      notes.push("the reply tripped the C2 scanner, so the controller replaced its text with a `[dropped: C2]` marker: that marker is what the trace holds, not the model's words, and it was never executed");
    }
    if (t.decidedBy === "fallback") {
      notes.push("this reply did not become the decision — the controller's fallback did (decided_by=fallback); the executed action below is the playbook's, not this reply's");
    }
    if (notes.length) {
      lines.push(el("div", { class: "trace-reply-note" }, notes.map((n) => el("div", { text: n }))));
    }
    const kind = rep.withheld ? "withheld" : rep.available ? "shown" : "none";
    return el("div", { class: `trace-reply trace-reply-${kind}` }, lines);
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
