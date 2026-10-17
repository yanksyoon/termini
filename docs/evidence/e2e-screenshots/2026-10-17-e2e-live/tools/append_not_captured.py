#!/usr/bin/env python3
"""Append the NOT CAPTURED section to INDEX-client.md (idempotent)."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(os.path.dirname(HERE), "INDEX-client.md")
MARK = "## NOT CAPTURED"

BLOCK = """
## NOT CAPTURED — the holes, named

| card step | what is missing | why |
| --- | --- | --- |
| 12 — deliberate failure view | **not captured** | The run reached its wall-clock budget. The plan for it is written and re-runnable (`capture-e2e-client.py 14`: point node-c's `TERMINI_GEMINI_MODEL` at a non-existent model, restart `termini-inference-proxy`, inject one fault, wait for `decision_source=fallback`, then restore and verify `/health`). It was NOT run, so no frame is offered for it and the demo's inference backend was left untouched. |
| 09 — audit row "behind the dialog" | the dialog itself did not open | The script selected an executed decision and clicked the audit link, but this build opened no dialog (`dialogs: []` in the sidecar). The frame shows the decision detail with the `audit 01M55T6…` link; the raw audit rows (action_kind, target, result, executed_at, LXD method/path/endpoint) are in `client/10-audit-trail-row.txt`. |
| 05/06 — the Apply path as a judge sees it | partially | The dashboard's own Apply is bounded by a 20 s client timeout, so the UI reported `NETWORK: controller unreachable: signal timed out` while the controller finished the apply server-side; `client/06-instances-running.txt` carries that error text plus the post-apply Agents panel (6 → 8 running instances on node-a). The plan's own vm ids are in `client/05-plan-accepted.txt` (plan `01M55S73GAJK1S1T03YZTKNK68`, allocation 0 accepted on node-a) and the materialisation is proven on the node side. |
| 10 — a reachable web terminal | the dashboard control does not exist | `#link-terminal` is in the markup but nothing unhides it; the frame shows `/terminal/` itself (200) and the sidecar carries the controller's node-SSH endpoint. |
| 02 — "node-c inference" | re-worded | Both real nodes report `role=compute` (the frozen role rule needs a >=12 GiB GPU and this fleet is CPU-only after the proxy pivot). node-c is the inference **service host**, which is what frame 02's sidecar says. |
"""


def main():
    with open(INDEX) as fh:
        text = fh.read()
    if MARK in text:
        print("already present")
        return
    with open(INDEX, "w") as fh:
        fh.write(text.rstrip() + "\n" + BLOCK)
    print("appended NOT CAPTURED")


if __name__ == "__main__":
    main()
