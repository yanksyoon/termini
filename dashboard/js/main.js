/*
 * Termini operations dashboard — application controller.
 * Wires config resolution, polling, the SSE log stream, charts and controls.
 */

import { $ } from "./ui/dom.js";
import { ApiClient, ApiError } from "./api.js";
import { LogStream } from "./stream.js";
import { resolveConfig } from "./core/config.js";
import { CHART_GROUPS, windowQuery, metricsQuery } from "./core/series.js";
import { renderOverview } from "./ui/overview.js";
import { LogPanel } from "./ui/logs.js";
import { MetricsPanel } from "./ui/metrics.js";
import { ControlsPanel } from "./ui/controls.js";
import { confirmDialog } from "./ui/modal.js";
import { showBanner, hideBanner } from "./ui/banner.js";
import { ok as toastOk, err as toastErr, warn as toastWarn } from "./ui/toast.js";
import { formatError } from "./ui/errors.js";
import { OfflineBackend, OfflineStream } from "./offline.js";

const STORE_KEY = "termini.dashboard.cfg";

const dom = {
  pillNodes: $("#pill-nodes"),
  pillNodesText: $("#pill-nodes-text"),
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
  charts: $("#charts"),
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
};

const state = {
  nodes: [],
  samples: new Map(),
  selectedNodeId: null,
  window: "15m",
  includeAll: false,
  loading: true,
  now: Date.now(),
  backend: { fails: 0, lastOk: null, lastError: null },
  pending: new Map(),
};

let cfg = resolveConfig(readInputs());
let api = new ApiClient(cfg);
let stream = null;
let pollTimer = null;
let metricsTimer = null;

const metricsPanel = new MetricsPanel(dom.charts);
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
  },
);

/* ------------------------------------------------------------------ config */

function readInputs() {
  const params = new URLSearchParams(location.search);
  const query = {};
  for (const [k, v] of params.entries()) query[k] = v;
  let stored = {};
  try {
    stored = JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {};
  } catch {
    stored = {};
  }
  const file = (typeof window !== "undefined" && window.TERMINI_CONFIG) || {};
  return { query, stored, file };
}

function persistConfig() {
  try {
    localStorage.setItem(
      STORE_KEY,
      JSON.stringify({ apiBase: cfg.apiBase, projectId: cfg.projectId, token: cfg.token, offline: cfg.offline }),
    );
  } catch {
    /* storage may be disabled — non-fatal */
  }
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

/* ------------------------------------------------------------------ polling */

async function refreshNodes() {
  if (missingConfig()) return; // the config banner already explains what to do
  try {
    const list = await backend.api.listNodes();
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
    syncNodeSelectors();
    updatePills();
    dom.overviewUpdated.textContent = `updated ${new Date().toLocaleTimeString("en-GB", { hour12: false })}`;
  } catch (e) {
    handleBackendError(e);
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
    metricsPanel.update(payload);
    state.backend.fails = 0;
    hideBanner();
  } catch (e) {
    handleBackendError(e);
  }
}

function metricsForView() {
  const groups = state.includeAll ? CHART_GROUPS : CHART_GROUPS.filter((g) => g.primary);
  const names = new Set();
  for (const g of groups) for (const m of g.metrics) names.add(m);
  return Array.from(names);
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
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  if (metricsTimer) clearInterval(metricsTimer);
  pollTimer = null;
  metricsTimer = null;
}

/* ------------------------------------------------------------------ UI sync */

function overviewHandlers() {
  return {
    onSelect: (id) => selectNode(id),
    onReboot: (id) => rebootNode(id),
    onDrain: (id) => drainNode(id),
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
  const kind = offline ? "err" : degraded ? "warn" : count ? "ok" : "muted";
  setPill(dom.pillNodes, dom.pillNodesText, kind, `nodes ${count}${degraded ? ` · ${degraded} degraded` : ""}${offline ? ` · ${offline} offline` : ""}`);
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
  state.window = cfg.windows[cfg.defaultWindow] ? cfg.defaultWindow : Object.keys(cfg.windows)[0];
  buildWindowButtons();
  dom.allMetrics.checked = state.includeAll;

  // One-click web terminal: revealed only when the composed console injected a
  // terminal base (console/serve.py); a standalone dashboard hides it.
  const termBase =
    (typeof window !== "undefined" && window.TERMINI_CONFIG && window.TERMINI_CONFIG.terminalUrl) || "";
  if (dom.terminalLink && termBase) {
    dom.terminalLink.href = termBase;
    dom.terminalLink.hidden = false;
  }

  backend = makeBackend();

  if (missingConfig()) {
    showBanner({
      kind: "warn",
      text: "No project configured. Open Settings to set the controller URL, project id and token (or use the offline demo).",
      actionLabel: "Open settings",
      onAction: () => dom.settingsBtn.click(),
    });
  }

  renderOverview(dom.overview, state, overviewHandlers());

  stream = backend.streamFactory();
  stream.start();

  refreshNodes().then(() => refreshMetrics());
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
