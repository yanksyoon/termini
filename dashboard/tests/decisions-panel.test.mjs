/*
 * Decisions panel — disclosure state across the 3 s decision poll.
 *
 * Regression test for t_393cc9cc. The panel is rebuilt on every
 * `update()` (once per `cfg.decisionMs`, 3000 ms by default), and it used to
 * rebuild by clearing the whole list — so an expanded "AI thinking log"
 * `<details>` was destroyed and recreated collapsed, at most one poll interval
 * after it was opened, and the row the reader was on was thrown away with it.
 *
 * The assertions here are about what the USER sees across a poll, not about
 * internals: same disclosure still open, a user-collapsed one still collapsed,
 * the row element still the one that was on screen, and nothing the "say it out
 * loud" rules require has moved inside a collapse.
 *
 * Run: node --test dashboard/tests/decisions-panel.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { installDom } from "./helpers/minidom.mjs";

// The mini-DOM has to exist before any module renders (js/ui/dom.js resolves
// `document` at call time, not import time, so installing it first is enough).
const document = installDom();
const { DecisionsPanel } = await import("../js/ui/decisions.js");

const TS = "2026-10-17T02:00:00.000Z";
const ID_A = "01J9Z3K4M5N6P7Q8R9S0T1U2V3";
const ID_B = "01J9Z3K4M5N6P7Q8R9S0T1U2W4";

function steps(decidedBy = "model") {
  return [
    { step: "threshold_fired", state: "ok", ts: TS, label: "ram_used >= 95%" },
    { step: "model_call", state: "ok", ts: TS, label: "model answered" },
    { step: "reply_received", state: "ok", ts: TS, label: "reply", detail: '{"action_kind":"restart"}' },
    { step: "action_decided", state: decidedBy === "fallback" ? "fallback" : "ok", ts: TS, label: "restart" },
  ];
}

function decision(overrides = {}) {
  return {
    id: ID_A,
    decided_at: TS,
    node_id: "node-a",
    rule_id: "ram_saturation",
    action_kind: "lxd.restart_instance",
    action_params: { instance: "web-1" },
    reason: "RAM above the frozen threshold",
    decision_source: "model",
    confidence: 0.82,
    event_id: null,
    decision_trace: { trace_version: 1, decided_by: "model", steps: steps() },
    ...overrides,
  };
}

/** A decision carrying the §4.2 incident record — the trace sits inside the card. */
function withIncident(overrides = {}) {
  return decision({
    incident_title: "RAM saturation on node-a",
    incident_explanation: "ram_used held above the frozen threshold for two windows.",
    incident_evidence: JSON.stringify([{ field: "ram_used_pct", value: 97.4 }]),
    incident_actions: JSON.stringify([{ action_kind: "lxd.restart_instance", target: { instance: "web-1" } }]),
    incident_generated: "model",
    ...overrides,
  });
}

/** A decision with no incident record — the trace renders at row level. */
function withoutIncident(overrides = {}) {
  return decision(overrides);
}

function makePanel() {
  const host = document.createElement("div");
  const empty = document.createElement("div");
  const list = document.createElement("ol");
  const count = document.createElement("div");
  const updated = document.createElement("div");
  host.appendChild(empty);
  host.appendChild(list);
  const panel = new DecisionsPanel(
    { list, emptyEl: empty, countEl: count, updatedEl: updated },
    { onSample: () => {}, onAudit: () => {}, onChart: () => {} },
  );
  return { panel, list };
}

const rowsOf = (list) => list.children.filter((n) => n.tagName === "LI");

function rowFor(list, id) {
  return rowsOf(list).find((li) => li.dataset.decisionId === id) || null;
}

function traceDetails(root) {
  return root ? root.querySelector("details.trace-details") : null;
}

/** What a user does: click the summary open (or closed) — `<details>` fires toggle. */
function setOpen(details, open) {
  assert.ok(details, "the trace <details> must exist");
  details.open = open;
  return details;
}

const poll = (panel, decisions, extra = {}) =>
  panel.update({ decisions, events: [], audits: [], seriesByNode: {}, ...extra });

/* ------------------------------------------------------------------ tests */

test("an expanded thinking log stays open across the next poll", () => {
  const { panel, list } = makePanel();
  const decisions = [withIncident()];

  poll(panel, decisions);
  const liBefore = rowFor(list, ID_A);
  const detailsBefore = traceDetails(liBefore);
  assert.equal(detailsBefore.open, false, "a trace starts collapsed");
  setOpen(detailsBefore, true);

  // One decision poll, same payload — exactly what the 3 s timer does.
  poll(panel, decisions);

  const liAfter = rowFor(list, ID_A);
  const detailsAfter = traceDetails(liAfter);
  assert.equal(detailsAfter.open, true, "the expanded thinking log closed itself on the poll");
  assert.equal(liAfter, liBefore, "the row was torn down and rebuilt instead of being kept");
  assert.equal(detailsAfter, detailsBefore, "the disclosure was torn down and rebuilt instead of being kept");
});

test("a trace the user collapsed stays collapsed across polls", () => {
  const { panel, list } = makePanel();
  const decisions = [withoutIncident()];

  poll(panel, decisions);
  setOpen(traceDetails(rowFor(list, ID_A)), true);
  poll(panel, decisions);
  setOpen(traceDetails(rowFor(list, ID_A)), false);

  poll(panel, decisions);
  poll(panel, decisions);
  assert.equal(traceDetails(rowFor(list, ID_A)).open, false, "a log the user closed reopened on its own");
});

test("a row whose content changed is rebuilt but keeps its open log", () => {
  const { panel, list } = makePanel();

  poll(panel, [withIncident()]);
  const liBefore = rowFor(list, ID_A);
  setOpen(traceDetails(liBefore), true);

  // The audit row lands: the row's content changed, so it must re-render.
  poll(panel, [withIncident()], {
    audits: [
      {
        id: "01J9Z3K4M5N6P7Q8R9S0T1U2X9",
        decision_id: ID_A,
        result: "ok",
        executed_at: TS,
      },
    ],
  });

  const liAfter = rowFor(list, ID_A);
  assert.notEqual(liAfter, liBefore, "a changed row should be re-rendered");
  assert.equal(traceDetails(liAfter).open, true, "re-rendering the row dropped the open state");
});

test("row identity survives an unchanged poll, per decision", () => {
  const { panel, list } = makePanel();
  const decisions = [withIncident(), withoutIncident({ id: ID_B, decision_source: "fallback", confidence: null })];

  poll(panel, decisions);
  const a = rowFor(list, ID_A);
  const b = rowFor(list, ID_B);
  poll(panel, decisions);

  assert.equal(rowFor(list, ID_A), a);
  assert.equal(rowFor(list, ID_B), b);
  assert.deepEqual(rowsOf(list).map((li) => li.dataset.decisionId), [ID_A, ID_B]);
});

test("filtering the list and coming back keeps the expanded log", () => {
  const { panel, list } = makePanel();
  const decisions = [withIncident(), withIncident({ id: ID_B })];
  poll(panel, decisions);
  setOpen(traceDetails(rowFor(list, ID_A)), true);

  // Filter to another source: row A leaves the list entirely…
  panel.filter = "deterministic";
  panel.render();
  assert.equal(rowFor(list, ID_A), null);
  // …and comes back on the next poll.
  panel.filter = "all";
  poll(panel, decisions);

  assert.equal(traceDetails(rowFor(list, ID_A)).open, true, "state was keyed to the element, not the decision");
});

test("every disclosure in the decision list is keyed, so the next one cannot regress", () => {
  const { panel, list } = makePanel();
  poll(panel, [withIncident(), withoutIncident({ id: ID_B })]);

  const all = list.querySelectorAll("details");
  assert.equal(all.length, 2, "both rows render a thinking log");
  for (const d of all) {
    assert.ok(d.dataset.disclosure, "a <details> in the list must carry a disclosure key");
  }
  assert.equal(new Set(all.map((d) => d.dataset.disclosure)).size, 2, "keys are unique per decision");
});

test("the say-it-out-loud lines stay outside the collapse", () => {
  const { panel, list } = makePanel();
  poll(panel, [
    withIncident({
      id: ID_B,
      decision_source: "fallback",
      confidence: null,
      fallback_reason: "model_timeout",
      decision_trace: { trace_version: 1, decided_by: "fallback", steps: steps("fallback") },
    }),
  ]);

  const li = rowFor(list, ID_B);
  const details = traceDetails(li);
  assert.equal(details.open, false);
  const alerts = li.querySelector(".trace-alerts");
  assert.ok(alerts, "the fallback line is rendered outside the disclosure");
  assert.equal(alerts.querySelector("details"), null, "nothing the reader must see is buried in the collapse");
  assert.match(alerts.textContent, /fallback reason model_timeout/);
  // The fallback trace is still visually distinct from a model trace.
  assert.equal(details.dataset.traceDecider, "fallback");
});

test("a row with no trace still says so, and renders no empty disclosure", () => {
  const { panel, list } = makePanel();
  poll(panel, [decision({ decision_trace: null })]);

  const li = rowFor(list, ID_A);
  assert.ok(li.querySelector(".trace-none"), "the trace-none block is rendered");
  assert.equal(li.querySelector("details"), null, "no disclosure is drawn for a trace that is absent");
});

test("focus inside the list survives a poll that rebuilds the row", () => {
  const host = document.createElement("div");
  const empty = document.createElement("div");
  const list = document.createElement("ol");
  host.appendChild(empty);
  host.appendChild(list);
  const panel = new DecisionsPanel({ list, emptyEl: empty }, { onSample: () => {} });

  const withEvent = (eventId) =>
    withIncident({ event_id: eventId, decision_trace: { trace_version: 1, decided_by: "model", steps: steps() } });

  poll(panel, [withEvent(null)]);
  setOpen(traceDetails(rowFor(list, ID_A)), true);
  // A control inside the row takes focus (the "telemetry sample" link).
  const button = rowFor(list, ID_A).querySelector("button");
  assert.ok(button, "the row has focusable controls");
  button.focus();

  // The decision gains an event id: the row re-renders with an extra control.
  poll(panel, [withEvent("01J9Z3K4M5N6P7Q8R9S0T1U2Y5")]);

  const li = rowFor(list, ID_A);
  assert.equal(traceDetails(li).open, true);
  assert.notEqual(document.activeElement, document.body, "focus was dropped back to the document on the poll");
  let node = document.activeElement;
  while (node && node !== li) node = node.parentNode;
  assert.equal(node, li, "focus left the row it was in");
});
