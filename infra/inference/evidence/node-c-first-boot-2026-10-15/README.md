# Node C's first boot installs the inference front door (t_359ed606)

**Claim under test.** A node created FROM THE IMAGE FAMILY comes up serving the
inference front door on `0.0.0.0:8080` with the llama.cpp worker moved to
`127.0.0.1:8081`, with **no manual installer step** — and the node's first-boot
`TERMINI_INFERENCE_BACKEND` is the shipped `gemini-proxy`, with `local` one
command away.

**Result: PASS.** Raw output: [`first-boot-live.txt`](first-boot-live.txt)
(621 lines, the whole run, nothing prettified).

## How the node was raised (nothing was run on it by hand)

```bash
# from the branch tree (the render comes from THIS commit)
TEAM_SSH_PUBKEY_FILE=~/.ssh/google_compute_engine.pub TOKEN_C=t359ed606-raise-c2 \
INFERENCE_MACHINE_TYPE=e2-standard-8 ONLY=c \
  bash infra/gcp/tools/raise-standing-fleet.sh --raise --only c --with-external-ip
```

`termini-node-c` was **deleted and created** from `--image-family termini-node-os`
(family resolves to `termini-node-os-5`, READY) with that render as its
cloud-init user-data; before the create,
`infra/gcp/cloud-init/node_c_front_door_check.py` was run on the rendered
user-data and **PASSed** (`front door + payload`, 106,923 bytes) — both the
forwarded and the Terraform render carry the front door
([`raise-and-render.txt`](raise-and-render.txt)).

Cloud-init did everything else. The acceptance below began **after** the node's
own boot finished, and no command below installs, copies or restarts anything on
node-c except the one documented backend switch.

## 1. The first boot ran the installer itself

From `/var/log/termini-llama-serve.log` (the first-boot log, written by
cloud-init's runcmd), instance created 12:05:15Z:

```
624:[2026-10-15T12:12:11Z] installing the inference front door (backend=gemini-proxy) via install-inference-proxy.sh
630:[install-inference-proxy] active backend:
632:[install-inference-proxy] install done — 'termini-inference-switch status' reports the active backend
633:[2026-10-15T12:12:13Z] inference front door installed: :8080 is termini-inference-proxy (backend gemini-proxy)
```

and the node's own artifacts carry that timestamp — the boot disk was created
minutes earlier, and nothing external touched it:

```
/etc/termini/inference-proxy.env   2026-10-15 12:12:11
/etc/termini/llama-worker.env      2026-10-15 12:12:11
/opt/termini/inference/proxy.py    2026-10-15 12:12:11
/usr/local/sbin/termini-inference-switch 2026-10-15 12:12:11
ActiveEnterTimestamp termini-llama-server.service       12:12:12
                        termini-inference-proxy.service 12:12:36
                        termini-llama-intent.service    12:12:11
```

## 2. Units, listeners and the state file (no manual step)

```
termini-inference-proxy.service  enabled active
termini-llama-server.service     enabled active
termini-llama-intent.service     enabled active
LISTEN 127.0.0.1:8081  llama-server   <-- the WORKER, loopback only
LISTEN 0.0.0.0:8080    python3        <-- the FRONT DOOR
LISTEN 0.0.0.0:8090    llama-server   <-- intent extraction (unchanged)
/run/termini/inference-backend.json -> "backend": "gemini-proxy"
/usr/lib/tmpfiles.d/termini-inference.conf -> d /run/termini 0755 termini-infer termini-infer -
```

The front door answered `HTTP 200` on the **13th** 30 s poll (≈6.5 min after the
create), the worker loaded immediately after — i.e. `:8080` was served by the
node's own first boot.

## 3. The installed bytes ARE the committed bytes

```
acf80f7c75dec188984258c16c85dbe93d423cf78354becc766588b8e8bdc671  /opt/termini/inference/proxy.py
85b1c8226f0258b9771291f1f85b77d46bbfea07a704bb79f60d4aca33915629  /usr/local/sbin/termini-inference-switch
--- repo
acf80f7c75dec188984258c16c85dbe93d423cf78354becc766588b8e8bdc671  infra/inference/proxy.py
85b1c8226f0258b9771291f1f85b77d46bbfea07a704bb79f60d4aca33915629  infra/inference/termini-inference-switch.sh
```

## 4. `/health`, `/v1/models`, and a PEER request from Node A (the acceptance)

On node-c:

```
GET /health   HTTP=200
{"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model",
 "egress": "external", "served_model": "gemini-2.5-flash",
 "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"},
 "destination": "aiplatform.googleapis.com", ...}
GET /v1/models {"object": "list", "data": [{"id": "gemma", ...}]}
worker /health HTTP=200
```

From **node-a** (a peer, `10.10.0.11` -> `http://10.10.0.13:8080`),
`infra/inference/verify-proxy.sh`:

```
PASS  /health reports a backend — backend='gemini-proxy'
PASS  /health backend == gemini-proxy — reported 'gemini-proxy'
PASS  the response labels its backend in-band — header='gemini-proxy' body='gemini-proxy'
PASS  streaming final chunk is labelled in-band
PASS  streaming from a forwarded backend names the destination — header='aiplatform.googleapis.com'
      median latency: 1.102s (backend reported by the endpoint: gemini-proxy)
RESULT: PASS — the front door is a drop-in and it labels what answered
VERIFY_EXIT=0
```

and the same peer, one raw request with no harness:

```
{"id": "chatcmpl-01M4ZQPWGYYBR0DRZYXZ59S20K", ..., "model": "gemma",
 "choices": [{"message": {"role": "assistant", "content": "FIRSTBOOT_OK"}}],
 "termini_backend": {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model",
                     "destination": "aiplatform.googleapis.com", ...}}
HTTP=200 total=0.471467s
```

## 5. Requirement 2 — the CPU path is ONE command away (and back)

```
$ sudo termini-inference-switch local          # state file -> "backend": "local"
PASS  a local answer is labelled answered_by=local-weights — got 'local-weights'
      median latency: 1.824s (backend reported by the endpoint: local)
VERIFY_EXIT=0

$ sudo termini-inference-switch gemini-proxy   # state file -> "backend": "gemini-proxy"
      median latency: 0.801s (backend reported by the endpoint: gemini-proxy)
VERIFY_EXIT=0
```

No reinstall, no rebuild, no file copied: the backend is the env file, exactly as
the proxy card promised.

## Findings (recorded, not smoothed over)

1. **A recreated node-c needs a FRESH single-use bootstrap token, and reusing a
   burned one blocks the WHOLE first boot.** The first raise of this run reused a
   token the (already deleted) first node-c had consumed; registration answered
   `401 UNAUTHENTICATED: bootstrap token already used`, `termini-bootstrap.service`
   parked in `REGISTRATION_FAILED` **while staying in `activating`**, which held
   `multi-user.target` open, so `cloud-final.service` never started and cloud-init
   never ran the runcmd — no llama.cpp build, no front door, and `cloud-init status`
   reporting `running` forever. Pre-existing behaviour of the baked bootstrap unit
   (`infra/bootstrap/node` + the node image), not introduced here; worth a
   `node-bootstrap` look, because the symptom presents as a silent hang rather
   than an error. The acceptance above was re-taken with a fresh token.
2. **A from-scratch node-c cannot build its tools without a public path.**
   `raise-standing-fleet.sh` creates the node `--no-address`, and Private Google
   Access covers Google APIs (`storage.googleapis.com` → 200) but neither
   `archive.ubuntu.com` nor `github.com`: the first boot's
   `apt-get install build-essential cmake git` failed with
   *"Cannot initiate the connection to asia-south1.gce.archive.ubuntu.com:80"*,
   `cloud-init status: error` ([`no-address-apt-failure.txt`](no-address-apt-failure.txt))
   and nothing served on `:8080`. The standing node-c only worked because its boot
   disk predates the external-IP removal and already carries the binary + weights.
   The new opt-in `--with-external-ip` flag is what makes a from-scratch raise
   work; it was used for this run. **The access config was NOT deleted in this
   run**: the worker that raised the node exhausted its iteration budget before
   teardown, so node-c was left with an ephemeral public IP. It was removed in
   the post-merge re-verification below (and the node kept serving `:8080` from
   the peer afterwards), so the standing posture is `--no-address` again. If the
   demo raises a node-c from scratch it must use that flag (or a NAT) — otherwise
   the node has no inference endpoint at all.
3. `n2-standard-2`/`n2-standard-8` were again `ZONE_RESOURCE_POOL_EXHAUSTED` in
   `asia-south1-a`; the run used `e2-standard-2`/`e2-standard-8` and restored the
   declared types at teardown.

## Post-merge re-verification (2026-10-15T12:25–12:42Z, on the LANDED commit)

The acceptance above was taken on the branch commit, before this card landed. The
claim was therefore re-taken against the landed state — `origin/main` = `9a01c9a`
(PR #160), whose tree is byte-identical to the branch (`git diff --stat origin/main HEAD`
prints nothing). Raw output, verbatim: [`post-merge-reverify.txt`](post-merge-reverify.txt).

- **The installed bytes ARE the merged bytes.** `/opt/termini/inference/proxy.py`
  `acf80f7c…` and `/usr/local/sbin/termini-inference-switch` `85b1c822…` match
  `origin/main`, and the installer cloud-init staged at
  `/usr/local/src/termini-inference/install-inference-proxy.sh` is `929afeef…` —
  the same digest as the merged repo file. The four staged files are stamped
  12:05 (the instance create); nothing was installed by hand.
- **The node's own first boot is still the only thing that installed it:**
  `/var/log/termini-llama-serve.log` lines 624/633, `ActiveEnterTimestamp`
  12:12:11Z–12:14:01Z, `cloud-init status: done`.
- **`/health` → `backend=gemini-proxy`** (`HTTP 200` in 1.5 ms), **`/v1/models` →
  `gemma`**, the llama.cpp worker answers on `127.0.0.1:8081` and is `HTTP=000` on
  the underlay address (loopback only), and `termini-inference-switch status`
  reports what is serving.
- **The PEER acceptance from node-a** with the committed `verify-proxy.sh`
  (`2b740a80…`, the same digest on the peer) is `VERIFY_EXIT=0` — median 1.379 s,
  every drop-in/labelling/streaming check PASS — and a bare peer request with no
  harness returns `REVERIFY_OK` with `backend=gemini-proxy`. The first peer
  invocation in PART 1/3 used a wrong argv (`<ip> <port> <backend>` where the
  harness takes `<ip> [backend]`) and FAILED that one check; PART 2/3 is the
  correct run. Both are in the capture, unedited.
- **Teardown:** the timed-out worker left node-c's ephemeral access config
  attached. It was deleted here, and the peer call then returned
  `POSTTEARDOWN_OK`. The fleet is left as it was found: controller + node-a
  RUNNING, node-b TERMINATED, node-c RUNNING internal-only.

## Reproduce

```bash
# 1. the render + the payload check (offline, no GCP)
python3 infra/gcp/cloud-init/node_c_front_door_check.py <rendered node-c user-data>
infra/gcp/verify-cloud-init.sh               # renders + checks every path

# 2. the live node (burns one single-use bootstrap token)
TOKEN_C=<fresh> TEAM_SSH_PUBKEY_FILE=~/.ssh/<key>.pub ONLY=c \
  infra/gcp/tools/raise-standing-fleet.sh --raise --only c --with-external-ip
# 3. from the peer, once the node's first boot has finished
bash infra/inference/verify-proxy.sh 10.10.0.13 gemini-proxy
```
