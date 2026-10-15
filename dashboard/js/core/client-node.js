/*
 * The client's own view of ONE node (task `t_5649d117`; frozen contract on
 * `t_074b1a14`, option (a) provider-issued single-use enrollment tokens).
 *
 * Under the frozen contract the client's ONLY read credential is the per-node
 * `node_token` the controller issued to their node at registration. It reads
 * exactly one node — `GET /v1/nodes/{node_id}/telemetry/recent`, own node only;
 * a cross-node id is refused with 403 — which is why there is deliberately NO
 * client-authenticated fleet list and NO client identity store here: a client
 * credential that could list nodes is the self-service (option (b)) feature the
 * user ruled out, and rebuilding it would contradict the ruling.
 *
 * Client isolation is therefore a property of the credential, not of UI code:
 * one client's credential cannot address another client's node at all.
 *
 * This module is pure (no DOM, no network) so the state machine, the health
 * summary and the empty state are unit-testable and identical wherever they are
 * rendered (the dashboard panel today, a CLI or a future surface tomorrow).
 */

/** Health thresholds, verbatim from the frozen dashboard contract §1.3. */
export const FRESH_S = 15;
export const OFFLINE_S = 45;

/** Registration states a client can see for their node. */
export const STATE = Object.freeze({
  NO_CREDENTIAL: "no-credential",
  PENDING: "pending",
  REGISTERED: "registered",
  DEGRADED: "degraded",
  UNREACHABLE: "unreachable",
  CREDENTIAL_REJECTED: "credential-rejected",
  CONTROLLER_UNREACHABLE: "controller-unreachable",
});

/** The documented customer one-liner (frozen contract one_liner, verbatim). */
export const INSTALL_ONE_LINER =
  "curl -fsSL https://<controller>/install/termini-node.sh | sudo sh -s -- " +
  "--controller https://<controller> --token <CREDENTIAL>";

/** Where a node keeps its own identity + credential, root-only (0600). */
export const NODE_IDENTITY_PATH = "/var/lib/termini/identity/node.json";

export function oneLiner(controller = "https://<controller>") {
  let base = String(controller || "https://<controller>").replace(/\/+$/, "");
  if (!base) base = "https://<controller>";
  if (!/^https?:\/\//.test(base)) base = `https://${base}`;
  return (
    `curl -fsSL ${base}/install/termini-node.sh | sudo sh -s -- ` +
    `--controller ${base} --token <CREDENTIAL>`
  );
}

/**
 * The instructive empty state (REQUIRED WORK 5): what to do when no node of
 * this client is enrolled yet, and what the client will see once one is.
 */
export function emptyState({ controller = "" } = {}) {
  return {
    headline: "No node connected yet",
    lead:
      "This view shows one node — yours — read with your node's own credential. " +
      "Nothing appears here until a node of yours is enrolled, and no other " +
      "client's node can ever be reachable from your credential.",
    steps: [
      {
        text: "Ask your Termini provider for a one-time enrollment credential for this node. It is single-use and expires within the hour.",
      },
      {
        text: "On the machine you want to enrol, run the documented one-liner:",
        code: oneLiner(controller),
      },
      {
        text: "It prints YOUR NODE IS REGISTERED, the node id and the node's first telemetry.",
      },
      {
        text: "Stream the node's health from that machine at any time (same credential, no provider involved):",
        code: "termini-node-status --watch",
      },
      {
        text:
          "To follow the same node here, paste its own two values below — node_id and " +
          `node_token, from ${NODE_IDENTITY_PATH} on that machine (root-only). ` +
          "The provider's project token is never needed and is never accepted here.",
      },
    ],
    promise: [
      "the node listed as registered (or pending, before its first heartbeat)",
      "when it enrolled and when it was last seen",
      "live heartbeat, CPU and memory, refreshing in place — no page reload",
    ],
  };
}

/**
 * Derive a registration state from what the per-node endpoint actually
 * returned. `error` is an ApiError (or a plain `{status, message}`).
 */
export function registrationView({ nodeId = "", nodeToken = "", samples = [], error = null, now = Date.now() } = {}) {
  if (!nodeId || !nodeToken) {
    return {
      state: STATE.NO_CREDENTIAL,
      label: "no node connected",
      tone: "muted",
      detail: "Add your node's own node_id and node_token to see its registration and health.",
      ageS: null,
      sample: null,
    };
  }
  if (error) {
    const status = Number(error.status) || 0;
    if (status === 401 || status === 403) {
      return {
        state: STATE.CREDENTIAL_REJECTED,
        label: "credential rejected",
        tone: "err",
        detail:
          `${status} — the controller does not accept this node credential for this node id. ` +
          "A node credential reads only its own node, so a copy/paste mismatch (or a " +
          "credential from a different node) looks exactly like this. Re-read node_id and " +
          `node_token from ${NODE_IDENTITY_PATH} on the node and try again.`,
        ageS: null,
        sample: null,
      };
    }
    return {
      state: STATE.CONTROLLER_UNREACHABLE,
      label: "controller unreachable",
      tone: "warn",
      detail:
        "The controller could not be reached from this browser " +
        `(${error.message || "network error"}). Check the REST base in Settings and that ` +
        "this browser has outbound access to it.",
      ageS: null,
      sample: null,
    };
  }
  const newest = newestSample(samples);
  if (!newest) {
    return {
      state: STATE.PENDING,
      label: "registered · waiting for the first telemetry",
      tone: "warn",
      detail:
        "The controller accepted this node but has no telemetry sample for it yet. " +
        "The daemon posts one per telemetry interval, so this should clear within a minute.",
      ageS: null,
      sample: null,
    };
  }
  const ageS = sampleAgeS(newest, now);
  if (ageS === null || ageS <= FRESH_S) {
    return {
      state: STATE.REGISTERED,
      label: "registered · online",
      tone: "ok",
      detail: ageS === null
        ? "registered and reporting (the sample carries no timestamp)"
        : `last sample ${Math.max(0, Math.round(ageS))}s ago`,
      ageS,
      sample: newest,
    };
  }
  if (ageS <= OFFLINE_S) {
    return {
      state: STATE.DEGRADED,
      label: "registered · telemetry late",
      tone: "warn",
      detail: `last sample ${Math.round(ageS)}s ago (contract §1.3: degraded past ${FRESH_S}s)`,
      ageS,
      sample: newest,
    };
  }
  return {
    state: STATE.UNREACHABLE,
    label: "registered · not reporting",
    tone: "err",
    detail:
      `last sample ${Math.round(ageS)}s ago (offline past ${OFFLINE_S}s, contract §1.3) — ` +
      "the node is enrolled but its daemon is not reaching the controller.",
    ageS,
    sample: newest,
  };
}

/** Newest sample by `ts` (falling back to `seq`, then to array order). */
export function newestSample(samples) {
  if (!Array.isArray(samples) || !samples.length) return null;
  let best = null;
  let bestKey = -Infinity;
  samples.forEach((s, i) => {
    if (!s || typeof s !== "object") return;
    const key = sampleKey(s, i);
    if (key >= bestKey) {
      bestKey = key;
      best = s;
    }
  });
  return best;
}

function sampleKey(s, index) {
  const ts = Date.parse(s.ts ?? "");
  if (Number.isFinite(ts)) return ts;
  const seq = Number(s.seq);
  if (Number.isFinite(seq)) return seq;
  return index;
}

/** Seconds since the sample was taken, or null when it carries no timestamp. */
export function sampleAgeS(sample, now = Date.now()) {
  const ts = Date.parse((sample && sample.ts) || "");
  if (!Number.isFinite(ts)) return null;
  return Math.max(0, (now - ts) / 1000);
}

/**
 * Flat health summary of one telemetry Sample (node-telemetry.md §3), limited
 * to the fields the first-telemetry story needs: heartbeat, CPU/mem, and the
 * node's own runtime facts.
 *
 * Note on "daemon version": the frozen telemetry contract has no daemon-version
 * field — no `Sample` field, and neither does the registration response — so
 * none is invented here. The runtime facts the contract does carry (`os_name`,
 * `os_version`, `kernel_version`, image-pinned per bake) are reported instead;
 * `schema_version` is reported where the payload carries it.
 */
export function healthSummary(sample) {
  if (!sample || typeof sample !== "object") return null;
  const ramTotal = numOrNull(sample.ram_total_bytes);
  const ramUsed = numOrNull(sample.ram_used_bytes);
  const disks = Array.isArray(sample.disk) ? sample.disk : [];
  const d0 = disks.find((d) => d && typeof d === "object") || null;
  const diskTotal = d0 ? numOrNull(d0.total_bytes) : null;
  const diskFree = d0 ? numOrNull(d0.free_bytes) : null;
  const gpus = Array.isArray(sample.gpu) ? sample.gpu : [];
  const g0 = gpus.find((g) => g && typeof g === "object") || null;
  return {
    heartbeat: {
      seq: numOrNull(sample.seq),
      ts: sample.ts || null,
      uptimeS: numOrNull(sample.uptime_sec),
    },
    cpu: { pct: numOrNull(sample.cpu_util_pct), cores: numOrNull(sample.cpu_core_count) },
    mem: {
      usedBytes: ramUsed,
      totalBytes: ramTotal,
      pct: ramTotal ? (100 * (ramUsed || 0)) / ramTotal : null,
    },
    load1: numOrNull(sample.load_avg_1m),
    disk: diskTotal ? { freeBytes: diskFree, totalBytes: diskTotal, freePct: (100 * (diskFree || 0)) / diskTotal } : null,
    runtime: {
      os: sample.os_name || null,
      osVersion: sample.os_version || null,
      kernel: sample.kernel_version || null,
      schemaVersion: numOrNull(sample.schema_version),
    },
    services: (Array.isArray(sample.services) ? sample.services : [])
      .filter((s) => s && typeof s === "object")
      .map((s) => ({ name: String(s.name || ""), status: String(s.status || "") })),
    gpu: g0 ? { model: g0.model || null, utilPct: numOrNull(g0.util_pct) } : null,
    inference: sample.inference && typeof sample.inference === "object" ? sample.inference : null,
  };
}

/**
 * The cards the panel renders, in order. Kept here (not in the DOM module) so
 * the "first telemetry" contract of the card is asserted by unit tests rather
 * than by looking at a screenshot.
 */
export function healthCards(summary) {
  if (!summary) return [];
  const h = summary.heartbeat || {};
  const cards = [
    { key: "heartbeat", label: "Heartbeat", value: h.seq === null ? "—" : `seq ${h.seq}`, hint: h.ts || "no timestamp" },
    {
      key: "cpu",
      label: "CPU",
      value: summary.cpu.pct === null ? "—" : `${round1(summary.cpu.pct)}%`,
      hint: summary.cpu.cores === null ? "" : `${summary.cpu.cores} cores`,
    },
    {
      key: "mem",
      label: "Memory",
      value: summary.mem.pct === null ? "—" : `${round1(summary.mem.pct)}%`,
      hint: summary.mem.totalBytes === null ? "" : `${summary.mem.usedBytes || 0} / ${summary.mem.totalBytes} bytes`,
    },
    { key: "uptime", label: "Uptime", value: h.uptimeS === null ? "—" : fmtDuration(h.uptimeS), hint: "" },
    { key: "load", label: "Load (1m)", value: summary.load1 === null ? "—" : String(round2(summary.load1)), hint: "" },
  ];
  if (summary.runtime.os || summary.runtime.kernel) {
    cards.push({
      key: "runtime",
      label: "Node runtime",
      value: [summary.runtime.os, summary.runtime.osVersion].filter(Boolean).join(" ") || "—",
      hint: summary.runtime.kernel ? `kernel ${summary.runtime.kernel}` : "",
    });
  }
  if (summary.services.length) {
    cards.push({
      key: "services",
      label: "Services",
      value: `${summary.services.filter((s) => s.status === "running").length}/${summary.services.length} running`,
      hint: summary.services.map((s) => `${s.name}:${s.status}`).join(" · "),
    });
  }
  if (summary.gpu) {
    cards.push({
      key: "gpu",
      label: "GPU",
      value: summary.gpu.utilPct === null ? "—" : `${round1(summary.gpu.utilPct)}%`,
      hint: summary.gpu.model || "",
    });
  }
  return cards;
}

function numOrNull(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function round1(n) {
  return Math.round(n * 10) / 10;
}

function round2(n) {
  return Math.round(n * 100) / 100;
}

function fmtDuration(sec) {
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
