/*
 * Assistant chat — context seeding, the chip lifecycle and the honest states.
 *
 * Covers (t_d1aa35c8 work item 6):
 *   - unit: context seeding + chip lifecycle (attach, follow-up, replace, remove)
 *   - unit: the three message states (`pending` never renders an answer, a typed
 *     error carries the controller's reason, `executed[]` needs its ids)
 *   - the offline responder's §4.10 contract shape (fixture-labelled, no
 *     fabricated execution)
 *   - panel-level: clicking "Ask AI" on an INCIDENT card seeds the panel with
 *     that incident's decision id, and the request carries the incident's own
 *     context id (the HTTP/served-page counterpart lives in served-page.test.mjs).
 *
 * Run: node --test dashboard/tests/assistant.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { installDom } from "./helpers/minidom.mjs";

const document = installDom();

const {
  AssistantSession,
  seedContext,
  contextChip,
  buildAskBody,
  suggestionsFor,
  claimsExecution,
  executionRows,
  errorView,
  CONTEXT_KINDS,
} = await import("../js/core/assistant.js");
const { ChatPanel } = await import("../js/ui/chat.js");
const { DecisionsPanel } = await import("../js/ui/decisions.js");
const { OfflineBackend } = await import("../js/offline.js");

const TS = "2026-10-17T02:00:00.000Z";
const DECISION_ID = "01J9Z3K4M5N6P7Q8R9S0T1U2V3";
const EVENT_ID = "01J9Z3K4M5N6P7Q8R9S0T1E9X";
const AUDIT_ID = "01J9Z3K4M5N6P7Q8R9S0T1A7U";
const NODE_ID = "01J9Z3K4M5N6P7Q8R9S0T1N0D";

/* ------------------------------------------------------------- unit: seeding */

test("seedContext keeps the id as data and refuses a context with no id", () => {
  const ctx = seedContext({ kind: "decision", id: DECISION_ID, label: "ram → restart" });
  assert.equal(ctx.kind, "decision");
  assert.equal(ctx.id, DECISION_ID);
  assert.throws(() => seedContext({ kind: "node" }), /context\.id is required/);
  assert.throws(() => seedContext({ kind: "planet", id: "x" }), /unknown context kind/);
  assert.deepEqual(seedContext({}).kind, "none");
  assert.deepEqual([...CONTEXT_KINDS], ["incident", "decision", "node", "metric", "remediation", "audit", "none"]);
});

test("the chip names the decision an incident card was rendered from, and titles both ids", () => {
  const chip = contextChip(
    seedContext({
      kind: "incident",
      id: EVENT_ID,
      label: "Memory exhaustion on node a",
      meta: { decisionId: DECISION_ID, eventId: EVENT_ID },
    }),
  );
  // The human line is the one the task asks for: kind · label · decision id.
  assert.match(chip.text, /^incident · Memory exhaustion on node a · decision 01J9Z3K4M5…/);
  // The id that TRAVELS is the one the controller resolves for kind=incident.
  assert.equal(chip.boundId, EVENT_ID);
  assert.equal(chip.decisionId, DECISION_ID);
  assert.match(chip.title, new RegExp(`context\\.id=${EVENT_ID}`));
  assert.match(chip.title, new RegExp(`decision_id=${DECISION_ID}`));
});

test("buildAskBody sends {kind,id} in context and never the human prose as an id", () => {
  const body = buildAskBody({
    messages: [{ role: "user", content: "did it work?" }],
    context: seedContext({ kind: "decision", id: DECISION_ID, label: "ram → restart" }),
    conversationId: "conv-1",
    messageId: "msg-1",
  });
  assert.deepEqual(body.context, { kind: "decision", id: DECISION_ID });
  assert.equal(body.conversation_id, "conv-1");
  assert.equal(body.message_id, "msg-1");
  assert.equal(body.messages.length, 1);
  assert.equal(body.messages[0].content, "did it work?");
  assert.throws(() => buildAskBody({ messages: [] }), /non-empty/);
});

/* ------------------------------------------------------ unit: chip lifecycle */

test("the chip survives follow-up questions until it is removed or replaced", () => {
  const session = new AssistantSession({ conversationId: "c-1" });
  session.attachContext({ kind: "node", id: NODE_ID, label: "node-a" });

  const first = session.begin("is this node healthy?");
  assert.deepEqual(first.body.context, { kind: "node", id: NODE_ID });
  session.settle(first.messageId, { answer: "yes", citations: [], executed: [] });

  // follow-up: no re-attach, the context is still there
  const second = session.begin("what did you do to it?");
  assert.deepEqual(second.body.context, { kind: "node", id: NODE_ID });
  // ...and the earlier exchange is replayed as history by message id
  assert.equal(second.body.conversation_id, "c-1");
  assert.notEqual(second.body.message_id, first.body.message_id);

  // replacing the element swaps the chip and keeps the history
  session.attachContext({ kind: "audit", id: AUDIT_ID, label: "audit ok" });
  const third = session.begin("and this audit row?");
  assert.deepEqual(third.body.context, { kind: "audit", id: AUDIT_ID });
  assert.ok(third.body.messages.length >= 3);

  // removing it detaches the reference without clearing the conversation
  session.clearContext();
  const fourth = session.begin("what about the fleet?");
  assert.deepEqual(fourth.body.context, { kind: "none" });
  assert.ok(fourth.body.messages.length >= 4);
});

/* --------------------------------------------------- unit: honest states */

test("a pending exchange carries no answer text, and settle() fills the citations", () => {
  const session = new AssistantSession();
  const begun = session.begin("what happened?");
  const pending = session.find(begun.messageId);
  assert.equal(pending.state, "pending");
  assert.equal(pending.text, "");

  session.settle(begun.messageId, {
    answer: "node-a was restarted by the operator at 03:12.",
    citations: [{ kind: "decision", id: DECISION_ID, label: "oom → restart" }],
    executed: [],
    proposed_actions: [],
    model: "m",
    decided_by: "assistant",
    elapsed_ms: 40,
    truncated: false,
    errors: [],
  });
  assert.equal(pending.state, "answered");
  assert.equal(pending.citations.length, 1);
  // A summary OF A PAST record is not a claim about this turn: no defect flag.
  assert.equal(claimsExecution(pending.text), false);
  assert.equal(pending.claimDefect, false);
});

test("an answer that claims it executed something without executed[] ids is flagged", () => {
  const session = new AssistantSession();
  const { messageId } = session.begin("restart the instance");
  const msg = session.settle(messageId, { answer: "I have restarted the instance.", executed: [] });
  assert.equal(claimsExecution(msg.text), true);
  assert.equal(msg.claimDefect, true);

  const ok = new AssistantSession();
  const second = ok.begin("restart the instance");
  const settled = ok.settle(second.messageId, {
    answer: "I restarted the instance.",
    executed: [{ action: "instance.restart", target: "vm-1", command_id: DECISION_ID, audit_id: AUDIT_ID, state: "ok" }],
  });
  assert.equal(settled.claimDefect, false);
  assert.equal(settled.executed[0].attributed, true);
});

test("executionRows marks an entry with a missing audit_id as unattributed", () => {
  const rows = executionRows([
    { action: "node.drain", target: NODE_ID, command_id: "c", audit_id: null, state: "queued" },
    { action: "instance.stop", target: "vm", command_id: "c2", audit_id: AUDIT_ID, state: "ok" },
  ]);
  assert.equal(rows[0].attributed, false);
  assert.equal(rows[1].attributed, true);
});

test("errorView types the controller's 503 and 422 reasons instead of inventing an answer", () => {
  const err503 = errorView({ status: 503, code: "MODEL_UNAVAILABLE", message: "the model did not answer: timeout" });
  assert.equal(err503.kind, "model_unavailable");
  assert.equal(err503.badge, "503 · MODEL_UNAVAILABLE");
  assert.equal(err503.retryable, true);
  assert.match(err503.message, /the model did not answer/);

  const err422 = errorView({ status: 422, code: "INVALID_ARGUMENT", message: "context.kind must be one of …" });
  assert.equal(err422.kind, "invalid_argument");
  assert.equal(err422.retryable, false);

  const session = new AssistantSession();
  const { messageId } = session.begin("why?");
  const failed = session.fail(messageId, { status: 503, code: "MODEL_UNAVAILABLE", message: "no model" });
  assert.equal(failed.state, "error");
  assert.equal(failed.text, "");
  assert.equal(failed.error.kind, "model_unavailable");
});

test("suggestions are concrete follow-ups per element, not one generic wall", () => {
  for (const kind of CONTEXT_KINDS) {
    const list = suggestionsFor(kind);
    assert.equal(list.length, 3);
    assert.ok(list.every((s) => typeof s === "string" && s.length > 10));
  }
  assert.notDeepEqual(suggestionsFor("node"), suggestionsFor("audit"));
});

/* --------------------------------------------- offline responder (contract) */

test("?offline=1 assistant answers are contract-shaped AND labelled fixture data", async () => {
  const backend = new OfflineBackend();
  const fixtures = backend.operatorFixtures();
  const decision = fixtures[1].decision;
  const event = fixtures[1].event;

  const res = await backend.assistantAsk({
    messages: [{ role: "user", content: "what did Termini do about this?" }],
    context: { kind: "incident", id: event.id },
    conversation_id: "c",
    message_id: "m",
  });
  for (const key of ["answer", "citations", "executed", "proposed_actions", "model", "decided_by", "elapsed_ms", "truncated", "errors"]) {
    assert.ok(key in res, `offline responder must carry ${key} (§4.10)`);
  }
  assert.equal(res.fixture, true, "offline answers are fixture data");
  assert.equal(res.model, "offline");
  assert.equal(res.decided_by, "assistant");
  assert.deepEqual(res.executed, []);
  assert.ok(res.citations.some((c) => c.kind === "decision" && c.id === decision.id));
  assert.ok(res.citations.every((c) => typeof c.id === "string" && c.id.length > 0));

  const nodeRes = await backend.assistantAsk({ messages: [{ role: "user", content: "is it healthy?" }], context: { kind: "node", id: "01H4KZ9Q2R3T5V7W9X1Y3Z5B1E" } });
  assert.equal(nodeRes.citations[0].kind, "node");

  // A question that asks for an action must NOT fabricate an execution.
  const act = await backend.assistantAsk({
    messages: [{ role: "user", content: "restart it now" }],
    context: { kind: "incident", id: event.id },
  });
  assert.deepEqual(act.executed, []);
  assert.ok(act.errors.some((e) => e.reason === "offline_no_executor"));
});

/* ------------------------------------------------------ served-page: panel */

function chatDom() {
  const panel = document.createElement("aside");
  const messages = document.createElement("ol");
  const chips = {
    panel,
    launcher: document.createElement("button"),
    closeBtn: document.createElement("button"),
    modeEl: document.createElement("span"),
    contextHost: document.createElement("div"),
    contextChip: document.createElement("span"),
    contextKind: document.createElement("span"),
    contextLabel: document.createElement("span"),
    contextClear: document.createElement("button"),
    suggestions: document.createElement("div"),
    messages,
    form: document.createElement("form"),
    input: document.createElement("textarea"),
    sendBtn: document.createElement("button"),
  };
  document.body.appendChild(panel);
  return chips;
}

function decisionFixture(overrides = {}) {
  return {
    id: DECISION_ID,
    event_id: EVENT_ID,
    decided_at: TS,
    node_id: NODE_ID,
    rule_id: "oom-guard-01",
    action_kind: "instance.restart",
    action_params: { instance_name: "vm-1" },
    reason: "RAM above the frozen threshold",
    decision_source: "model",
    confidence: 0.9,
    incident_title: "Memory exhaustion on node a",
    incident_explanation: "ram_used held above the frozen threshold.",
    incident_evidence: JSON.stringify([{ field: "ram_used_bytes", value: 33000000000 }]),
    incident_actions: JSON.stringify([{ action_kind: "instance.restart", target: "vm-1" }]),
    incident_generated: "model",
    decision_trace: null,
    ...overrides,
  };
}

function decisionsPanelDom() {
  const host = document.createElement("div");
  const empty = document.createElement("div");
  const list = document.createElement("ol");
  const count = document.createElement("div");
  const updated = document.createElement("div");
  host.appendChild(empty);
  host.appendChild(list);
  host.appendChild(count);
  host.appendChild(updated);
  document.body.appendChild(host);
  return { list, emptyEl: empty, countEl: count, updatedEl: updated, deciderEl: document.createElement("div"), filterHost: document.createElement("div") };
}

test("panel: clicking 'Ask AI' on an incident seeds the panel with that incident's decision id", () => {
  const chat = new ChatPanel(chatDom(), { respond: async () => ({ answer: "x", citations: [], executed: [] }) });
  const captured = [];
  const panel = new DecisionsPanel(decisionsPanelDom(), { onAskAi: (ctx) => captured.push(chat.attachContext(ctx)) });

  panel.update({
    decisions: [decisionFixture()],
    events: [{ id: EVENT_ID, node_id: NODE_ID, rule_id: "oom-guard-01", type: "oom", metric: "ram_used_bytes", value: 33000000000, threshold: ">= 95%", fired_at: TS, breach_started_at: TS, evidence: [] }],
    audits: [{ id: AUDIT_ID, decision_id: DECISION_ID, node_id: NODE_ID, rule_id: "oom-guard-01", action_kind: "instance.restart", result: "ok", detail: "state transitioned", executed_at: TS }],
    seriesByNode: {},
  });

  // The incident card's own affordance (not the row-level one).
  const card = panel.dom.list.querySelector(".incident-card");
  assert.ok(card, "the incident card rendered");
  const askButtons = card.querySelectorAll("button").filter((b) => b.dataset.askAiKind === "incident");
  assert.equal(askButtons.length, 1, "the incident card carries exactly one Ask AI affordance");

  askButtons[0].dispatchEvent({ type: "click" });

  // 1) the chip is bound to the decision the card was rendered from...
  const chipEl = chat.dom.contextChip;
  assert.equal(chipEl.dataset.contextDecisionId, DECISION_ID);
  assert.match(chipEl.dataset.contextText, new RegExp(`decision ${DECISION_ID.slice(0, 10)}`));
  // 2) ...and carries the id the controller resolves for kind=incident.
  assert.equal(chipEl.dataset.contextKind, "incident");
  assert.equal(chipEl.dataset.contextId, EVENT_ID);
  assert.equal(captured[0].meta.decisionId, DECISION_ID);
});

test("asking with a seeded incident sends {kind,id} as data and renders citations as jump buttons", async () => {
  const citations = [];
  let requestBody = null;
  const chat = new ChatPanel(chatDom(), {
    respond: async (body) => {
      requestBody = body;
      return {
        answer: "The decision restarted the instance; the audit row records result ok.",
        citations: [{ kind: "decision", id: DECISION_ID, label: "oom → restart" }],
        executed: [{ action: "instance.restart", target: "vm-1", command_id: DECISION_ID, audit_id: AUDIT_ID, state: "ok" }],
        proposed_actions: [],
        model: "mock",
        decided_by: "assistant",
        elapsed_ms: 12,
        truncated: false,
        errors: [],
      };
    },
    onCitation: (c) => citations.push(c),
  });

  chat.attachContext({ kind: "incident", id: EVENT_ID, label: "Memory exhaustion on node a", meta: { decisionId: DECISION_ID } });
  await chat.ask("did it work?");

  assert.deepEqual(requestBody.context, { kind: "incident", id: EVENT_ID });
  assert.ok(requestBody.message_id, "the message id anchors execution idempotency");
  assert.ok(requestBody.conversation_id, "the conversation id is kept for the session");

  const list = chat.dom.messages;
  const answer = list.querySelector(".chat-answer");
  assert.match(answer.textContent, /audit row records result ok/);

  // citations are buttons that name a record and jump to it
  const cite = list.querySelector(".chat-cite");
  assert.match(cite.textContent, /decision · oom → restart/);
  cite.dispatchEvent({ type: "click" });
  assert.deepEqual(citations[0], { kind: "decision", id: DECISION_ID, label: "oom → restart" });

  // executed[] shows what ran WITH its ids, and links to the audit row
  const exec = list.querySelector(".chat-executed");
  assert.ok(exec, "executed[] is rendered");
  const ids = Array.from(exec.querySelectorAll(".chat-exec-id")).map((n) => n.textContent);
  assert.deepEqual(ids, [DECISION_ID, AUDIT_ID]);
  const auditLink = exec.querySelector(".chat-exec-link");
  auditLink.dispatchEvent({ type: "click" });
  assert.equal(citations[1].kind, "audit");
  assert.equal(citations[1].id, AUDIT_ID);
  assert.match(citations[1].label, /^audit /);
});

test("the panel renders a pending state that is not an answer, and a typed 503 error", async () => {
  let release;
  const chat = new ChatPanel(chatDom(), {
    respond: () =>
      new Promise((resolve) => {
        release = resolve;
      }),
  });

  const pending = chat.ask("is node a healthy?");
  const pendingBubble = chat.dom.messages.querySelector(".chat-state-pending");
  assert.ok(pendingBubble, "a pending bubble exists");
  assert.match(pendingBubble.textContent, /no answer yet/);
  assert.equal(pendingBubble.querySelector(".chat-answer"), null, "pending state renders no answer text");
  assert.equal(pendingBubble.getAttribute("aria-busy"), "true");

  release({ answer: "ok", citations: [], executed: [] });
  await pending;
  assert.equal(chat.dom.messages.querySelector(".chat-state-pending"), null);

  let fail;
  const chat2 = new ChatPanel(chatDom(), {
    respond: () =>
      new Promise((_resolve, reject) => {
        fail = reject;
      }),
  });
  const errored = chat2.ask("why did this happen?");
  fail(Object.assign(new Error("the model did not answer: timeout"), { status: 503, code: "MODEL_UNAVAILABLE" }));
  await errored;
  const errBox = chat2.dom.messages.querySelector(".chat-error");
  assert.ok(errBox, "the error state renders");
  assert.match(errBox.textContent, /503 · MODEL_UNAVAILABLE/);
  assert.match(errBox.textContent, /the model did not answer: timeout/);
  assert.equal(chat2.dom.messages.querySelector(".chat-answer"), null, "a failure never renders an answer");
});

test("the offline panel labels its answers as fixture data", async () => {
  const backend = new OfflineBackend();
  const chat = new ChatPanel(chatDom(), {
    respond: (body) => backend.assistantAsk(body),
    offline: true,
  });
  assert.match(chat.dom.modeEl.textContent, /fixture data/);
  chat.attachContext({ kind: "node", id: "01H4KZ9Q2R3T5V7W9X1Y3Z5B1E", label: "termini-node-a" });
  await chat.ask("is it healthy?");
  const fixtureNote = chat.dom.messages.querySelector(".chat-fixture");
  assert.ok(fixtureNote, "offline answers are marked");
  assert.match(fixtureNote.textContent, /not a model answer/);
});

test("removing the chip detaches the context but keeps the conversation", async () => {
  let lastBody = null;
  const chat = new ChatPanel(chatDom(), {
    respond: async (body) => {
      lastBody = body;
      return { answer: "ok", citations: [], executed: [] };
    },
  });
  chat.attachContext({ kind: "node", id: NODE_ID, label: "node-a" });
  await chat.ask("first");
  assert.deepEqual(lastBody.context, { kind: "node", id: NODE_ID });

  chat.dom.contextClear.dispatchEvent({ type: "click" });
  assert.equal(chat.session.context, null);
  assert.equal(chat.dom.contextHost.hidden, true);
  await chat.ask("second");
  assert.deepEqual(lastBody.context, { kind: "none" });
  assert.ok(chat.dom.messages.querySelectorAll(".chat-msg-user").length >= 2, "history survives the chip removal");
});
