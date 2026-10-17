/*
 * Agents — the client's own AI agents, read from node telemetry.
 *
 * SOURCE OF TRUTH: `lxd_instances[]` in each node's latest telemetry Sample
 * (node-telemetry.md §4.6), fetched through `GET /projects/{pid}/nodes/{nid}` →
 * `metrics_latest`. There is deliberately no second inventory: the panel shows
 * what the nodes report, and every fact it cannot get from that contract is
 * rendered as an explicit "not reported" instead of being invented.
 *
 * Three properties of that contract shape this module:
 *
 *  1. `vm_id` is a one-way hash of the LXD instance name, not the name. It is
 *     what the client is allowed to see, so it is the id this UI shows and the
 *     id it addresses an agent by (the controller resolves it — §4.6/§4.10).
 *  2. The field is OMITTED when the node cannot read its own LXD, and an empty
 *     array means "LXD answered with zero instances". Those are different
 *     facts and are rendered differently; a missing field is never drawn as
 *     "no agents".
 *  3. Only RUNNING instances are reported, so a client-stopped agent vanishes
 *     from telemetry. The panel keeps the rows it acted on (`retained`) and
 *     labels them "stopped by you — no longer reported", so a stop is visible
 *     instead of the row silently disappearing.
 */

export const INVENTORY = { REPORTED: "reported", ABSENT: "absent", NO_SAMPLE: "no-sample" };

export function shortId(vmId) {
  const s = String(vmId || "");
  if (s.length <= 10) return s || "—";
  return `${s.slice(0, 8)}…`;
}

function num(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function str(v) {
  return typeof v === "string" && v ? v : null;
}

/** Normalise one `lxd_instances[]` entry. Every field stays optional/nullable. */
export function agentFromInstance(node, inst) {
  if (!inst || typeof inst !== "object") return null;
  const vmId = str(inst.vm_id);
  if (!vmId) return null;
  return {
    vmId,
    label: shortId(vmId),
    nodeId: node.node_id,
    nodeLabel: node.registration_id || shortId(node.node_id),
    nodeStatus: node.status || "unknown",
    state: str(inst.state) || "unknown",
    cpuPct: num(inst.cpu_util_pct),
    ramUsed: num(inst.ram_used_bytes),
    ramLimit: num(inst.ram_limit_bytes),
    diskUsed: num(inst.disk_used_bytes),
    stateAgeS: num(inst.state_age_sec),
    restarts: num(inst.restart_count),
    retained: false,
    source: "telemetry",
  };
}

/**
 * Group the reported agents by node, with every edge state made explicit.
 *
 * `samples` is a Map(node_id -> latest Sample). `retained` is a Map(vm_id ->
 * row) of agents this tab stopped (they are no longer in `lxd_instances[]`).
 */
export function agentGroups({ nodes = [], samples = new Map(), retained = new Map(), now = Date.now() } = {}) {
  const groups = [];
  const seen = new Set();

  for (const node of nodes) {
    if (node.synthetic) continue; // a seeder row is not hardware (contract §1.1)
    const sample = samples instanceof Map ? samples.get(node.node_id) : samples[node.node_id];
    let inventory = INVENTORY.NO_SAMPLE;
    let agents = [];
    if (sample && Array.isArray(sample.lxd_instances)) {
      inventory = INVENTORY.REPORTED;
      agents = sample.lxd_instances.map((i) => agentFromInstance(node, i)).filter(Boolean);
      for (const a of agents) seen.add(a.vmId);
    } else if (sample) {
      inventory = INVENTORY.ABSENT;
    }
    agents.sort((a, b) => a.label.localeCompare(b.label));
    groups.push({
      nodeId: node.node_id,
      label: node.registration_id || shortId(node.node_id),
      role: node.role || "compute",
      status: node.status || "unknown",
      inventory,
      agents,
      updatedAt: sample && sample.ts ? sample.ts : null,
    });
  }

  // Agents this tab stopped: telemetry no longer lists them (only running
  // instances are reported), so their node group gets them back, labelled.
  for (const [vmId, row] of retained) {
    if (seen.has(vmId)) continue;
    const group = groups.find((g) => g.nodeId === row.nodeId) || groups.find((g) => g.status !== "offline");
    if (!group) continue;
    group.agents.push({ ...row, vmId, label: shortId(vmId), state: "stopped", retained: true });
    group.agents.sort((a, b) => a.label.localeCompare(b.label));
  }

  const all = groups.flatMap((g) => g.agents);
  const running = all.filter((a) => a.state === "running").length;
  const stopped = all.filter((a) => a.state === "stopped").length;
  return {
    groups,
    agents: all,
    total: all.length,
    running,
    stopped,
    // "we do not know" is not "none": only a reported inventory can say zero.
    known: groups.some((g) => g.inventory === INVENTORY.REPORTED),
    inventoryMissing: groups.filter((g) => g.inventory !== INVENTORY.REPORTED).length,
    nodes: groups.length,
    at: now,
  };
}

/** The header pill text: never "0 agents" when no node reported an inventory. */
export function agentsSummary(view) {
  if (!view || !view.nodes) return { kind: "muted", text: "agents —" };
  const degraded = view.inventoryMissing
    ? ` · ${view.inventoryMissing} node${view.inventoryMissing === 1 ? "" : "s"} no inventory`
    : "";
  if (!view.known) return { kind: "warn", text: `agents unknown${degraded}` };
  const kind = view.total === 0 ? "muted" : view.stopped ? "warn" : "ok";
  const stopped = view.stopped ? ` · ${view.stopped} stopped` : "";
  return { kind, text: `agents ${view.total}${stopped}${degraded}` };
}

/**
 * The honest inference label, taken from what the API reported with the answer
 * (`task.provenance`, agent-runtime.md §8.3 / controller-api.md §4.9):
 *
 *   backend=gemini-proxy / forwarded=true / egress=external
 *       -> a forwarded MANAGED model, explicitly NOT local GPU inference
 *   backend=local
 *       -> local inference on the node
 *   absent / unknown
 *       -> say the endpoint carried no label; never imply local inference
 */
export function inferenceLabel(provenance, model) {
  const p = provenance && typeof provenance === "object" ? provenance : null;
  const alias = str(p && p.alias) || str(model) || null;
  if (!p) {
    return {
      kind: "unknown",
      text: "inference backend not yet reported",
      detail: "no task has answered on this panel yet — run one to get the endpoint's own provenance label",
    };
  }
  const backend = str(p.backend) || "unknown";
  const forwarded =
    p.forwarded === true || p.forwarded === "true" || backend === "gemini-proxy" || p.egress === "external";
  const answeredBy = str(p.answered_by) || "unlabelled";
  const destination = str(p.destination) || null;
  if (forwarded) {
    return {
      kind: "forwarded",
      text: `${alias || "model"} · forwarded managed model (${backend})`,
      detail:
        `answered by ${answeredBy}${destination ? ` at ${destination}` : ""} — ` +
        `this is NOT local GPU inference on your node (egress ${p.egress || "external"})`,
      provenance: p,
    };
  }
  if (backend === "local") {
    return {
      kind: "local",
      text: `${alias || "model"} · local inference (${backend})`,
      detail: `answered by ${answeredBy} — served on the agent's own node, no external egress`,
      provenance: p,
    };
  }
  return {
    kind: "unknown",
    text: `${alias || "model"} · backend ${backend}`,
    detail:
      "the endpoint carried no backend label; this panel does not claim it was served locally " +
      `(source: ${str(p.source) || "unknown"})`,
    provenance: p,
  };
}

/** Task states a row renders (queued | running | done | error) + the answer. */
export function taskView(task) {
  if (!task) return null;
  const state = str(task.state) || "queued";
  return {
    taskId: task.task_id || null,
    state,
    answer: typeof task.answer === "string" ? task.answer : null,
    model: str(task.model),
    provenance: task.provenance && typeof task.provenance === "object" ? task.provenance : null,
    errorCode: str(task.error_code),
    errorMessage: str(task.error_message) || str(task.error && task.error.message) || null,
    durationMs: num(task.duration_ms),
    submittedAt: str(task.submitted_at),
    terminal: state === "done" || state === "error",
  };
}

/**
 * `vm_id = 26 Crockford base32 chars of sha256("termini-vm-id:" + name)`
 * (node-telemetry.md §4.6). Implemented here for ONE purpose: after `Apply
 * plan`, the controller returns instance NAMES, and this lets the panel point at
 * the exact rows those names become instead of claiming "they will appear".
 * The node's telemetry remains the only thing that can actually list an agent.
 */
export async function vmIdForName(instanceName) {
  const subtle = globalThis.crypto && globalThis.crypto.subtle;
  if (!subtle) throw new Error("crypto.subtle is unavailable (needs a secure context)");
  const bytes = new TextEncoder().encode(`termini-vm-id:${instanceName}`);
  const digest = new Uint8Array(await subtle.digest("SHA-256", bytes));
  let value = 0n;
  for (let i = 0; i < 17; i++) value = (value << 8n) | BigInt(digest[i]);
  value >>= 6n;
  const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  let out = "";
  for (let i = 0; i < 26; i++) {
    out = alphabet[Number(value & 0x1fn)] + out;
    value >>= 5n;
  }
  return out;
}

/** Rows shown for a just-applied plan, until telemetry reports the instances. */
export function expectedRows(names, existing, now = Date.now()) {
  const have = new Set((existing || []).map((a) => a.vmId));
  return (names || []).map((name) => ({
    name,
    // null hash until vmIdForName resolves; the row renders "waiting for the node"
    vmId: null,
    label: null,
    state: "provisioning",
    expected: true,
    seen: have.has(name),
    at: now,
  }));
}
