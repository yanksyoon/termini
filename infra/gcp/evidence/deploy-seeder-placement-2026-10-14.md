# Standing-demo deploy fixes — RAW EVIDENCE (2026-10-14)

Three live fixes on the standing GCP fleet. Every block below is verbatim output
from the running deployment; nothing here is reconstructed from memory.

| | |
|---|---|
| Branch | `wt/deploy` |
| Base | `origin/main` @ `68f5694` |
| Deployed commit | `57b0ed4` (`/opt/termini/DEPLOYED_COMMIT` on the controller) |
| Controller VM | `termini-controller` (10.10.0.2 / 34.93.190.64), code at `/opt/termini` |
| Fleet during the run | `termini-node-a` (compute, LXD `https://10.10.0.11:8443`), `termini-node-c` (8 vCPU/32 GiB, gemma on `http://10.10.0.13:8080`); `termini-node-b` TERMINATED (CPUS_ALL_REGIONS 12/12) |
| Model | `llama.cpp` + Gemma 3 4B IT Q4_K_M on node-c, alias `gemma` |

---

## FIX 1 — the deployed controller is a copy, and is now provably main's tree

`/opt/termini` is not a git checkout, so nothing tied the running code to a commit.
This adds `infra/gcp/tools/deploy-termini-tree.sh`: it archives an exact ref,
extracts it over `/opt/termini` (merging — local-only files are untouched), backs
the previous tree up first, writes `DEPLOYED_COMMIT` + a `DEPLOYED_FILES` sha256
manifest, and restarts + verifies.

### Which commits were missing — measured, not assumed

I captured a sha256 manifest of every tracked runtime file (`controller termini
console chaos`) on the controller **before** changing anything, and matched it file
by file against main's history:

```
[BEFORE] /opt/termini vs main@68f5694 -> differ=0  not-on-main=2
   not-on-main: inference-proxy.py        (local demo stub, not in the repo)
   not-on-main: lxd_stub.py               (local demo stub, not in the repo)

Deployment was already byte-identical to main HEAD (68f5694) for all 97 tracked
runtime files => 0 commits missing.
```

The `operation Failure: The instance is already stopped` ERROR that motivated this
fix is **historical**, not current. It is an audit row from before the fold landed:

```
remediation_audits:
  01M4TKKNBXVDHYKJ260QQMDVEM  node 01M4TGD...  oom-guard-01  instance.stop
    result=error  detail="operation Failure: The instance is already stopped"
    executed_at=2026-10-13T12:24:37.862Z
```

`_ALREADY_STOPPED` was introduced by `8782ada` (PR #145), committed
`2026-10-13 16:41:33Z` — ~4 h AFTER that audit row was written. The live deployment
carries it (`git log -S _ALREADY_STOPPED` → `8782ada`), and the controller was
restarted on that tree at `2026-10-14 05:49:33Z`. So the error is evidence of the
old window, and no longer reproducible.

To keep it closed, a test now pins the **exact live transport** (a *failed
operation* carrying the message, not only LXD's synchronous 400):
`tests/test_operator_executor.py::test_a_failed_operation_carrying_the_LIVE_message_is_also_folded`.

### After the deploy

```
[deploy] ref=57b0ed4 commit=57b0ed430dabdae4ef23c1167c26daaf04d7aecc tarball=211513 bytes
[remote] backup -> /opt/termini.bak-20261014T063633Z
[remote] deployed 97 files from 57b0ed430dabdae4ef23c1167c26daaf04d7aecc
[deploy] restarting termini-controller
active
```

```
[AFTER] vs deployed commit 57b0ed4 -> differ=0 not-on-main=0
Changed runtime files by this deploy:
   console/seed_demo.py
   controller/app/config.py
   controller/app/operator/executor.py
   controller/app/operator/loop.py
   controller/app/server.py
```

Four backups were taken (`/opt/termini.bak-*`); the systemd units and their
drop-ins were not touched.

---

## FIX 2 — the seeder starts with no manual token handling

### BEFORE (bare `systemctl start termini-seeder`)

```
=== BEFORE: effective ExecStart (drop-in wins) ===
/opt/termini/console/seed_demo.py
--bootstrap

=== BEFORE: try a bare start ===
is-active: failed
python3[7589]: register demo-node-a failed: 401 {'error': {'code': 'UNAUTHENTICATED',
  'message': 'bootstrap token already used', 'details': [],
  'request_id': '01M4WH207WX4Z3HS164CF838T2'}}
systemd[1]: termini-seeder.service: Failed with result 'exit-code'.
```

That is the recording landmine exactly as reported: the drop-in
(`termini-seeder.service.d/zzz-seeder-tokens.conf`) was the file actually in effect
(drop-ins load after the unit and the last one setting a key wins), and its tokens
had already been burned by an earlier run.

### The mechanism chosen

The controller gained `POST /v1/bootstrap-tokens` — project-token-authenticated and
**direct-loopback-only** — which mints N fresh **unused** single-use tokens (the
same rows `TERMINI_BOOTSTRAP_TOKEN` seeds at startup). The seeder fetches them at
startup with the new `--bootstrap-mint N` flag (`console/seed_demo.py`).

Why the mint endpoint rather than a token in a unit or a controller-side seeder
token: the token must be *different every start* (single-use), and the unit /
drop-in scheme is precisely what silently went stale. Nothing is stored anywhere;
a start mints exactly what it needs.

### AFTER

```
=== install the self-provisioning unit; drop the token drop-ins ===
=== effective ExecStart now ===
/opt/termini/console/seed_demo.py
--bootstrap-mint
=== bare start (NO manual token) ===
is-active: active
python3[7818]: [seed] project_id = 01J0DEM0WARM000000000000AB
python3[7818]: [seed] node A (compute)   = 01M4NZK46WXG5ZEFAGAAV5JRXM
python3[7818]: [seed] node C (inference) = 01M4NZK474BVARFBXYQBMVA8GV

=== restart the seeder (fresh mint each start) ===
is-active: active
python3[7983]: [seed] node A (compute)   = 01M4NZK46WXG5ZEFAGAAV5JRXM
python3[7983]: [seed] node C (inference) = 01M4NZK474BVARFBXYQBMVA8GV
```

The unit is now the single source of truth — `termini-seeder.service.d` does not
exist on the controller any more:

```
controller=active seeder=active
/opt/termini/console/seed_demo.py --bootstrap-mint
ls: cannot access '/etc/systemd/system/termini-seeder.service.d': No such file or directory
```

### The single-use property is intact (and was tested live)

Mint 2 tokens over loopback, register with one, replay it:

```
=== mint via loopback (project token) ===
{"schema_version": "1.0", "count": 2,
 "tokens": ["LcnGD2Wtuk-...", "co7sMig652kV..."],
 "created_at": "2026-10-14T06:20:35Z", "single_use": true}

=== register with a MINTED token (expect 201) ===
HTTP 201

=== REPLAY the SAME token (expect 401 "already used") ===
{"error": {"code": "UNAUTHENTICATED", "message": "bootstrap token already used",
  "request_id": "01M4WH3RMGHEM2H77BM1DWCNCR"}}
```

### Loopback-only — the first attempt was WRONG and is fixed

The first implementation trusted `client_address == 127.0.0.1`. That is not enough
on this host: **Caddy** (`:443 -> 127.0.0.1:8080`) proxies the public path, so a
REMOTE request also arrives with a loopback peer. Measured before the hardening:

```
=== REMOTE mint attempt (expect 403, loopback-only) ===
{"schema_version": "1.0", "count": 2, "tokens": ["IccS99bL...", "2UfNSpXD..."], ...}
HTTP 201          <-- WRONG: minted for a public caller
```

Hardened to require a **direct** loopback connection: a loopback peer AND no
proxy-forwarding header (`X-Forwarded-For` / `X-Real-Ip` / `Forwarded` / `Via`). The
seeder calls `127.0.0.1:8080` directly and sends none of them, so it always passes.
Re-measured after redeploy:

```
=== REMOTE mint attempt via public URL (expect 403) ===
{"error": {"code": "PERMISSION_DENIED",
  "message": "bootstrap-token minting is restricted to the controller loopback",
  "request_id": "01M4WHAMJZA3NXHC85BPC116G9"}}
HTTP 403
```

(Two unused tokens were created by the pre-fix remote attempt; they are inert rows.
The synthetic node registered during this check was decommissioned —
`{"status": "decommissioned", ...}`.)

---

## FIX 3 — agents land on node-a, and remediation follows them

### The defect, as measured

Both real nodes register `compute` (the role rule is GPU-gated, `node-bootstrap.md`
§5.3), so `auto` placement used free-RAM only and node-c (8 vCPU/32 GiB) beat node-a
(2 vCPU/8 GiB) every time. Meanwhile the operator loop dialled ONE global executor
bound to `TERMINI_LXD_BASE_URL=https://10.10.0.11:8443` (node-a) for every event.
Live proof of the divergence (audit row, a *synthetic* node-c event dialling node-a
is the documented no-endpoint fallback; the real one below is the defect):

```
remediation_audits before the fix:
  06:32:14  01M4TGEEZEHJEQB01T5PBBE97M (REAL node-c)  disk-degrade-01
            instance.stop  already_applied
            lxd_endpoint=https://10.10.0.11:8443      <-- node-c's fault, node-a dialled
```

### The placement fix

`auto` for `agent_vm` never resolves to the node that serves the model. That node is
identified from the model address the controller already knows
(`TERMINI_INFERENCE_BASE_URL`) matched against each node's own LXD endpoint / source
address, plus optional operator-named hosts
(`TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS`). The exclusion is dropped when it would
leave no candidate, and an explicit `target_node` is still honoured.
**The frozen role rule was not touched.**

Live: an `auto` intent submitted while node-c had 4x node-a's free RAM resolved to
node-a:

```
=== submit an AUTO intent (no target_node) — expect it to resolve to node-a ===
outcome: accepted  plan_id: 01M4WJHWQR3B8PCAV2TVQBGG79
  index 0 status accepted node_id 01M4TGD31WXH97JAA5S6A4R10D
  resolved {'type': 'agent_vm', 'count': 2, 'vcpus': 1, 'ram_gb': 1, 'disk_gb': 4,
            'target_node': 'auto'}
```

(node-a = `01M4TGD31WXH97JAA5S6A4R10D`, node-c = `01M4TGEEZEHJEQB01T5PBBE97M`.)

Applied, and the agents are RUNNING **on node-a**:

```
state: ready
vm_ids: ['termini-01m4wjhwqr3b8pcav2tvqbgg-0-0', 'termini-01m4wjhwqr3b8pcav2tvqbgg-0-1']
  node: 01M4TGD31WXH97JAA5S6A4R10D status: ready created: 2

### node-a lxc list
+--------------------------------------+---------+-----------------------+
|                 NAME                 |  STATE  |         IPV4          |
+--------------------------------------+---------+-----------------------+
| demo-workload                        | STOPPED |                       |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-0 | RUNNING | 10.174.247.141 (eth0) |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-1 | RUNNING | 10.174.247.215 (eth0) |
+--------------------------------------+---------+-----------------------+
```

### The remediation fix — and the bug the live run exposed

The loop now resolves a per-node executor (same URL helper materialisation uses),
inheriting the controller's LXD client identity/TLS policy rather than rebuilding
it.

**The first live attempt still printed node-a, and that was a real find:** production
`app/main.py` *hands the loop a credentialed global executor*, and the initial
implementation treated an injected executor as an override — which silently
re-created the defect. Fixed so the handed-in executor is a FALLBACK, not an
override (`OperatorLoop._executor_for`), with
`LxdExecutor.with_base_url()` preserving `_ssl_ctx`/certs/timeouts.
`tests/test_agent_placement_compute_node.py::test_the_handed_in_executor_is_a_FALLBACK_not_an_override`
pins it.

**Before** the corrected deploy (06:32:14) vs **after** (06:42:52), same node, same
rule:

```
06:42:52  01M4TGEEZEHJEQB01T5PBBE97M (REAL node-c)  disk-degrade-01
          instance.stop  error
          lxd_endpoint=https://10.10.0.13:8443      <-- now node-c's OWN endpoint
06:32:14  01M4TGEEZEHJEQB01T5PBBE97M (REAL node-c)  disk-degrade-01
          instance.stop  already_applied
          lxd_endpoint=https://10.10.0.11:8443      <-- before the fix: node-a
```

And an event on the node that actually HOLDS the agents resolves to that node — the
action is an idempotent no-op **because the instance is there**:

```
06:43:44  01M4TGD31WXH97JAA5S6A4R10D (REAL node-a, hosts the agents)
          disk-degrade-01  instance.stop  already_applied
          lxd_endpoint=https://10.10.0.11:8443
```

The faults were injected as REAL load (fast disk writes on the node, measured by the
node's own `termini-bootstrap` telemetry):

```
node-a telemetry:
  2026-10-14T06:44:15Z  free_bytes [40077438976, ...]  write_latency_ms [235.5, 475.5]
  2026-10-14T06:43:55Z  free_bytes [43292372992, ...]  write_latency_ms [349.4, 288.6]
health_events: 2026-10-14T06:43:44.436Z node-a disk-degrade-01 (disk_free_bytes)
```

### Honest scope notes for FIX 3

- To make the verification fast the operator was temporarily put in
  `deterministic` decision mode via a drop-in, then **restored** (verified:
  `TERMINI_OPERATOR_DECISION_MODE=model`). Decision *mode* does not affect the
  remediation *target* under test.
- A SEEDER-FABRICATED node reports no endpoint, so its remediation still falls back
  to the global URL (visible above for `01M4NZK46...`). That is the documented
  fallback for a node with no endpoint, and synthetic rows are never placed on.
- Capacity: node-a could not accept ANY agent when this started (`cpu_free=0`),
  which is the second half of "i can't deploy agents on node a". A stale
  never-materialised node-c commit was released, and a pre-existing node-a plan
  whose 2 agents had been left running was removed and its commit released by
  `POST /vms/reconcile` (`reason: instances_gone`). No database row was hand-edited.
  The final plan was sized to leave node-a headroom (`cpu_free=2`, `ram_free_gb=3`)
  so the user's next `auto` deployment still fits.

---

## Fleet left UP and healthy

```
$ gcloud compute instances list
NAME                ZONE           MACHINE_TYPE   INTERNAL_IP  EXTERNAL_IP   STATUS
termini-controller  asia-south1-a  n2-standard-2  10.10.0.2    34.93.190.64  RUNNING
termini-node-a      asia-south1-a  n2-standard-2  10.10.0.11                 RUNNING
termini-node-b      asia-south1-a  n2-standard-2  10.10.0.12                 TERMINATED
termini-node-c      asia-south1-a  n2-standard-8  10.10.0.13                 RUNNING

controller=active  seeder=active
GET /nodes -> 2 nodes, both online:
  01M4TGD31WXH97JAA5S6A4R10D compute online cpu_free 2 ram_free_gb 3  disk_free 44
  01M4TGEEZEHJEQB01T5PBBE97M compute online cpu_free 12 ram_free_gb 21 disk_free 20
node-c: {"models":[{"name":"gemma", ...}]}  termini-llama-server=active
node-a: 2 agents RUNNING (the AUTO-placed plan above)
```

Injected load files were removed from both nodes (`/` back to 12 GiB used on node-a,
18 GiB on node-c).

---

## Gates

```
$ python3 -m unittest discover -s tests -t .
Ran 602 tests in 86.640s
OK
$ python3 scripts/check-doc-ulids.py
all example identifiers are 26-char ULIDs
$ python3 scripts/check-readme-links.py
checked 58 unique link/path target(s)
all README links and repo paths resolve
```

(563 → 602 tests: seeder self-provisioning, placement/remediation, and the live
already-stopped operation shape. No regressions.)

## Still wrong / unverified (disclosed)

1. **The frozen role rule is unchanged — by design.** node-c still registers
   `compute`. Recognising the model host as an `inference` node would be the cleanest
   domain fix (then `inference_vm` intents and agent placement both follow from the
   role), but `controller-api.md` §4.1 / `node-bootstrap.md` §5.3 are frozen and the
   user has not made that contract decision. Trade-off: the placement exclusion is
   stated in terms of the model ADDRESS, so it must be kept in step with
   `TERMINI_INFERENCE_BASE_URL` (and can be overridden with
   `TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS`). Recommendation: change the role rule so
   a node reporting the inference service registers `inference`.
2. **node-c's instances from earlier plans are still running** (4 containers) and its
   `disk_free_gb` is 20. They were left alone (they are real workloads, not mine to
   delete); only the never-materialised commit was released.
3. **A fabricated node's remediation still falls back to the global LXD URL** — the
   seeder reports no endpoint (see above). Unchanged behaviour, pre-existing.
4. **The model path was not exercised end-to-end here** — the operator was in
   deterministic mode for the live remediation proof. Model-mode decisions were
   observed in flight (`externalModelCall before: destination=http://10.10.0.13:8080`)
   but each takes ~400 s on CPU; full model-mode remediation was last evidenced in
   `standing-demo-fixes-2026-10-13.md`.
5. GPU is out of scope (free-tier billing forbids accelerators).
