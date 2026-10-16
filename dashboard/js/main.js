/*
 * Termini operations dashboard — application controller.
 * Wires config resolution, polling, the SSE log stream, charts and controls.
 */

import { $ } from "./ui/dom.js";
import { ApiClient, ApiError } from "./api.js";
import { LogStream } from "./stream.js";
import { resolveConfig, apiUrl, stripKeysFromQuery } from "./core/config.js";
import { CHART_GROUPS, windowQuery, metricsQuery } from "./core/series.js";
import { parseEgressLine, annotationsForNode, sourceMeta } from "./core/decisions.js";
import { renderOverview } from "./ui/overview.js";
import { LogPanel } from "./ui/logs.js";
import { DecisionsPanel } from "./ui/decisions.js";
import { MetricsPanel } from "./ui/metrics.js";
import { ControlsPanel } from "./ui/controls.js";
import { confirmDialog, detailDialog } from "./ui/modal.js";
import { showBanner, hideBanner } from "./ui/banner.js";
import { ok as toastOk, err as toastErr, warn as toastWarn, toast } from "./ui/toast.js";
import { formatError } from "./ui/errors.js";
import { OfflineBackend, OfflineStream } from "./offline.js";
import { MyNodePanel } from "./ui/mynode.js";
import { registrationView, healthSummary } from "./core/client-node.js";

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
};

let cfg = resolveConfig(readInputs());
// `?synthetic=1` starts with the demo/seeder nodes revealed (contract §1.1).
state.includeSynthetic = !!cfg.includeSynthetic;
let api = new ApiClient(cfg);
let stream = null;
let pollTimer = null;
let metricsTimer = null;
let decisionTimer = null;

const metricsPanel = new MetricsPanel(dom.charts);

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
    state.metricsByNode.set(nodeId, seriesIndex(payload));
    metricsPanel.setAnnotations(annotationsForNode(decisionsPanel.rows, nodeId));
    metricsPanel.update(payload);
    state.backend.fails = 0;
    hideBanner();
  } catch (e) {
    handleBackendError(e);
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
