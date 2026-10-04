/* Shared error/detail rendering for controller responses (contract §3). */

export function formatError(e) {
  if (!e) return "unknown error";
  const code = e.code || "ERROR";
  const status = e.status ? `HTTP ${e.status}` : "";
  const parts = [`${code}${status ? ` · ${status}` : ""}: ${e.message || ""}`.trim()];
  if (Array.isArray(e.details) && e.details.length) {
    for (const d of e.details.slice(0, 6)) {
      if (d && typeof d === "object") {
        const parts2 = Object.entries(d)
          .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
          .join(" ");
        parts.push(`  – ${parts2}`);
      } else if (d) {
        parts.push(`  – ${d}`);
      }
    }
  }
  if (e.request_id) parts.push(`  request_id=${e.request_id}`);
  return parts.join("\n");
}

export function summarizeAllocations(plan) {
  if (!plan || !Array.isArray(plan.allocations)) return [];
  return plan.allocations.map((a, i) => {
    const req = a.requested || {};
    const resolved = a.resolved || {};
    const clamps = (a.guardrail && a.guardrail.clamped_fields) || [];
    const rejects = (a.guardrail && a.guardrail.reject_reasons) || [];
    return {
      index: a.index ?? i,
      status: a.status || "unknown",
      type: req.type || resolved.type || "?",
      count: req.count ?? resolved.count ?? 1,
      node_id: a.node_id || null,
      clamped: clamps,
      rejected: rejects,
    };
  });
}
