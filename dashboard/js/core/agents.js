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
 *
 * Two more rules shape what the panel may OFFER (task t_df3120a8). A row is
 * evidence about the fleet; an ACTION on it is a separate claim:
 *
 *  4. A row whose node is fabricated by the demo seeder (`node.synthetic ===
 *     true`, contract §1.1) is still rendered — it says something about what
 *     reported it — but it is not taskable, because no agent runtime is behind
 *     it. The reason is rendered beside the disabled action instead of being
 *     discovered as a 404. The decision comes from the node's own flag and the
 *     row's provenance, NEVER from the shape of the id: an id is a hash, and a
 *     hash that looks plausible is exactly how an unanswerable offer got made.
 *  5. When the controller DOES refuse a task, the refusal is rendered as the
 *     controller's answer (`refusalView`): code, message, `request_id` and every
 *     field of `details[0]` it sent — `reason`, `unscanned_nodes`,
 *     `attributed_nodes`, `known_agent_ids`, `literal_name`, `derived_agent_id`,
 *     `sources`. A 503 that names an unreadable node reads as "the node holding
 *     this agent could not be read", not as a generic failure. Fields the
 *     controller did not send are not invented; `known_vm_ids` is gone from the
 *     contract (internal instance names are not client-visible), so it is not
 *     rendered.
 */

export const INVENTORY = { REPORTED: "reported", ABSENT: "absent", NO_SAMPLE: "no-sample" };

/*
 * After `Apply plan` the panel holds the plan's instances as placeholder rows
 * until a node's telemetry lists them. Two bounded states, because "provisioning"
 * is a claim with a shelf life:
 *
 *  - STALE: after 2 min no node has listed it. The row stops claiming it is
 *    provisioning and says so (the apply may have been rolled back, or the node
 *    cannot read its LXD — both are visible facts).
 *  - DROP: after 10 min the row is removed. Telemetry is the only inventory;
 *    a row nothing ever reports must not sit in the panel forever.
 */
export const EXPECTED_STATE = "provisioning";
export const EXPECTED_UNCONFIRMED = "unconfirmed";
export const EXPECTED_STALE_MS = 120_000;
export const EXPECTED_DROP_MS = 600_000;

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

/* ------------------------------------------------------------ taskability */

/*
 * The reasons an action is NOT offered, written for the reader standing in
 * front of the panel. They are rendered beside the disabled control, so the
 * panel cannot offer something that will 404 and leave the reader guessing.
 */
export const TASK_REFUSAL = {
  DEMO: "demo row: seeder-fabricated instance, no agent runtime behind it",
  STOPPED: "stopped by you — start it before giving it a task",
  NO_ROW: "this row is not an agent instance",
};

/**
 * May this row be given a task, and if not, why?
 *
 * Pure, and decided from facts the panel already holds:
 *  - `synthetic` — the node that attested the instance is fabricated by the
 *    demo seeder (contract §1.1). The row is shown; the action is not offered.
 *  - `retained` / `state === "stopped"` — the instance is not running, so the
 *    runtime inside it is not listening either. (Its Start/Stop controls stay
 *    available: start is how the reader brings it back.)
 *
 * `actions` is false only when NO control on the row can work (a demo row has
 * no instance at all, so start/stop/restart would 404 exactly like the task).
 *
 * Deliberately NOT decided from `vmId`: the id is a one-way hash, so its shape
 * carries no evidence about whether a runtime answers behind it.
 */
export function taskability(row) {
  if (!row || typeof row !== "object") return { taskable: false, reason: TASK_REFUSAL.NO_ROW, actions: false };
  if (row.synthetic === true) return { taskable: false, reason: TASK_REFUSAL.DEMO, actions: false };
  if (row.retained === true || row.state === "stopped") return { taskable: false, reason: TASK_REFUSAL.STOPPED, actions: true };
  return { taskable: true, reason: null, actions: true };
}

/** A row with its taskability merged in, so every renderer reads one field. */
export function withTaskability(row) {
  const t = taskability(row);
  return { ...row, taskable: t.taskable, taskableReason: t.reason, actionsAvailable: t.actions };
}

/** Normalise one `lxd_instances[]` entry. Every field stays optional/nullable. */
export function agentFromInstance(node, inst) {
  if (!inst || typeof inst !== "object") return null;
  const vmId = str(inst.vm_id);
  if (!vmId) return null;
  return withTaskability({
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
    // Provenance: which kind of node attested this row. A row from a synthetic
    // node is shown as evidence, never offered as a target (t_df3120a8).
    synthetic: node.synthetic === true,
  });
}

/**
 * Group the reported agents by node, with every edge state made explicit.
 *
 * `samples` is a Map(node_id -> latest Sample). `retained` is a Map(vm_id ->
 * row) of agents this tab stopped (they are no longer in `lxd_instances[]`).
 *
 * A synthetic (seeder-fabricated) node still gets a group and its rows, because
 * those rows are evidence — but the group is marked `synthetic`, its rows are
 * not taskable, and they are NOT counted as the client's agents (`demo` counts
 * them separately). Hiding them would have made the panel claim a clean fleet
 * it had not looked at; offering an action on them is what produced the bare
 * `AGENT_NOT_FOUND` this card is about (t_df3120a8).
 */
export function agentGroups({ nodes = [], samples = new Map(), retained = new Map(), now = Date.now() } = {}) {
  const groups = [];
  const seen = new Set();

  for (const node of nodes) {
    const synthetic = node.synthetic === true;
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
      synthetic,
      inventory,
      agents,
      updatedAt: sample && sample.ts ? sample.ts : null,
    });
  }

  // Agents this tab stopped: telemetry no longer lists them (only running
  // instances are reported), so their node group gets them back, labelled. The
  // fallback is a REAL node: a stopped agent is not evidence about a demo row.
  for (const [vmId, row] of retained) {
    if (seen.has(vmId)) continue;
    const group =
      groups.find((g) => !g.synthetic && g.nodeId === row.nodeId) ||
      groups.find((g) => !g.synthetic && g.status !== "offline");
    if (!group) continue;
    group.agents.push(withTaskability({ ...row, vmId, label: shortId(vmId), state: "stopped", retained: true, synthetic: false }));
    group.agents.sort((a, b) => a.label.localeCompare(b.label));
  }

  const isReal = (g) => !g.synthetic;
  const real = groups.filter(isReal).flatMap((g) => g.agents);
  const demo = groups.filter((g) => g.synthetic).flatMap((g) => g.agents);
  const running = real.filter((a) => a.state === "running").length;
  const stopped = real.filter((a) => a.state === "stopped").length;
  return {
    groups,
    agents: real,
    demoAgents: demo,
    total: real.length,
    running,
    stopped,
    demo: demo.length,
    // "we do not know" is not "none": only a reported inventory can say zero.
    known: groups.filter(isReal).some((g) => g.inventory === INVENTORY.REPORTED),
    inventoryMissing: groups.filter((g) => isReal(g) && g.inventory !== INVENTORY.REPORTED).length,
    nodes: groups.length,
    realNodes: groups.filter(isReal).length,
    at: now,
  };
}

/** The header pill text: never "0 agents" when no node reported an inventory. */
export function agentsSummary(view) {
  if (!view || !view.nodes) return { kind: "muted", text: "agents —" };
  const degraded = view.inventoryMissing
    ? ` · ${view.inventoryMissing} node${view.inventoryMissing === 1 ? "" : "s"} no inventory`
    : "";
  // Demo rows are named, never folded into the client's agent count: a
  // seeder-fabricated row is not one of the client's agents.
  const demo = view.demo ? ` · ${view.demo} demo` : "";
  if (!view.known) return { kind: "warn", text: `agents unknown${demo}${degraded}` };
  const kind = view.total === 0 ? "muted" : view.stopped ? "warn" : "ok";
  const stopped = view.stopped ? ` · ${view.stopped} stopped` : "";
  return { kind, text: `agents ${view.total}${stopped}${demo}${degraded}` };
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
    // A REFUSED submission carries the controller's whole answer (see
    // `refusalView`); the panel renders it instead of a one-line message.
    refusal: task.refusal && typeof task.refusal === "object" ? task.refusal : null,
    durationMs: num(task.duration_ms),
    submittedAt: str(task.submitted_at),
    terminal: state === "done" || state === "error",
  };
}

/* --------------------------------------------------------------- refusals */

/*
 * A refused task is the controller's ANSWER, and the panel must render that
 * answer — not the transport that carried it. Before t_df3120a8 the panel
 * showed a bare `AGENT_NOT_FOUND`, which reads as "the product is broken" when
 * the truth is "this id is not on your nodes" or "the node holding it could not
 * be read".
 *
 * Every field below comes from the response: nothing is invented, and a field
 * the controller did not send is not rendered. `known_vm_ids` is deliberately
 * absent from the contract now (it held internal instance names); the client-
 * safe field is `known_agent_ids`.
 */

/** Fields the controller sends first, in the order a reader needs them. */
const DETAIL_ORDER = [
  "reason",
  "unscanned_nodes",
  "attributed_nodes",
  "known_agent_ids",
  "literal_name",
  "derived_agent_id",
  "sources",
];

/*
 * Fields that left the contract and must not be drawn even when an older
 * controller still sends them: `known_vm_ids` held internal LXD instance NAMES,
 * which the client is never shown (t_a1fa0567 removed it for that reason). The
 * client-safe form is `known_agent_ids` — the one-way ids the panel already
 * shows. Rendering the superseded field would re-leak what the contract stopped
 * sending, so it is dropped here rather than trusted to the deployment's ref.
 */
const SUPERSEDED_DETAIL_FIELDS = ["known_vm_ids"];

/** One `unscanned_nodes[]` / object entry as a single readable line. */
function detailValue(v) {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) {
    if (!v.length) return "none";
    return v
      .map((x) => {
        if (x && typeof x === "object") {
          const id = x.node_id || x.id || x.node || null;
          const why = x.why || x.reason || x.detail || null;
          if (id && why) return `${id} — ${why}`;
          if (id) return String(id);
          return Object.entries(x).map(([k, y]) => `${k}=${y}`).join(" ");
        }
        return String(x);
      })
      .join("; ");
  }
  if (typeof v === "object") return Object.entries(v).map(([k, y]) => `${k}=${y}`).join(" ");
  return String(v);
}

/** The kind of refusal, from the code the controller answered with. */
function refusalKind(status, code, reason) {
  if (code === "NODE_UNREACHABLE" || reason === "node_unreachable") return "node_unreadable";
  if (code === "NODE_OFFLINE" || reason === "node_offline") return "node_offline";
  if (code === "AGENT_NOT_FOUND" || status === 404) return "not_on_project";
  if (code === "AGENT_UNREACHABLE") return "agent_unreachable";
  if (!status) return "network";
  return "refused";
}

const HEADLINES = {
  node_unreadable:
    "The node holding this agent could not be read, so whether it carries this id is unknown — nothing was dispatched.",
  node_offline: "The node holding this agent is offline — nothing was dispatched.",
  not_on_project:
    "That id is not on this project: none of your nodes carries it — nothing was dispatched.",
  agent_unreachable:
    "The instance exists, but its agent runtime did not answer — the task was not accepted.",
  network: "The controller could not be reached, so the task was not accepted.",
  refused: "The controller refused this task.",
};

/**
 * Render an `ApiError` (or anything shaped like one) as the refusal it is.
 *
 * @param {{status?:number, code?:string, message?:string, request_id?:string, details?:object[]}} err
 */
export function refusalView(err) {
  const e = err && typeof err === "object" ? err : {};
  const status = typeof e.status === "number" ? e.status : 0;
  const code = str(e.code) || (status ? "ERROR" : "NETWORK");
  const message = str(e.message) || "the request failed";
  const requestId = str(e.request_id) || null;
  const first =
    Array.isArray(e.details) && e.details.length && e.details[0] && typeof e.details[0] === "object"
      ? e.details[0]
      : {};
  const reason = str(first.reason) || null;
  const kind = refusalKind(status, code, reason);

  // EVERY field the controller sent, in a stable reading order, then anything
  // else it sent that this panel has not learned about yet — minus the fields
  // that left the contract (they may still arrive from an older deployment).
  const keep = (k) => !SUPERSEDED_DETAIL_FIELDS.includes(k);
  const keys = [
    ...DETAIL_ORDER.filter((k) => keep(k) && Object.prototype.hasOwnProperty.call(first, k)),
    ...Object.keys(first).filter(
      (k) => keep(k) && !DETAIL_ORDER.includes(k) && k !== "field" && k !== "field_name",
    ),
  ];
  const fields = keys.map((k) => ({ name: k, value: detailValue(first[k]) }));
  if (Object.prototype.hasOwnProperty.call(first, "field")) {
    fields.unshift({ name: "field", value: detailValue(first.field) });
  }

  const nodes = Array.isArray(first.unscanned_nodes)
    ? first.unscanned_nodes
        .map((n) => (n && typeof n === "object" ? { nodeId: n.node_id || n.id || null, why: n.why || n.reason || null } : null))
        .filter((n) => n && n.nodeId)
    : [];
  const attributed = Array.isArray(first.attributed_nodes)
    ? first.attributed_nodes.map((n) => (n && typeof n === "object" ? n.node_id || n.id : String(n))).filter(Boolean)
    : [];

  const headline = HEADLINES[kind] || HEADLINES.refused;
  const text = [headline, message, ...fields.map((f) => `${f.name}: ${f.value}`)]
    .concat(requestId ? [`request_id: ${requestId}`] : [])
    .join("\n");

  return {
    kind,
    status: status || null,
    code,
    message,
    requestId,
    reason,
    nodes,
    attributed,
    fields,
    headline,
    text,
  };
}

/**
 * A row the panel itself refuses to task (no request is sent at all). The shape
 * matches `refusalView` so the panel has ONE way to draw a refusal.
 */
export function refusedLocally(reason, vmId = null) {
  const why = str(reason) || TASK_REFUSAL.NO_ROW;
  const fields = [{ name: "reason", value: why }].concat(vmId ? [{ name: "vm_id", value: vmId }] : []);
  const headline = "No task was sent: this row cannot answer one.";
  return {
    kind: "not_taskable",
    status: null,
    code: "NOT_TASKABLE",
    message: why,
    requestId: null,
    reason: "not_taskable",
    nodes: [],
    attributed: [],
    fields,
    headline,
    text: [headline, why].join("\n"),
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

/**
 * Rows shown for a just-applied plan, until telemetry reports the instances.
 *
 * This is the panel's FIRST stage after `Apply plan`: the controller answers with
 * the LXD instance NAMES it materialised, the §4.6 rule turns a name into the id
 * the node will report, and the row is drawn as `provisioning` so the plan's
 * outcome lands in the Agents panel immediately instead of only in a toast. The
 * node's own telemetry remains the only thing that can turn it RUNNING.
 *
 * History (t_6d63e8a2): this function used to take a bare name list and return
 * `vmId: null` for every row while `main.js` kept a second, inline copy of the
 * real loop. The copy is gone: there is ONE path from an apply response to a
 * row, it hashes the names, and it reads both shapes the controller has answered
 * with — per-allocation `allocations[].vm_ids` and the flat top-level `vm_ids`.
 * A response with `allocations` but no names in them is the shape that made the
 * placeholder silently never render on the live fleet (2026-10-17).
 *
 * @param {object} job  the `POST /vms/apply` 202 body
 * @param {Iterable} existing  agents already reported (rows with `.vmId`, or ids)
 * @param {number} now
 * @param {(name: string) => Promise<string>} hash  the §4.6 derivation
 * @returns {Promise<object[]>} rows; empty when nothing was materialised
 */
export async function expectedRows(job, existing = [], now = Date.now(), hash = vmIdForName) {
  const have = new Set();
  for (const item of existing || []) {
    const id = typeof item === "string" ? item : item && item.vmId;
    if (id) have.add(id);
  }

  const rows = [];
  const pushed = new Set();
  const seenNames = [];
  const add = async (name, nodeId) => {
    if (typeof name !== "string" || !name || pushed.has(name)) return;
    pushed.add(name);
    seenNames.push(name);
    let vmId = null;
    try {
      vmId = await hash(name);
    } catch {
      // Crypto unavailable (a non-secure context): the row still names the
      // instance and the node, and says it has no id to match on yet.
      vmId = null;
    }
    if (vmId && have.has(vmId)) return; // the node already reports it
    rows.push({
      nodeId: nodeId || null,
      name,
      vmId: vmId || null,
      hashed: !!vmId,
      label: null,
      state: EXPECTED_STATE,
      expected: true,
      at: now,
      staleAt: now + EXPECTED_STALE_MS,
      dropAt: now + EXPECTED_DROP_MS,
    });
  };

  const allocations = Array.isArray(job && job.allocations) ? job.allocations : [];
  for (const alloc of allocations) {
    // `skipped` / `failed` materialised nothing and were rolled back; a
    // `host_service` reference is the node's always-on inference service, not an
    // agent. Neither may become a placeholder row.
    if (!alloc || alloc.status === "skipped" || alloc.status === "failed") continue;
    for (const name of Array.isArray(alloc.vm_ids) ? alloc.vm_ids : []) {
      await add(name, alloc.node_id);
    }
  }
  // A response that carries instance names but no usable per-allocation detail
  // (a controller/mock version skew): the flat roll-up is still instance names,
  // so the rows can still be drawn — with the node unknown, never invented.
  if (!seenNames.length && Array.isArray(job && job.vm_ids)) {
    for (const name of job.vm_ids) await add(name, null);
  }
  return rows;
}

/** The instance names an apply response carries, deduped, in response order. */
export function materialisedNames(job) {
  const out = [];
  const seen = new Set();
  const push = (name) => {
    if (typeof name === "string" && name && !seen.has(name)) {
      seen.add(name);
      out.push(name);
    }
  };
  for (const alloc of (job && Array.isArray(job.allocations) ? job.allocations : [])) {
    if (!alloc || alloc.status === "skipped" || alloc.status === "failed") continue;
    for (const name of Array.isArray(alloc.vm_ids) ? alloc.vm_ids : []) push(name);
  }
  if (!out.length && job && Array.isArray(job.vm_ids)) for (const name of job.vm_ids) push(name);
  return out;
}

/**
 * The prune every refresh runs: drop a placeholder the node now reports (its
 * `vm_id` is in the telemetry), stop claiming "provisioning" for a row that no
 * node has listed for a long time, and finally drop it. Without this the panel
 * could show "provisioning" forever for an instance that was rolled back, and a
 * row that has been reported but kept would double-count the agent.
 *
 * @param {object[]} rows   current placeholders
 * @param {Iterable} reported  the `vm_id`s telemetry lists now
 * @param {number} now
 */
export function pruneExpected(rows, reported = [], now = Date.now()) {
  const seen = new Set();
  for (const item of reported || []) {
    const id = typeof item === "string" ? item : item && item.vmId;
    if (id) seen.add(id);
  }
  const out = [];
  for (const row of rows || []) {
    if (!row) continue;
    if (row.vmId && seen.has(row.vmId)) continue;          // reported: the real row supersedes it
    if (row.dropAt && now >= row.dropAt) continue;          // never reported: stop showing it
    if (row.staleAt && now >= row.staleAt && row.state !== EXPECTED_UNCONFIRMED) {
      out.push({ ...row, state: EXPECTED_UNCONFIRMED });     // still honest, no longer "provisioning"
      continue;
    }
    out.push(row);
  }
  return out;
}

/**
 * A cheap identity for a placeholder set, so a poll only re-renders the panel
 * when the set actually changed (a re-render must not eat a half-typed prompt —
 * drafts live on the panel, but rebuilding the list still costs focus).
 */
export function expectedSignature(rows) {
  return (rows || [])
    .map((r) => `${r.nodeId || ""}:${r.name || ""}:${r.vmId || ""}:${r.state || ""}`)
    .join("|");
}
