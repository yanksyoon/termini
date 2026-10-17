# Environment the backend capture ran against

| | |
|---|---|
| run-id | `2026-10-17-e2e-live` (shared with the client half, `INDEX-client.md`) |
| UTC window | `2026-10-17T19:56Z` → (see `tools/capture-log.tsv` for each step's exact start) |
| capture host | the shared build host, `/home/ubuntu/.worktrees/t_2191eca4` (branch `wt/t_2191eca4-e2e-backend-captures`, from `origin/main`) |
| deployment under test | `a89f8521254c1f2b1af25f24d91d4bbc12194873` — `/opt/termini/DEPLOYED_COMMIT` on `termini-controller`, deployed 2026-10-17T19:35Z |
| proof the tree is that commit | `sha256(/opt/termini/controller/app/server.py)` = `58db4820…b2d1`, identical to `git show origin/main:controller/app/server.py` at `a89f852` |
| GCP project / zone | `termini-510609` / `asia-south1-a` |
| fleet | `termini-controller` (e2-standard-2, 10.10.0.2, public 34.93.190.64), `termini-node-a` (e2-standard-2, 10.10.0.11), `termini-node-c` (e2-standard-8, 10.10.0.13); `termini-node-b` TERMINATED (12/12 vCPU quota) |
| node boot image | `termini-node-os-5` (family `termini-node-os`) on both nodes — captured in step 01 |
| controller API (public) | `https://34-93-190-64.sslip.io/v1` (sslip.io → the controller's static address; the dashboard's `apiBase`) |
| auth | project token — the demo project token that is already published inside the dashboard URL (`README`, `SUBMISSION.md`); no other credential is used or printed by these steps |
| inference front door | node-c `:8080` → `termini-inference-proxy` (python), backend `gemini-proxy`, alias `gemma`, `answered_by=forwarded-managed-model`, destination `aiplatform.googleapis.com`; local llama.cpp worker on `127.0.0.1:8081` and `:8090` |

## How the captures were taken

* Driver: `tools/capture-backend-live.sh` — one snippet per step under `tools/steps/`, each
  executed through `script -qec "timeout 540 bash <snippet>" <raw>`.
* The only transformation applied to a transcript afterwards is stripping the `CR` the PTY
  adds to each line (`tr -d '\r'`); no byte of command output is edited.
* PNGs are rendered from the finished transcript by `tools/render-terminal-png.sh`
  (ffmpeg `drawtext` on a dark canvas, lines wrapped for display only). The `.txt` is the
  train of truth; the PNG is the readable form.
* Re-run: `bash tools/capture-backend-live.sh` (all steps) or `... 03 04` (selected steps).
  Step 08 writes the decision id into `/tmp/termini-e2e-backend/state.env`, which step 10 reads.

## What this run did NOT do, and why

* **No GCP compute was created or started.** The standing fleet was already up (raised by the
  approved window recorded on `t_ece9d14f`); this card's cost guard forbids
  `gcloud compute instances create|start`. Step 01 therefore captures the fleet listing and
  boot-disk provenance rather than a fresh raise.
* **No instance was terminated and the controller URL was not taken down** (step 11). The
  fleet is shared with the concurrently-running client capture (`t_270d09b9`), and the owner
  decided on 2026-10-17 13:12Z to keep the demo running (`t_f3e25715`). Step 11 captures the
  part that does not need a teardown; `tools/verify-pause-and-billing.sh` finishes it in an
  approved window.
* **The seeder was left running** (step 04 asked for it stopped). Stopping it would blank the
  client capture's charts. Real-node telemetry is instead separated by node id and
  cross-checked against each machine's own `uname`/`hostname` in step 04.
* **One OOM fault was injected on the real node-a** (`2026-10-17T20:37:28Z`, 180 s, cleared
  after the decision) to produce a genuine `decision_source=model` decision with a real
  remediation. The injection id is in step 08's transcript; the fleet's demo workload was not
  touched by this card.

## Pre-commit / secrets

`pre-commit` ran on the committed paths and passed: no keys, no `.env`, no runtime state. The
only credential-shaped string in this pack is the published demo project token inside the
echoed `curl` commands (it is already in the dashboard URL); the OAuth access token the proxy
uses is never printed — step 07 shows the metadata-server call's HTTP status and `expires_in`
only, and records that no key file exists on node-c.
