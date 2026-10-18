/*
 * REST client for the Termini controller (dashboard.md surfaces, contract
 * controller-api.md §3 error envelope, §1.5 Idempotency-Key).
 *
 * Every method returns parsed JSON or throws ApiError. The caller decides how
 * to surface failures; nothing here touches the DOM.
 */

import { apiUrl } from "./core/config.js";

export class ApiError extends Error {
  constructor(status, envelope, url) {
    const e = envelope && envelope.error ? envelope.error : {};
    super(e.message || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = e.code || (status === 0 ? "NETWORK" : "INTERNAL");
    this.details = e.details || [];
    this.request_id = e.request_id || null;
    this.url = url;
    // Full parsed body (a rejected /intents returns the plan alongside the
    // error envelope, contract §4.2 — keep it so the UI can render it).
    this.body = envelope || null;
  }
}

/**
 * Client-side deadlines, per class of call.
 *
 * ONE blanket timeout cannot serve both a 3 s polling read and a call that runs
 * an on-prem model or materialises LXD instances: at 20 s the dashboard aborted
 * its own `/intents` and `/vms/apply` requests while the controller was still
 * working (live controller log: `200` written to a socket the browser had
 * already closed — `BrokenPipeError`), and the reader was told the controller
 * was "unreachable" when it was in fact busy. Reads stay short so a dead backend
 * cannot wedge the page; anything that reaches a model or creates infrastructure
 * states its own budget, and a client timeout says so instead of pretending the
 * backend vanished.
 *
 * The budgets below are the controller's own server-side bounds plus slack:
 *   model      — config.intent_model_timeout_s (120 s) + round-trip
 *   provision  — config.lxd_create_timeout_s + lxd_start_timeout_s per instance
 *   assistant  — the model call behind /assistant/ask
 */
export const TIMEOUT_MS = {
  read: 20000,
  model: 180000,
  provision: 600000,
  assistant: 120000,
};

export class ApiClient {
  constructor(cfg) {
    this.cfg = cfg;
    // optional hook: (info) => void, used for the degraded-backend banner
    this.onResult = null;
  }

  _project() {
    if (!this.cfg.projectId) {
      throw new ApiError(0, { error: { code: "INVALID_ARGUMENT", message: "projectId is not configured" } }, "");
    }
    return encodeURIComponent(this.cfg.projectId);
  }

  async _request(method, path, { body, query, idempotent, timeoutMs = TIMEOUT_MS.read } = {}) {
    const url = apiUrl(this.cfg, path, query);
    const headers = { "X-Termini-Version": "1", Accept: "application/json" };
    if (this.cfg.token) headers.Authorization = `Bearer ${this.cfg.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotent) headers["Idempotency-Key"] = newKey();

    let res;
    try {
      res = await fetch(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        // per-call deadline: a read must not outlive a dead backend, and a model
        // or provisioning call must outlive the controller's own work (TIMEOUT_MS)
        signal: AbortSignal.timeout ? AbortSignal.timeout(timeoutMs) : undefined,
      });
    } catch (err) {
      // A client-side deadline is NOT an unreachable backend: the controller is
      // (usually) still working, and the operation may have completed. Saying
      // "unreachable" here sent readers to retry a request that was in fact
      // being served (and, for /vms/apply, to create a second set of instances).
      const timedOut = err && (err.name === "TimeoutError" || err.name === "AbortError");
      const error = timedOut
        ? {
            code: "CLIENT_TIMEOUT",
            message:
              `gave up after ${Math.round(timeoutMs / 1000)}s — the controller may still be ` +
              `working on ${path}; check the panel/record before retrying`,
          }
        : { code: "NETWORK", message: `controller unreachable: ${err.message}` };
      const e = new ApiError(0, { error }, url);
      if (this.onResult) this.onResult({ ok: false, error: e });
      throw e;
    }

    let parsed = null;
    const text = await res.text();
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = null;
      }
    }
    if (!res.ok) {
      const e = new ApiError(res.status, parsed, url);
      if (this.onResult) this.onResult({ ok: false, error: e });
      throw e;
    }
    if (this.onResult) this.onResult({ ok: true, url, status: res.status });
    return parsed;
  }

  listNodes({ includeSynthetic = false } = {}) {
    return this._request("GET", `/projects/${this._project()}/nodes`, {
      // contract §1.1: `?include=synthetic` returns every row (labelled). The
      // default view is the real fleet, so this is opt-in only.
      query: includeSynthetic ? { include: "synthetic" } : undefined,
    });
  }

  /**
   * Operator decision log (operator-decision.md §11). Token-scoped, NOT
   * project-scoped: GET /v1/operator/decisions.
   */
  operatorDecisions(limit = 50) {
    return this._request("GET", "/operator/decisions", { query: { limit } });
  }

  /** The health events the decisions were made for (§5 trigger linkage). */
  operatorEvents(limit = 50) {
    return this._request("GET", "/operator/events", { query: { limit } });
  }

  /** The remediation audit rows: what was executed, against which endpoint. */
  operatorAudits(limit = 50) {
    return this._request("GET", "/operator/audits", { query: { limit } });
  }

  getNode(nodeId) {
    return this._request("GET", `/projects/${this._project()}/nodes/${encodeURIComponent(nodeId)}`);
  }

  getMetrics(nodeId, { start, end, resolution, metrics } = {}) {
    return this._request("GET", `/projects/${this._project()}/nodes/${encodeURIComponent(nodeId)}/metrics`, {
      query: { start, end, resolution, metrics },
    });
  }

  /** As-built fallback when the node list omits `health` (server.py _node_view). */
  latestSample(nodeId) {
    return this._request("GET", "/telemetry/recent", { query: { node: nodeId, limit: 1 } });
  }

  /**
   * The CLIENT view of one node (task t_5649d117). Authenticated with that
   * node's own `node_token` — never the project token — and project-scoped
   * nowhere: `GET /v1/nodes/{node_id}/telemetry/recent` reads exactly the node
   * the credential was issued to (a cross-node id is refused with 403). The
   * credential rides in the Authorization header; the URL carries only the
   * limit.
   */
  nodeTelemetryRecent(nodeId, limit = 5) {
    return this._request("GET", `/nodes/${encodeURIComponent(nodeId)}/telemetry/recent`, {
      query: { limit },
    });
  }

  rebootNode(nodeId, delayS = 0) {
    return this._request("POST", `/projects/${this._project()}/nodes/${encodeURIComponent(nodeId)}/actions/reboot`, {
      body: { delay_s: delayS },
      idempotent: true,
    });
  }

  drainNode(nodeId, { reason = "manual", evacuate_to } = {}) {
    const body = { reason };
    if (evacuate_to) body.evacuate_to = evacuate_to;
    return this._request("POST", `/projects/${this._project()}/nodes/${encodeURIComponent(nodeId)}/actions/drain`, {
      body,
      idempotent: true,
    });
  }

  submitIntent(body) {
    // Runs the on-prem model extraction synchronously (controller bound:
    // config.intent_model_timeout_s, 120 s) — not a 20 s read.
    return this._request("POST", `/projects/${this._project()}/intents`, {
      body,
      idempotent: true,
      timeoutMs: TIMEOUT_MS.model,
    });
  }

  applyPlan(planId) {
    // Creates AND starts one LXD instance per committed VM before answering
    // (lxd_create_timeout_s + lxd_start_timeout_s each). A short deadline here
    // could only ever abort a call that was about to succeed.
    return this._request("POST", `/projects/${this._project()}/vms/apply`, {
      body: { plan_id: planId },
      idempotent: true,
      timeoutMs: TIMEOUT_MS.provision,
    });
  }

  /* --------------------------------------------------------------- agents */

  /**
   * Start / stop / restart one agent (controller-api.md §4.10). `vmId` is the
   * identifier the panel was given — the telemetry `vm_id` — and the controller
   * resolves it to the LXD instance on the owning node. Human-issued, so it is
   * audited exactly like a node action (a command row + an operator-log line).
   */
  agentAction(vmId, action) {
    return this._request(
      "POST",
      `/projects/${this._project()}/agents/${encodeURIComponent(vmId)}/actions/${encodeURIComponent(action)}`,
      { body: {}, idempotent: true },
    );
  }

  /**
   * Give one agent a task (controller-api.md §4.9). The controller proxies it
   * to the instance's loopback surface through the owning node, so the client
   * never needs a route to the VM and never holds instance credentials.
   */
  submitAgentTask(vmId, { prompt, maxTokens = 256, actor = "human" } = {}) {
    const body = { prompt, actor };
    if (maxTokens) body.max_tokens = maxTokens;
    return this._request(
      "POST",
      `/projects/${this._project()}/agents/${encodeURIComponent(vmId)}/tasks`,
      { body, idempotent: true },
    );
  }

  getAgentTask(vmId, taskId) {
    return this._request(
      "GET",
      `/projects/${this._project()}/agents/${encodeURIComponent(vmId)}/tasks/${encodeURIComponent(taskId)}`,
    );
  }

  listAgentTasks(vmId, limit = 10) {
    return this._request(
      "GET",
      `/projects/${this._project()}/agents/${encodeURIComponent(vmId)}/tasks`,
      { query: { limit } },
    );
  }

  approveRemediation(commandId, note) {
    const body = note ? { note } : {};
    return this._request("POST", `/projects/${this._project()}/remediations/${encodeURIComponent(commandId)}/approve`, {
      body,
      idempotent: true,
    });
  }

  denyRemediation(commandId, { reason, note } = {}) {
    const body = {};
    if (reason) body.reason = reason;
    if (note) body.note = note;
    return this._request("POST", `/projects/${this._project()}/remediations/${encodeURIComponent(commandId)}/deny`, {
      body,
      idempotent: true,
    });
  }

  /**
   * The AI assistant (controller-api.md §4.10): grounded Q&A over the
   * controller's own records, with guardrail-admitted execute authority.
   *
   * `body` is built by `core/assistant.js buildAskBody()` — `{messages,
   * context?, conversation_id, message_id}`. No Idempotency-Key header is sent
   * here on purpose: execution idempotency is anchored on the body's
   * `conversation_id`/`message_id` (§4.10), and a fresh header key per retry
   * would only muddy that. Docs and README must never claim more than the
   * controller returns — `executed[]` is what ran, nothing else.
   */
  assistantAsk(body) {
    // The answer is a model call on the controller's side; give it its own budget.
    return this._request("POST", `/projects/${this._project()}/assistant/ask`, {
      body,
      timeoutMs: TIMEOUT_MS.assistant,
    });
  }
}

export function newKey() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
