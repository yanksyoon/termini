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

  async listNodes() {
    const now = Date.now();
    return {
      nodes: NODES.map((n, i) => ({
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
      })),
    };
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
