/*
 * Termini operations dashboard — application controller.
 * Wires config resolution, polling, the SSE log stream, charts and controls.
 */

import { $ } from "./ui/dom.js";
import { ApiClient, ApiError } from "./api.js";
import { LogStream } from "./stream.js";
import { resolveConfig, apiUrl, stripKeysFromQuery } from "./core/config.js";
import { CHART_GROUPS, windowQuery, metricsQuery, requestedMetrics } from "./core/series.js";
import { parseEgressLine, annotationsForNode, sourceMeta } from "./core/decisions.js";
import { renderOverview } from "./ui/overview.js";
import { LogPanel } from "./ui/logs.js";
import { DecisionsPanel } from "./ui/decisions.js";
import { MetricsPanel } from "./ui/metrics.js";
import { processes } from "./core/health.js";
import { ControlsPanel } from "./ui/controls.js";
import { confirmDialog, detailDialog } from "./ui/modal.js";
import { showBanner, hideBanner } from "./ui/banner.js";
import { ok as toastOk, err as toastErr, warn as toastWarn, toast } from "./ui/toast.js";
import { formatError } from "./ui/errors.js";
import { OfflineBackend, OfflineStream } from "./offline.js";
import { MyNodePanel } from "./ui/mynode.js";
import { AgentsPanel } from "./ui/agents.js";
import {
  agentGroups,
  agentsSummary,
  expectedRows,
  expectedSignature,
  inferenceLabel,
  materialisedNames,
  pruneExpected,
  refusedLocally,
  refusalView,
  taskView,
  vmIdForName,
} from "./core/agents.js";
import { registrationView, healthSummary } from "./core/client-node.js";
import { ChatPanel } from "./ui/chat.js";
import { seedContext } from "./core/assistant.js";
import { shortId } from "./core/format.js";

// Non-secret connection settings live in localStorage; the CREDENTIALS do not.
// The project credential and the client's node credential are held in
// sessionStorage — one tab, gone when the tab closes, never in a URL and so
// never inside a link that anyone can copy or share (task t_5649d117).
const STORE_KEY = "termini.dashboard.cfg";
const TOKEN_KEY = "termini.dashboard.token";
const NODE_KEY = "termini.dashboard.node";

const dom = {
  pillNodes: $("#pill-nodes"),
  pillNodesText: $("#pill-nodes-text"),
  pillAgents: $("#pill-agents"),
  pillAgentsText: $("#pill-agents-text"),
  pillBackend: $("#pill-backend"),
  pillBackendText: $("#pill-backend-text"),
  pillStream: $("#pill-stream"),
  pillStreamText: $("#pill-stream-text"),
  clock: $("#clock"),
  settingsBtn: $("#btn-settings"),
  terminalLink: $("#link-terminal"),
  overview: $("#overview"),
  overviewUpdated: $("#overview-updated"),
  refreshBtn: $("#btn-refresh"),
  logState: $("#log-state"),
  logPause: $("#btn-log-pause"),
  logClear: $("#btn-log-clear"),
  severityHost: $("#severity-filters"),
  nodeFilter: $("#node-filter"),
  actorFilter: $("#actor-filter"),
  logCount: $("#log-count"),
  logViewport: $("#log-viewport"),
  logEmpty: $("#log-empty"),
  logList: $("#log-list"),
  chartNode: $("#chart-node"),
  windowButtons: $("#window-buttons"),
  allMetrics: $("#all-metrics"),
  includeSynthetic: $("#include-synthetic"),
  charts: $("#charts"),
  decisionFilters: $("#decision-filters"),
  decisionsUpdated: $("#decisions-updated"),
  decisionsCount: $("#decisions-count"),
  decisionsEmpty: $("#decisions-empty"),
  decisionList: $("#decision-list"),
  decisionViewport: $("#decision-viewport"),
  decider: $("#decider"),
  controlsTarget: $("#controls-target"),
  controlsNodeLabel: $("#controls-node-label"),
  rebootBtn: $("#btn-reboot"),
  drainBtn: $("#btn-drain"),
  nodeActionResult: $("#node-action-result"),
  intentPrompt: $("#intent-prompt"),
  intentBtn: $("#btn-intent"),
  applyBtn: $("#btn-apply"),
  intentResult: $("#intent-result"),
  pendingList: $("#pending-list"),
  remediationResult: $("#remediation-result"),
  settingsDialog: $("#settings-dialog"),
  settingsForm: $("#settings-form"),
  setApi: $("#set-api"),
  setProject: $("#set-project"),
  setToken: $("#set-token"),
  setOffline: $("#set-offline"),
  mynodeEmpty: $("#mynode-empty"),
  mynodeLive: $("#mynode-live"),
  mynodeStateDot: $("#mynode-state-dot"),
  mynodeStateText: $("#mynode-state-text"),
  mynodeDetail: $("#mynode-detail"),
  mynodeEnrolled: $("#mynode-enrolled"),
  mynodeLastSeen: $("#mynode-last-seen"),
  mynodeError: $("#mynode-error"),
  mynodeCards: $("#mynode-cards"),
  mynodeEndpoint: $("#mynode-endpoint"),
  mynodeNodeId: $("#mynode-node-id"),
  mynodeToken: $("#mynode-node-token"),
  mynodeConnect: $("#btn-mynode-connect"),
  mynodeForget: $("#btn-mynode-forget"),
  agentsGroups: $("#agents-groups"),
  agentsEmpty: $("#agents-empty"),
  agentsInference: $("#agents-inference"),
  agentsStatus: $("#agents-status"),
  agentsRefreshBtn: $("#btn-agents-refresh"),
  chatLauncher: $("#btn-chat"),
  chatPanel: $("#chat-panel"),
  chatClose: $("#btn-chat-close"),
  chatMode: $("#chat-mode"),
  chatContextHost: $("#chat-context"),
  chatContextChip: $("#chat-context-chip"),
  chatContextKind: $("#chat-context-kind"),
  chatContextLabel: $("#chat-context-label"),
  chatContextClear: $("#chat-context-clear"),
  chatSuggestions: $("#chat-suggestions"),
  chatMessages: $("#chat-messages"),
  chatForm: $("#chat-form"),
  chatInput: $("#chat-input"),
  chatSend: $("#chat-send"),
};

const state = {
  nodes: [],
  samples: new Map(),
  metricsByNode: new Map(), // node_id -> last /metrics payload (threshold resolution)
  selectedNodeId: null,
  window: "15m",
  includeAll: false,
  includeSynthetic: false,
  loading: true,
  now: Date.now(),
  backend: { fails: 0, lastOk: null, lastError: null },
  pending: new Map(),
  decisionsReady: false,
  lastAudits: [],
  // Agents panel (t_44a2b87b): the rows this tab stopped (telemetry reports only
  // RUNNING instances), the last task per agent, the plan outcome waiting to be
  // reported by a node, and the last answer's provenance for the honest label.
  retainedAgents: new Map(),
  agentTasks: new Map(),
  expectedAgents: [],
  agentPollSeq: new Map(),
  lastProvenance: null,
  lastModel: null,
};

let cfg = resolveConfig(readInputs());
// `?synthetic=1` starts with the demo/seeder nodes revealed (contract §1.1).
state.includeSynthetic = !!cfg.includeSynthetic;
let api = new ApiClient(cfg);
let stream = null;
let pollTimer = null;
let metricsTimer = null;
let decisionTimer = null;

const metricsPanel = new MetricsPanel(dom.charts, {
  onAskAi: (ctx) => askAi(ctx),
  nodeLabel: (nodeId) => {
    const n = state.nodes.find((x) => x.node_id === nodeId);
    return n ? n.registration_id || n.node_id.slice(0, 10) : null;
  },
});

const decisionsPanel = new DecisionsPanel(
  {
    list: dom.decisionList,
    emptyEl: dom.decisionsEmpty,
    countEl: dom.decisionsCount,
    updatedEl: dom.decisionsUpdated,
    deciderEl: dom.decider,
    filterHost: dom.decisionFilters,
  },
  {
    onAudit: (row) => showAuditDetail(row),
    onSample: (row) => showSampleDetail(row),
    onChart: (row) => showOnChart(row),
    onAskAi: (ctx) => askAi(ctx),
  },
);
const logPanel = new LogPanel(
  {
    list: dom.logList,
    emptyEl: dom.logEmpty,
    viewport: dom.logViewport,
    stateHost: dom.logState,
    severityHost: dom.severityHost,
    nodeFilter: dom.nodeFilter,
    actorFilter: dom.actorFilter,
    countEl: dom.logCount,
    pauseBtn: dom.logPause,
    clearBtn: dom.logClear,
    max: cfg.logBufferMax,
  },
  {
    onApprove: (commandId) => decideRemediation(commandId, "approve"),
    onDeny: (commandId) => decideRemediation(commandId, "deny"),
  },
);

const controls = new ControlsPanel(
  {
    rebootBtn: dom.rebootBtn,
    drainBtn: dom.drainBtn,
    nodeLabel: dom.controlsNodeLabel,
    controlsTarget: dom.controlsTarget,
    nodeActionResult: dom.nodeActionResult,
    intentPrompt: dom.intentPrompt,
    intentBtn: dom.intentBtn,
    applyBtn: dom.applyBtn,
    intentResult: dom.intentResult,
    pendingList: dom.pendingList,
    remediationResult: dom.remediationResult,
  },
  {
    onReboot: () => rebootSelected(),
    onDrain: () => drainSelected(),
    onValidateIntent: (prompt) => validateIntent(prompt),
    onApply: (planId) => applyPlan(planId),
    onApprove: (commandId) => decideRemediation(commandId, "approve"),
    onDeny: (commandId) => decideRemediation(commandId, "deny"),
    onAskAi: (ctx) => askAi(ctx),
  },
);

const agentsPanel = new AgentsPanel(
  {
    groups: dom.agentsGroups,
    empty: dom.agentsEmpty,
    inference: dom.agentsInference,
    status: dom.agentsStatus,
    refreshBtn: dom.agentsRefreshBtn,
  },
  {
    onAction: (vmId, action, agent) => agentAction(vmId, action, agent),
    onTask: (vmId, prompt, agent) => giveAgentTask(vmId, prompt, agent),
    onRefresh: () => refreshAgents({ force: true }),
    onDeployIntent: () => {
      if (!dom.intentPrompt) return;
      dom.intentPrompt.scrollIntoView({ behavior: "smooth", block: "center" });
      dom.intentPrompt.focus();
      toast("info", "Deploy an agent", "Describe the agents you want, Validate intent, then Apply plan — the rows appear here.", 8000);
    },
  },
);

/* ------------------------------------------------------------------ config */

function readInputs() {
  const params = new URLSearchParams(location.search);
  const query = {};
  for (const [k, v] of params.entries()) query[k] = v;
  // `query` may still carry ?token=… — resolveConfig refuses it by rule and
  // reports it back as `urlTokenIgnored`; the app then scrubs it from the
  // address bar (scrubUrlCredential()). Nothing below honours it.
  let stored = {};
  try {
    stored = JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {};
  } catch {
    stored = {};
  }
  try {
    const token = sessionStorage.getItem(TOKEN_KEY) || "";
    if (token) stored = { ...stored, token };
  } catch {
    /* storage may be disabled — non-fatal */
  }
  const file = (typeof window !== "undefined" && window.TERMINI_CONFIG) || {};
  return { query, stored, file };
}

function persistConfig() {
  try {
    // The credential is deliberately NOT written here: localStorage outlives the
    // tab and is readable by any script on the origin, so it holds only
    // non-secret connection settings.
    localStorage.setItem(
      STORE_KEY,
      JSON.stringify({ apiBase: cfg.apiBase, projectId: cfg.projectId, offline: cfg.offline }),
    );
  } catch {
    /* storage may be disabled — non-fatal */
  }
  persistToken(cfg.token);
}

function persistToken(token) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

function readNodeCredential() {
  try {
    const raw = sessionStorage.getItem(NODE_KEY);
    if (!raw) return { nodeId: "", nodeToken: "" };
    const obj = JSON.parse(raw) || {};
    return { nodeId: String(obj.nodeId || ""), nodeToken: String(obj.nodeToken || "") };
  } catch {
    return { nodeId: "", nodeToken: "" };
  }
}

function persistNodeCredential(cred) {
  try {
    if (cred && cred.nodeId && cred.nodeToken) sessionStorage.setItem(NODE_KEY, JSON.stringify(cred));
    else sessionStorage.removeItem(NODE_KEY);
  } catch {
    /* ignore */
  }
}

/**
 * A legacy link carrying `?token=…` is not honoured: a credential in a URL is
 * shareable by construction (address bar, history, Referer, any copied link).
 * Strip it from the address bar and the history entry, and tell the viewer to
 * sign in instead.
 */
function scrubUrlCredential() {
  if (!cfg.urlTokenIgnored) return false;
  try {
    const clean = stripKeysFromQuery(location.search);
    history.replaceState(null, "", `${location.pathname}${clean}${location.hash || ""}`);
  } catch {
    /* non-fatal */
  }
  return true;
}

/** The controller origin (install one-liner / docs) from the REST base. */
function controllerOrigin() {
  const base = (cfg.apiBase || "").replace(/\/+$/, "").replace(/\/v1$/, "");
  return base || "";
}

/** The REST root incl. the version segment — the same normalisation apiUrl uses. */
function restRoot() {
  const base = (cfg.apiBase || "").replace(/\/+$/, "");
  if (!base) return "/v1";
  return base.endsWith("/v1") || base.includes("/v1") ? base : `${base}/v1`;
}

/* ------------------------------------------------------------------ backend */

function makeBackend() {
  if (cfg.offline) {
    const b = new OfflineBackend();
    return { kind: "offline", api: b, streamFactory: () => new OfflineStream(b, streamHandlers()) };
  }
  return { kind: "live", api, streamFactory: () => new LogStream(cfg, streamHandlers()) };
}

let backend = null;

function streamHandlers() {
  return {
    onLine: (line) => {
      logPanel.addLine(line);
      trackRemediation(line);
      // The egress record (`externalModelCall … backend=… model=… destination=…`)
      // is how the dashboard learns WHO answered the decision-step call. It is
      // read off the stream — never hardcoded.
      const egress = parseEgressLine(line.message);
      if (egress && (!decisionsPanel.decider || egress.phase !== "before" || decisionsPanel.decider.phase !== "after")) {
        decisionsPanel.setDecider(egress);
      }
    },
    onStatus: ({ state: st, detail }) => {
      logPanel.setStreamState(st, detail);
      setPill(dom.pillStream, dom.pillStreamText, st === "open" ? "ok" : st === "stopped" ? "muted" : "warn", `stream ${st}`);
    },
    onError: () => {
      /* EventSource retries on its own; state is surfaced via onStatus. */
    },
  };
}

/* -------------------------------------------------- the client's own node */

/**
 * Client view (task t_5649d117). One node, read with THAT NODE's own
 * credential: `GET /v1/nodes/{node_id}/telemetry/recent` — own node only, a
 * cross-node id is refused with 403. That per-node `node_token` is the only
 * read credential the frozen enrollment contract gives a client (no client
 * identity store, no fleet list), so isolation is a property of the credential
 * itself: this view cannot address anyone else's node. The credential travels
 * in the Authorization header and is never part of a URL.
 */
let nodeApi = null;
let nodeCredential = readNodeCredential();
let nodeTimer = null;

const myNodePanel = new MyNodePanel(
  {
    empty: dom.mynodeEmpty,
    live: dom.mynodeLive,
    stateDot: dom.mynodeStateDot,
    stateText: dom.mynodeStateText,
    detail: dom.mynodeDetail,
    enrolledAt: dom.mynodeEnrolled,
    lastSeen: dom.mynodeLastSeen,
    error: dom.mynodeError,
    cards: dom.mynodeCards,
    endpoint: dom.mynodeEndpoint,
    nodeId: dom.mynodeNodeId,
    token: dom.mynodeToken,
    connectBtn: dom.mynodeConnect,
    forgetBtn: dom.mynodeForget,
  },
  {
    onConnect: ({ nodeId, nodeToken }) => {
      nodeCredential = { nodeId, nodeToken };
      persistNodeCredential(nodeCredential);
      nodeApi = null;
      myNodePanel.setCredential(nodeCredential);
      if (!nodeId || !nodeToken) {
        toastWarn("Enter both values from your node: the node_id and the node_token.");
      }
      refreshClientNode();
    },
    onForget: () => {
      nodeCredential = { nodeId: "", nodeToken: "" };
      persistNodeCredential(nodeCredential);
      nodeApi = null;
      myNodePanel.setCredential(nodeCredential);
      myNodePanel.render(registrationView({}), null, {});
      dom.mynodeEndpoint.textContent = "—";
      toastOk("Forgot this node's credential in this tab.");
    },
  },
);

function clientNodeApi() {
  if (!nodeApi || nodeApi.cfg.token !== nodeCredential.nodeToken) {
    // Its OWN client: the node credential never mixes with the project
    // credential above, and this path sends no project id at all.
    nodeApi = new ApiClient({ ...cfg, token: nodeCredential.nodeToken });
  }
  return nodeApi;
}

async function refreshClientNode() {
  if (!nodeCredential.nodeId || !nodeCredential.nodeToken) {
    myNodePanel.render(registrationView({}), null, {});
    return;
  }
  const endpoint = `${restRoot()}/nodes/${nodeCredential.nodeId}/telemetry/recent`;
  let samples = [];
  let error = null;
  try {
    const res = await clientNodeApi().nodeTelemetryRecent(nodeCredential.nodeId, 5);
    samples = (res && res.samples) || [];
  } catch (e) {
    error = e;
  }
  const view = registrationView({ ...nodeCredential, samples, error, now: Date.now() });
  myNodePanel.render(view, healthSummary(view.sample), { endpoint, stateValue: view.state });
}

/* ------------------------------------------------------------------ polling */

async function refreshNodes() {
  if (missingConfig()) return; // the config banner already explains what to do
  try {
    const list = await backend.api.listNodes({ includeSynthetic: state.includeSynthetic });
    const nodes = (list && list.nodes) || [];
    state.nodes = nodes;
    state.backend.fails = 0;
    state.backend.lastOk = Date.now();
    hideBanner();

    if (!state.selectedNodeId && nodes.length) selectNode(nodes[0].node_id);
    if (state.selectedNodeId && !nodes.some((n) => n.node_id === state.selectedNodeId)) {
      selectNode(nodes.length ? nodes[0].node_id : null);
    }

    // as-built fallback: derive health from the latest sample when the
    // controller does not yet send a `health` object.
    await fillMissingSamples(nodes);

    state.loading = false;
    state.now = Date.now();
    renderOverview(dom.overview, state, overviewHandlers());
    logPanel.setNodes(nodes);
    decisionsPanel.setNodes(nodes);
    syncNodeSelectors();
    updatePills();
    await refreshAgents();
    dom.overviewUpdated.textContent = `updated ${new Date().toLocaleTimeString("en-GB", { hour12: false })}`;
  } catch (e) {
    handleBackendError(e);
  }
}

/* ---------------------------------------------------------------- agents */

/**
 * The Agents panel's whole data path: each node's latest telemetry Sample
 * (`metrics_latest`, dashboard.md §1.2) is the ONLY inventory — one row per
 * `lxd_instances[]` entry (node-telemetry.md §4.6). Nothing here fabricates an
 * instance; a node that cannot report its inventory renders as unknown.
 *
 * Synthetic (seeder) nodes are read too, because the panel shows their rows as
 * evidence (t_df3120a8) — only to the reader who asked for them (`?synthetic=1`
 * or the Demo-nodes toggle, which is what puts them in `state.nodes`). Their
 * rows are never counted as the client's agents and never offered a task.
 */
async function refreshAgents({ force = false } = {}) {
  if (missingConfig() || !state.nodes.length) {
    agentsPanel.render();
    return;
  }
  const nodes = state.nodes;
  await Promise.all(
    nodes.map(async (n) => {
      try {
        const detail = await backend.api.getNode(n.node_id);
        const sample = detail && detail.metrics_latest ? detail.metrics_latest : null;
        if (sample) state.samples.set(n.node_id, sample);
      } catch {
        /* keep the last known sample: it is the last fact the node reported */
      }
    }),
  );
  renderAgents();
}

function renderAgents() {
  const view = agentGroups({
    nodes: state.nodes,
    samples: state.samples,
    retained: state.retainedAgents,
    now: Date.now(),
  });

  // A row telemetry now reports (again) supersedes the retained "stopped by you".
  for (const agent of view.agents) {
    if (!agent.retained) state.retainedAgents.delete(agent.vmId);
  }

  // A just-applied plan's instances: drop the placeholder once reported, stop
  // calling it "provisioning" once a long time has passed, and finally drop it.
  // The prune is a pure function so this cannot silently regress to "the row is
  // dropped before it ever renders" (t_6d63e8a2).
  if (state.expectedAgents.length) {
    const remaining = pruneExpected(state.expectedAgents, view.agents, Date.now());
    const signature = expectedSignature(remaining);
    if (signature !== expectedSignature(state.expectedAgents)) {
      state.expectedAgents = remaining;
      agentsPanel.setExpected(remaining);
    }
  }

  agentsPanel.setInference(inferenceLabel(state.lastProvenance, state.lastModel));
  agentsPanel.render(view, state.agentTasks);

  const summary = agentsSummary(view);
  setPill(dom.pillAgents, dom.pillAgentsText, summary.kind, summary.text);
  if (dom.agentsStatus) {
    dom.agentsStatus.textContent = `updated ${new Date().toLocaleTimeString("en-GB", { hour12: false })}`;
  }
}

/**
 * Start / stop / restart one agent (controller-api.md §4.10). Human-issued and
 * audited exactly like a node Restart: the controller writes the command row and
 * the operator-log line, and the result says what LXD actually answered.
 */
async function agentAction(vmId, action, agent) {
  if (agent && agent.actionsAvailable === false) {
    // Same rule as the task action: nothing is sent for a row that has no
    // instance behind it (t_df3120a8).
    toastWarn("No action sent", `${agent.label || shortId(vmId)}: ${agent.taskableReason || "this row cannot be actioned"}`);
    return;
  }
  if (typeof backend.api.agentAction !== "function") {
    toastErr("No agent action API on this backend", "The offline demo has no agent operations; use the live controller.");
    return;
  }
  const label = (agent && agent.label) || vmId;
  const verb = action === "restart" ? "Restart" : action === "stop" ? "Stop" : "Start";
  if (action !== "start") {
    const confirmed = await confirmDialog({
      title: `${verb} agent ${label}?`,
      message:
        action === "stop"
          ? "The controller stops the instance and confirms the state. A stopped agent is no longer reported by telemetry (only RUNNING instances are), so its row stays here labelled “stopped by you”."
          : "The controller restarts the instance and confirms it is RUNNING again. The restart is recorded as a human-issued command.",
      confirmLabel: `${verb} agent`,
      danger: action === "stop",
    });
    if (!confirmed) return;
  }
  agentsPanel.setBusy(vmId, action);
  try {
    const res = await backend.api.agentAction(vmId, action);
    const ok = res && res.state === "succeeded";
    const msg = `${res && res.type ? res.type : action} · ${res && res.state ? res.state : "?"} · command ${res && res.command_id ? res.command_id.slice(0, 10) + "…" : "—"}`;
    if (ok) toastOk(`${verb} issued`, `${label}: ${msg}`);
    else toastWarn(`${verb} did not take effect`, `${label}: ${msg} — ${res && res.result ? res.result.detail : "see the operator log"}`);
    if (ok && action === "stop") {
      state.retainedAgents.set(vmId, { ...agent, state: "stopped", retained: true, retainedAt: Date.now() });
    }
    if (ok && (action === "start" || action === "restart")) {
      state.retainedAgents.delete(vmId);
    }
    await refreshAgents({ force: true });
  } catch (e) {
    toastErr(`${verb} failed`, `${label}: ${formatError(e)}`);
    await refreshAgents({ force: true });
  } finally {
    agentsPanel.setBusy(vmId, null);
  }
}

/**
 * Give one agent a task (controller-api.md §4.9) and follow it to a terminal
 * state. The answer and the endpoint's own provenance label are rendered with
 * the agent identified, so "which agent produced which answer" is never implied.
 *
 * A row the panel knows cannot answer is refused HERE as well as in the UI, so
 * no request leaves the page for it (t_df3120a8). A refusal that does come back
 * is rendered as the controller's answer — code, message, request_id and every
 * `details[0]` field — not as a one-line transport error.
 */
async function giveAgentTask(vmId, prompt, agent) {
  if (agent && agent.taskable === false) {
    const refusal = refusedLocally(agent.taskableReason, vmId);
    state.agentTasks.set(vmId, taskView({ state: "error", error_code: refusal.code, error_message: refusal.message, refusal, task_id: null }));
    agentsPanel.setBusy(vmId, null);
    renderAgents();
    toastWarn("No task sent", `${agent.label || shortId(vmId)}: ${refusal.message}`);
    return;
  }
  if (typeof backend.api.submitAgentTask !== "function") {
    toastErr("No agent task API on this backend", "This demo backend has no agent surface; use the live controller.");
    agentsPanel.setBusy(vmId, null);
    return;
  }
  try {
    const task = await backend.api.submitAgentTask(vmId, { prompt });
    state.agentTasks.set(vmId, taskView(task));
    agentsPanel.setBusy(vmId, null);
    renderAgents();
    toastOk("Task accepted", `agent ${vmId.slice(0, 8)}… · task ${task && task.task_id ? task.task_id.slice(0, 10) + "…" : "?"} · ${task && task.state ? task.state : "queued"}`);
    pollAgentTask(vmId, task.task_id);
  } catch (e) {
    const refusal = refusalView(e);
    const view = taskView({
      state: "error",
      error_code: refusal.code,
      error_message: refusal.message,
      refusal,
      task_id: null,
    });
    state.agentTasks.set(vmId, view);
    agentsPanel.setBusy(vmId, null);
    renderAgents();
    toastErr("Task not accepted", `agent ${vmId.slice(0, 8)}…: ${refusal.headline}`);
  }
}

async function pollAgentTask(vmId, taskId) {
  if (!taskId) return;
  const seq = (state.agentPollSeq.get(vmId) || 0) + 1;
  state.agentPollSeq.set(vmId, seq);
  const deadline = Date.now() + 300_000; // controller deadline is 300 s (§4.9)
  let last = state.agentTasks.get(vmId);
  while (Date.now() < deadline && state.agentPollSeq.get(vmId) === seq) {
    await sleep(1500);
    let task;
    try {
      task = await backend.api.getAgentTask(vmId, taskId);
    } catch (e) {
      // One failed poll is not a failed task; keep the last known state and try
      // again. A 404 means the record is not ours to read — say so and stop.
      if (e && (e.status === 404 || e.status === 403)) {
        state.agentTasks.set(vmId, taskView({ state: "error", error_code: e.code || "NOT_FOUND", error_message: "the task record is not readable with this credential", task_id: taskId }));
        renderAgents();
        return;
      }
      continue;
    }
    const view = taskView(task);
    if (!view) return;
    const changed =
      !last ||
      last.state !== view.state ||
      last.answer !== view.answer ||
      last.errorCode !== view.errorCode;
    last = view;
    state.agentTasks.set(vmId, view);
    if (changed) {
      // The panel-level honesty label follows the most recent real answer.
      if (view.provenance) {
        state.lastProvenance = view.provenance;
        state.lastModel = view.model;
      }
      renderAgents();
    }
    if (view.terminal) {
      if (view.state === "done") {
        toastOk("Answer from your agent", `${vmId.slice(0, 8)}… · ${view.model || "model"} · ${view.durationMs !== null ? view.durationMs + " ms" : "done"}`);
      } else {
        toastErr("Agent task failed", `${vmId.slice(0, 8)}… · ${view.errorCode || "ERROR"}: ${view.errorMessage || ""}`);
      }
      return;
    }
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * After `Apply plan`: turn the controller's returned instance NAMES into the
 * telemetry ids they will be reported as (the §4.6 hash rule), show them as
 * `provisioning` rows, and poll until the node's own telemetry lists them.
 * This is what makes the plan's outcome land in the panel instead of a toast.
 *
 * The rows themselves come from `expectedRows()` (one tested path, which reads
 * both `allocations[].vm_ids` and the flat `vm_ids`); this function only owns the
 * panel state. `t_6d63e8a2`: the previous inline copy read `allocations[]` only
 * and returned silently when the response had none, so a response shape it did
 * not expect produced NO rows and NO message — the failure looked like "the
 * placeholder never renders".
 */
async function expectAgentsFromJob(job) {
  const known = Array.from(state.samples.values()).flatMap((s) =>
    Array.isArray(s && s.lxd_instances) ? s.lxd_instances.map((i) => i && i.vm_id) : [],
  );
  const rows = await expectedRows(job, known, Date.now(), vmIdForName);
  const names = materialisedNames(job);

  if (!rows.length) {
    if (!names.length) {
      // Say it, do not go quiet: an apply answered no instance names, so there
      // is nothing to point at until telemetry reports something.
      toastWarn(
        "Plan applied — no instance names",
        "The controller's apply response listed no instances, so there is no row to point at yet; the panel will show them when a node reports them.",
      );
    }
    // Otherwise every instance is already on screen: nothing to add.
    return;
  }

  // Merge, do not replace: a second plan applied while the first is still
  // waiting must not hide the first one's rows.
  const merged = new Map(state.expectedAgents.map((r) => [r.name, r]));
  for (const row of rows) merged.set(row.name, row);
  state.expectedAgents = Array.from(merged.values());
  agentsPanel.setExpected(state.expectedAgents);

  for (const delay of [1500, 4000, 9000, 16000]) {
    setTimeout(() => refreshAgents({ force: true }), delay);
  }
}

async function fillMissingSamples(nodes) {
  const missing = nodes.filter((n) => !n.health || !n.health.services);
  if (missing.length === 0) return;
  await Promise.all(
    missing.map(async (n) => {
      if (state.samples.has(n.node_id) && n.health) return;
      try {
        const detail = await backend.api.getNode(n.node_id);
        const sample = detail && detail.metrics_latest ? detail.metrics_latest : null;
        if (sample) state.samples.set(n.node_id, sample);
      } catch {
        /* keep whatever we had */
      }
    }),
  );
}

async function refreshMetrics() {
  const nodeId = state.selectedNodeId;
  if (!nodeId || missingConfig()) return;
  const secs = cfg.windows[state.window] || 900;
  const q = windowQuery(state.window, secs);
  const names = metricsQuery(metricsForView());
  try {
    const payload = await backend.api.getMetrics(nodeId, { ...q, metrics: names });
    state.metricsByNode.set(nodeId, seriesIndex(payload));
    metricsPanel.setAnnotations(annotationsForNode(decisionsPanel.rows, nodeId));
    metricsPanel.update(payload);
    // The same raw telemetry sample the overview uses: it feeds both the
    // "top processes" card (§4.10) and the metric-expansion groups
    // (§4.12–§4.14), which are per-sample facts rather than time series.
    const sample = await fetchLatestSample(nodeId);
    metricsPanel.setProcesses(sample ? processes(sample) : []);
    metricsPanel.setSample(sample);
    state.backend.fails = 0;
    hideBanner();
  } catch (e) {
    handleBackendError(e);
  }
}

/**
 * The selected node's latest raw telemetry Sample (`metrics_latest`, dashboard.md
 * §1.2). Re-read on every metrics poll so "latest sample" means latest, and cached
 * only as the fallback for a failed fetch — the metric-expansion groups are read
 * from this one sample, so the panel and the node card cannot disagree about what
 * the node reported.
 */
async function fetchLatestSample(nodeId) {
  try {
    const detail = await backend.api.getNode(nodeId);
    const sample = detail && detail.metrics_latest ? detail.metrics_latest : null;
    if (sample) state.samples.set(nodeId, sample);
    return sample;
  } catch {
    return state.samples.get(nodeId) || null; // last known sample, never an invention
  }
}

/** node_id -> { metricName: seriesEntry } for threshold resolution. */
function seriesIndex(payload) {
  const out = {};
  for (const s of (payload && payload.series) || []) {
    if (s && typeof s.name === "string") out[s.name] = s;
  }
  return out;
}

/**
 * The metric names the panel asks the controller for: ONLY the frozen controller
 * allowlist (dashboard.md §1.4), which is what `requestedMetrics()` returns. The
 * §4.12–§4.14 metrics are read from the raw sample instead — naming one of them
 * here would make the controller reject the WHOLE request (`unknown metric(s)`,
 * 400) and blank every chart on the panel.
 */
function metricsForView() {
  return requestedMetrics(state.includeAll);
}

function handleBackendError(e) {
  state.backend.fails += 1;
  state.backend.lastError = e;
  const code = e instanceof ApiError ? e.code : "ERROR";
  if (state.backend.fails >= 3) {
    showBanner({
      kind: "err",
      text: `Controller unreachable for ${state.backend.fails} attempts (${code}). Showing last known data.`,
      actionLabel: "Retry now",
      onAction: () => {
        refreshNodes();
        refreshMetrics();
      },
    });
  } else {
    showBanner({
      kind: "warn",
      text: `Controller error (${code}): ${e.message}. Retrying…`,
      actionLabel: "Retry now",
      onAction: () => {
        refreshNodes();
        refreshMetrics();
      },
    });
  }
  setPill(dom.pillBackend, dom.pillBackendText, "err", `backend ${code}`);
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => refreshNodes(), cfg.pollMs);
  metricsTimer = setInterval(() => {
    if (document.visibilityState === "visible") refreshMetrics();
  }, cfg.pollMs);
  // The decision surface has no SSE transport — poll it at its own cadence
  // (contract 2-5 s for the dashboards' live views).
  decisionTimer = setInterval(() => {
    if (document.visibilityState === "visible") refreshDecisions();
  }, cfg.decisionMs);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  if (metricsTimer) clearInterval(metricsTimer);
  if (decisionTimer) clearInterval(decisionTimer);
  pollTimer = null;
  metricsTimer = null;
  decisionTimer = null;
}

/* ---------------------------------------------------- operator decisions */

/**
 * Poll the decision surface (contract has no SSE for it): decisions + the
 * health events they were made for + the audit rows that record what executed.
 * The three are separate endpoints and are joined client-side.
 */
async function refreshDecisions() {
  if (missingConfig()) return;
  try {
    const [dec, ev, aud] = await Promise.all([
      backend.api.operatorDecisions(cfg.decisionLimit),
      backend.api.operatorEvents(cfg.decisionLimit),
      backend.api.operatorAudits(cfg.decisionLimit),
    ]);
    decisionsPanel.update({
      decisions: (dec && dec.decisions) || [],
      events: (ev && ev.events) || [],
      audits: (aud && aud.audits) || [],
      seriesByNode: seriesByNodeIndex(),
    });
    // Keep the audit rows for citation jumps (the chat's "open audit row" and
    // an `audit` citation both land here rather than re-fetching).
    state.lastAudits = (aud && aud.audits) || [];
    state.decisionsReady = true;
  } catch (e) {
    // A 404 means this controller build has no operator-decision surface at all.
    // Say that, instead of showing an empty panel as if nothing had been decided.
    decisionsPanel.setUnavailable(e);
  }
}

/** node_id -> { metricName: seriesEntry } for every node we have plotted. */
function seriesByNodeIndex() {
  const out = {};
  for (const [nodeId, series] of state.metricsByNode) out[nodeId] = series;
  return out;
}

function showAuditDetail(row) {
  const a = row.audit;
  if (!a) {
    detailDialog({
      title: "No audit row for this decision",
      subtitle: row.auditMissing
        ? `The decision exists (${row.id}) but its audit row is not in the last ${row.auditWindow} audits fetched — widen the fetch or open the API directly.`
        : `The decision exists (${row.id}) but produced no audit row.`,
      rows: [
        ["decision", row.id],
        ["event", row.eventId],
        ["rule", row.ruleId],
        ["action", row.actionKind],
        ["decided at", row.decidedAt],
      ],
      url: apiUrl(cfg, "/operator/audits", { limit: cfg.decisionLimit }),
      json: row.audit || { decision: row.id, note: "no audit row returned by /v1/operator/audits" },
    });
    return;
  }
  detailDialog({
    title: `Audit ${a.id}`,
    subtitle: `${a.action_kind || row.actionKind} on ${row.nodeId} — result ${a.result}`,
    url: apiUrl(cfg, "/operator/audits", { limit: cfg.decisionLimit }),
    rows: [
      ["decision", a.decision_id],
      ["health event", a.event_id],
      ["node", a.node_id],
      ["rule", a.rule_id],
      ["action", a.action_kind],
      ["result", a.result],
      ["executed at", a.executed_at],
      ["endpoint", a.lxd_endpoint],
      ["LXD call", `${a.lxd_method || ""} ${a.lxd_path || ""}`.trim()],
      ["idempotency key", a.idempotency_key],
      ["dry run", a.dry_run ? "yes" : "no"],
      ["detail", a.detail],
    ],
    json: a,
    actions: [
      {
        label: "Ask AI about this audit row",
        title: `attach audit ${a.id} to the chat`,
        onClick: () =>
          askAi({
            kind: "audit",
            id: a.id,
            label: `${a.action_kind || row.actionKind || "action"} · ${a.result}`,
            summary: `audit · ${a.action_kind || row.actionKind || "action"} on ${a.node_id || row.nodeId} · audit ${shortId(a.id, 10, 4)}`,
            meta: { nodeId: a.node_id, decisionId: a.decision_id, ruleId: a.rule_id, result: a.result },
          }),
      },
    ],
  });
}

function showSampleDetail(row) {
  const ev = row.event;
  if (!ev) {
    detailDialog({
      title: "Telemetry event not in the fetched window",
      subtitle: `Decision ${row.id} references health event ${row.eventId || "(none)"}, which /v1/operator/events did not return.`,
      rows: [["decision", row.id], ["event", row.eventId], ["rule", row.ruleId]],
      url: apiUrl(cfg, "/operator/events", { limit: cfg.decisionLimit }),
      json: { decision_id: row.id, event_id: row.eventId, note: "event row not returned" },
    });
    return;
  }
  const samples = Array.isArray(ev.evidence) ? ev.evidence : [];
  const firing = samples.find((s) => s && s.seq === ev.evidence_seq_end) || samples[samples.length - 1] || null;
  const start = ev.breach_started_at || ev.fired_at;
  const end = ev.fired_at ? new Date(Date.parse(ev.fired_at) + 120000).toISOString() : undefined;
  const url = apiUrl(
    cfg,
    `/projects/${encodeURIComponent(cfg.projectId)}/nodes/${encodeURIComponent(row.nodeId)}/metrics`,
    { start, end, resolution: "5s", metrics: ev.metric || undefined },
  );
  detailDialog({
    title: `Telemetry that fired ${ev.rule_id}`,
    subtitle: `health event ${ev.id} — the sample the decision was made on`,
    url,
    rows: [
      ["metric", ev.metric],
      ["value at trigger", `${ev.value}${ev.metric && ev.metric.includes("bytes") ? ` (${row.breach ? row.breach.valueText : ""})` : ""}`],
      ["threshold", ev.threshold],
      ["fired at", ev.fired_at],
      ["breach started", ev.breach_started_at],
      ["sample seq", `${ev.evidence_seq_start} → ${ev.evidence_seq_end}`],
      ["firing sample ts", firing ? firing.ts : null],
      ["firing sample", firing ? JSON.stringify({ ...firing, disk: undefined, net: undefined, gpu: undefined, lxd_instances: undefined }) : null],
      ["node", ev.node_id],
      ["event type", ev.type],
    ],
    json: { event: ev, sample: firing },
  });
}

/**
 * Cause-and-effect view: plot the breaching metric for the decision's own node
 * with the decision marker and the threshold line, so a viewer sees the fault
 * rise, the decision land, and the fleet answer. Demo (synthetic) nodes are
 * revealed by the documented `?include=synthetic` listing and labelled.
 */
async function showOnChart(row) {
  if (!row.breach || !row.breach.metric) {
    toastWarn("Nothing to plot", "This decision has no health event in the fetched window.");
    return;
  }
  if (!state.includeSynthetic) {
    state.includeSynthetic = true;
    if (dom.includeSynthetic) dom.includeSynthetic.checked = true;
    await refreshNodes();
  }
  state.window = "15m";
  for (const c of dom.windowButtons.children) c.classList.toggle("on", c.textContent === state.window);
  selectNode(row.nodeId);
  await refreshMetrics();
  const group = CHART_GROUPS.find((g) => g.metrics.includes(row.breach.metric));
  const card = group ? dom.charts.querySelector(`[data-chart="${group.id}"]`) : null;
  if (card) {
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.add("flash");
    setTimeout(() => card.classList.remove("flash"), 2500);
  }
  toastOk(
    "Fault view",
    `${row.breach.metric} on ${row.nodeId} — ${row.ruleId} decided at ${row.decidedAt || "?"}`,
  );
}

/* ------------------------------------------------------------ AI assistant */

let chat = null;

/**
 * The assistant panel (t_d1aa35c8). Docked on the right, reachable from the
 * topbar in every view; `respond` is the ONLY network call it makes, and it is
 * the parent card's frozen route (§4.10) through the same REST client every
 * other panel uses — no second route, no second credential.
 */
function buildChatPanel() {
  chat = new ChatPanel(
    {
      panel: dom.chatPanel,
      launcher: dom.chatLauncher,
      closeBtn: dom.chatClose,
      modeEl: dom.chatMode,
      contextHost: dom.chatContextHost,
      contextChip: dom.chatContextChip,
      contextKind: dom.chatContextKind,
      contextLabel: dom.chatContextLabel,
      contextClear: dom.chatContextClear,
      suggestions: dom.chatSuggestions,
      messages: dom.chatMessages,
      form: dom.chatForm,
      input: dom.chatInput,
      sendBtn: dom.chatSend,
    },
    {
      respond: (body) => backend.api.assistantAsk(body),
      onCitation: (cite) => jumpToCitation(cite),
      offline: backend.kind === "offline",
    },
  );
  return chat;
}

/**
 * Seed the chat with the element the reader clicked. The id travels as DATA
 * (`context.kind` + `context.id`), never inside the prose, and the panel keeps
 * it attached across follow-up questions until it is removed or replaced.
 */
function askAi(input) {
  if (!chat) return;
  try {
    chat.attachContext(seedContext(input));
  } catch (e) {
    toastErr("Cannot attach that element", e.message);
  }
}

function flashElement(node) {
  if (!node) return false;
  if (typeof node.scrollIntoView === "function") node.scrollIntoView({ behavior: "smooth", block: "center" });
  if (node.classList) {
    node.classList.add("flash");
    setTimeout(() => node.classList.remove("flash"), 2500);
  }
  return true;
}

/**
 * A citation is a claim about a specific record, so its button must land on that
 * record — or say it cannot. `incident` citations carry the health-event id and
 * land on the decision row that event fired; `metric` citations carry the NODE
 * id (§4.10) and land on that node's charts; `audit` opens the audit row itself.
 */
function jumpToCitation(cite) {
  if (!cite || !cite.kind || !cite.id) return;
  const kind = cite.kind;
  const id = cite.id;
  if (kind === "audit") {
    const row = state.lastAudits.find((a) => a && a.id === id);
    if (row) {
      showAuditDetail({ audit: row, id: row.decision_id, nodeId: row.node_id, node_id: row.node_id, ruleId: row.rule_id, actionKind: row.action_kind });
      return;
    }
    toastWarn(
      "Audit row not in the fetched window",
      `audit ${id} is not among the last ${cfg.decisionLimit} audit rows — widen the fetch to see it.`,
    );
    return;
  }
  if (kind === "incident") {
    const row = dom.decisionList.querySelector(`[data-event-id="${cssEscape(id)}"]`);
    if (flashElement(row)) return;
    toastWarn("Incident not in the fetched window", `health event ${id} is not on the decision panel right now.`);
    return;
  }
  if (kind === "decision") {
    const row = dom.decisionList.querySelector(`[data-decision-id="${cssEscape(id)}"]`);
    if (flashElement(row)) return;
    toastWarn("Decision not in the fetched window", `decision ${id} is not on the decision panel right now.`);
    return;
  }
  if (kind === "node" || kind === "metric") {
    const node = state.nodes.find((n) => n.node_id === id);
    if (!node) {
      toastWarn("Node not in the fetched window", `node ${id} is not in the current node list.`);
      return;
    }
    selectNode(id);
    const card = dom.overview.querySelector(`[data-node-id="${cssEscape(id)}"]`);
    flashElement(card);
    if (kind === "metric") {
      dom.charts.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    return;
  }
  if (kind === "remediation") {
    const item = dom.pendingList.querySelector(`[data-command-id="${cssEscape(id)}"]`);
    if (flashElement(item)) return;
    toastWarn("Remediation not pending", `command ${id} is not in the pending list (it may already be decided).`);
    return;
  }
  toastWarn("Cannot jump to that citation", `${kind} ${id} has no surface on this dashboard.`);
}

/** Escape a ULID for an attribute selector (ids are Crockford base32, but be safe). */
function cssEscape(value) {
  return String(value).replace(/["\\]/g, "\\$&");
}

/* ------------------------------------------------------------------ UI sync */

function overviewHandlers() {
  return {
    onSelect: (id) => selectNode(id),
    onReboot: (id) => rebootNode(id),
    onDrain: (id) => drainNode(id),
    onAskAi: (ctx) => askAi(ctx),
  };
}

function selectNode(id) {
  state.selectedNodeId = id;
  syncNodeSelectors();
  renderOverview(dom.overview, state, overviewHandlers());
  const node = state.nodes.find((n) => n.node_id === id) || null;
  controls.setTarget(node);
  refreshMetrics();
}

function syncNodeSelectors() {
  const want = state.selectedNodeId || "";
  if (dom.chartNode.value !== want) dom.chartNode.value = want;
  for (const n of state.nodes) {
    if (!Array.from(dom.chartNode.options).some((o) => o.value === n.node_id)) {
      dom.chartNode.appendChild(new Option(n.registration_id || n.node_id.slice(0, 10), n.node_id));
    }
  }
  dom.chartNode.value = want;
}

function updatePills() {
  const count = state.nodes.length;
  const degraded = state.nodes.filter((n) => n.status === "degraded").length;
  const offline = state.nodes.filter((n) => n.status === "offline").length;
  const fenced = state.nodes.filter((n) => n.status === "fenced").length;
  const kind = offline || fenced ? "err" : degraded ? "warn" : count ? "ok" : "muted";
  setPill(dom.pillNodes, dom.pillNodesText, kind, `nodes ${count}${degraded ? ` · ${degraded} degraded` : ""}${offline ? ` · ${offline} offline` : ""}${fenced ? ` · ${fenced} fenced` : ""}`);
  const b = state.backend;
  setPill(dom.pillBackend, dom.pillBackendText, b.fails ? "err" : "ok", b.fails ? "backend error" : "backend ok");
}

function setPill(pill, textNode, kind, label) {
  if (textNode) textNode.textContent = label;
  if (!pill) return;
  const dot = pill.querySelector(".dot");
  if (dot) dot.className = `dot dot-${kind}`;
}

/* ------------------------------------------------------------------ actions */

function targetNode() {
  return state.nodes.find((n) => n.node_id === state.selectedNodeId) || null;
}

async function rebootNode(nodeId) {
  const node = state.nodes.find((n) => n.node_id === nodeId) || targetNode();
  if (!node) {
    toastWarn("No node selected", "Pick a node first.");
    return;
  }
  const name = node.registration_id || node.node_id;
  const confirmed = await confirmDialog({
    title: `Restart ${name}?`,
    message:
      node.status === "offline"
        ? "This node is offline — the controller will return 503 NODE_OFFLINE."
        : "The controller queues node.reboot (approval: required). Running workloads are interrupted.",
    confirmLabel: "Restart node",
  });
  if (!confirmed) return;
  if (node.node_id === state.selectedNodeId) controls.nodeResult("reboot", "requesting…", true);
  try {
    const res = await backend.api.rebootNode(node.node_id, 0);
    const msg = `node.reboot queued · command ${res && res.command_id ? res.command_id.slice(0, 10) + "…" : "—"} · state ${res && res.state ? res.state : "queued"}`;
    toastOk("Restart requested", msg);
    if (node.node_id === state.selectedNodeId) controls.nodeResult("reboot", msg, true);
    appendLocalLog("info", node.node_id, `Human issued node.reboot for ${name} (command ${res && res.command_id ? res.command_id : "?"}).`, { command_id: res && res.command_id, type: "node.reboot", status: res && res.state });
  } catch (e) {
    const msg = formatError(e);
    toastErr("Restart failed", msg);
    if (node.node_id === state.selectedNodeId) controls.nodeResult("reboot", msg, false);
  }
}

async function rebootSelected() {
  const node = targetNode();
  if (node) rebootNode(node.node_id);
}

async function drainNode(nodeId) {
  const node = state.nodes.find((n) => n.node_id === nodeId) || targetNode();
  if (!node) {
    toastWarn("No node selected", "Pick a node first.");
    return;
  }
  const name = node.registration_id || node.node_id;
  const rest = state.nodes.filter((n) => n.node_id !== node.node_id).map((n) => ({ value: n.node_id, label: n.registration_id || n.node_id }));
  const choice = await confirmDialog({
    title: `Drain ${name}?`,
    message: "The controller queues node.drain (approval: required) and evacuates workloads.",
    confirmLabel: "Drain node",
    fields: [
      {
        name: "reason",
        label: "reason",
        type: "select",
        value: "manual",
        options: [
          { value: "manual", label: "manual" },
          { value: "disk_degradation", label: "disk_degradation" },
          { value: "disk_failure", label: "disk_failure" },
          { value: "maintenance", label: "maintenance" },
        ],
      },
      { name: "evacuate_to", label: "evacuate to (optional)", type: "select", value: "", options: [{ value: "", label: "controller picks" }, ...rest] },
    ],
  });
  if (!choice) return;
  try {
    const res = await backend.api.drainNode(node.node_id, {
      reason: choice.reason || "manual",
      evacuate_to: choice.evacuate_to || undefined,
    });
    const msg = `node.drain queued · command ${res && res.command_id ? res.command_id.slice(0, 10) + "…" : "—"} · reason ${choice.reason}`;
    toastOk("Drain requested", msg);
    if (node.node_id === state.selectedNodeId) controls.nodeResult("drain", msg, true);
    appendLocalLog("warn", node.node_id, `Human issued node.drain for ${name} (reason ${choice.reason}).`, { command_id: res && res.command_id, type: "node.drain", status: res && res.state });
  } catch (e) {
    const msg = formatError(e);
    toastErr("Drain failed", msg);
    if (node.node_id === state.selectedNodeId) controls.nodeResult("drain", msg, false);
  }
}

async function drainSelected() {
  const node = targetNode();
  if (node) drainNode(node.node_id);
}

async function validateIntent(prompt) {
  if (!prompt) {
    controls.intentResult("", "Enter a prompt or structured intent first.", false);
    return;
  }
  dom.intentBtn.disabled = true;
  try {
    const plan = await backend.api.submitIntent({ prompt });
    controls.showPlan(plan);
    toastOk("Intent validated", `plan ${plan && plan.plan_id ? plan.plan_id.slice(0, 10) + "…" : "—"} · ${plan && plan.outcome}`);
  } catch (e) {
    // 422 GUARDRAIL_REJECTED returns the plan alongside the error envelope.
    if (e instanceof ApiError && e.status === 422 && e.body && Array.isArray(e.body.allocations)) {
      controls.showPlan(e.body);
      toastWarn("Plan rejected", "Guardrail rejected the intent — see details.");
    } else {
      controls.intentError(e);
      toastErr("Intent failed", formatError(e));
    }
  } finally {
    dom.intentBtn.disabled = false;
  }
}

async function applyPlan(planId) {
  dom.applyBtn.disabled = true;
  try {
    const job = await backend.api.applyPlan(planId);
    controls.applyResult(job);
    toastOk("Plan applied", `job ${job && job.job_id ? job.job_id : "?"} · ${(job && job.vm_ids ? job.vm_ids.length : 0)} vm(s)`);
    // The plan's outcome must land in the Agents panel, not only in a toast:
    // its instances are shown as provisioning rows until the node reports them.
    await expectAgentsFromJob(job);
    await refreshAgents({ force: true });
  } catch (e) {
    controls.intentResult("", formatError(e), false);
    toastErr("Apply failed", formatError(e));
  } finally {
    dom.applyBtn.disabled = false;
  }
}

async function decideRemediation(commandId, action) {
  if (!commandId) return;
  const entry = state.pending.get(commandId);
  try {
    let res;
    if (action === "approve") {
      res = await backend.api.approveRemediation(commandId);
      toastOk("Remediation approved", `${commandId.slice(0, 10)}… → ${res && res.state ? res.state : "approved"}`);
    } else {
      const choice = await confirmDialog({
        title: "Deny remediation",
        message: "The command will never execute; the AI operator records the denial.",
        confirmLabel: "Deny",
        danger: true,
        fields: [{ name: "reason", label: "reason (optional)", type: "text", placeholder: "maintenance_window" }],
      });
      if (!choice) return;
      res = await backend.api.denyRemediation(commandId, { reason: choice.reason || undefined });
      toastWarn("Remediation denied", `${commandId.slice(0, 10)}… → ${res && res.state ? res.state : "denied"}`);
    }
    state.pending.delete(commandId);
    controls.renderPending(Array.from(state.pending.values()));
    controls.remResult(`${action}d ${commandId}: ${res && res.state ? res.state : action}`, true);
    appendLocalLog("info", entry ? entry.node_id : null, `Human ${action}d remediation ${commandId}.`, { command_id: commandId, type: entry ? entry.type : null, status: res && res.state });
  } catch (e) {
    const msg = formatError(e);
    toastErr(`${action === "approve" ? "Approve" : "Deny"} failed`, msg);
    controls.remResult(msg, false);
    // refresh pending so a stale item (already decided → 409) drops off
    setTimeout(() => refreshNodes(), 800);
  }
}

/* ------------------------------------------------------------------ logs */

function trackRemediation(line) {
  if (!line.remediation || !line.remediation.command_id) return;
  const cid = line.remediation.command_id;
  if (line.remediation.status === "pending_approval") {
    state.pending.set(cid, {
      command_id: cid,
      type: line.remediation.type,
      status: line.remediation.status,
      node_id: line.node_id,
      message: line.message,
      ts: Date.now(),
    });
  } else {
    state.pending.delete(cid);
  }
  prunePending(Date.now(), false);
  controls.renderPending(Array.from(state.pending.values()));
}

/**
 * Client-side hygiene for the pending list: the contract auto-denies a
 * pending_approval after 300 s (§4.4), and the demo can emit many, so age out
 * stale entries and cap the list the operator has to scan.
 */
function prunePending(nowMs, render = true) {
  const timeout = 300_000;
  let changed = false;
  for (const [cid, item] of state.pending) {
    if (item.ts && nowMs - item.ts > timeout) {
      state.pending.delete(cid);
      changed = true;
    }
  }
  while (state.pending.size > 10) {
    state.pending.delete(state.pending.keys().next().value);
    changed = true;
  }
  if (changed && render) controls.renderPending(Array.from(state.pending.values()));
  return changed;
}

function appendLocalLog(severity, nodeId, message, remediation) {
  logPanel.addLine({
    log_id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    ts: new Date().toISOString(),
    node_id: nodeId,
    severity,
    actor: "human",
    message,
    remediation: remediation && remediation.command_id ? remediation : null,
  });
}

/* ------------------------------------------------------------------ boot */

function buildWindowButtons() {
  dom.windowButtons.replaceChildren();
  for (const label of Object.keys(cfg.windows)) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `chip ${label === state.window ? "on" : ""}`;
    b.textContent = label;
    b.addEventListener("click", () => {
      state.window = label;
      for (const c of dom.windowButtons.children) c.classList.toggle("on", c.textContent === label);
      refreshMetrics();
    });
    dom.windowButtons.appendChild(b);
  }
}

function bindUi() {
  dom.refreshBtn.addEventListener("click", () => {
    refreshNodes();
    refreshMetrics();
  });
  dom.chartNode.addEventListener("change", (e) => selectNode(e.target.value));
  dom.allMetrics.addEventListener("change", (e) => {
    state.includeAll = e.target.checked;
    metricsPanel.setIncludeAll(state.includeAll);
    refreshMetrics();
  });
  if (dom.includeSynthetic) {
    dom.includeSynthetic.addEventListener("change", (e) => {
      // Contract §1.1: the default view is the REAL fleet; `?include=synthetic`
      // is the opt-in that reveals seeder-fabricated rows, labelled "demo".
      state.includeSynthetic = e.target.checked;
      refreshNodes().then(() => refreshDecisions());
    });
  }
  dom.settingsBtn.addEventListener("click", () => {
    dom.setApi.value = cfg.apiBase || "";
    dom.setProject.value = cfg.projectId || "";
    dom.setToken.value = cfg.token || "";
    dom.setOffline.checked = !!cfg.offline;
    dom.settingsDialog.showModal();
  });
  dom.settingsForm.addEventListener("submit", (e) => {
    if (e.submitter && e.submitter.value === "save") {
      cfg = {
        ...cfg,
        apiBase: dom.setApi.value.trim(),
        projectId: dom.setProject.value.trim(),
        token: dom.setToken.value.trim(),
        offline: dom.setOffline.checked,
      };
      persistConfig();
      location.reload();
      return;
    }
    // cancel — let the dialog close
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refreshNodes();
      refreshMetrics();
      refreshClientNode();
      refreshAgents({ force: true });
    }
  });
}

function startClock() {
  let ticks = 0;
  const tick = () => {
    state.now = Date.now();
    dom.clock.textContent = new Date(state.now).toLocaleTimeString("en-GB", { hour12: false });
    if (++ticks % 10 === 0) prunePending(state.now);
  };
  tick();
  setInterval(tick, 1000);
}

function missingConfig() {
  if (cfg.offline) return false;
  return !cfg.projectId;
}

function init() {
  bindUi();
  startClock();
  controls.setTarget(null);
  agentsPanel.setInference(inferenceLabel(null, null));
  agentsPanel.render();
  state.window = cfg.windows[cfg.defaultWindow] ? cfg.defaultWindow : Object.keys(cfg.windows)[0];
  buildWindowButtons();
  dom.allMetrics.checked = state.includeAll;
  if (dom.includeSynthetic) dom.includeSynthetic.checked = state.includeSynthetic;

  // One-click web terminal: revealed only when the composed console injected a
  // terminal base (console/serve.py); a standalone dashboard hides it.
  const termBase =
    (typeof window !== "undefined" && window.TERMINI_CONFIG && window.TERMINI_CONFIG.terminalUrl) || "";
  if (dom.terminalLink && termBase) {
    dom.terminalLink.href = termBase;
    dom.terminalLink.hidden = false;
  }

  backend = makeBackend();
  buildChatPanel();

  myNodePanel.setController(controllerOrigin());
  myNodePanel.setCredential(nodeCredential);

  if (missingConfig()) {
    showBanner({
      kind: "warn",
      text: "No project configured. Open Settings to set the controller URL and project id and sign in (or use the offline demo).",
      actionLabel: "Open settings",
      onAction: () => dom.settingsBtn.click(),
    });
  }

  if (scrubUrlCredential()) {
    // A warning banner is not enough on its own here: the very next poll is
    // likely to 401 for someone arriving on a legacy link, and that error
    // banner replaces this one. The toast survives it.
    const text =
      "A credential in the URL (?token=…) is not accepted and has been removed from the address bar. " +
      "Open Settings and sign in — from then on it is sent in the Authorization header only.";
    showBanner({
      kind: "warn",
      text,
      actionLabel: "Open settings",
      onAction: () => dom.settingsBtn.click(),
    });
    toast("warn", "Credential in the URL refused", text, 20000);
  }

  renderOverview(dom.overview, state, overviewHandlers());

  stream = backend.streamFactory();
  stream.start();

  refreshNodes().then(() => {
    refreshMetrics();
    refreshDecisions();
  });
  refreshClientNode();
  nodeTimer = setInterval(refreshClientNode, 5000);
  startPolling();
}

init();

// surface a single top-level guard so an unexpected error never yields a
// silent blank dashboard (keeps the "never looks broken" guarantee).
window.addEventListener("error", (ev) => {
  console.error("dashboard error", ev.error || ev.message);
});
window.addEventListener("unhandledrejection", (ev) => {
  console.error("dashboard unhandled rejection", ev.reason);
});
