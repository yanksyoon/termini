/*
 * Decision-view model (telemetry -> model -> action -> result). Pure: no DOM,
 * no network — unit-tested under node.
 *
 * Sources (all live controller responses, verified against the standing demo):
 *   GET /v1/operator/decisions  -> {decisions:[{id, event_id, node_id, rule_id,
 *        action_kind, action_params, reason, decided_at, decision_source,
 *        confidence, fallback_reason}]}
 *   GET /v1/operator/events     -> {events:[{id, node_id, rule_id, type, metric,
 *        value, threshold, evidence_seq_start, evidence_seq_end, fired_at,
 *        breach_started_at, evidence:[Sample]}]}
 *   GET /v1/operator/audits     -> {audits:[{id, decision_id, event_id, node_id,
 *        rule_id, action_kind, result, detail, lxd_method, lxd_path, lxd_body,
 *        idempotency_key, dry_run, executed_at, lxd_endpoint}]}
 *
 * The dashboard joins them client-side: a decision has NO audit id of its own,
 * the audit row points back with `decision_id`; the breaching metric/value/
 * threshold live on the health event the decision was made for.
 */

import { METRIC_UNITS, metricLabel } from "./series.js";
import { bytes, num, pct, shortId } from "./format.js";

/** Frozen decision sources (operator-decision.md §2/§11). */
export const DECISION_SOURCES = ["model", "fallback", "deterministic"];

/**
 * What a decision source MEANS, and how loud the UI should be about it.
 * A fallback is deliberately NOT presented like a model decision: the model did
 * not answer, the deterministic playbook did (operator-decision.md §8).
 */
export function sourceMeta(source) {
  switch (source) {
    case "model":
      return {
        key: "model",
        label: "AI model",
        tone: "model",
        byModel: true,
        note: "decided by the model",
      };
    case "fallback":
      return {
        key: "fallback",
        label: "fallback",
        tone: "fallback",
        byModel: false,
        note: "the model did NOT decide this — the deterministic playbook did",
      };
    default:
      return {
        key: "deterministic",
        label: "deterministic",
        tone: "det",
        byModel: false,
        note: "playbook only (no model call)",
      };
  }
}

/**
 * The persisted provenance of the §4.2 incident record
 * (`remediation_decisions.incident_generated`, operator-decision.md §4.2/§8.1).
 */
export const INCIDENT_SOURCES = ["model", "fallback"];

/**
 * What the record's PROVENANCE means, and how loudly the UI must say it.
 *
 * `generated` is `"model"` only when the string in the record is the model's own
 * validated output (source == model AND the response passed guardrail G9). The
 * §8.1 fallback also writes a record — built by the controller from the fired
 * event — and it must NEVER be presented like model output. A record that
 * arrives with no provenance is not "unknown quality", it is a contract
 * violation: a reader could not tell "the model said this" from "the controller
 * said this", so the view model refuses to attribute it at all (`bug: true`).
 */
export function incidentSourceMeta(generated) {
  switch (generated) {
    case "model":
      return {
        key: "model",
        label: "AI model",
        tone: "model",
        byModel: true,
        badge: "AI-GENERATED INCIDENT",
        note: "the model's own output — validated by guardrail G9 before it was persisted",
      };
    case "fallback":
      return {
        key: "fallback",
        label: "controller-generated, model unavailable",
        tone: "fallback",
        byModel: false,
        badge: "CONTROLLER-GENERATED · MODEL UNAVAILABLE",
        note: "the model did NOT write this: the controller built the record from the fired event because the model was unavailable (§8.1)",
      };
    default:
      return {
        key: "unknown",
        label: "record with no provenance",
        tone: "bug",
        byModel: false,
        bug: true,
        badge: "RECORD WITH NO PROVENANCE",
        note: "an incident record was persisted without `incident_generated`, so it can be attributed to neither the model nor the controller — a contract violation (§4.2/§8.1), shown as unlabelled rather than as model output",
      };
  }
}

/** Units a telemetry field name carries by the frozen naming convention. */
const FIELD_SUFFIX_UNITS = [
  ["_bytes", "bytes"],
  ["_ms", "ms"],
  ["_pct", "percent"],
];

/**
 * One evidence value as text. The unit comes from the field's own name — the
 * frozen telemetry registry first, then the naming convention every metric in
 * `node-telemetry.md` §4 follows (`*_bytes`, `*_ms`, `*_pct`) — and nothing is
 * invented for a name that carries neither: such a value is printed raw.
 */
export function evidenceValueText(field, value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value !== "number") {
    const text = String(value);
    return text.length ? text : "—";
  }
  if (!Number.isFinite(value)) return "—";
  const unit =
    METRIC_UNITS[field] || (FIELD_SUFFIX_UNITS.find(([s]) => String(field).endsWith(s)) || [])[1];
  if (unit === "bytes") return bytes(value);
  if (unit === "percent") return pct(value);
  if (unit === "ms") return `${num(value, 1)} ms`;
  if (unit) return `${num(value, 2)} ${unit}`;
  return Number.isInteger(value) ? num(value, 0) : num(value, 2);
}

/** A JSON-TEXT column that a caller may not have parsed (defensive, not a guess). */
function asList(raw) {
  if (Array.isArray(raw)) return raw;
  if (typeof raw !== "string" || !raw.trim()) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/**
 * `evidence[]` as the view model keeps it: ONLY the §4.2 keys
 * `{field, value, seq?, ts?}`. An entry that is not an object, or that has no
 * `field` name, is dropped rather than rendered with blanks — the record is the
 * model's claim, and a claim without its field is not checkable against the
 * telemetry store.
 */
export function incidentEvidence(raw) {
  const out = [];
  for (const entry of asList(raw)) {
    if (!entry || typeof entry !== "object") continue;
    if (typeof entry.field !== "string" || !entry.field) continue;
    const item = { field: entry.field, value: entry.value };
    item.valueText = evidenceValueText(item.field, item.value);
    if (Number.isInteger(entry.seq) && entry.seq >= 0) item.seq = entry.seq;
    if (typeof entry.ts === "string" && entry.ts.trim()) item.ts = entry.ts;
    out.push(item);
  }
  return out;
}

/**
 * `actions[]` as the view model keeps it: what the model DECIDED
 * (`{action_kind, target?, note?}`), never an outcome. The executed result and
 * its audit id come from the `remediation_audits` join and are rendered apart,
 * so the record can never imply an outcome the executor did not produce.
 */
export function incidentActions(raw) {
  const out = [];
  for (const entry of asList(raw)) {
    if (!entry || typeof entry !== "object") continue;
    if (typeof entry.action_kind !== "string" || !entry.action_kind) continue;
    const item = { action_kind: entry.action_kind };
    if (typeof entry.target === "string" && entry.target) item.target = entry.target;
    if (typeof entry.note === "string" && entry.note) item.note = entry.note;
    out.push(item);
  }
  return out;
}

/** The five persisted incident columns (migration 0016, §11). */
const INCIDENT_COLUMNS = [
  "incident_title",
  "incident_explanation",
  "incident_evidence",
  "incident_actions",
  "incident_generated",
];

/**
 * The §4.2 incident record of one decision row, or `null` when the row carries
 * none. `null` is the deterministic-mode row (§8.1 writes no incident columns)
 * and it must stay `null` — an "empty card" would assert an incident that the
 * controller never recorded.
 */
export function incidentView(decision) {
  if (!decision || typeof decision !== "object") return null;
  const present = INCIDENT_COLUMNS.some((k) => decision[k] !== undefined && decision[k] !== null && decision[k] !== "");
  if (!present) return null;
  const generated = typeof decision.incident_generated === "string" && decision.incident_generated
    ? decision.incident_generated
    : null;
  const meta = incidentSourceMeta(generated);
  const evidence = incidentEvidence(decision.incident_evidence);
  const actions = incidentActions(decision.incident_actions);
  const title = typeof decision.incident_title === "string" ? decision.incident_title : "";
  const explanation = typeof decision.incident_explanation === "string" ? decision.incident_explanation : "";
  return {
    title,
    explanation,
    evidence,
    actions,
    generated,
    meta,
    modelSourced: meta.byModel,
    bug: !!meta.bug,
  };
}

/* ------------------------------------------------ decision trace (§4.3) */

/** The frozen §4.3 step vocabulary — the nine names, in the contract's order. */
export const TRACE_STEPS = [
  "threshold_fired",
  "evidence_window",
  "prompt_built",
  "model_call",
  "reply_received",
  "gates",
  "action_decided",
  "executed",
  "audit_recorded",
];

/** The §4.3 step states. `ok` is the only one that has nothing to report. */
export const TRACE_STATES = ["ok", "clamped", "dropped", "rejected", "fallback", "skipped"];

/** `decided_by` (§4.3) — who wrote the trace. */
export const TRACE_DECIDED_BY = ["model", "fallback", "deterministic"];

/**
 * Client-side caps, mirroring the controller's. The panel re-enforces them
 * rather than trusting the response: it is a reader of the trace, never a
 * second egress path, so it must not paint more than the contract allows even
 * if a row arrives carrying more.
 */
export const TRACE_MAX_STEPS = 24;
export const TRACE_STEP_LABEL_MAX_CHARS = 128;
export const TRACE_STEP_DETAIL_MAX_CHARS = 512;
export const TRACE_REPLY_MAX_CHARS = 4096;
export const TRACE_VERSION = 1;

/** The single step whose `detail` IS the raw model reply. */
export const TRACE_REPLY_STEP = "reply_received";

/**
 * What `decided_by` MEANS, and how loudly the UI must say it (§4.3).
 *
 * A `fallback` trace is a CONTROLLER document: the model did not answer, and the
 * controller recorded the fallback. It must never be presented as the model's
 * reasoning, exactly as §8.1's controller-built record is never presented as
 * model output. A trace that arrives without `decided_by` cannot be attributed
 * to anyone (`bug: true`), and one that claims `deterministic` contradicts §8.1
 * (deterministic mode writes no trace at all) — both are shown as violations
 * rather than quietly rendered as a reasoning record.
 */
export function traceDeciderMeta(decidedBy) {
  switch (decidedBy) {
    case "model":
      return {
        key: "model",
        label: "AI model",
        tone: "model",
        byModel: true,
        badge: "AI THINKING LOG",
        note: 'decided_by: "model" — the steps were recorded on the model call path (§4.3)',
      };
    case "fallback":
      return {
        key: "fallback",
        label: "controller-generated (fallback)",
        tone: "fallback",
        byModel: false,
        badge: "CONTROLLER THINKING LOG · FALLBACK",
        note: 'decided_by: "fallback" — the model did NOT answer: this trace was written by the controller when it fell back to the deterministic playbook (§4.3/§8.1)',
      };
    case "deterministic":
      return {
        key: "deterministic",
        label: "playbook (deterministic)",
        tone: "det",
        byModel: false,
        bug: true,
        badge: "TRACE CLAIMING decided_by: deterministic",
        note: 'deterministic mode writes no trace at all (§8.1) — a trace that claims decided_by: "deterministic" contradicts the contract and is shown as written, never as a reasoning record',
      };
    default:
      return {
        key: "unknown",
        label: "trace with no decided_by",
        tone: "bug",
        byModel: false,
        bug: true,
        badge: "TRACE WITH NO decided_by",
        note: "the trace arrived without its required `decided_by`, so a reader could not tell model reasoning from controller text — a §4.3 violation, shown unlabelled rather than as model output",
      };
  }
}

/** Clamp text to a cap, reporting whether anything was cut (never silently). */
function clampText(value, cap) {
  const text = typeof value === "string" ? value : value === undefined || value === null ? "" : String(value);
  if (text.length <= cap) return { text, clamped: false };
  return { text: text.slice(0, cap), clamped: true, cut: text.length - cap };
}

/** First `key=value` in a step detail, or null. Details are `k=v` space-joined. */
function detailField(detail, key) {
  if (typeof detail !== "string") return null;
  const m = new RegExp(`(?:^|\\s)${key}=(\\S+)`).exec(detail);
  return m ? m[1] : null;
}

/**
 * One trace step, or `null` when the mapper refuses it (the caller counts the
 * refusals and SAYS SO — a withheld step is never rendered and never silently
 * dropped either).
 *
 * Only the §4.3 step keys survive (`step`, `ts`, `label`, `detail`, `state`, and
 * the reply step's `truncated`); anything else in the object is discarded. The
 * step NAME is the trace's own index into the §4.3 step table, so a name that
 * table does not define cannot be placed in a chronological reasoning log
 * without inventing what it means: it is withheld and counted.
 */
export function traceStepView(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  if (typeof raw.step !== "string" || !TRACE_STEPS.includes(raw.step)) return null;
  // The reply step carries the raw model reply, so it gets the reply bound
  // (§4.3: ≤4096 chars, truncation flagged) — every other step gets 512.
  const detailCap = raw.step === TRACE_REPLY_STEP ? TRACE_REPLY_MAX_CHARS : TRACE_STEP_DETAIL_MAX_CHARS;
  const label = clampText(raw.label, TRACE_STEP_LABEL_MAX_CHARS);
  const detail = clampText(raw.detail, detailCap);
  const step = {
    step: raw.step,
    ts: typeof raw.ts === "string" && raw.ts.trim() ? raw.ts : null,
    label: label.text,
    detail: detail.text,
    // A missing or unknown state is NOT flattened to `ok`: the reader must be
    // able to see that the trace did not classify this step.
    state: typeof raw.state === "string" && TRACE_STATES.includes(raw.state) ? raw.state : null,
  };
  if (step.state === null) step.unclassified = true;
  if (label.clamped || detail.clamped) step.clamped = true;
  // `truncated` belongs to the reply step only (§4.3 field reference).
  if (raw.step === TRACE_REPLY_STEP && raw.truncated === true) step.truncated = true;
  return step;
}

const TRACE_ABSENT = {
  present: false,
  steps: [],
  decidedBy: null,
  decidedByMeta: null,
  version: null,
  versionMismatch: false,
  truncated: false,
  totalSteps: 0,
  shownSteps: 0,
  cap: TRACE_MAX_STEPS,
  droppedSteps: 0,
  clampedSteps: 0,
  unclassifiedSteps: 0,
  reordered: false,
  warnings: [],
  reply: null,
  fallback: null,
  problem: null,
};

/**
 * The §4.3 decision trace of one decision row as the view model keeps it.
 *
 * `present:false` means NO trace was recorded for this row (a deterministic-mode
 * row writes none, §8.1). It must stay `false`: an empty timeline would assert a
 * reasoning record the controller never wrote.
 *
 * The mapper keeps only what the contract defines — unknown keys are dropped
 * (the panel is not a second egress path), the caps are re-enforced client-side,
 * and the steps are put in chronological order. Nothing is hidden: steps the
 * mapper refuses are counted (`droppedSteps`), non-`ok` states are listed in
 * `warnings`, and a truncated array reports `totalSteps` alongside `shownSteps`.
 */
export function traceView(raw) {
  const absent = { ...TRACE_ABSENT };
  let obj = raw;
  if (typeof obj === "string") {
    const text = obj.trim();
    if (!text) return { ...absent, problem: "the row carries decision_trace as an empty string" };
    try {
      obj = JSON.parse(text);
    } catch {
      return {
        ...absent,
        problem:
          "the row carries decision_trace as a string that is not JSON (the API parses it — §11); no timeline is drawn from it",
      };
    }
  }
  if (obj === null || obj === undefined) return absent;
  if (typeof obj !== "object" || Array.isArray(obj)) {
    return { ...absent, problem: `the row carries decision_trace as a ${Array.isArray(obj) ? "list" : typeof obj}, not the §4.3 object` };
  }
  const known = ["trace_version", "decided_by", "steps"].some((k) => obj[k] !== undefined && obj[k] !== null);
  if (!known) return { ...absent, problem: "the row carries a decision_trace object with none of the §4.3 keys" };

  const version = Number.isInteger(obj.trace_version) ? obj.trace_version : null;
  const decidedBy = typeof obj.decided_by === "string" && obj.decided_by ? obj.decided_by : null;
  const rawSteps = Array.isArray(obj.steps) ? obj.steps : [];
  const totalSteps = rawSteps.length;
  const truncated = totalSteps > TRACE_MAX_STEPS;
  const kept = truncated ? rawSteps.slice(0, TRACE_MAX_STEPS) : rawSteps;

  let steps = [];
  let droppedSteps = 0;
  for (const entry of kept) {
    const step = traceStepView(entry);
    if (step) steps.push(step);
    else droppedSteps += 1;
  }

  // Chronological order: the array order is the contract's (§4.3 writes oldest
  // first), so it is only overridden when the timestamps contradict it — and
  // then the fact is reported instead of silently fixed.
  const times = steps.map((s) => Date.parse(s.ts || ""));
  let reordered = false;
  if (steps.length > 1 && times.every((t) => Number.isFinite(t))) {
    const outOfOrder = times.some((t, i) => i > 0 && t < times[i - 1]);
    if (outOfOrder) {
      const order = steps.map((_, i) => i).sort((a, b) => times[a] - times[b] || a - b);
      steps = order.map((i) => steps[i]);
      reordered = true;
    }
  }

  const replyStep = [...steps].reverse().find((s) => s.step === TRACE_REPLY_STEP) || null;
  const reply = replyStep
    ? {
        at: replyStep.ts,
        state: replyStep.state,
        text: clampText(replyStep.detail, TRACE_REPLY_MAX_CHARS).text,
        truncated: !!replyStep.truncated,
        // `skipped` is the controller saying it retained no reply text at all
        // ("no raw reply text retained") — so there is no model output to show.
        available: replyStep.state !== "skipped",
        // `rejected` means the reply text was replaced by the C2 marker: the
        // trace carries a guardrail verdict, not the model's words.
        withheld: replyStep.state === "rejected",
      }
    : null;

  const actionStep = [...steps].reverse().find((s) => s.step === "action_decided") || null;
  const fallback =
    decidedBy === "fallback"
      ? {
          reason: actionStep ? detailField(actionStep.detail, "reason") : null,
          rejectingGate: actionStep ? detailField(actionStep.detail, "rejecting_gate") : null,
        }
      : null;

  const clampedSteps = steps.filter((s) => s.clamped).length;
  const unclassifiedSteps = steps.filter((s) => s.unclassified).length;
  return {
    present: true,
    steps,
    decidedBy,
    decidedByMeta: traceDeciderMeta(decidedBy),
    version,
    versionMismatch: version !== TRACE_VERSION,
    truncated,
    totalSteps,
    shownSteps: steps.length,
    cap: TRACE_MAX_STEPS,
    droppedSteps,
    clampedSteps,
    unclassifiedSteps,
    reordered,
    // Every step that is not a plain `ok`: the collapsed card must be able to
    // say "1 rejected" out loud instead of burying it in an unopened log.
    warnings: steps
      .filter((s) => s.state !== "ok")
      .map((s) => ({ step: s.step, state: s.state, label: s.label })),
    reply,
    fallback,
    problem: null,
    sourceMismatch: false, // filled by buildDecisionRows, which knows decision_source
  };
}

/**
 * Why a row has no thinking log. Same distinction as `noIncidentReason`:
 * deterministic mode records none (§8.1), the model path may legitimately have
 * recorded none, but a `fallback` row with no trace contradicts §4.3.
 */
export function noTraceReason(decision) {
  const source = decision ? decision.decision_source : null;
  if (source === "deterministic") return "no decision trace (deterministic mode writes none — §8.1)";
  if (source === "fallback") return "no decision trace (a fallback row records one — §4.3)";
  if (source === "model") return "no decision trace (the model path recorded none for this decision)";
  return "no decision trace";
}

/**
 * Why a row has no incident card. The distinction is the point: deterministic
 * mode writes no record (§8.1), the model may legitimately return none of the
 * four fields (§4.2) — but a `fallback` row with no record contradicts §8.1 and
 * is reported as such instead of being smoothed over.
 */
export function noIncidentReason(decision) {
  const source = decision ? decision.decision_source : null;
  if (source === "deterministic") return "no incident record (deterministic mode)";
  if (source === "fallback") return "no incident record (fallback row with no record — §8.1 says the fallback always writes one)";
  if (source === "model") return "no incident record (the model returned none of the §4.2 fields)";
  return "no incident record";
}

/** Index a list of records by one of its fields (first occurrence wins). */
export function indexBy(rows, key) {
  const map = new Map();
  for (const r of rows || []) {
    if (!r || r[key] === undefined || r[key] === null) continue;
    if (!map.has(r[key])) map.set(r[key], r);
  }
  return map;
}

/** The action's target instance, when the executor action carries one. */
export function actionTarget(decision) {
  const p = (decision && decision.action_params) || {};
  const name = p.instance_name || p._instance_name || null;
  if (!name) return { name: null, text: "(no instance target)" };
  return { name, text: name };
}

/**
 * Parse one `externalModelCall` operator log line (the egress record) into the
 * decider's identity. The controller writes
 *   externalModelCall before|after endpoint=... backend=... egress=...
 *   model=... destination=... answered_by=... outcome=... request_id=...
 * so the dashboard can label WHO answered instead of assuming local GPU.
 * Returns null for any other log line. `termini_backend` never appears in the
 * body here — the label is taken from this record, never hardcoded.
 */
export function parseEgressLine(message) {
  if (typeof message !== "string") return null;
  if (!message.includes("externalModelCall")) return null;
  const re = /([a-z_]+)=("[^"]*"|\S+)/g;
  const out = {};
  let m;
  while ((m = re.exec(message)) !== null) {
    out[m[1]] = m[2].replace(/^"|"$/g, "");
  }
  const phase = /externalModelCall\s+(before|after)/.exec(message);
  const rec = {
    phase: phase ? phase[1] : null,
    endpoint: out.endpoint || null,
    backend: out.backend || null,
    egress: out.egress || null,
    model: out.model || null,
    destination: out.destination || null,
    answered_by: out.answered_by || null,
    outcome: out.outcome || null,
    request_id: out.request_id || null,
  };
  if (!rec.backend && !rec.answered_by && !rec.model) return null;
  return rec;
}

const FORWARDED_BACKEND = "gemini-proxy";
const FORWARDED_ANSWER = "forwarded-managed-model";
const LOCAL_BACKEND = "local";
const LOCAL_ANSWER = "local-weights";

/**
 * Honest decider label derived from the egress record (criterion: never imply
 * local GPU inference). `known:false` when nothing has been observed — the UI
 * must say so rather than guess.
 */
export function deciderMeta(record) {
  if (!record) {
    return {
      known: false,
      kind: "unknown",
      label: "decider unknown",
      detail: "no externalModelCall record on the operator log stream yet",
      fields: [],
    };
  }
  const forwarded =
    record.backend === FORWARDED_BACKEND || record.answered_by === FORWARDED_ANSWER;
  const local = record.backend === LOCAL_BACKEND || record.answered_by === LOCAL_ANSWER;
  const fields = [
    ["backend", record.backend],
    ["model", record.model],
    ["egress", record.egress],
    ["destination", record.destination],
    ["answered by", record.answered_by],
    ["endpoint", record.endpoint],
  ].filter(([, v]) => v);

  if (forwarded) {
    return {
      known: true,
      kind: "forwarded",
      label: `forwarded managed model${record.model ? ` · ${record.model}` : ""}`,
      detail:
        "the decision was not computed by local weights on this fleet: the operator's model call was answered by a forwarded managed model reached over egress",
      fields,
    };
  }
  if (local) {
    return {
      known: true,
      kind: "local",
      label: `local weights${record.model ? ` · ${record.model}` : ""}`,
      detail: "the decision was answered by model weights served on this fleet (no external egress)",
      fields,
    };
  }
  return {
    known: true,
    kind: "unknown",
    label: `decider reported as ${record.backend || record.answered_by || "unknown"}`,
    detail: "the endpoint named a backend this dashboard does not classify — shown verbatim",
    fields,
  };
}

/** Render a metric value using the frozen unit registry (contract §1.4). */
export function formatMetricValue(metric, value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const v = Number(value);
  const unit = METRIC_UNITS[metric];
  if (unit === "bytes") return bytes(v);
  if (unit === "percent") return pct(v);
  if (unit === "ms") return `${num(v, 1)} ms`;
  return `${num(v, 2)}${unit ? ` ${unit}` : ""}`;
}

/**
 * The trigger value, with a unit only when the API's own text justifies one.
 *
 * `/v1/operator/events.value` is the value that crossed the threshold, and the
 * controller does NOT carry a unit for it: `ram_used_bytes` arrives as `96.04`
 * (a percentage) and `disk_free_bytes` arrives as `120.0` (the write latency, in
 * ms). Stamping the *metric's* unit on it would print "96 B" for a RAM breach —
 * worse than no unit. So a unit is claimed only when the threshold text proves
 * it, and otherwise the raw number is labelled as raw.
 */
export function breachValueText(event) {
  const raw = event ? event.value : null;
  if (raw === null || raw === undefined || raw === "") return "—";
  const v = Number(raw);
  if (!Number.isFinite(v)) return "—";
  const text = String((event && event.threshold) || "");
  const metric = event && event.metric;
  const pctOf = /([<>]=?)\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*of\s+([a-z0-9_]+)/i.exec(text);
  if (pctOf) {
    if (v >= 0 && v <= 100) return `${num(v, 2)}% of ${pctOf[3]}`;
    if (METRIC_UNITS[metric] === "bytes") return bytes(v);
  }
  if (metric) {
    const named = new RegExp(`(?:^|[\\s(])${metric}\\s*(<=|>=|<|>)`, "i").test(text);
    if (named) return formatMetricValue(metric, v);
  }
  return `${num(v, 2)} (raw)`;
}

/** Last numeric value of a `/metrics` series entry, ignoring gaps. */
export function seriesLast(seriesEntry) {
  const pts = (seriesEntry && seriesEntry.points) || [];
  for (let i = pts.length - 1; i >= 0; i -= 1) {
    const v = Number(pts[i].v);
    if (Number.isFinite(v)) return v;
  }
  return null;
}

/**
 * The numeric line a chart can draw for a fired threshold, or null when the
 * threshold text cannot be resolved unambiguously (a chart line that guesses is
 * worse than none — the row still shows the threshold text).
 *
 * Two forms are resolvable:
 *   ">= 95% of ram_total_bytes ..."  -> pct x <total metric value>
 *   "<metric> >= <number>"           -> absolute, only when <metric> is the
 *                                       event metric and the chart has it
 */
export function thresholdMarker(event, seriesByMetric = {}) {
  const text = (event && event.threshold) || "";
  if (!text) return null;
  const pctOf = /([<>]=?)\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*of\s+([a-z0-9_]+)/i.exec(text);
  if (pctOf) {
    const total = seriesLast(seriesByMetric[pctOf[3]]);
    if (total === null || !(total > 0)) return null;
    return {
      value: (Number(pctOf[2]) / 100) * total,
      basis: `${pctOf[2]}% of ${pctOf[3]}`,
      op: pctOf[1],
    };
  }
  const metric = event && event.metric;
  if (!metric) return null;
  const re = new RegExp(`(?:^|[\\s(])${metric}\\s*(<=|>=|<|>)\\s*([0-9]+(?:\\.[0-9]+)?)`, "i");
  const abs = re.exec(text);
  if (abs && seriesByMetric[metric]) {
    return { value: Number(abs[2]), basis: `${metric} ${abs[1]} ${abs[2]}`, op: abs[1] };
  }
  return null;
}

/**
 * Join the three live lists into one row per decision, in the order the API
 * returned them (newest first).
 *
 * @param {{decisions?:Array, events?:Array, audits?:Array,
 *          seriesByNode?:Record<string, Record<string, object>>}} input
 *        `seriesByNode[nodeId][metricName]` is a `/metrics` series entry — it is
 *        what lets a threshold expressed as a percentage of a total be drawn.
 * @param {number} auditWindow how many audits were fetched (for an honest
 *        "audit outside the fetched window" note instead of a silent blank)
 */
export function buildDecisionRows({ decisions = [], events = [], audits = [], seriesByNode = {} } = {}, { auditWindow = 0 } = {}) {
  const eventsById = indexBy(events, "id");
  const auditsByDecision = indexBy(audits, "decision_id");
  return (decisions || []).map((d) => {
    const event = eventsById.get(d.event_id) || null;
    const audit = auditsByDecision.get(d.id) || null;
    const meta = sourceMeta(d.decision_source);
    const target = actionTarget(d);
    const series = (seriesByNode && seriesByNode[d.node_id]) || {};
    const breach = event
      ? {
          metric: event.metric || null,
          metricLabel: event.metric ? metricLabel(event.metric) : null,
          value: event.value,
          valueText: breachValueText(event),
          threshold: event.threshold || null,
          firedAt: event.fired_at || null,
          breachStartedAt: event.breach_started_at || null,
          sampleSeq: event.evidence_seq_end ?? null,
        }
      : null;
    const threshold = event ? thresholdMarker(event, series) : null;
    const incident = incidentView(d);
    // §4.3 thinking log. The trace rides in on the SAME decisions response (the
    // API returns the row with `decision_trace` parsed), so nothing extra is
    // fetched here — the panel joins it exactly as it joins the audit row.
    const trace = traceView(d.decision_trace);
    if (trace.present) {
      trace.sourceMismatch = !!(
        trace.decidedBy &&
        d.decision_source &&
        trace.decidedBy !== d.decision_source
      );
    }
    return {
      id: d.id,
      decidedAt: d.decided_at || null,
      nodeId: d.node_id || null,
      ruleId: d.rule_id || null,
      actionKind: d.action_kind || null,
      actionParams: d.action_params || {},
      target: target.name,
      rationale: d.reason || null,
      source: meta.key,
      sourceMeta: meta,
      confidence: d.decision_source === "model" ? d.confidence : null,
      fallbackReason: d.fallback_reason || null,
      // §4.2 incident record: the structured answer to what happened / why / on
      // what evidence / what it decided. `null` for a deterministic-mode row
      // (the controller writes no incident columns there) and `incidentSource`
      // carries the persisted provenance (`model` | `fallback`) verbatim.
      incident,
      incidentSource: incident ? incident.generated : null,
      incidentMeta: incident ? incident.meta : null,
      noIncidentReason: incident ? null : noIncidentReason(d),
      // §4.3 decision trace: the ordered reasoning steps behind the answer.
      // `trace.present === false` for a deterministic-mode row (no trace
      // columns at all, §8.1) — the card says so instead of drawing an empty
      // timeline that would assert a record nobody wrote.
      trace,
      traceSource: trace.present ? trace.decidedBy : null,
      traceMeta: trace.present ? trace.decidedByMeta : null,
      traceBug:
        trace.present &&
        (!!trace.decidedByMeta.bug || trace.sourceMismatch || trace.versionMismatch),
      noTraceReason: trace.present ? null : noTraceReason(d),
      eventId: d.event_id || null,
      event,
      breach,
      threshold,
      audit,
      auditId: audit ? audit.id : null,
      auditMissing: !audit && auditWindow > 0,
      auditWindow,
      execution: audit
        ? {
            result: audit.result || "unknown",
            ok: audit.result === "ok",
            detail: audit.detail || null,
            endpoint: audit.lxd_endpoint || null,
            method: audit.lxd_method || null,
            path: audit.lxd_path || null,
            body: audit.lxd_body || null,
            at: audit.executed_at || null,
            idempotencyKey: audit.idempotency_key || null,
            dryRun: !!audit.dry_run,
          }
        : null,
    };
  });
}

/** Short one-line breach summary for the row, e.g. "RAM used 64.0 GiB (>= 95% …)". */
export function breachSummary(row) {
  if (!row || !row.breach) return "telemetry event not in the fetched window";
  const b = row.breach;
  return `${b.metricLabel || b.metric} ${b.valueText}`;
}

/**
 * Chart annotations for one node's decisions: a marker at the decision time
 * (coloured by source) and, when resolvable, the threshold line the event
 * crossed — so cause (telemetry) and effect (action) line up on the same axis.
 */
export function annotationsForNode(rows, nodeId) {
  const out = [];
  for (const r of rows || []) {
    if (!r || r.nodeId !== nodeId) continue;
    const metric = r.breach && r.breach.metric;
    const t = Date.parse(r.decidedAt || r.breach?.firedAt || "");
    if (!metric || !Number.isFinite(t)) continue;
    out.push({
      metric,
      t,
      kind: r.source,
      label: `${r.ruleId || "rule"}${r.actionKind ? ` → ${r.actionKind}` : ""}`,
      value: r.breach.value,
      threshold: r.threshold ? { ...r.threshold, label: `${r.ruleId || "rule"}: ${r.threshold.basis}` } : null,
    });
  }
  return out;
}

/** Compact id for badges (kept for callers that only need a short form). */
export function shortAuditId(id) {
  return shortId(id, 8, 4);
}
