/*
 * Assistant chat — the pure half (t_d1aa35c8).
 *
 * The panel is a management assistant over Termini's OWN records, not a
 * generic chat box: every request carries an optional structured `context`
 * (`{kind, id}`) that the controller resolves to the exact C0 records it may
 * reason over and cite (controller-api.md §4.10). Nothing in this file touches
 * the DOM or the network, so the context-seeding rules, the chip lifecycle and
 * the honest states are unit-testable on their own.
 *
 * Two contract facts drive the shapes below (read from the parent card, do not
 * invent a second route):
 *
 *   - `context.kind` is the frozen enum
 *     `incident | decision | node | metric | remediation | audit | none`, and
 *     `id` is required whenever `kind != "none"`. The controller resolves those
 *     ids ITSELF: `incident` → a health_events id, `metric` → a node id (the
 *     node's metric window), `decision`/`audit` → that row's id, `remediation`
 *     → a command_id. So the id that travels is always the id the controller
 *     can resolve, never a prettified label.
 *   - an answer that claims an execution must carry the `executed[]` entry that
 *     attributes it (`command_id` + `audit_id`). An unbacked claim is a defect
 *     to SURFACE, not something to style away.
 */

import { shortId } from "./format.js";

/** Frozen `context.kind` enum (controller-api.md §4.10). */
export const CONTEXT_KINDS = Object.freeze([
  "incident",
  "decision",
  "node",
  "metric",
  "remediation",
  "audit",
  "none",
]);

/** Frozen `messages[].role` enum (§4.10). */
export const MESSAGE_ROLES = Object.freeze(["system", "user", "assistant"]);

/** `messages[].content` bound (§4.10). */
export const CONTENT_MAX_CHARS = 4000;

/** How many message bubbles the panel keeps in view (the API call carries less). */
export const SESSION_MAX_MESSAGES = 60;

/** How many past turns are replayed to the controller. */
export const REQUEST_HISTORY_TURNS = 6;

const ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/**
 * An opaque, ULID-shaped id for `conversation_id` / `message_id`. These anchor
 * the controller's execution idempotency key
 * (`assistant:{conversation_id}:{message_id}:{action}:{target}`), so they must
 * be stable for a given message — the panel stores them, it does not regenerate
 * them on retry.
 */
export function newId(nowMs = Date.now(), random = Math.random) {
  let time = "";
  let n = Math.floor(nowMs);
  for (let i = 0; i < 10; i += 1) {
    time = ID_ALPHABET[n % 32] + time;
    n = Math.floor(n / 32);
  }
  let tail = "";
  for (let i = 0; i < 16; i += 1) {
    tail += ID_ALPHABET[Math.floor(random() * 32) % 32];
  }
  return time + tail;
}

/**
 * Normalise a seed from a panel affordance into an attachable context.
 *
 * `id` is the id that travels to the controller as DATA. It is never baked into
 * the question text: the prose is the user's, the id is the request's.
 * `meta` is client-side only (jump targets, the decision a card was rendered
 * from) and is never sent to the controller.
 */
export function seedContext(input = {}) {
  const kind = input.kind || "none";
  if (!CONTEXT_KINDS.includes(kind)) {
    throw new TypeError(`unknown context kind ${JSON.stringify(kind)}`);
  }
  const id = input.id === null || input.id === undefined ? "" : String(input.id).trim();
  if (kind !== "none" && !id) {
    throw new TypeError(`context.id is required when context.kind is ${kind}`);
  }
  return {
    kind,
    id: kind === "none" ? "" : id,
    label: input.label ? String(input.label) : "",
    summary: input.summary ? String(input.summary) : "",
    meta: input.meta && typeof input.meta === "object" ? { ...input.meta } : {},
  };
}

/**
 * The chip a reader sees: kind, a human label, and the id the chip is bound to.
 *
 * When the record the reader clicked is not the record the controller resolves
 * (an incident card is rendered from a DECISION, but `kind: "incident"` resolves
 * a health EVENT), the human line names the decision the reader clicked while
 * the title carries both ids. That keeps the chip honest about which record is
 * attached and which row it came from.
 */
export function contextChip(ctx) {
  if (!ctx || ctx.kind === "none" || !ctx.kind) {
    return { kind: "none", text: "no context attached", title: "", boundId: "", decisionId: "" };
  }
  const decisionId = ctx.meta && ctx.meta.decisionId ? String(ctx.meta.decisionId) : "";
  const label = ctx.label || ctx.summary || "";
  const ref = decisionId
    ? `decision ${shortId(decisionId, 10, 4)}`
    : shortId(ctx.id, 10, 4);
  const parts = [ctx.kind, label, ref].filter(Boolean);
  const title = [
    `context.kind=${ctx.kind}`,
    `context.id=${ctx.id}`,
    decisionId ? `decision_id=${decisionId}` : "",
    ctx.meta && ctx.meta.eventId ? `event_id=${ctx.meta.eventId}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return {
    kind: ctx.kind,
    text: parts.join(" · "),
    title,
    boundId: ctx.id,
    decisionId,
  };
}

/**
 * The request body for `POST /v1/projects/{pid}/assistant/ask` (§4.10).
 * Throws on an empty message list — the controller answers 422 for one, and
 * there is no point spending a round-trip to learn that.
 */
export function buildAskBody({ messages, context, conversationId, messageId } = {}) {
  const out = [];
  for (const m of Array.isArray(messages) ? messages : []) {
    const role = m && m.role;
    const content = m && typeof m.content === "string" ? m.content : "";
    if (!MESSAGE_ROLES.includes(role) || !content.trim()) continue;
    out.push({ role, content: content.slice(0, CONTENT_MAX_CHARS) });
  }
  if (out.length === 0) {
    throw new TypeError("messages must be a non-empty array of {role, content}");
  }
  const body = { messages: out };
  // No attached element is still a context, and it is `none` explicitly: the
  // enum has that member so a general question says so, instead of the panel and
  // the controller disagreeing about what "no context" looks like on the wire.
  if (context && context.kind && context.kind !== "none") {
    const kind = context.kind;
    if (!CONTEXT_KINDS.includes(kind)) throw new TypeError(`unknown context kind ${kind}`);
    const id = String(context.id || "").trim();
    if (!id) throw new TypeError(`context.id is required when context.kind is ${kind}`);
    body.context = { kind, id };
  } else {
    body.context = { kind: "none" };
  }
  if (conversationId) body.conversation_id = String(conversationId);
  if (messageId) body.message_id = String(messageId);
  return body;
}

/** Short, concrete follow-ups per attached element — never a generic prompt wall. */
export function suggestionsFor(kind) {
  switch (kind) {
    case "incident":
      return [
        "What did Termini do about this, and did it work?",
        "Was this a model decision or the fallback playbook?",
        "Show me the telemetry behind this incident.",
      ];
    case "decision":
      return [
        "Why this action instead of the alternatives?",
        "Did it run, and what did the fleet answer?",
        "What would you do if this happened again?",
      ];
    case "node":
      return [
        "Is this node healthy right now?",
        "What has Termini done to this node?",
        "What is using its memory?",
      ];
    case "metric":
      return [
        "Is this inside its frozen threshold?",
        "When did it last breach, and what was decided?",
        "What is driving this value?",
      ];
    case "remediation":
      return [
        "Why is this waiting for approval?",
        "What happens if I approve it?",
        "Has this command run before?",
      ];
    case "audit":
      return [
        "Did this action succeed, and what exactly ran?",
        "Which decision produced this audit row?",
        "Was it retried?",
      ];
    default:
      return [
        "What has Termini done to my nodes?",
        "Which nodes are unhealthy right now?",
        "Show me the last incident and what was executed.",
      ];
  }
}

/**
 * Does the answer claim it executed something *in this turn*? Deliberately
 * narrow: a summary of a past record ("the operator restarted it at 03:12") is
 * not a claim about this exchange, so it does not trip the check. First-person
 * or passive "has/have been" claims do.
 */
export function claimsExecution(answer) {
  const text = String(answer || "");
  const verbs = "restarted|rebooted|stopped|drained|deleted|pruned|executed|scaled|applied";
  const firstPerson = new RegExp(
    `\\b(I|we)(?:'ve| have|\\s+just|\\s+now)?\\b[^.!?]{0,60}\\b(${verbs})\\b`,
    "i",
  );
  const passive = new RegExp(`\\b(has|have) been (${verbs})\\b`, "i");
  return firstPerson.test(text) || passive.test(text);
}

/** `executed[]` rows, normalised, with attribution stated per row (§4.10, D1). */
export function executionRows(executed) {
  return (Array.isArray(executed) ? executed : []).map((e) => {
    const commandId = e && e.command_id ? String(e.command_id) : "";
    const auditId = e && e.audit_id ? String(e.audit_id) : "";
    return {
      action: (e && e.action) || "unknown action",
      target: e && e.target ? String(e.target) : "",
      commandId,
      auditId,
      state: (e && e.state) || "unknown",
      reason: e && e.reason ? String(e.reason) : "",
      attributed: Boolean(commandId && auditId),
    };
  });
}

/** `citations[]` rows, dropping anything that is not a `{kind, id}` citation. */
export function citationRows(citations) {
  return (Array.isArray(citations) ? citations : [])
    .filter((c) => c && typeof c.kind === "string" && typeof c.id === "string" && c.id)
    .map((c) => ({
      kind: c.kind,
      id: c.id,
      label: c.label ? String(c.label) : `${c.kind} ${shortId(c.id, 10, 4)}`,
    }));
}

/**
 * A typed view of a failed ask. 503 is the controller saying the model did not
 * answer (never a fabricated answer); 422 is a bad request (unknown context
 * kind, a context that is not C0-safe); 404 is an id the controller does not
 * have. The panel shows the controller's own reason, not a generic apology.
 */
export function errorView(err) {
  const status = err && typeof err.status === "number" ? err.status : 0;
  const code = (err && err.code) || (status === 0 ? "NETWORK" : "ERROR");
  const message = (err && err.message) || String(err || "the request failed");
  let kind = "error";
  if (status === 503) kind = "model_unavailable";
  else if (status === 422) kind = "invalid_argument";
  else if (status === 404) kind = "not_found";
  else if (status === 401 || status === 403) kind = "unauthenticated";
  else if (status === 0) kind = "network";
  return {
    status,
    code,
    message,
    kind,
    requestId: (err && err.request_id) || null,
    badge: status ? `${status} · ${code}` : code,
    retryable: status === 0 || status === 429 || status >= 500,
  };
}

/** Bounded history for one request: the last completed turns + the new question. */
export function requestMessages(messages, limit = REQUEST_HISTORY_TURNS) {
  const done = [];
  for (const m of messages || []) {
    if (m.state === "pending" || m.state === "error") continue;
    const content = m.state === "answered" ? m.text : m.question;
    if (!content) continue;
    done.push({ role: m.role, content: String(content).slice(0, CONTENT_MAX_CHARS) });
  }
  const cut = done.slice(-limit * 2);
  return cut;
}

/**
 * The conversation state machine: message history, the attached context and its
 * lifecycle, and the three honest message states (`pending`, `answered`,
 * `error`). Purely in-memory; the panel renders it.
 */
export class AssistantSession {
  constructor({ conversationId = null, maxMessages = SESSION_MAX_MESSAGES, now = () => Date.now() } = {}) {
    this.conversationId = conversationId || newId(now());
    this.maxMessages = maxMessages;
    this.now = now;
    this.context = null;
    this.messages = [];
  }

  /**
   * Attach the element the reader clicked. Replaces any previous context — the
   * chip is a single reference, not a pile — and leaves the message history
   * untouched, so follow-up questions keep the reference until the reader
   * removes it or clicks another element.
   */
  attachContext(input) {
    const ctx = seedContext(input);
    this.context = ctx;
    return ctx;
  }

  clearContext() {
    const had = this.context;
    this.context = null;
    return had;
  }

  /**
   * Begin one exchange: the user's question plus a PENDING assistant bubble.
   * The pending bubble carries no answer text at all — it must never read as
   * something the controller said.
   */
  begin(question, { messageId = null } = {}) {
    const text = String(question || "").trim();
    if (!text) throw new TypeError("a question is required");
    const user = {
      id: newId(this.now()),
      role: "user",
      question: text.slice(0, CONTENT_MAX_CHARS),
      text: text.slice(0, CONTENT_MAX_CHARS),
      state: "answered",
      at: this.now(),
    };
    const assistant = {
      id: messageId || newId(this.now()),
      role: "assistant",
      state: "pending",
      question: user.text,
      text: "",
      citations: [],
      executed: [],
      proposed: [],
      errors: [],
      model: null,
      decidedBy: null,
      elapsedMs: null,
      truncated: false,
      fixture: false,
      claimDefect: false,
      at: this.now(),
    };
    this.messages.push(user, assistant);
    this._trim();
    const body = buildAskBody({
      messages: requestMessages(this.messages),
      context: this.context,
      conversationId: this.conversationId,
      messageId: assistant.id,
    });
    return { messageId: assistant.id, body, user, message: assistant };
  }

  find(messageId) {
    return this.messages.find((m) => m.id === messageId) || null;
  }

  /**
   * The controller answered. `fixture` is set for the `?offline=1` responder —
   * offline answers are fixture data and are labelled as such, never passed off
   * as a model answer.
   */
  settle(messageId, response = {}, { fixture = false } = {}) {
    const msg = this.find(messageId);
    if (!msg || msg.state !== "pending") return null;
    const answer = typeof response.answer === "string" ? response.answer : "";
    msg.state = "answered";
    msg.text = answer || "(the controller returned an empty answer)";
    msg.citations = citationRows(response.citations);
    msg.executed = executionRows(response.executed);
    msg.proposed = (Array.isArray(response.proposed_actions) ? response.proposed_actions : []).filter(
      (a) => a && a.action,
    );
    msg.errors = Array.isArray(response.errors) ? response.errors : [];
    msg.model = response.model || null;
    msg.decidedBy = response.decided_by || null;
    msg.elapsedMs = typeof response.elapsed_ms === "number" ? response.elapsed_ms : null;
    msg.truncated = !!response.truncated;
    msg.fixture = Boolean(fixture || response.fixture === true || response.model === "offline");
    // An answer that claims an execution with no attributed executed[] row is a
    // defect in the answer, surfaced on the bubble (never silently restyled).
    msg.claimDefect = claimsExecution(msg.text) && msg.executed.length === 0;
    msg.at = this.now();
    return msg;
  }

  fail(messageId, err) {
    const msg = this.find(messageId);
    if (!msg) return null;
    msg.state = "error";
    msg.text = "";
    msg.error = errorView(err);
    msg.at = this.now();
    return msg;
  }

  _trim() {
    if (this.messages.length <= this.maxMessages) return;
    this.messages.splice(0, this.messages.length - this.maxMessages);
  }
}

/** The audit row a citation of an `executed[]` entry points at (for the jump). */
export function auditCitationFor(entry) {
  if (!entry || !entry.auditId) return null;
  return { kind: "audit", id: entry.auditId, label: `audit ${shortId(entry.auditId, 10, 4)}` };
}
