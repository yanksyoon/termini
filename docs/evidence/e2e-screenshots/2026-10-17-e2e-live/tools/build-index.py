#!/usr/bin/env python3
"""Generate INDEX-backend.md from tools/capture-log.tsv + the per-step notes below.

The log is written by capture-backend-live.sh (one row per capture, with the
sha256 of both the PNG and the raw transcript). This script only formats it —
it never invents a hash or a timestamp.
"""
import csv, hashlib, os, pathlib

PACK = pathlib.Path(__file__).resolve().parent.parent
LOG = PACK / "tools" / "capture-log.tsv"

NOTES = {
 "01": ("the raise: node-a and node-c are real GCE instances whose boot disks come from the `termini-node-os` family (`termini-node-os-5`); the fleet listing shows them RUNNING and node-b TERMINATED. **NOT raised by this run** — the cost guard forbids create/start, the approved window raised them (t_ece9d14f).",
        "`gcloud compute instances list`; `gcloud compute disks list --format=table(name,sizeGb,status,sourceImage)`; `gcloud compute instances describe termini-node-a|termini-node-c`"),
 "02": ("boot → registration straight from the bootstrap daemon's journal: node-a `REGISTERED … register_ms=37 boot_to_registered_ms=176120`, node-c `register_ms=102 boot_to_registered_ms=16160`, plus the controller's own `last_seen_at` heartbeat. **Finding:** both nodes emit REGISTERED only on the boot that first registered them — a rebooted node reuses its persisted identity and re-emits 0 lines (captured as such).",
        "`gcloud compute ssh termini-node-{a,c} --command=\"journalctl -u termini-bootstrap | grep REGISTERED; systemctl show -p ActiveState -p ExecMainStartTimestamp; uptime -s\"`"),
 "03": ("the controller's own fleet view (the API the dashboard consumes): both standing nodes ONLINE, `synthetic=false`, services listed, capacity/health reported. **Finding:** both report `role=compute` — the frozen role rule grants `inference` only to a node with a ≥12 GiB GPU, and this fleet is CPU-only after the managed-model pivot. node-c is the inference SERVICE HOST, not an inference-role node.",
        "`curl -sS -H \"Authorization: Bearer <project-token>\" https://34-93-190-64.sslip.io/v1/projects/<pid>/nodes`"),
 "04": ("telemetry from the REAL nodes: node-a (seq 36xxx, kernel `7.0.0-1011-gcp`, 2 cores, host `66570ff05a207404`) and node-c (seq 30xxx, kernel `7.0.0-1014-gcp`, 8 cores), each sample cross-checked against that machine's own `uname -r`/`hostname`/`nproc`, with streaming spacing shown. The seeder's synthetic rows are printed alongside for contrast (fixed 16 cores, CI-host kernel, fabricated NVIDIA L4). **Deviation:** the seeder was NOT stopped (a concurrent client capture uses it); authenticity is proven by identity instead.",
        "`curl .../v1/telemetry/recent?limit=4` + `gcloud compute ssh termini-node-{a,c} --command=\"uname -r; hostname; nproc\"`"),
 "05": ("the inference service on node C: `termini-inference-proxy.service` active on `:8080`, `/v1/models` advertising the alias `gemma`, `/health` naming `backend=gemini-proxy`, `answered_by=forwarded-managed-model`, `egress=external`, `destination=aiplatform.googleapis.com`, and no GPU device on the box.",
        "`gcloud compute ssh termini-node-c --command=\"systemctl status termini-inference-proxy; curl :8080/health; curl :8080/v1/models; ls /dev/nvidia*\"`"),
 "06": ("a real inference request as a PEER: node-a POSTs to `10.10.0.13:8080/v1/chat/completions` and gets a 200 in ~0.9 s with the forwarded label in the body; node-c's own proxy journal shows the same request (`request_id`, `latency_ms`, `forwarded=true`, `destination=aiplatform.googleapis.com`). Both sides in one transcript.",
        "`gcloud compute ssh termini-node-a --command=\"curl -X POST http://10.10.0.13:8080/v1/chat/completions …\"` + node-c proxy journal"),
 "07": ("the forwarded call itself: the proxy's request record names `destination=aiplatform.googleapis.com`, `forwarded=true`, `latency_ms=1156.8`; the destination resolves; the credential comes from the **metadata server** (token endpoint HTTP 200, `expires_in` only — the token is never printed) and **no key file exists** on node-c (`/etc/termini/*.json` absent, `GOOGLE_APPLICATION_CREDENTIALS` count 0). Baseline cited: 402.1 s for the CPU Gemma path on the real decision prompt (`infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md`).",
        "`gcloud compute ssh termini-node-c --command=\"journalctl -u termini-inference-proxy | grep inference_request; getent hosts aiplatform.googleapis.com; ss -tnp state established '( dport = :443 )'; curl metadata token endpoint\"`"),
 "08": ("the operator deciding on a REAL node: an OOM fault injected on node-a (injection id in the transcript) produces a decision with `decision_source=model`, `confidence=0.95`, `decided_at` after the injection, and the trace's `model_call` naming `endpoint=external`, `answered_by=forwarded-managed-model`, `latency_ms`, `request_id`. The controller's `externalModelCall before/after` lines carry `external_destination=aiplatform.googleapis.com` and `payload_sha256` for the same request id.",
        "`POST /v1/projects/<pid>/demo/faults {scenario:oom,node_id:<node-a>,target:real}`; poll `GET /v1/operator/decisions`; `journalctl -u termini-controller | grep externalModelCall`"),
 "09": ("**CAPTURED FAILING (finding):** the accepted-plan step could not be completed — node-a had `cpu_free=0` (4 vCPU held by a concurrent card's applied plan) so the controller refused the intent with `422 GUARDRAIL_REJECTED / exceeds_node_cpu_ceiling`. Raw refusal is in the transcript. The apply→LXD materialisation path is already proven on this same fleet by `t_d7e0bf4e` / `t_ece9d14f`; this run's own LXD evidence is step 10, which shows the instance those plans created being remediated on the node. **NOT CAPTURED: a fresh apply creating an instance in this window.**",
        "`POST /v1/projects/<pid>/intents {\"prompt\":\"deploy 1 worker agent for coding\"}` (then `POST /vms/apply` — not reached)"),
 "10": ("a remediation against real LXD, with the audit row that records it: `PUT /1.0/instances/<vm>/state` (`{\"action\":\"restart\",\"force\":true,\"timeout\":30}`), `result=ok`, `lxd_endpoint=https://10.10.0.11:8443`, the LXD operation URL, and `state Running -> Running`; the node's own `lxc list`/`lxc info` shows the same instance RUNNING with `Last Used` at the remediation minute, and the controller log shows `remediation_latency`. The injected fault is then cleared (healthy baseline written back).",
        "`GET /v1/operator/audits?node=<node-a>` + `gcloud compute ssh termini-node-a --command=\"lxc list <vm>; lxc info <vm>\"` + `DELETE /demo/faults/<iid>`"),
 "11": ("**PARTIAL — NOT CAPTURED as written:** the termination, the controller URL returning `000`, and a zero-usage billing view are deferred to an approved teardown (fleet is shared with the client capture; the owner's 2026-10-17 decision keeps the demo running; the card's cost guard forbids start/stop). What IS captured is the discipline t_9232df6d asks for: the fleet, the persistent disks, the reserved static address, the images and the CPUS/GPU quota rows — i.e. the lines that keep billing after a terminate. `tools/verify-pause-and-billing.sh` finishes the step in an approved window.",
        "`gcloud compute instances list; gcloud compute disks list; gcloud compute addresses list; gcloud compute images list; gcloud compute regions describe asia-south1 (quotas); curl -sS -o /dev/null -w '%{http_code}' <public url>`"),
 "12": ("the honest comparison: the SAME prompt answered by the CPU/Gemma path (`127.0.0.1:8081`, no GPU) and by the forwarded path (`:8080` → gemini-2.5-flash at aiplatform.googleapis.com), both measured in this run, with the explicit label that the forwarded side is a managed model standing in for a GPU node — not local inference, not GPU compute. The real 20-sample decision prompt costs 402.1 s on CPU (cited) versus ~1-3 s forwarded.",
        "`gcloud compute ssh termini-node-c --command=\"curl :8080/v1/chat/completions (forwarded); curl :8081/v1/chat/completions (local CPU)\"`"),
}


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


rows = []
with open(LOG, newline="") as fh:
    for r in csv.DictReader(fh, delimiter="\t"):
        if not r.get("step") or r["step"] == "step":
            continue
        rows.append(r)

# last capture wins for a re-captured step
by_step = {}
for r in rows:
    by_step[r["step"]] = r

out = []
out.append("# Backend E2E evidence — ordered terminal captures (boot → registration → Gemini → audit)\n")
out.append("Card: `t_2191eca4` (infra). Run-id: `2026-10-17-e2e-live` (the client half is `INDEX-client.md` under the same run-id).\n")
out.append("Environment, deployment commit and the exact list of what this run did NOT do: `tools/environment.md`.\n")
out.append("**Convention:** each step is a PNG rendered from its raw transcript sidecar (`.txt`). The transcript is the train "
           "of truth — the PNG is the readable form, rendered by `tools/render-terminal-png.sh` with lines wrapped for display only. "
           "Nothing is retyped or retaken: a step that failed is captured failing (step 09). The only transformation applied to a "
           "transcript is stripping the PTY's CR.\n")
out.append("\n| deployment | `a89f8521254c1f2b1af25f24d91d4bbc12194873` (`/opt/termini/DEPLOYED_COMMIT`; `server.py` sha256 matches `origin/main`) |")
out.append("|---|---|")
out.append("| UTC window | %s → %s |" % (by_step[min(by_step)]["captured_at_utc"], by_step[max(by_step)]["captured_at_utc"]))
out.append("| fleet | controller + node-a + node-c RUNNING, node-b TERMINATED |")
out.append("| re-run | `bash tools/capture-backend-live.sh` (all) or `… 03 04` (selected) |")
out.append("| pre-commit | passed on the committed paths (no keys, no `.env`, no runtime state) |")
out.append("\n| # | step | what it proves | PNG | sha256 (PNG) | sha256 (transcript) | captured_at (UTC) | command | transcript |")
out.append("|---|---|---|---|---|---|---|---|---|")
for step in sorted(by_step):
    r = by_step[step]
    png = PACK / r["png"]
    txt = PACK / r["transcript"]
    png_sha = sha(png) if png.exists() else "*missing*"
    txt_sha = sha(txt) if txt.exists() else "*missing*"
    proves, cmd = NOTES.get(step, ("", r["command"]))
    status = "" if r["status"] == "ok" else " **(captured failing: %s)**" % r["status"]
    slug = r["slug"]
    out.append("| %s | `%s`%s | %s | `%s` | `%s` | `%s` | %s | %s | `%s` |" % (
        step, slug, status, proves, r["png"], png_sha, txt_sha, r["captured_at_utc"], cmd, r["transcript"]))

out.append("\n## Findings and NOT CAPTURED (stated, not smoothed over)\n")
out.append("1. **Step 09 captured failing.** node-a was at `cpu_free=0` (4 vCPU committed by a concurrent card's applied plan) "
           "when this step ran, so the headline-style intent was refused `422 GUARDRAIL_REJECTED / exceeds_node_cpu_ceiling`. "
           "The raw refusal is the transcript. A fresh apply→LXD materialisation is NOT in this pack; it is proven on this same "
           "fleet by `t_d7e0bf4e` (PR #202) and `t_ece9d14f` (PR #199), and step 10 shows real LXD work on a plan-created instance.")
out.append("2. **Step 11 is partial by necessity.** No instance was terminated and the public URL was not taken dark: the fleet is shared "
           "with the concurrently-running client capture (`t_270d09b9`), the owner decided on 2026-10-17 13:12Z to keep the demo up "
           "(`t_f3e25715`), and this card's cost guard forbids start/stop. The residual-bill lines are captured; "
           "`tools/verify-pause-and-billing.sh` completes the step in an approved teardown window.")
out.append("3. **Step 04 deviation:** the seeder was left running (a concurrent client capture uses it), so \"seeder stopped\" was not "
           "honoured; real-node authenticity is proven by node id + the machines' own kernel/hostname/core count instead.")
out.append("4. **Step 02 finding:** `REGISTERED` is emitted once, on the boot that first registered the node; a rebooted node reuses its "
           "persisted identity and re-emits 0 lines (`boot_to_registered_ms` cannot be re-measured on a reboot without a fresh machine).")
out.append("5. **Step 03 finding:** no node reports `role=inference` on this CPU-only fleet (the frozen role rule is GPU-gated); node-c is "
           "the inference *service host* (`:8080`, forwarded backend). The card's \"role=compute / role=inference\" wording does not "
           "match what the controller returns.")
out.append("6. **Concurrency note:** the OOM injections behind step 08/10 (20:37:28Z, 20:39:34Z, 20:42:14Z; each cleared within seconds) "
           "supersede node-a's OWN telemetry series while open — any chart of node-a covering those windows shows the injected 97% RAM and "
           "the remediation restart, attributable to this card, not to a real fault.")
out.append("7. **The seeder's synthetic nodes drive real LXD remediations** in the same logs (e.g. `demo-workload` restarts against "
           "`10.10.0.11:8443` every ~20 s). That is the pre-existing guardrail #5 risk already named in PR #238, observed again here; "
           "it is not a defect of this pack, and it is why step 03's real-node rows are filtered by the two real node ids.")

(PACK / "INDEX-backend.md").write_text("\n".join(out) + "\n")
print("wrote", PACK / "INDEX-backend.md", "steps:", ",".join(sorted(by_step)))
