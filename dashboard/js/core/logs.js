/* Severity helpers + AI-operator log envelope handling (contract §2.1). Pure. */

export const SEVERITIES = ["debug", "info", "warn", "error"];

const RANK = { debug: 0, info: 1, warn: 2, error: 3 };

export function severityRank(sev) {
  return RANK[sev] ?? 1;
}

/** True when `sev` is at or above the lowest enabled severity. */
export function severityVisible(sev, enabledSet) {
  if (!enabledSet || enabledSet.size === 0) return true;
  return enabledSet.has(sev);
}

export function normalizeSeverity(sev) {
  return SEVERITIES.includes(sev) ? sev : "info";
}

/**
 * Normalize one streaming line from either transport:
 *   * dashboard.md §2.1 full envelope (log_id/ts/node_id/severity/actor/message/remediation)
 *   * legacy thin shape (ts/level/text) — accepted so the panel still works
 *     against a partially-migrated controller.
 */
export function normalizeLine(raw) {
  if (!raw || typeof raw !== "object") return null;
  const severity = normalizeSeverity(raw.severity || raw.level);
  const message = typeof raw.message === "string" ? raw.message : typeof raw.text === "string" ? raw.text : "";
  if (!message) return null;
  const rem = raw.remediation && typeof raw.remediation === "object" ? raw.remediation : null;
  return {
    log_id: typeof raw.log_id === "string" ? raw.log_id : null,
    ts: raw.ts || null,
    node_id: raw.node_id ?? null,
    severity,
    actor: raw.actor === "human" ? "human" : raw.actor === "ai_operator" ? "ai_operator" : "ai_operator",
    message,
    remediation: rem
      ? {
          command_id: rem.command_id ?? null,
          type: rem.type ?? null,
          status: rem.status ?? null,
        }
      : null,
    truncated: raw.truncated === true,
  };
}

/** Newest-first append with a hard client cap (ring-buffer semantics §2.3). */
export function appendLines(buffer, lines, max) {
  const next = buffer.concat(lines);
  if (next.length <= max) return next;
  return next.slice(next.length - max);
}
