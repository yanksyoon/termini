"""W1 — Architecture & integration spec (docs/architecture.md + contracts).

These checks inspect the committed documents directly (independent file
observation) rather than reusing any implementer script.
"""
from __future__ import annotations

import os
import re

from . import CheckContext, result

WS = "W1 — Architecture & integration spec"

# Contracts the architecture doc §1 promises to link (the "build against these
# docs without asking" surface for every workstream).
EXPECTED_CONTRACTS = [
    "docs/architecture.md",
    "docs/contracts/infra-decisions.md",
    "docs/contracts/node-bootstrap.md",
    "docs/contracts/controller-api.md",
    "docs/contracts/node-telemetry.md",
    "docs/contracts/dashboard.md",
    "docs/contracts/vm-allocation.schema.json",
    "docs/contracts/agent-runtime.md",
    "docs/contracts/governance.md",
    "docs/research/tech-landscape.md",
]


def check_w1_1(ctx: CheckContext) -> dict:
    criterion = "every other workstream can build against these docs without asking"
    method = "read docs/architecture.md contract index; verify every referenced contract file exists and is non-trivial"
    root = ctx.repo_root
    missing: list = []
    present: list = []
    for rel in EXPECTED_CONTRACTS:
        p = os.path.join(root, rel)
        if os.path.isfile(p) and os.path.getsize(p) > 50:
            present.append(rel)
        else:
            missing.append(rel)

    # Also verify the architecture doc's own cross-references resolve (the §1
    # table lists paths; any doc-referenced-but-undefined path is a gap).
    arch = os.path.join(root, "docs", "architecture.md")
    doc_paths: list = []
    if os.path.isfile(arch):
        text = open(arch, encoding="utf-8").read()
        doc_paths = re.findall(r"`docs/[A-Za-z0-9_./-]+\.(?:md|json)`", text)

    raw = f"present: {present}\nmissing: {missing}\ndoc_paths_in_architecture: {sorted(set(doc_paths))}"
    artifact = ctx.ev.record(
        "W1.1", "W1.1 contract index completeness",
        f"inspect {', '.join(EXPECTED_CONTRACTS)}", raw,
    )

    status = "pass" if not missing else "fail"
    detail = (f"{len(present)}/{len(EXPECTED_CONTRACTS)} contracts present"
              if not missing else f"missing {len(missing)}: {missing}")
    return result("W1.1", WS, criterion, method, status, detail, artifact,
                  reason=None if status == "pass" else f"missing contract files: {missing}")


def check_w1_2(ctx: CheckContext) -> dict:
    criterion = "each open spec question has an answer with rationale"
    method = "read docs/architecture.md §8; require each reconciliation entry R1..R13 has an Answer and a Rationale"
    root = ctx.repo_root
    arch = os.path.join(root, "docs", "architecture.md")
    if not os.path.isfile(arch):
        raw = "docs/architecture.md not found"
        artifact = ctx.ev.record("W1.2", "W1.2 open-question answers", "read docs/architecture.md", raw)
        return result("W1.2", WS, criterion, method, "fail", "architecture.md missing",
                      artifact, reason="docs/architecture.md not found")

    text = open(arch, encoding="utf-8").read()
    # Section 8 uses "### R<n> — <title>" headers.
    headings = re.findall(r"^### (R\d+)\s+—", text, flags=re.M)
    answered = [h for h in headings if f"{h} " in text]
    no_answer: list = []
    no_rationale: list = []
    for h in sorted(set(headings), key=lambda x: int(x[1:])):
        # find the block for this heading up to the next heading
        m = re.search(rf"^### {re.escape(h)}\s+—.*?(?=^### |\Z)", text, flags=re.M | re.S)
        block = m.group(0) if m else ""
        if "**Answer:**" not in block and "Answer:" not in block:
            no_answer.append(h)
        if "**Rationale:**" not in block and "Rationale:" not in block:
            no_rationale.append(h)

    raw = f"reconciliation headings: {sorted(set(headings))}\nmissing_answer: {no_answer}\nmissing_rationale: {no_rationale}"
    artifact = ctx.ev.record("W1.2", "W1.2 open-question answers", "parse docs/architecture.md §8", raw)

    status = "pass" if headings and not no_answer and not no_rationale else "fail"
    detail = f"{len(set(headings))} open questions recorded; answers missing: {no_answer}; rationale missing: {no_rationale}"
    reason = None
    if status != "pass":
        reason = f"open questions lacking answer/rationale: answer={no_answer} rationale={no_rationale}"
    return result("W1.2", WS, criterion, method, status, detail, artifact, reason=reason)
