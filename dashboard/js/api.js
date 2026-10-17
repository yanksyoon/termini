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

  async _request(method, path, { body, query, idempotent } = {}) {
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
        // keep the SSE/polling path from wedging on a dead backend
        signal: AbortSignal.timeout ? AbortSignal.timeout(20000) : undefined,
      });
    } catch (err) {
      const e = new ApiError(0, { error: { code: "NETWORK", message: `controller unreachable: ${err.message}` } }, url);
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
    return this._request("POST", `/projects/${this._project()}/intents`, { body, idempotent: true });
  }

  applyPlan(planId) {
    return this._request("POST", `/projects/${this._project()}/vms/apply`, { body: { plan_id: planId }, idempotent: true });
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
    return this._request("POST", `/projects/${this._project()}/assistant/ask`, { body });
  }
}

export function newKey() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
