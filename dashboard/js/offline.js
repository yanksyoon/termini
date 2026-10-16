/*
 * Zero-backend offline demo backend.
 *
 * Enabled with `?offline=1` or via Settings. It produces contract-shaped
 * payloads in the browser so the dashboard can be shown on a projector with no
 * controller running at all. It is NOT used when a live controller is
 * configured; it exists only so the demo never looks broken.
 */

import { ApiError } from "./api.js";

const NODES = [
  { node_id: "01H4KZ9Q2R3T5V7W9X1Y3Z5B1E", registration_id: "termini-node-a", role: "compute" },
  { node_id: "01H4KZ9Q2R3T5V7W9X1Y3Z5C2F", registration_id: "termini-node-b", role: "compute" },
  { node_id: "01H4KZ9Q2R3T5V7W9X1Y3Z5D3G", registration_id: "termini-node-c", role: "inference" },
];

function wobble(seed, t, lo, hi) {
  const v = Math.sin(t / 9000 + seed) * 0.5 + Math.sin(t / 2300 + seed * 2.3) * 0.5;
  return lo + ((v + 1) / 2) * (hi - lo);
}

export class OfflineBackend {
  constructor() {
    this.started = Date.now();
    this.commands = new Map();
    this.pending = new Map();
    this._seq = 0;
  }

  _seed(role) {
    // node-c (inference) drifts hotter; node-a degrades over time for the demo
    return role === "inference" ? 1.7 : 0.4;
  }

  _sample(node, tMs) {
    const s = this._seed(node.role);
    const hot = node.node_id.endsWith("B1E") ? 40 : 0; // node-a runs hotter
    const gpu = node.role === "inference";
    const ramTotal = 34359738368;
    const ramUsed = ramTotal * ((wobble(s, tMs, 0.35, 0.72) ) * 0 + Math.min(0.9, ramUsedPct(tMs, s, hot)) / 100);
    const sample = {
      seq: ++this._seq,
      ts: new Date(tMs).toISOString(),
      uptime_sec: Math.floor((tMs - this.started) / 1000) + 3600,
      cpu_util_pct: round1(wobble(s, tMs, 12 + hot, 55 + hot)),
      cpu_core_count: 16,
      ram_total_bytes: ramTotal,
      ram_used_bytes: Math.round(ramUsed),
      swap_used_bytes: 0,
      load_avg_1m: round2(wobble(s + 1, tMs, 0.4, 6.5)),
      cpu_temp_celsius: round1(wobble(s + 2, tMs, 40, 62)),
      disk: [
        {
          path: "/dev/sda",
          total_bytes: 274877906944,
          free_bytes: Math.round(274877906944 * wobble(s + 3, tMs, 0.28, 0.55)),
          write_latency_ms: round1(node.node_id.endsWith("C1E") ? wobble(s + 4, tMs, 6, 62) : wobble(s + 4, tMs, 1.5, 12)),
          mount_ro: false,
        },
      ],
      net: [{ iface: "eth0", rx_bytes: Math.round(wobble(s + 5, tMs, 2e5, 3.2e7)), tx_bytes: Math.round(wobble(s + 6, tMs, 1e5, 1.4e7)), latency_ms: round1(wobble(s + 7, tMs, 0.3, 4.5)) }],
      gpu: gpu
        ? [{ model: "NVIDIA L4", util_pct: round1(wobble(s + 8, tMs, 35, 96)), vram_total_bytes: 25769803776, vram_used_bytes: Math.round(wobble(s + 9, tMs, 0.4, 0.85) * 25769803776), temp_celsius: round1(wobble(s + 10, tMs, 52, 78)) }]
        : [],
      lxd_instances: [{ vm_id: "01H4KZ9Q2R3T5V7W9X1Y3Z5E4H", state: "running", cpu_util_pct: 12, ram_used_bytes: 3.3e9, ram_limit_bytes: 4.29e9, disk_used_bytes: 2.3e9 }],
      inference: gpu ? { server_up: true, model: "deepseek-flash", active_requests: Math.round(wobble(s + 11, tMs, 0, 6)), queue_depth: 0, tokens_per_s: round1(wobble(s + 12, tMs, 20, 70)), vram_used_bytes: 12.8e9 } : undefined,
      services: [
        { name: "termini-bootstrap", status: "running" },
        { name: "lxd", status: "running" },
        { name: "sshd", status: "running" },
        ...(gpu ? [{ name: "llama-cpp", status: "running" }] : []),
      ],
    };
    return sample;
  }

  async listNodes({ includeSynthetic = false } = {}) {
    const now = Date.now();
    const nodes = NODES.map((n, i) => ({
        ...n,
        status: i === 2 ? "degraded" : "online",
        last_seen_at: new Date(now - (i === 2 ? 22000 : 1200)).toISOString(),
        hardware: {
          cpu: { model: i === 2 ? "AMD EPYC 7B13" : "Intel Xeon Gold", threads: 16, cores: 8 },
          memory: { total_bytes: 34359738368 },
          disks: [{ device: "/dev/sda", type: "ssd", size_bytes: 274877906944 }],
          gpus: i === 2 ? [{ vendor: "nvidia", model: "L4", vram_bytes: 25769803776 }] : [],
        },
        capacity: { cpu_cores: 16, ram_gb: 32, disk_gb: 256, gpu_free: i === 2 ? 1 : 0, cpu_allocated: 4, ram_allocated_gb: 8 },
      }));
    return { nodes };
  }

  /**
   * Offline decision fixtures: shaped exactly like /v1/operator/{decisions,
   * events,audits} so the decisions panel (and its join) can be exercised with
   * no controller at all. Deliberately contains BOTH a model decision and a
   * deterministic fallback, and no egress block is invented here — the decider
   * label comes from the stream line below.
   */
  operatorFixtures() {
    if (this._fixtures) return this._fixtures;
    const now = Date.now();
    const [a, , c] = NODES;
    const instance = "01H4KZ9Q2R3T5V7W9X1Y3Z5E4H";
    const mk = (i, over) => {
      const decided_at = new Date(now - (i + 1) * 96_000).toISOString();
      const event_id = ulidish();
      const id = ulidish();
      const decision = {
        id,
        event_id,
        node_id: over.node_id,
        rule_id: over.rule_id,
        playbook: over.rule_id,
        action_kind: over.action_kind,
        action_params: over.action_params,
        reason: over.reason,
        decided_at,
        decision_source: over.source,
        confidence: over.confidence ?? null,
        fallback_reason: over.fallback_reason ?? null,
        // §4.2 incident record columns. All five are `null` for a
        // deterministic-mode row (that mode writes no record at all, §8.1) —
        // which is exactly the state the dashboard must render as
        // "no incident record (deterministic mode)".
        incident_title: over.incident_title ?? null,
        incident_explanation: over.incident_explanation ?? null,
        incident_evidence: over.incident_evidence ?? null,
        incident_actions: over.incident_actions ?? null,
        incident_generated: over.incident_generated ?? null,
      };
      const event = {
        id: event_id,
        node_id: over.node_id,
        rule_id: over.rule_id,
        type: over.type,
        metric: over.metric,
        value: over.value,
        threshold: over.threshold,
        evidence_seq_start: 1000 + i * 10,
        evidence_seq_end: 1002 + i * 10,
        fired_at: decided_at,
        breach_started_at: new Date(Date.parse(decided_at) - 8000).toISOString(),
        evidence: [0, 1, 2].map((k) => this._sample(NODES.find((n) => n.node_id === over.node_id) || a, now - (i + 1) * 96_000 + k * 1000)),
      };
      const audit = {
        id: ulidish(),
        decision_id: id,
        event_id,
        node_id: over.node_id,
        rule_id: over.rule_id,
        action_kind: over.action_kind,
        result: over.result,
        detail: over.result === "ok" ? "instance restarted" : "HTTP 404: Instance not found",
        lxd_method: "PUT",
        lxd_path: `/1.0/instances/${instance}/state`,
        lxd_body: JSON.stringify({ action: over.action_kind.split(".")[1], force: true }),
        idempotency_key: `${over.node_id}:${over.rule_id}:${over.action_kind}:${instance}:${i}`,
        dry_run: 0,
        executed_at: decided_at,
        lxd_endpoint: "https://10.10.0.11:8443",
      };
      return { decision, event, audit };
    };
    const rows = [
      mk(0, { node_id: c.node_id, rule_id: "disk-degrade-01", type: "disk_degrade", metric: "disk_write_latency_ms", value: 120, threshold: "free <= 5% OR write_latency_ms >= 50 OR free slope <= -5000000.0 bytes/s", action_kind: "instance.stop", action_params: { instance_name: instance, force: true }, reason: "disk write latency on /dev/sda is 120ms, exceeding the 50ms threshold; stopping the instance with the highest disk usage will reduce disk I/O.", source: "model", confidence: 0.85, result: "ok",
        // §4.2 record as the model returned it (generated: "model").
        incident_generated: "model",
        incident_title: "Disk write latency on /dev/sda",
        incident_explanation: "write_latency_ms on /dev/sda reached 120 ms against a 50 ms threshold while free space stayed above the 5% floor, so this is I/O contention rather than a full disk. The single instance on this node holds the write path, so stopping it removes the load without draining the node.",
        incident_evidence: [
          { field: "disk_write_latency_ms", value: 120, seq: 1120, ts: new Date(now - 96_000).toISOString() },
          { field: "disk_free_bytes", value: 74_000_000_000, seq: 1120 },
        ],
        incident_actions: [
          { action_kind: "instance.stop", target: instance, note: "stop the instance holding the write path" },
        ],
      }),
      mk(1, { node_id: a.node_id, rule_id: "oom-guard-01", type: "oom", metric: "ram_used_bytes", value: 33114165248, threshold: ">= 95% of ram_total_bytes for 2 consecutive samples", action_kind: "instance.restart", action_params: { instance_name: instance, force: true, timeout_s: 30 }, reason: "host RAM at 96.4% of total with one instance holding 29%; restarting it reclaims memory.", source: "model", confidence: 0.9, result: "ok",
        incident_generated: "model",
        incident_title: "Memory exhaustion on node a",
        incident_explanation: "ram_used_bytes reached 96.4% of ram_total_bytes and stayed there for two consecutive samples, and one instance holds 29% of the node's RAM by itself. That is a single-instance leak rather than fleet-wide pressure, so restarting that instance reclaims the memory without touching the node.",
        incident_evidence: [
          { field: "ram_used_bytes", value: 33114165248, seq: 1110, ts: new Date(now - 192_000).toISOString() },
          { field: "ram_total_bytes", value: 34359738368, seq: 1110, ts: new Date(now - 192_000).toISOString() },
          { field: "lxd_instances.ram_used_bytes", value: 9_960_000_000, seq: 1110 },
        ],
        incident_actions: [
          { action_kind: "instance.restart", target: instance, note: "restart the heaviest instance to reclaim its resident memory" },
        ],
      }),
      mk(2, { node_id: a.node_id, rule_id: "oom-guard-01", type: "oom", metric: "ram_used_bytes", value: 33000000000, threshold: ">= 95% of ram_total_bytes for 2 consecutive samples", action_kind: "instance.restart", action_params: { instance_name: instance, force: true, timeout_s: 30 }, reason: "playbook: restart the heaviest instance (RAM 96%)", source: "fallback", fallback_reason: "inference_unavailable", result: "error",
        // §8.1: the fallback writes a record too — built from the fired event's
        // own C0 fields and LABELLED, so it is never rendered as model output.
        incident_generated: "fallback",
        incident_title: "oom on node a",
        incident_explanation: `model unavailable (inference_unavailable); the deterministic playbook chose instance.restart on ${instance} for rule oom-guard-01`,
        incident_evidence: [
          { field: "ram_used_bytes", value: 33000000000, seq: 1122, ts: new Date(now - 288_000).toISOString() },
        ],
        incident_actions: [
          { action_kind: "instance.restart", target: instance },
        ],
      }),
      mk(3, { node_id: c.node_id, rule_id: "latency-01", type: "latency", metric: "net_latency_ms", value: 240, threshold: "net_latency_ms >= 200 for 3 consecutive samples", action_kind: "instance.restart", action_params: { instance_name: instance }, reason: "network latency 240ms sustained; restart the agent instance.", source: "deterministic", result: "ok" }),
      mk(4, { node_id: c.node_id, rule_id: "gpu-oos-01", type: "gpu_oos", metric: "gpu_vram_used_bytes", value: 27000000000, threshold: "gpu_vram_used_bytes >= 95% of gpu_vram_total_bytes", action_kind: "image.prune", action_params: {}, reason: "VRAM pressure surfaced only, no autonomous action.", source: "fallback", fallback_reason: "parse_error", result: "error",
        incident_generated: "fallback",
        incident_title: "gpu_oos on node c",
        incident_explanation: "model unavailable (parse_error); the deterministic playbook chose image.prune for rule gpu-oos-01",
        incident_evidence: [
          { field: "gpu_vram_used_bytes", value: 27000000000, seq: 986, ts: new Date(now - 384_000).toISOString() },
        ],
        incident_actions: [{ action_kind: "image.prune" }],
      }),
    ];
    this._fixtures = rows;
    return rows;
  }

  async operatorDecisions(limit = 50) {
    return { decisions: this.operatorFixtures().map((r) => r.decision).slice(0, limit) };
  }

  async operatorEvents(limit = 50) {
    return { events: this.operatorFixtures().map((r) => r.event).slice(0, limit) };
  }

  async operatorAudits(limit = 50) {
    return { audits: this.operatorFixtures().map((r) => r.audit).slice(0, limit) };
  }

  async getNode(nodeId) {
    const node = (await this.listNodes()).nodes.find((n) => n.node_id === nodeId);
    return { node, metrics_latest: this._sample(node, Date.now()) };
  }

  async latestSample(nodeId) {
    const node = NODES.find((n) => n.node_id === nodeId) || NODES[0];
    return { node_id: nodeId, samples: [this._sample(node, Date.now())] };
  }

  async getMetrics(nodeId, { start, end, resolution } = {}) {
    const node = NODES.find((n) => n.node_id === nodeId) || NODES[0];
    const endMs = end ? Date.parse(end) : Date.now();
    const startMs = start ? Date.parse(start) : endMs - 900_000;
    const bucket = resolution === "5m" ? 300_000 : resolution === "1m" ? 60_000 : 5000;
    const points = [];
    for (let t = startMs; t <= endMs; t += bucket) {
      const s = this._sample(node, t);
      points.push(s);
    }
    const mk = (name, unit, fn) => ({ name, unit, points: points.map((s) => ({ t: new Date(s.ts).toISOString(), v: fn(s) })) });
    const series = [
      mk("cpu_util_pct", "percent", (s) => s.cpu_util_pct),
      mk("ram_total_bytes", "bytes", (s) => s.ram_total_bytes),
      mk("ram_used_bytes", "bytes", (s) => s.ram_used_bytes),
      mk("load_avg_1m", "loadavg", (s) => s.load_avg_1m),
      mk("cpu_temp_celsius", "celsius", (s) => s.cpu_temp_celsius),
      mk("disk_total_bytes", "bytes", (s) => s.disk[0].total_bytes),
      mk("disk_free_bytes", "bytes", (s) => s.disk[0].free_bytes),
      mk("disk_write_latency_ms", "ms", (s) => s.disk[0].write_latency_ms),
      mk("net_rx_bytes", "bytes", (s) => s.net[0].rx_bytes),
      mk("net_tx_bytes", "bytes", (s) => s.net[0].tx_bytes),
      mk("net_latency_ms", "ms", (s) => s.net[0].latency_ms),
    ];
    if (node.role === "inference") {
      series.push(
        mk("gpu_util_pct", "percent", (s) => s.gpu[0].util_pct),
        mk("gpu_vram_used_bytes", "bytes", (s) => s.gpu[0].vram_used_bytes),
        mk("gpu_vram_total_bytes", "bytes", (s) => s.gpu[0].vram_total_bytes),
        mk("inference_tokens_per_s", "tok/s", (s) => s.inference.tokens_per_s),
        mk("inference_active_requests", "count", (s) => s.inference.active_requests),
      );
    }
    return { node_id: nodeId, resolution: resolution || "5s", series };
  }

  async rebootNode(nodeId) {
    const command_id = ulidish();
    this.commands.set(command_id, { command_id, type: "node.reboot", state: "queued" });
    return { command_id, type: "node.reboot", state: "queued", issued_by: "human" };
  }

  async drainNode(nodeId) {
    const command_id = ulidish();
    this.commands.set(command_id, { command_id, type: "node.drain", state: "queued" });
    return { command_id, type: "node.drain", state: "queued", issued_by: "human" };
  }

  async submitIntent({ prompt }) {
    if (/reject|too big|impossible/i.test(prompt || "")) {
      const plan = {
        plan_id: ulidish(),
        outcome: "rejected",
        allocations: [{ index: 0, status: "rejected", requested: { type: "agent_vm", count: 4, vcpus: 2, ram_gb: 4 }, guardrail: { clamped_fields: [], reject_reasons: ["exceeds_node_ram_ceiling"] } }],
      };
      throw new ApiError(422, { error: { code: "GUARDRAIL_REJECTED", message: "plan rejected by guardrail" }, ...plan }, "");
    }
    return {
      plan_id: ulidish(),
      project_id: "offline",
      outcome: "accepted",
      allocations: [
        { index: 0, status: "accepted", requested: { type: "agent_vm", count: 3, vcpus: 2, ram_gb: 4 }, resolved: { type: "agent_vm", count: 3, vcpus: 2, ram_gb: 4 }, guardrail: { clamped_fields: [], reject_reasons: [] }, node_id: NODES[0].node_id },
        { index: 1, status: "accepted", requested: { type: "agent_vm", count: 1, vcpus: 12, ram_gb: 4 }, resolved: { type: "agent_vm", count: 1, vcpus: 8, ram_gb: 4 }, guardrail: { clamped_fields: [{ field: "vcpus", decision: "clamped", from: 12, to: 8, reason: "preferred_range" }], reject_reasons: [] }, node_id: NODES[1].node_id },
      ],
    };
  }

  async applyPlan(planId) {
    return { job_id: ulidish(), state: "provisioning", vm_ids: [ulidish(), ulidish()] };
  }

  async approveRemediation(commandId) {
    return { command_id: commandId, state: "approved", approved_by: "human", approved_at: new Date().toISOString() };
  }

  async denyRemediation(commandId) {
    return { command_id: commandId, state: "denied", denied_by: "human", denied_at: new Date().toISOString() };
  }
}

export class OfflineStream {
  constructor(backend, handlers = {}) {
    this.backend = backend;
    this.handlers = handlers;
    this.timer = null;
  }

  start() {
    const h = this.handlers;
    if (h && h.onStatus) h.onStatus({ state: "open", detail: "offline fixture" });
    const emit = () => {
      const line = randomLine(this.backend);
      if (h && h.onLine) h.onLine(line);
    };
    // a short backfill burst, then a steady stream
    let burst = 12;
    const iv = setInterval(() => {
      emit();
      if (--burst <= 0) clearInterval(iv);
    }, 250);
    this.timer = setInterval(emit, 4200);
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    if (this.handlers && this.handlers.onStatus) this.handlers.onStatus({ state: "stopped" });
  }
}

let lastPendingEmitted = false;

function randomLine(backend) {
  const nodes = NODES;
  const node = nodes[Math.floor(Math.random() * nodes.length)];
  const pool = [
    { severity: "info", message: `Telemetry sample accepted from ${node.registration_id}.` },
    { severity: "debug", message: "Sampling interval 1000 ms (demo profile)." },
    { severity: "info", message: `Health flags recomputed for ${node.registration_id}.` },
    {
      severity: "info",
      message:
        "externalModelCall after endpoint=http://127.0.0.1:8085 backend=gemini-proxy " +
        "egress=external destination=aiplatform.googleapis.com model=gemini-2.5-flash " +
        "answered_by=forwarded-managed-model outcome=returned request_id=" + ulidish(),
    },
    { severity: "warn", message: `disk-degrade-01 fired on ${node.registration_id}: write_latency_ms ≥ 50 (sample), flag + alert only.` },
    { severity: "error", message: `oom-guard-01 fired on ${node.registration_id}: ram_used_bytes ≥ 95% ×2, advising vm.resize (auto).`, remediation: { type: "vm.resize", status: "succeeded", command_id: ulidish() } },
  ];
  if (!lastPendingEmitted) {
    lastPendingEmitted = true;
    const command_id = ulidish();
    const rem = { command_id, type: "node.drain", status: "pending_approval" };
    backend.pending.set(command_id, rem);
    return { log_id: ulidish(), ts: new Date().toISOString(), node_id: nodes[0].node_id, severity: "warn", actor: "ai_operator", message: "Node A disk failure predicted (disk-fail-01). Issuing node.drain — awaiting human approval.", remediation: rem };
  }
  const pick = pool[Math.floor(Math.random() * pool.length)];
  return {
    log_id: ulidish(),
    ts: new Date().toISOString(),
    node_id: node.node_id,
    severity: pick.severity,
    actor: "ai_operator",
    message: pick.message,
    remediation: pick.remediation || null,
  };
}

let counter = 0;
function ulidish() {
  counter += 1;
  const base = "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C";
  const tail = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  let out = "";
  let n = counter;
  for (let i = 0; i < 5; i++) {
    out = tail[n % 32] + out;
    n = Math.floor(n / 32);
  }
  return base + out + "0";
}

function ramUsedPct(t, s, hot) {
  return wobble(s + 13, t, 38 + hot, 72 + hot);
}

function round1(v) {
  return Math.round(v * 10) / 10;
}
function round2(v) {
  return Math.round(v * 100) / 100;
}
