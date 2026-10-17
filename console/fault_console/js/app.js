/* Operator fault console (controller-api.md §4.7).
 *
 * Talks to the controller's operator-only demo-fault surface directly:
 *   GET    /v1/projects/{pid}/demo/faults            list (active + recent)
 *   POST   /v1/projects/{pid}/demo/faults            start one injection
 *   DELETE /v1/projects/{pid}/demo/faults/{iid}      clear one injection
 *   POST   /v1/projects/{pid}/demo/faults/clear      clear every open injection
 *
 * Auth is the PROJECT TOKEN, supplied by the operator in the form below and kept
 * only in this tab's sessionStorage. The page is served without a token on
 * purpose: an unauthenticated visitor can load it and do nothing with it (the
 * controller answers 401/403). See the repo README/docs for the guardrail.
 *
 * `window.__TERMINI_FAULT_DEFAULTS__` (injected by console/serve.py) carries the
 * controller URL and project id — never the token.
 */
(() => {
  "use strict";

  const DEFAULTS = window.__TERMINI_FAULT_DEFAULTS__ || {};
  const SESSION_KEY = "termini.faultConsole";

  const el = (id) => document.getElementById(id);
  const dom = {
    apiBase: el("api-base"), projectId: el("project-id"), token: el("token"),
    connect: el("connect"), forget: el("forget"), connectStatus: el("connect-status"),
    node: el("node"), scenario: el("scenario"), duration: el("duration"), target: el("target"),
    inject: el("inject"), clearAll: el("clear-all"), fleetNote: el("fleet-note"),
    injectCard: el("inject-card"), activeCard: el("active-card"), logCard: el("log-card"),
    tbody: document.querySelector("#injections tbody"),
    noInjections: el("no-injections"), responses: el("responses"),
  };

  const SCENARIOS = [
    ["oom", "oom — RAM past the OOM threshold (oom-guard-01)"],
    ["disk-fail", "disk-fail — disk remounted read-only (disk-fail-01)"],
  ];

  let state = { apiBase: "", projectId: "", token: "", nodes: [], timer: null };

  // ----------------------------------------------------------------- helpers
  function log(label, obj) {
    const text = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
    dom.responses.textContent =
      `[${new Date().toISOString()}] ${label}\n${text}\n\n` + dom.responses.textContent;
  }

  function status(node, text, cls) {
    node.textContent = text;
    node.className = "status " + (cls || "");
  }

  async function api(method, path, body) {
    const url = `${state.apiBase}/v1/projects/${state.projectId}${path}`;
    const opts = {
      method,
      // X-Termini-Actor: a person at this console is the actor, and saying so keeps
      // the audit line "cleared by the operator" instead of the honest-but-useless
      // "un-attributed project-token caller" a header-less caller gets
      // (controller-api.md §4.7; kanban t_9c1362f6).
      headers: {
        "Authorization": "Bearer " + state.token,
        "Content-Type": "application/json",
        "X-Termini-Actor": "human",
      },
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    let payload;
    const text = await res.text();
    try { payload = text ? JSON.parse(text) : {}; } catch (e) { payload = { raw: text }; }
    return { status: res.status, ok: res.ok, payload };
  }

  function save() {
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify({
        apiBase: state.apiBase, projectId: state.projectId, token: state.token,
      }));
    } catch (e) { /* private mode — the console simply won't remember */ }
  }

  function restore() {
    let saved = {};
    try { saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "{}"); } catch (e) { saved = {}; }
    state.apiBase = saved.apiBase || DEFAULTS.apiBase || state.apiBase;
    state.projectId = saved.projectId || DEFAULTS.projectId || "";
    state.token = saved.token || "";
    dom.apiBase.value = state.apiBase;
    dom.projectId.value = state.projectId;
    dom.token.value = state.token;
  }

  function setEnabled(on) {
    for (const card of [dom.injectCard, dom.activeCard, dom.logCard]) {
      card.classList.toggle("disabled", !on);
    }
  }

  // ------------------------------------------------------------------ connect
  async function connect() {
    state.apiBase = dom.apiBase.value.trim().replace(/\/+$/, "");
    state.projectId = dom.projectId.value.trim();
    state.token = dom.token.value.trim();
    if (!state.apiBase || !state.projectId || !state.token) {
      status(dom.connectStatus, "controller URL, project id and project token are all required", "err");
      return false;
    }
    status(dom.connectStatus, "checking the fleet listing (project token)…");
    try {
      const res = await api("GET", "/nodes?include=synthetic");
      if (!res.ok) {
        log(`GET /nodes -> ${res.status}`, res.payload);
        status(dom.connectStatus,
          res.status === 401 ? "401 UNAUTHENTICATED — no/invalid project token"
            : res.status === 403 ? "403 PERMISSION_DENIED — not a project token for this project"
              : `controller refused (HTTP ${res.status})`, "err");
        setEnabled(false);
        return false;
      }
      state.nodes = res.payload.nodes || [];
      fillNodes();
      fillScenarios();
      setEnabled(true);
      save();
      status(dom.connectStatus, `connected — ${state.nodes.length} node(s) in the fleet listing`, "ok");
      await refresh();
      startPolling();
      return true;
    } catch (err) {
      status(dom.connectStatus, "cannot reach the controller: " + err.message, "err");
      setEnabled(false);
      return false;
    }
  }

  function fillNodes() {
    dom.node.innerHTML = "";
    for (const n of state.nodes) {
      const opt = document.createElement("option");
      opt.value = n.node_id;
      const prov = n.synthetic ? "synthetic" : "real";
      opt.textContent = `${n.registration_id || n.node_id} · ${n.role} · ${prov} · ${n.status}`;
      opt.dataset.synthetic = String(Boolean(n.synthetic));
      dom.node.appendChild(opt);
    }
    syncTargetFromNode();
    const real = state.nodes.filter((n) => !n.synthetic).length;
    const syn = state.nodes.length - real;
    dom.fleetNote.textContent =
      `Fleet: ${real} real · ${syn} synthetic. A fault targets one node's telemetry series; ` +
      "while an injection is open the node's own samples are superseded, so the rule holds " +
      "instead of flapping. Clear restores the healthy series.";
  }

  function fillScenarios() {
    dom.scenario.innerHTML = "";
    for (const [value, label] of SCENARIOS) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      dom.scenario.appendChild(opt);
    }
  }

  function syncTargetFromNode() {
    const opt = dom.node.selectedOptions[0];
    if (!opt) return;
    dom.target.value = opt.dataset.synthetic === "true" ? "synthetic" : "real";
  }

  // ------------------------------------------------------------------- inject
  async function inject() {
    const body = {
      node_id: dom.node.value,
      scenario: dom.scenario.value,
      duration_s: Number(dom.duration.value || 30),
      target: dom.target.value,
    };
    const res = await api("POST", "/demo/faults", body);
    log(`POST /demo/faults ${JSON.stringify(body)} -> ${res.status}`, res.payload);
    if (res.ok) {
      status(dom.connectStatus,
        `injected ${body.scenario} on ${body.node_id} for ${body.duration_s}s ` +
        `(injection ${res.payload.injection_id})`, "ok");
    } else {
      const err = (res.payload && res.payload.error) || {};
      const why = (err.details || []).map((d) => `${d.field}=${d.reason}`).join(", ");
      status(dom.connectStatus,
        `refused ${res.status} ${err.code || ""} — ${err.message || "see response"} ${why ? "[" + why + "]" : ""}`,
        "err");
    }
    await refresh();
    return res;
  }

  async function clearOne(id) {
    const res = await api("DELETE", `/demo/faults/${id}`);
    log(`DELETE /demo/faults/${id} -> ${res.status}`, res.payload);
    if (res.ok) {
      status(dom.connectStatus,
        `cleared ${id} — state=${res.payload.state}; the healthy series is being restored ` +
        `(${res.payload.clear_samples_needed} sample(s) to re-arm the rule)`, "ok");
    }
    await refresh();
  }

  async function clearAll() {
    const res = await api("POST", "/demo/faults/clear", { reason: "cleared_from_console" });
    log(`POST /demo/faults/clear -> ${res.status}`, res.payload);
    if (res.ok) {
      status(dom.connectStatus, `cleared ${res.payload.count} injection(s)`, "ok");
    }
    await refresh();
  }

  // ------------------------------------------------------------------ render
  async function refresh() {
    const res = await api("GET", "/demo/faults");
    if (!res.ok) {
      log(`GET /demo/faults -> ${res.status}`, res.payload);
      return;
    }
    render(res.payload.faults || []);
  }

  function render(faults) {
    dom.tbody.innerHTML = "";
    dom.noInjections.style.display = faults.length ? "none" : "block";
    for (const f of faults) {
      const tr = document.createElement("tr");
      const ttl = f.open
        ? `${f.duration_s}s · expires ${f.expires_at}`
        : `${f.clear_reason || "—"}${f.cleared_at ? " @ " + f.cleared_at : ""}`;
      tr.innerHTML =
        `<td>${f.node_registration_id || f.node_id}<br><span class="hint">${f.node_id}</span></td>` +
        `<td>${f.scenario}</td><td>${f.rule_id}</td><td>${f.target}</td>` +
        `<td><span class="state ${f.state}">${f.state}</span></td>` +
        `<td>${ttl}</td><td>${f.samples_written}</td>` +
        `<td>${f.recovery_samples}/${f.clear_samples_needed}</td><td></td>`;
      const cell = tr.lastElementChild;
      if (f.open) {
        const btn = document.createElement("button");
        btn.className = "danger";
        btn.textContent = "Clear";
        btn.addEventListener("click", () => clearOne(f.injection_id));
        cell.appendChild(btn);
      }
      dom.tbody.appendChild(tr);
    }
  }

  function startPolling() {
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(() => { if (state.token) refresh(); }, 2000);
  }

  // -------------------------------------------------------------------- wire
  dom.connect.addEventListener("click", connect);
  dom.forget.addEventListener("click", () => {
    try { sessionStorage.removeItem(SESSION_KEY); } catch (e) { /* ignore */ }
    dom.token.value = "";
    state.token = "";
    setEnabled(false);
    status(dom.connectStatus, "token forgotten", "warn");
  });
  dom.node.addEventListener("change", syncTargetFromNode);
  dom.inject.addEventListener("click", inject);
  dom.clearAll.addEventListener("click", clearAll);

  // Prefill anything the (non-secret) runtime config provides; the token is never
  // provided by the page — that is the point of this console.
  dom.apiBase.value = DEFAULTS.apiBase || "";
  dom.projectId.value = DEFAULTS.projectId || "";
  restore();
  setEnabled(false);
  if (state.token) connect();
})();
