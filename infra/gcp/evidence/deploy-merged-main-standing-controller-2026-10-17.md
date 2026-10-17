# Live: the standing controller now runs merged `main`, and the headline prompt returns `200`

Everything below is a real `gcloud` / `ssh` / `curl` run against the **standing demo
fleet** — the same controller and the same published link the judge types into
(`https://34-93-190-64.sslip.io`). Outputs are pasted verbatim; commands are in
[Reproduce](#reproduce).

**Headline:** `/opt/termini` was redeployed from merged `origin/main`
(`845922cec7613e5390df6293e93358d2002cb3ee`, i.e. PR #202 `0669106` + PR #203
`845922c`) over a foreign branch tree that had been live since 03:27:20Z, and the
deployed `controller/app/server.py` sha256 now **equals `main`'s**
(`6ea7cd02…`). The exact demo prompt — `deploy 3 worker agents for coding plus a
fast LLM` — returns **`200 accepted`** on the controller *and* on the published
link: 3× `agent_vm` → node-a, `inference_vm` → node-c with
`realized_as: host_service` and `vm_ids: []`. A genuinely wrong target
(`inference_vm` → node-a) still returns **`422 invalid_target_node_type`**. Nothing
was applied, so no instance was created; node-a is back at its `0 / 4 / 5 / 52`
baseline.

## Provenance

| | | |
|---|---|---|
| UTC window | 2026-10-17 03:43:21 → 03:46:44 |
| Deployed controller tree | `DEPLOYED_COMMIT` = `845922cec7613e5390df6293e93358d2002cb3ee` (read back from `/opt/termini` on the VM) |
| Deployed `server.py` sha256 | `6ea7cd022dc490007a27911444b078b10209674875827a354024a63c50f2e6cf` — **identical** to `git show 845922c:controller/app/server.py \| sha256sum` |
| Deployed `store.py` sha256 | `d11ee0f2b1665688c40bc92a395e1fedf839e39ac00a86215f67fb2382ade74d` — also identical to `845922c` |
| Controller | `termini-controller`, `asia-south1-a`, internal `10.10.0.2`, `34.93.190.64`, published link `https://34-93-190-64.sslip.io` |
| Service after deploy | `systemctl is-active` = `active`, MainPID `95167`, `ActiveEnterTimestamp = Sat 2026-10-17 03:44:36 UTC`, `NRestarts=0` |
| node-a | `termini-node-a`, `10.10.0.11`, `node_id` `01M4TGD31WXH97JAA5S6A4R10D`, `role=compute` |
| node-c (inference front door) | `termini-node-c`, `10.10.0.13`, `node_id` `01M4ZQ7Q2J80ATY3K32PY3PPJH`, `role=compute` |
| Project | `01J0DEM0WARM000000000000AB` |
| Plans exercised | `01M53ZE83GHVBZQE8WC9TDX8PP` (on the controller), `01M53ZFWDT3W2530NJGXDSJMN7` (on the published link) — both intent-only, both released |
| Instances this run created | **none** (nothing applied, per the card) |
| Deploy method | `infra/gcp/tools/deploy-termini-tree.sh --ref 845922c…` (backup `/opt/termini.bak-20261017T034422Z`, 129 files, restart `termini-controller`) |

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
