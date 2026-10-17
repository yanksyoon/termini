# Live: release tears down the LXD instances it materialised — RAW EVIDENCE

Everything below is a real `gcloud` / `ssh` / `lxc` / `curl` run against the
**standing demo fleet**, on the deployed controller tree, not a stub and not a test
node. Outputs are pasted verbatim; the commands are in [Reproduce](#reproduce).

**Headline:** `POST /v1/projects/{pid}/vms/release` now deletes the instances the
plan materialised, not just its capacity. A plan was applied (instance **RUNNING**
on node-a, capacity committed), released (**instance GONE** from node-a's own
`lxc list`, capacity back to zero), released a second time (`already_released`,
nothing deleted twice), and a never-applied plan returned
`unknown_instances: true`. Unrelated containers on the shared node — including two
named `termini-<other-plan>-*` from *already released* plans — were untouched.

The same operator log shows the before/after inside one afternoon: releases made
**before** the deploy (03:20:29Z, 03:25:52Z) tore nothing down and their containers
are still on node-a; the first release **after** the deploy (03:29:23Z, another
worker's 3-VM plan) tore down all three.

## Provenance

| | |
|---|---|
| UTC window | 2026-10-17 03:27:05 → 03:35:56 |
| Deployed controller tree | `DEPLOYED_COMMIT` = `696460669cde41787bd1c43ac10dd7b49e58eb86` (`t_28ab1be5` branch commit; read back from `/opt/termini` on the VM) |
| Controller | `termini-controller`, `asia-south1-a`, internal `10.10.0.2`, `systemctl is-active` = `active` after the deploy |
| node-a | `termini-node-a`, `10.10.0.11`, `node_id` `01M4TGD31WXH97JAA5S6A4R10D`, `e2-standard-2` (2 vCPU / 8 GiB) |
| node-c | `termini-node-c`, `10.10.0.13`, `node_id` `01M4ZQ7Q2J80ATY3K32PY3PPJH` (not used by this run) |
| Project | `01J0DEM0WARM000000000000AB` |
| Plans exercised | `01M53YE6M21R00WJMHN941B7K9` (applied then released), `01M53YS0X43MYDYS132WB9WHE7` (intent only, never applied) |
| Instances this run created and deleted | `termini-01m53ye6m21r00wjmhn941b7-0-0` |
| Deploy method | `infra/gcp/tools/deploy-termini-tree.sh --ref 6964606` (backup `/opt/termini.bak-20261017T032705Z`, restart `termini-controller`) |

## 1. Baseline — node-a's committed capacity and its containers

`GET /v1/projects/01J0…AB/nodes` at 03:27Z (before the run):

```
{"node_id": "01M4TGD31WXH97JAA5S6A4R10D", "registration_id": "ba1f1faeacb4501aed12cf8ea1e5713b", "role": "compute", "status": "online"}
   capacity: {"cpu_allocated": 3, "cpu_total": null, "disk_allocated_gb": 24, "disk_total_gb": null, "ram_allocated_gb": 3, "ram_total_gb": null}
{"node_id": "01M4ZQ7Q2J80ATY3K32PY3PPJH", "registration_id": "33678f54a5e3e25cc03620b1bd2486e6", "role": "compute", "status": "online"}
   capacity: {"cpu_allocated": 0, "cpu_total": null, "disk_allocated_gb": 0, "disk_total_gb": null, "ram_allocated_gb": 0, "ram_total_gb": null}
{"node_id": "01M53X03YFPR71J2SAZVRG37JY", "registration_id": "2a11b4a8b9747feb12f4504ff90cabe8", "role": "compute", "status": "degraded"}
   capacity: {"cpu_allocated": 0, "cpu_total": null, "disk_allocated_gb": 0, "disk_total_gb": null, "ram_allocated_gb": 0, "ram_total_gb": null}
```

(`cpu_total`/`ram_total_gb` are `null` in `GET /nodes` — that is the pre-existing
finding that the route omits the registered capacity; the allocated side is what
this card moves. The 3 GiB committed at 03:27 belonged to another worker's live
plan, `01M53Y2H…` — see §6.)

node-a's own `sudo lxc list -c ns4` at 03:27Z, before my apply:

```
| demo-workload                        | RUNNING |                       |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-0 | RUNNING | 10.174.247.141 (eth0) |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-1 | RUNNING | 10.174.247.215 (eth0) |
| termini-01m53y2h2qxv99cf7eqhkw65-0-0 | RUNNING | 10.174.247.17 (eth0)  |
| termini-01m53y2h2qxv99cf7eqhkw65-0-1 | RUNNING | 10.174.247.249 (eth0) |
| termini-01m53y2h2qxv99cf7eqhkw65-0-2 | RUNNING | 10.174.247.167 (eth0) |
| termini-01m537g26sm8qgkmztn8stbt-0-0 | STOPPED |                       |
| termini-01m537km0trb189a6e1gz546-0-0 | RUNNING | 10.174.247.138 (eth0) |
| verif-fail                           | RUNNING | 10.174.247.11 (eth0)  |
| verif-proof                          | RUNNING | 10.174.247.176 (eth0) |
```

Two of these (`…01m537g26…`, `…01m537km0…`) are the leak named in the card; two
more (`…01m4wjhw…`) are the same defect from a 03:20:29Z release. The node is a
shared resource — none of them is mine, none of them is touched by this run.

## 2. Plan → apply: a RUNNING instance and committed capacity

`POST /intents` (1 × `agent_vm`, 1 vCPU / 1 GiB / 4 GB, `target_node` = node-a),
then `POST /vms/apply` — `202` in **67 s**, terminal state `ready`:

```
== POST /vms/apply {"plan_id": "01M53YE6M21R00WJMHN941B7K9"}  (start: 03:28:10Z)
HTTP 202
{
    "job_id": "01M53YE6SC4JXMCHJFS237C3MM",
    "plan_id": "01M53YE6M21R00WJMHN941B7K9",
    "state": "ready",
    "vm_ids": [
        "termini-01m53ye6m21r00wjmhn941b7-0-0"
    ],
    "allocations": [
        {
            "index": 0,
            "status": "ready",
            "node_id": "01M4TGD31WXH97JAA5S6A4R10D",
            "realized_as": "vm",
            "vm_ids": [
                "termini-01m53ye6m21r00wjmhn941b7-0-0"
            ],
            "created": 1,
            "already_applied": 0
        }
    ]
}
(end: 03:29:17Z)
```

The node agrees — instance present, **RUNNING**, with the requested limits
(`sudo lxc list` + `lxc config show`, 03:30:42Z):

```
Sat Oct 17 03:30:42 UTC 2026
| termini-01m53ye6m21r00wjmhn941b7-0-0 | RUNNING | 10.174.247.29 (eth0) | fd42:…:82ae (eth0) | CONTAINER | 0 |
---
  limits.cpu: "1"
  limits.memory: 1GiB
  volatile.base_image: c3d039642a619e100ddf680c3d60e4d2641e568f377f5ed9ef420ad886b181e4
```

and capacity is committed (`GET /nodes`, 03:30Z):

```
{"node_id": "01M4TGD31WXH97JAA5S6A4R10D", ...}
   capacity: {"cpu_allocated": 1, "cpu_total": null, "disk_allocated_gb": 4, "disk_total_gb": null, "ram_allocated_gb": 1, "ram_total_gb": null}
```

## 3. Release: capacity back AND the instance gone

`POST /vms/release`, 03:32:27Z:

```
HTTP 200
{
    "plan_id": "01M53YE6M21R00WJMHN941B7K9",
    "status": "released",
    "released": {
        "cpu_allocated": 1,
        "ram_allocated_gb": 1,
        "disk_allocated_gb": 4
    },
    "rows_released": 1,
    "released_at": "2026-10-17T03:32:27Z",
    "deleted": [
        "termini-01m53ye6m21r00wjmhn941b7-0-0"
    ],
    "failed": [],
    "unknown_instances": false
}
```

The operator-log row, captured **live** off `GET /v1/projects/…/logs` (SSE
subscribed before the call):

```
"message": "released plan 01M53YE6M21R00WJMHN941B7K9: 1 GiB RAM, 1 vCPU returned to the pool; tore down termini-01m53ye6m21r00wjmhn941b7-0-0"
```

Both halves asserted on their own source of truth — node-a's `lxc list` at
03:33:10Z, in which the instance is **absent** (`grep -c` = `0`):

```
Sat Oct 17 03:33:10 UTC 2026
== grep for the plan instance:
0
== full lxc list:
| demo-workload                        | RUNNING | 10.174.247.148 (eth0) |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-0 | RUNNING | 10.174.247.141 (eth0) |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-1 | RUNNING | 10.174.247.215 (eth0) |
| termini-01m537g26sm8qgkmztn8stbt-0-0 | STOPPED |                       |
| termini-01m537km0trb189a6e1gz546-0-0 | RUNNING | 10.174.247.138 (eth0) |
| verif-fail                           | RUNNING | 10.174.247.11 (eth0)  |
| verif-proof                          | RUNNING | 10.174.247.176 (eth0) |
```

…and capacity back to zero (`GET /nodes`, 03:33:37Z and again 03:35:56Z after the
§5 case, both `0/0/0` for node-a):

```
   capacity: {"cpu_allocated": 0, "cpu_total": null, "disk_allocated_gb": 0, "disk_total_gb": null, "ram_allocated_gb": 0, "ram_total_gb": null}
```

## 4. Release twice: `already_released`, nothing deleted a second time

```
== SECOND release (idempotency)
HTTP 200
{
    "plan_id": "01M53YE6M21R00WJMHN941B7K9",
    "status": "already_released",
    "released": {"cpu_allocated": 0, "ram_allocated_gb": 0, "disk_allocated_gb": 0},
    "rows_released": 0,
    "released_at": null,
    "deleted": [],
    "failed": [],
    "unknown_instances": false
}
```

A second delete would have failed loudly (the instance was already gone) — it is
not even attempted: the teardown reads the plan's **LIVE** rows before the
soft-release, and a re-release has none. Unit-tested at the LXD-request level
(`test_second_release_is_already_released_and_deletes_nothing_twice`: DELETE count
unchanged after the second call).

## 5. `vm_ids_json` NULL → `unknown_instances: true`

An intent committed capacity and was **never applied** (the shape an interrupted
apply leaves), so no instance name was ever recorded. Release succeeds — the
reservation is real — and says it cannot know what to tear down:

```
== POST /intents (1 agent_vm, 1 vCPU / 1 GiB / 4 GB) — NEVER applied
plan_id=01M53YS0X43MYDYS132WB9WHE7

== release WITHOUT apply (vm_ids_json is NULL)
HTTP 200
{
    "plan_id": "01M53YS0X43MYDYS132WB9WHE7",
    "status": "released",
    "released": {"cpu_allocated": 1, "ram_allocated_gb": 1, "disk_allocated_gb": 4},
    "rows_released": 1,
    "released_at": "2026-10-17T03:34:05Z",
    "deleted": [],
    "failed": [],
    "unknown_instances": true
}
```

The DB row it keyed off (`/data/controller.db`, read on the controller):

```
{'plan_id': '01M53YS0X43MYDYS132WB9WHE7', 'cpu_allocated': 1, 'ram_allocated_gb': 1, 'disk_allocated_gb': 4, 'vm_ids_json': None, 'released_at': '2026-10-17T03:34:05Z', 'release_reason': 't_28ab1be5 unknown-instances proof'}
```

and the operator log records it at `warn`, not silently:

```
2026-10-17T03:34:05.102Z warn released plan 01M53YS0X43MYDYS132WB9WHE7: 1 GiB RAM, 1 vCPU returned to the pool; vm_ids unknown (apply never recorded the instances) — check the node for leftovers
```

No name was guessed: `lxc list` on node-a before and after shows no
`termini-01m53ys0…` instance (the apply never ran), and the release issued no
DELETE.

## 6. Containers that are not this plan's are untouched

The §3 release deleted exactly one name. On the same node, all of the following
are **still present and RUNNING** after it:

| container | why it matters |
|---|---|
| `demo-workload` | the always-on demo agent — not recorded against the plan |
| `verif-proof`, `verif-fail` | another worker's containers (named in the card's do-not-delete list) |
| `termini-01m4wjhwqr3b8pcav2tvqbgg-0-0`, `…-0-1` | a **different plan's** instances, already released at 03:20:29Z. A teardown that swept by the `termini-*` pattern would have deleted these; it did not, because only `vm_ids_json` names are deleted |
| `termini-01m537g26…`, `termini-01m537km0…` | the two leftovers the card was filed for |

Independent corroboration from the same deploy, on a plan **this card did not
create**: at 03:29:23Z the other worker released their 3-VM plan and the new code
tore all three down (operator log + `lxc list` before/after in §1 and §3):

```
2026-10-17T03:29:23.013Z info released plan 01M53Y2H2QXV99CF7EQHKW6583: 3 GiB RAM, 3 vCPU returned to the pool; tore down termini-01m53y2h2qxv99cf7eqhkw65-0-0, termini-01m53y2h2qxv99cf7eqhkw65-0-1, termini-01m53y2h2qxv99cf7eqhkw65-0-2
```

### Before/after in one log (the defect and its repair, same endpoint)

```
2026-10-17T03:34:05.102Z warn released plan 01M53YS0X43MYDYS132WB9WHE7: … vm_ids unknown …
2026-10-17T03:32:34.064Z info released plan 01M53YE6M21R00WJMHN941B7K9: … tore down termini-01m53ye6m21r00wjmhn941b7-0-0
2026-10-17T03:29:23.013Z info released plan 01M53Y2H2QXV99CF7EQHKW6583: … tore down termini-01m53y2h2qxv99cf7eqhkw65-0-0, …-0-1, …-0-2
2026-10-17T03:25:52.329Z info released plan 01M53Y85M5WZZ81S3GKMZZSZ88: 1 GiB RAM, 1 vCPU returned to the pool    <- pre-deploy: no teardown
2026-10-17T03:20:29.881Z info released plan 01M4WJHWQR3B8PCAV2TVQBGG79: 2 GiB RAM, 2 vCPU returned to the pool    <- pre-deploy: no teardown
2026-10-17T02:54:14.115Z info released plan 01M53W8CS85XQ7KCGT9T3F2ZSD: 2 GiB RAM, 2 vCPU returned to the pool    <- pre-deploy: no teardown
```

The 03:20:29Z release's two containers are exactly the `termini-01m4wjhw…`
instances still RUNNING on node-a; the post-deploy releases leave nothing behind.

## 7. Verification run

```
$ python3 -m unittest tests.test_release_teardown -v
Ran 9 tests in 12.715s
OK

$ python3 -m unittest discover -s tests -t .        # full suite
Ran 975 tests in 388.821s
OK
```

The 9 new tests (all against a real HTTP stub-LXD, i.e. real DELETE calls):
release deletes + capacity back (and the executor's `force=True` stop precedes the
delete), second release deletes nothing twice, `vm_ids_json` NULL →
`unknown_instances: true` with no DELETE issued, unrelated containers untouched,
a per-instance delete failure reported in `failed[]` without aborting the release
(the stuck container stays and is named), an already-gone instance counted as
deleted, reconcile still releasing a commit whose instances are gone (and issuing
no DELETE), reconcile leaving a live plan alone, and
`store.live_allocation_rows` distinguishing NULL from a recorded empty list.

## 8. What is left, stated plainly

- **node-a still carries four containers from plans released before this deploy**
  (`termini-01m4wjhw…-0-0/-0-1`, `termini-01m537km0…-0-0`, `termini-01m537g26…-0-0`),
  and `free -g` still reads `used 4` with `0` committed. This change stops new
  leaks; it cannot retroactively delete containers whose release happened under
  the old code — a release is `already_released` the second time and, by design,
  deletes nothing then. They are left to their owners/operator:
  `sudo lxc delete -f <name>` on node-a once no card is using them as evidence.
  I did not delete another card's containers.
- **`decommission_node` releases capacity only** — it does not tear down the
  instances on the retired node. Out of scope here (the card names the release
  path); worth its own card if retired nodes are expected to be wiped.
- The fleet was left as found apart from this card's own plan: my instance
  deleted, my capacity released, `demo-workload` and the other workers' containers
  untouched, node-b left TERMINATED, no GCP instance created or started.

## Reproduce

```bash
# controller tree under test
infra/gcp/tools/deploy-termini-tree.sh --ref 6964606     # writes /opt/termini/DEPLOYED_COMMIT

# driver used above (runs ON the controller; reads the project token from the unit)
scp live-release-teardown.sh termini-controller:/tmp/
ssh termini-controller 'bash /tmp/live-release-teardown.sh plan-apply'
gcloud compute ssh termini-node-a --tunnel-through-iap --command='sudo lxc list -c ns4'
ssh termini-controller 'bash /tmp/live-release-teardown.sh release'
gcloud compute ssh termini-node-a --tunnel-through-iap --command='sudo lxc list'
ssh termini-controller 'bash /tmp/live-release-teardown.sh unknown'
ssh termini-controller 'bash /tmp/live-release-teardown.sh nodes'

# unit + full suite
python3 -m unittest tests.test_release_teardown -v
python3 -m unittest discover -s tests -t .
```
