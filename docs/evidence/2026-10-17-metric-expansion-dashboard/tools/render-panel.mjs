#!/usr/bin/env node
/**
 * Render the §4.12–§4.14 panel from a raw telemetry sample, with NO browser.
 *
 * This runs the SAME pure view model the panel uses (`buildSampleView` in
 * `dashboard/js/core/series.js`, fed by the bounded selectors in
 * `health.js`) — it is not a re-implementation. What it does NOT prove is the DOM:
 * the browser capture (`capture_panel.py`) reads the rendered text out of a real
 * Chromium. Both are committed, and the INDEX says which is which.
 *
 * Usage:
 *   node tools/render-panel.mjs <sample.json> [--out-dir DIR --prefix NAME]
 * Writes <prefix>-panel.offline-view.txt and <prefix>-panel.view-model.json when
 * --out-dir is given, and prints the text either way.
 */
import fs from "node:fs";
import path from "node:path";
import { buildSampleView, SAMPLE_GROUPS } from "../../../../dashboard/js/core/series.js";

const [, , payloadPath, ...rest] = process.argv;
if (!payloadPath) {
  console.error("usage: render-panel.mjs <sample.json> [--out-dir DIR --prefix NAME]");
  process.exit(2);
}
const arg = (name, dflt = "") => {
  const i = rest.indexOf(name);
  return i >= 0 && rest[i + 1] ? rest[i + 1] : dflt;
};
const outDir = arg("--out-dir");
const prefix = arg("--prefix", path.basename(payloadPath, ".json"));

const sample = JSON.parse(fs.readFileSync(payloadPath, "utf8"));
const view = buildSampleView(sample);

const lines = [];
lines.push(`# panel view model — ${path.basename(payloadPath)}`);
lines.push(`# sample ts=${sample.ts ?? "?"} seq=${sample.seq ?? "?"}  (pure view model; the DOM capture is beside this file)`);
for (const g of view) {
  lines.push("");
  lines.push(`${g.title}   [rank ${g.rank ?? "-"} · rule ${g.rule ?? "-"}]   reported ${g.reportedCount}/${g.rows.length}`);
  if (g.reportedCount === 0) {
    lines.push("  not reported — no §4.12–§4.14 fields on this node's latest sample (absent, not zero)");
  } else {
    for (const r of g.rows) {
      const flags = [r.cumulative ? "cumulative" : "", r.derived ? `derived: ${r.derivedFrom}` : ""].filter(Boolean).join(" · ");
      const where = r.entry ? `${r.entry} · ` : "";
      lines.push(`  ${where}${r.label.padEnd(20)} ${String(r.reported ? `${r.display} ${r.unitLabel}` : "not reported").padStart(14)}  ${flags}`);
    }
  }
  const bits = [g.basisLabel];
  if (g.rule) bits.push(`leading indicator for ${g.rule}`);
  if (g.entryCount !== null) bits.push(`${g.entryCount} entries${g.truncated ? ` (${g.truncated} not shown, cap ${g.maxEntries})` : ""}`);
  if (g.missingCount > 0 && g.reportedCount > 0) bits.push(`${g.missingCount} of ${g.rows.length} values not reported`);
  lines.push(`  — ${bits.join("  |  ")}`);
}
const text = lines.join("\n") + "\n";

if (outDir) {
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, `${prefix}-panel.offline-view.txt`), text);
  fs.writeFileSync(
    path.join(outDir, `${prefix}-panel.view-model.json`),
    JSON.stringify({ sample: path.basename(payloadPath), groups: SAMPLE_GROUPS.map((g) => g.id), view }, null, 2) + "\n",
  );
}
process.stdout.write(text);
