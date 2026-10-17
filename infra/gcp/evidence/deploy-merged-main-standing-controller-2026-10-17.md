# Live: the standing controller now runs merged `main`, and the headline prompt returns `200`

Everything below is a real `gcloud` / `ssh` / `curl` run against the **standing demo
fleet** — the same controller and the same published link the judge types into
(`https://34-93-190-64.sslip.io`). Outputs are pasted verbatim; commands are in
[Reproduce](#reproduce).

**Headline:** `/opt/termini` was redeployed **twice** from merged `origin/main` —
first `845922cec7613e5390df6293e93358d2002cb3ee` (PR #202 `0669106` + PR #203
`845922c`) over a foreign branch tree that had been live since 03:27:20Z, then,
after two peer PRs merged mid-run, the **current** `origin/main`
`8d7ac5afdd4e08d113cb6d088ef2ab599c04341f` (§7). The deployed
`controller/app/server.py` sha256 now **equals `main`'s** (`06d7dd3a…`). The exact
demo prompt — `deploy 3 worker agents for coding plus a fast LLM` — returns
**`200 accepted`** on the controller *and* on the published link, on both deploys:
3× `agent_vm` → node-a, `inference_vm` → node-c with `realized_as: host_service`
and `vm_ids: []`. A genuinely wrong target (`inference_vm` → node-a) still returns
**`422 invalid_target_node_type`**. Nothing was applied, so no instance was
created; node-a is back at its `0 / 4 / 5 / 52` baseline on the live link.

## Provenance

| | | |
|---|---|---|
| UTC window | 2026-10-17 03:43:21 → 03:55:15 |
| Deployed controller tree | **final:** `DEPLOYED_COMMIT` = `8d7ac5afdd4e08d113cb6d088ef2ab599c04341f` (read back from `/opt/termini`); first deploy `845922cec7613e5390df6293e93358d2002cb3ee` (§2) |
| Deployed `server.py` sha256 | **final:** `06d7dd3a48ac0fc55a448e834f63bde7315ebb1dabbebd9b197bebe705a1edcf` — **identical** to `git show 8d7ac5a:controller/app/server.py \| sha256sum`; first deploy `6ea7cd02…` (= `845922c`) |
| Deployed `store.py` sha256 | `d11ee0f2b1665688c40bc92a395e1fedf839e39ac00a86215f67fb2382ade74d` at the first deploy (= `845922c`), unchanged by the peer merges |
| Controller | `termini-controller`, `asia-south1-a`, internal `10.10.0.2`, `34.93.190.64`, published link `https://34-93-190-64.sslip.io` |
| Service after final deploy | `systemctl is-active` = `active`, MainPID `98862`, `ActiveEnterTimestamp = Sat 2026-10-17 03:54:25 UTC` (first deploy: MainPID 95167, 03:44:36 UTC) |
| node-a | `termini-node-a`, `10.10.0.11`, `node_id` `01M4TGD31WXH97JAA5S6A4R10D`, `role=compute` |
| node-c (inference front door) | `termini-node-c`, `10.10.0.13`, `node_id` `01M4ZQ7Q2J80ATY3K32PY3PPJH`, `role=compute` |
| Project | `01J0DEM0WARM000000000000AB` |
| Plans exercised | `01M53ZE83GHVBZQE8WC9TDX8PP` (on the controller), `01M53ZFWDT3W2530NJGXDSJMN7` (on the published link), `01M53ZV3XYHAVR63W6ZB78X1VZ` (post-merge re-check), `01M53ZZHPYKZKKCHBYX95ZR8A1` (final acceptance) — all intent-only, all released |
| Instances this run created | **none** (nothing applied, per the card) |
| Deploy method | `infra/gcp/tools/deploy-termini-tree.sh --ref 845922c…` then `--ref 8d7ac5a…` (backups `/opt/termini.bak-20261017T034422Z` / `…-20261017T035412Z`, restart `termini-controller`); `/data/controller.db` backed up to `/tmp/termini-db.pre-deploy-t_20eeb595-20261017T035356Z.db` (integrity `ok`) before the §7 restart |

## 1. Pre-state — the tree that was live was not `main`

Read first, destroyed nothing:

```
[pre] UTC 20261017T034418Z
[pre] /opt/termini/DEPLOYED_COMMIT: 696460669cde41787bd1c43ac10dd7b49e58eb86
[pre] live server.py sha256: 0b7d4b4172416e6cf554b166712b5ef89c5516bcc54580bbfa4547686d2cdefe
[pre] backup /tmp/server.py.pre-deploy-t_20eeb595 sha256: 0b7d4b4172416e6cf554b166712b5ef89c5516bcc54580bbfa4547686d2cdefe
[pre] service: active 88918 Sat 2026-10-17 03:27:20 UTC
```

That is exactly the foreign tree `t_d7e0bf4e` warned about: `t_28ab1be5`'s branch
commit `6964606` (`server.py` `0b7d4b41…`), live since 03:27:20Z, **not** on
`origin/main`. Its live window closed at 03:35:56Z and the card completed at
03:42Z before this deploy began, so nothing was clobbered.

node-a capacity before the run (also the card's required pre-check):

```
01M4TGD31WXH97JAA5S6A4R10D | compute | online | cpu_allocated 0 | cpu_free 4 | ram_allocated_gb 0 | ram_free_gb 5 | disk_free_gb 52
01M4ZQ7Q2J80ATY3K32PY3PPJH | compute | online | cpu_allocated 0 | cpu_free 16 | ram_allocated_gb 0 | ram_free_gb 29 | disk_free_gb 52
01M53X03YFPR71J2SAZVRG37JY | compute | degraded | cpu_allocated 0 | cpu_free 4 | ram_allocated_gb 0 | ram_free_gb 5 | disk_free_gb 52
```

## 2. Deploy merged `main`

```
[deploy] ref=845922cec7613e5390df6293e93358d2002cb3ee commit=845922cec7613e5390df6293e93358d2002cb3ee tarball=382498 bytes
[remote] backup -> /opt/termini.bak-20261017T034422Z
[remote] deployed 129 files from 845922cec7613e5390df6293e93358d2002cb3ee
[deploy] restarting termini-controller
active
[deploy] done: termini-controller /opt/termini now carries 845922cec7613e5390df6293e93358d2002cb3ee
```

Read back after the restart:

```
[post] UTC 2026-10-17T03:44:50Z
[post] DEPLOYED_COMMIT: 845922cec7613e5390df6293e93358d2002cb3ee
[post] live server.py sha256: 6ea7cd022dc490007a27911444b078b10209674875827a354024a63c50f2e6cf
[post] service: active
MainPID=95167
NRestarts=0
ActiveEnterTimestamp=Sat 2026-10-17 03:44:36 UTC
```

`/opt/termini/DEPLOYED_FILES` (129 entries) lists the same two shas for the files
the change touches, so this is the merged commit, not a hand-copy:

```
6ea7cd022dc490007a27911444b078b10209674875827a354024a63c50f2e6cf  controller/app/server.py
d11ee0f2b1665688c40bc92a395e1fedf839e39ac00a86215f67fb2382ade74d  controller/app/store.py
```

The front door still answers, and still says what it is (`curl -i
http://10.10.0.13:8080/health` from the controller):

```
HTTP/1.1 200 OK
Server: termini-inference-proxy/1.0 Python/3.12.3
X-Termini-Backend: gemini-proxy
X-Termini-Answered-By: forwarded-managed-model
X-Termini-Model: gemma
X-Termini-Model-Id: gemini-2.5-flash
X-Termini-Egress: external
X-Termini-Residency: resident
X-Termini-External-Destination: aiplatform.googleapis.com
```

## 3. THE EXACT PROMPT → `200 accepted` (twice: controller, then published link)

### 3.1 On the controller (`127.0.0.1:8080`, project token)

Request, verbatim: `{"prompt":"deploy 3 worker agents for coding plus a fast LLM"}`.
`started_utc: 2026-10-17T03:45:20Z`, `finished_utc: 2026-10-17T03:45:40Z`,
`HTTP_STATUS: 200`:

```json
{
    "plan_id": "01M53ZE83GHVBZQE8WC9TDX8PP",
    "request_id": "01M53ZE83HEQTJEM2DF0HQSBW9",
    "project_id": "01J0DEM0WARM000000000000AB",
    "outcome": "accepted",
    "created_at": "2026-10-17T03:45:40Z",
    "allocations": [
        {
            "index": 0, "status": "accepted",
            "resolved": {"type": "agent_vm", "count": 3, "vcpus": 1, "ram_gb": 1,
                         "disk_gb": 8, "passthrough_gpu": false, "target_node": "auto"},
            "guardrail": {"clamped_fields": [], "reject_reasons": []},
            "node_id": "01M4TGD31WXH97JAA5S6A4R10D",
            "realized_as": "vm", "vm_ids": []
        },
        {
            "index": 1, "status": "accepted",
            "resolved": {"type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16,
                         "disk_gb": 64, "passthrough_gpu": false, "target_node": "auto"},
            "guardrail": {"clamped_fields": [], "reject_reasons": []},
            "node_id": "01M4ZQ7Q2J80ATY3K32PY3PPJH",
            "realized_as": "host_service", "vm_ids": []
        }
    ],
    "audit": {"actor": "human", "guardrail": "termini.intent.clamp_validate"}
}
```

Exactly the contract: 3 `agent_vm` on node-a `01M4TGD31…`, `inference_vm` on
node-c `01M4ZQ7Q2…` as `realized_as: host_service`, and **`vm_ids: []`** on both
(nothing materialised yet — that is `apply`'s job).

### 3.2 On the published link the judge types into

`POST https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WARM000000000000AB/intents`
with the same body, `HTTP_STATUS: 200`, `time_total: 19.244382` (03:46:14Z →
03:46:34Z):

```json
{
    "plan_id": "01M53ZFWDT3W2530NJGXDSJMN7",
    "request_id": "01M53ZFWDVJD2CNFT7T4G48FVZ",
    "outcome": "accepted",
    "created_at": "2026-10-17T03:46:34Z",
    "allocations": [
        {"index": 0, "status": "accepted", "node_id": "01M4TGD31WXH97JAA5S6A4R10D",
         "realized_as": "vm", "vm_ids": []},
        {"index": 1, "status": "accepted", "node_id": "01M4ZQ7Q2J80ATY3K32PY3PPJH",
         "realized_as": "host_service", "vm_ids": []}
    ]
}
```

(The published link accepts the demo project token, so this is the same path the
dashboard/judge uses, TLS included.)

## 4. A wrong target still rejects — `422 invalid_target_node_type`

`POST /intents` with an explicit spec pointing `inference_vm` at node-a
(`01M4TGD31WXH97JAA5S6A4R10D`, `role=compute`, not the front door) → `422`:

```json
{
    "error": {
        "code": "GUARDRAIL_REJECTED",
        "message": "allocation plan rejected by guardrail",
        "details": [
            {
                "index": 0,
                "reason": "invalid_target_node_type",
                "detail": "inference_vm must resolve to the inference node or to the node serving the agent-facing inference service: node 01M4TGD31WXH97JAA5S6A4R10D is neither an inference node (a node reporting a >= 12 GiB GPU) nor the host of the agent-facing inference service"
            }
        ],
        "request_id": "01M53ZG61EMXKZMAXH1QDTYXWN"
    }
}
```

The guardrail is intact: the service-host resolution widened *which node* satisfies
the reference class, it did not make an arbitrary target acceptable.

## 5. Fleet state left behind — baseline, nothing applied, no instance created

Accepting an intent **reserves** the plan's capacity (§5.2 step 7 commits the
allocation rows at plan build), so each of the two `200`s above moved node-a from
`cpu_allocated 0` to `3` (3 × `agent_vm` 1 vCPU / 1 GiB / 8 GB = `3 / 3 / 24`;
node-c stayed at `0` — `host_service` commits nothing). Both plans were released
immediately, which is why the link is left usable:

```
== release via public link ==
HTTP_STATUS:200
{"plan_id": "01M53ZFWDT3W2530NJGXDSJMN7", "status": "released",
 "released": {"cpu_allocated": 3, "ram_allocated_gb": 3, "disk_allocated_gb": 24},
 "rows_released": 1, "released_at": "2026-10-17T03:46:43Z",
 "deleted": [], "failed": [], "unknown_instances": true}
```

(the controller-side release of `01M53ZE83GHVBZQE8WC9TDX8PP` at 03:46:02Z is the
same response — `released {3, 3, 24}`, `deleted []`, `unknown_instances true`,
because nothing was ever applied, so there were no instance names to look up).

Capacity after both releases, via the published link at 03:46:44Z:

```
01M4TGD31WXH97JAA5S6A4R10D | compute online | cpu_allocated 0 | cpu_free 4 | ram_allocated_gb 0 | ram_free_gb 5 | disk_free_gb 52
01M4ZQ7Q2J80ATY3K32PY3PPJH | compute online | cpu_allocated 0 | cpu_free 16 | ram_allocated_gb 0 | ram_free_gb 29 | disk_free_gb 52
01M53X03YFPR71J2SAZVRG37JY | compute degraded | cpu_allocated 0 | cpu_free 4 | ram_allocated_gb 0 | ram_free_gb 5 | disk_free_gb 52
```

node-a's own `lxc list` still shows exactly the pre-existing containers and
**nothing** from this run:

```
demo-workload                                 RUNNING  10.174.247.148 (eth0)  239.19MiB
termini-01m4wjhwqr3b8pcav2tvqbgg-0-0          RUNNING  10.174.247.141 (eth0)   92.46MiB
termini-01m4wjhwqr3b8pcav2tvqbgg-0-1          RUNNING  10.174.247.215 (eth0)   91.59MiB
termini-01m537g26sm8qgkmztn8stbt-0-0          STOPPED
termini-01m537km0trb189a6e1gz546-0-0          RUNNING  10.174.247.138 (eth0)   90.94MiB
verif-fail                                    RUNNING  10.174.247.11 (eth0)   114.36MiB
verif-proof                                   RUNNING  10.174.247.176 (eth0)  207.83MiB
```

## 6. Operational note for the demo

Because `accept` reserves capacity, **the headline prompt is not free to repeat
indefinitely**: node-a has `cpu_free 4`, each accepted prompt reserves `3`, so a
second prompt still fits (`4 → 1`) but a third would be rejected
`exceeds_node_cpu_ceiling` unless a plan is released. `POST /vms/release` (or
`apply` → `release`) restores the pool. Release of a plan whose `vm_ids` were never
recorded reports `unknown_instances: true` — that is the correct signal for "this
plan never materialised", not an error.

## 7. Second deploy — current merged `main`, after two peer PRs landed mid-run

While this card was running, two other cards merged into `main`, one of them
touching `controller/app/server.py` (PR #205 `af8532f`, the `/assistant/ask`
endpoint, migrations `0018`/`0019`), and a watchdog then reverted a red peer
commit (`8d7ac5a`). The deployed tree was therefore no longer byte-identical to
`main` even though the headline-prompt fix was live. Since no other card had a
live window open (`board: 0 other running deploy tasks` at 03:53) and merged
`main` is what the demo link is supposed to run, the tree was moved to the
**current** `origin/main` and the whole acceptance was re-run on it.

Safety rails, because this restart applies peer migrations to the live demo DB:

* `/data/controller.db` (703 MB) backed up first with the SQLite online backup
  API → `/tmp/termini-db.pre-deploy-t_20eeb595-20261017T035356Z.db`,
  `PRAGMA integrity_check` = `ok`, `schema_migrations` = `1…17`,
  `commands` rows `0`, `remediation_decisions` rows `1742`;
* `/opt/termini` backed up by the deploy script → `/opt/termini.bak-20261017T035412Z`;
* the previous tree (`845922c`) is still recoverable from
  `/opt/termini.bak-20261017T034422Z`.

Deploy:

```
[deploy] ref=8d7ac5afdd4e08d113cb6d088ef2ab599c04341f commit=8d7ac5afdd4e08d113cb6d088ef2ab599c04341f tarball=393896 bytes
[remote] backup -> /opt/termini.bak-20261017T035412Z
[remote] deployed 132 files from 8d7ac5afdd4e08d113cb6d088ef2ab599c04341f
[deploy] restarting termini-controller
active
```

Read back, and the startup migration log:

```
[post2] UTC 2026-10-17T03:54:40Z
[post2] DEPLOYED_COMMIT: 8d7ac5afdd4e08d113cb6d088ef2ab599c04341f
[post2] server.py sha256: 06d7dd3a48ac0fc55a448e834f63bde7315ebb1dabbebd9b197bebe705a1edcf
[post2] service: active
MainPID=98862  NRestarts=0  ActiveEnterTimestamp=Sat 2026-10-17 03:54:25 UTC
Started termini-controller.service - Termini controller (control plane).
2026-10-17 03:54:26,295 INFO termini.controller applied migrations: 0018_assistant, 0019_assistant_command_actor
2026-10-17 03:54:26,314 INFO termini.controller controller listening on 0.0.0.0:8080 (db=/data/controller.db)
2026-10-17 03:54:26,365 INFO termini.operator.loop operator loop started (poll=2.0s, dry_run=False)
schema_migrations now: [1 … 19]
```

`06d7dd3a…` is `git show 8d7ac5a:controller/app/server.py | sha256sum`, so the
deployed file is **byte-identical to current `main`**.

### 7.1 The exact prompt again, on the published link (final acceptance)

`POST https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WARM000000000000AB/intents`
at 03:54:48Z, same body, `HTTP_STATUS: 200`, `time_total: 19.136921`, plan
`01M53ZZHPYKZKKCHBYX95ZR8A1`:

```json
{"plan_id": "01M53ZZHPYKZKKCHBYX95ZR8A1", "outcome": "accepted",
 "created_at": "2026-10-17T03:55:07Z",
 "allocations": [
   {"index": 0, "status": "accepted", "node_id": "01M4TGD31WXH97JAA5S6A4R10D",
    "realized_as": "vm", "vm_ids": [],
    "resolved": {"type": "agent_vm", "count": 3, "vcpus": 1, "ram_gb": 1, "disk_gb": 8}},
   {"index": 1, "status": "accepted", "node_id": "01M4ZQ7Q2J80ATY3K32PY3PPJH",
    "realized_as": "host_service", "vm_ids": [],
    "resolved": {"type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16, "disk_gb": 64}}]}
```

### 7.2 Wrong target still rejects, on the same deployed tree

`422` at 03:55:07Z, verbatim `details[0]`:

```
{"index": 0, "reason": "invalid_target_node_type",
 "detail": "inference_vm must resolve to the inference node or to the node serving the
            agent-facing inference service: node 01M4TGD31WXH97JAA5S6A4R10D is neither an
            inference node (a node reporting a >= 12 GiB GPU) nor the host of the
            agent-facing inference service"}
```

### 7.3 Reservation released, fleet back to baseline

```
release HTTP_STATUS:200
{"plan_id": "01M53ZZHPYKZKKCHBYX95ZR8A1", "status": "released",
 "released": {"cpu_allocated": 3, "ram_allocated_gb": 3, "disk_allocated_gb": 24},
 "rows_released": 1, "released_at": "2026-10-17T03:55:08Z",
 "deleted": [], "failed": [], "unknown_instances": true}

01M4TGD31WXH97JAA5S6A4R10D compute online cpu_allocated 0 cpu_free 4 ram_allocated_gb 0 ram_free_gb 5 disk_free_gb 52
01M4ZQ7Q2J80ATY3K32PY3PPJH compute online cpu_allocated 0 cpu_free 16 ram_allocated_gb 0 ram_free_gb 29 disk_free_gb 52
01M53X03YFPR71J2SAZVRG37JY compute degraded cpu_allocated 0 cpu_free 4 ram_allocated_gb 0 ram_free_gb 5 disk_free_gb 52
```

### 7.4 Sanity probe of the peer code this deploy also brought in

Because §7 put PR #205's `/assistant/ask` on the link without its own live probe,
one read-only question was sent (nothing executed):

```
ask HTTP_STATUS:200 time:1.333373
{"answer": "I cannot answer your question because I do not have access to any operational
            records about nodes or their CPU usage.", "citations": [], "executed": [],
 "proposed_actions": [], "model": "gemma", "decided_by": "assistant",
 "elapsed_ms": 921, "truncated": false, "errors": []}
```

The route is live and the model round-trip works; the empty answer is the
contract-shaped response for a request with **no `context`** (nothing was fetched
to ground it), not an error. It is not a substitute for #205's own acceptance of
the grounded/execute paths — that card verified those against a local fixture.

## Reproduce

```bash
# 1. read the deployed tree FIRST (never deploy over another worker's window)
gcloud compute ssh termini-controller --zone=asia-south1-a \
  --command='sha256sum /opt/termini/controller/app/server.py; cat /opt/termini/DEPLOYED_COMMIT; systemctl is-active termini-controller'

# 2. deploy merged main (backs up to /opt/termini.bak-<stamp>.tgz and restarts)
infra/gcp/tools/deploy-termini-tree.sh --ref 845922cec7613e5390df6293e93358d2002cb3ee

# 3. the exact prompt, on the published link
curl -s -w '\n%{http_code}\n' -X POST \
  -H 'Authorization: Bearer demo-project-token' -H 'Content-Type: application/json' \
  -d '{"prompt":"deploy 3 worker agents for coding plus a fast LLM"}' \
  https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WARM000000000000AB/intents

# 3b. the wrong target must still reject
#   POST …/intents \
#   {"spec":{"allocations":[{"type":"inference_vm","count":1,"vcpus":2,"ram_gb":4,
#                            "target_node":"01M4TGD31WXH97JAA5S6A4R10D"}]}}

# 4. return the reservation the accept made (nothing was applied)
#   POST …/vms/release {"plan_id":"<plan_id>","reason":"..."}

# 5. capacity + containers
curl -s -H 'Authorization: Bearer demo-project-token' \
  https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WARM000000000000AB/nodes
gcloud compute ssh termini-node-a --zone=asia-south1-a --tunnel-through-iap \
  --command='sudo lxc list -c ns4m --format compact'
```

## Caveats

* **Nothing was applied** (the card's instruction): no `agent_vm` instance was
  materialised, so this note proves the *acceptance* path on the merged tree, not
  apply. Apply of 3 agents was proven on this fleet by `t_d7e0bf4e` (03:21–03:29Z)
  and `t_ece9d14f` before it.
* The pre-existing containers on node-a (two `termini-01m4wjhw…` from an
  already-released 2026-10-14 plan, `termini-01m537km0…`, a stopped
  `termini-01m537g26…`, plus `demo-workload` / `verif-*`) are other cards'
  evidence and were **not** touched — the same do-not-touch rule `t_28ab1be5`
  followed.
* node-b remains `TERMINATED`; no GCP instance was started or stopped by this run.
* The third registered node `01M53X03YFPR71J2SAZVRG37JY` (`degraded`, another
  card's throwaway) is visible in `GET /nodes` and was not touched.
