# Allocation release — GCP-free acceptance (t_03cd93cd)

Date: 2026-10-15
Worktree: throwaway `git worktree add --detach` of `origin/main` @ `6ac1372`
(`t_03cd93cd/repo`). The shared `/home/ubuntu` checkout was never touched.

This is the controller-only half of t_33c7e847: prove the landed release path
(PR #134 — `controller/app/migrations/0013_allocation_release_node_decommission.sql`,
`store.py`, `server.py`) end-to-end with no GCP and no live node. The release
path is controller + SQLite only (POST /v1/projects/{pid}/intents -> allocation
commit -> release), so a synthetic 30 GiB compute node is sufficient. The
decommission / phantom-OFFLINE-node half still needs real hardware and stays on
t_33c7e847.

## Method

Driven over the real HTTP surface (not by calling `store.*` directly except
where a step explicitly says "write directly"). A synthetic node is registered
with the compute hardware that yields a 30 GiB allocatable RAM ceiling
(8 cores / 32 GiB total -> 32 - 2 reserve = 30 GiB, matching
`tests/test_capacity_clamp.py`). Step 5 exercises the *startup* reconcile by
running `python3 -m app.main` as a real subprocess against a DB that already
contains a stale commit, and observes the self-heal with no release request.

## Result — all 7 acceptance steps PASS

1. 3x10 GiB plan -> 200 `accepted` (30 GiB committed).
2. 3x4 GiB plan -> 422 `GUARDRAIL_REJECTED` with `exceeds_node_ram_ceiling`.
3. `POST /vms/release` -> 200 `released`, 30 GiB returned to the pool.
4. The SAME 3x4 GiB plan -> 200 `accepted`, `reject_reasons` absent.
5. Startup reconcile self-heals a directly-written stale commit
   (reason `instances_gone`) with no request at all.
6. Steps 1-4 looped 3x: committed RAM trace `[0, 0, 0, 0, 0, 0]` — no upward drift.
7. Audit trail survives: 6 commit rows soft-released (NOT deleted), each with
   `released_at` + `release_reason`; 6 operator-log release rows. No DB wipe.

## Raw transcript

```
========================================================================
PART A — steps 1-4, 6, 7 over the real HTTP surface
========================================================================

[setup] synthetic 30 GiB compute node registered: 01M4ZCRFJW7B0PV128BHDNRA60

########################################################################
# CYCLE 1  (steps 1-4)
########################################################################

--- step 1 — 3x10 GiB plan -> HTTP 200 ---
{
  "allocations": [
    {
      "guardrail": {
        "clamped_fields": [],
        "reject_reasons": []
      },
      "index": 0,
      "node_id": "01M4ZCRFJW7B0PV128BHDNRA60",
      "realized_as": "vm",
      "resolved": {
        "count": 3,
        "disk_gb": 8,
        "passthrough_gpu": false,
        "ram_gb": 10,
        "target_node": "auto",
        "type": "agent_vm",
        "vcpus": 2
      },
      "status": "accepted",
      "vm_ids": []
    }
  ],
  "audit": {
    "actor": "human",
    "guardrail": "termini.intent.clamp_validate"
  },
  "created_at": "2026-10-15T09:02:15Z",
  "outcome": "accepted",
  "plan_id": "01M4ZCRFKMMX69XBDQPEMQM5SY",
  "project_id": "01J0DEM0WARM000000000000AB",
  "request_id": "01M4ZCRFKNHB18F6M7QKS306P7"
}
[step 1] committed capacity now: {'cpu_allocated': 6, 'ram_allocated_gb': 30, 'disk_allocated_gb': 24}

--- step 2 — 3x4 GiB plan (saturated node) -> HTTP 422 ---
{
  "error": {
    "code": "GUARDRAIL_REJECTED",
    "details": [
      {
        "detail": "aggregate RAM exceeds the node's free capacity: node 01M4ZCRFJW7B0PV128BHDNRA60 had ram_free_gb=0 GiB free and the request needed 3\u00d72 vCPU / 3\u00d74 GiB RAM (already allocated 6 vCPU / 30 GiB RAM)",
        "index": 0,
        "reason": "exceeds_node_ram_ceiling"
      }
    ],
    "message": "allocation plan rejected by guardrail",
    "request_id": "01M4ZCRFM46TXA85DG7CKZGWX6"
  }
}
[step 2] reject reasons: ['exceeds_node_ram_ceiling']

--- step 3 — POST /vms/release -> HTTP 200 ---
{
  "plan_id": "01M4ZCRFKMMX69XBDQPEMQM5SY",
  "released": {
    "cpu_allocated": 6,
    "disk_allocated_gb": 24,
    "ram_allocated_gb": 30
  },
  "released_at": "2026-10-15T09:02:15Z",
  "rows_released": 1,
  "status": "released"
}
[step 3] committed capacity after release: {'cpu_allocated': 0, 'ram_allocated_gb': 0, 'disk_allocated_gb': 0}

--- step 4 — SAME 3x4 GiB plan (capacity returned) -> HTTP 200 ---
{
  "allocations": [
    {
      "guardrail": {
        "clamped_fields": [],
        "reject_reasons": []
      },
      "index": 0,
      "node_id": "01M4ZCRFJW7B0PV128BHDNRA60",
      "realized_as": "vm",
      "resolved": {
        "count": 3,
        "disk_gb": 8,
        "passthrough_gpu": false,
        "ram_gb": 4,
        "target_node": "auto",
        "type": "agent_vm",
        "vcpus": 2
      },
      "status": "accepted",
      "vm_ids": []
    }
  ],
  "audit": {
    "actor": "human",
    "guardrail": "termini.intent.clamp_validate"
  },
  "created_at": "2026-10-15T09:02:15Z",
  "outcome": "accepted",
  "plan_id": "01M4ZCRFMJ7A7Q11CPQQ2A4189",
  "project_id": "01J0DEM0WARM000000000000AB",
  "request_id": "01M4ZCRFMK8GRG56MMMH1EW9CH"
}
[step 4] reject_reasons: None
[cycle 1] released the step-4 plan; pool back to {'cpu_allocated': 0, 'ram_allocated_gb': 0, 'disk_allocated_gb': 0}

########################################################################
# CYCLE 2  (steps 1-4)
########################################################################

--- step 1 — 3x10 GiB plan -> HTTP 200 ---
{
  "allocations": [
    {
      "guardrail": {
        "clamped_fields": [],
        "reject_reasons": []
      },
      "index": 0,
      "node_id": "01M4ZCRFJW7B0PV128BHDNRA60",
      "realized_as": "vm",
      "resolved": {
        "count": 3,
        "disk_gb": 8,
        "passthrough_gpu": false,
        "ram_gb": 10,
        "target_node": "auto",
        "type": "agent_vm",
        "vcpus": 2
      },
      "status": "accepted",
      "vm_ids": []
    }
  ],
  "audit": {
    "actor": "human",
    "guardrail": "termini.intent.clamp_validate"
  },
  "created_at": "2026-10-15T09:02:15Z",
  "outcome": "accepted",
  "plan_id": "01M4ZCRFNKG6N2J45PE7F58P59",
  "project_id": "01J0DEM0WARM000000000000AB",
  "request_id": "01M4ZCRFNM5WBH7EN25Z4EPT6G"
}
[step 1] committed capacity now: {'cpu_allocated': 6, 'ram_allocated_gb': 30, 'disk_allocated_gb': 24}

--- step 2 — 3x4 GiB plan (saturated node) -> HTTP 422 ---
{
  "error": {
    "code": "GUARDRAIL_REJECTED",
    "details": [
      {
        "detail": "aggregate RAM exceeds the node's free capacity: node 01M4ZCRFJW7B0PV128BHDNRA60 had ram_free_gb=0 GiB free and the request needed 3\u00d72 vCPU / 3\u00d74 GiB RAM (already allocated 6 vCPU / 30 GiB RAM)",
        "index": 0,
        "reason": "exceeds_node_ram_ceiling"
      }
    ],
    "message": "allocation plan rejected by guardrail",
    "request_id": "01M4ZCRFP4SCRBZ9W4VXTD3886"
  }
}
[step 2] reject reasons: ['exceeds_node_ram_ceiling']

--- step 3 — POST /vms/release -> HTTP 200 ---
{
  "plan_id": "01M4ZCRFNKG6N2J45PE7F58P59",
  "released": {
    "cpu_allocated": 6,
    "disk_allocated_gb": 24,
    "ram_allocated_gb": 30
  },
  "released_at": "2026-10-15T09:02:15Z",
  "rows_released": 1,
  "status": "released"
}
[step 3] committed capacity after release: {'cpu_allocated': 0, 'ram_allocated_gb': 0, 'disk_allocated_gb': 0}

--- step 4 — SAME 3x4 GiB plan (capacity returned) -> HTTP 200 ---
{
  "allocations": [
    {
      "guardrail": {
        "clamped_fields": [],
        "reject_reasons": []
      },
      "index": 0,
      "node_id": "01M4ZCRFJW7B0PV128BHDNRA60",
      "realized_as": "vm",
      "resolved": {
        "count": 3,
        "disk_gb": 8,
        "passthrough_gpu": false,
        "ram_gb": 4,
        "target_node": "auto",
        "type": "agent_vm",
        "vcpus": 2
      },
      "status": "accepted",
      "vm_ids": []
    }
  ],
  "audit": {
    "actor": "human",
    "guardrail": "termini.intent.clamp_validate"
  },
  "created_at": "2026-10-15T09:02:15Z",
  "outcome": "accepted",
  "plan_id": "01M4ZCRFPNEKBKJK1GA2V5HW4G",
  "project_id": "01J0DEM0WARM000000000000AB",
  "request_id": "01M4ZCRFPPHJV8GB9DJD7N0NV2"
}
[step 4] reject_reasons: None
[cycle 2] released the step-4 plan; pool back to {'cpu_allocated': 0, 'ram_allocated_gb': 0, 'disk_allocated_gb': 0}

########################################################################
# CYCLE 3  (steps 1-4)
########################################################################

--- step 1 — 3x10 GiB plan -> HTTP 200 ---
{
  "allocations": [
    {
      "guardrail": {
        "clamped_fields": [],
        "reject_reasons": []
      },
      "index": 0,
      "node_id": "01M4ZCRFJW7B0PV128BHDNRA60",
      "realized_as": "vm",
      "resolved": {
        "count": 3,
        "disk_gb": 8,
        "passthrough_gpu": false,
        "ram_gb": 10,
        "target_node": "auto",
        "type": "agent_vm",
        "vcpus": 2
      },
      "status": "accepted",
      "vm_ids": []
    }
  ],
  "audit": {
    "actor": "human",
    "guardrail": "termini.intent.clamp_validate"
  },
  "created_at": "2026-10-15T09:02:15Z",
  "outcome": "accepted",
  "plan_id": "01M4ZCRFQJRS22NNE6NC82Z4MX",
  "project_id": "01J0DEM0WARM000000000000AB",
  "request_id": "01M4ZCRFQK2VJ0ZHBF5726PC6H"
}
[step 1] committed capacity now: {'cpu_allocated': 6, 'ram_allocated_gb': 30, 'disk_allocated_gb': 24}

--- step 2 — 3x4 GiB plan (saturated node) -> HTTP 422 ---
{
  "error": {
    "code": "GUARDRAIL_REJECTED",
    "details": [
      {
        "detail": "aggregate RAM exceeds the node's free capacity: node 01M4ZCRFJW7B0PV128BHDNRA60 had ram_free_gb=0 GiB free and the request needed 3\u00d72 vCPU / 3\u00d74 GiB RAM (already allocated 6 vCPU / 30 GiB RAM)",
        "index": 0,
        "reason": "exceeds_node_ram_ceiling"
      }
    ],
    "message": "allocation plan rejected by guardrail",
    "request_id": "01M4ZCRFR52RK57SV1FCD2NT9H"
  }
}
[step 2] reject reasons: ['exceeds_node_ram_ceiling']

--- step 3 — POST /vms/release -> HTTP 200 ---
{
  "plan_id": "01M4ZCRFQJRS22NNE6NC82Z4MX",
  "released": {
    "cpu_allocated": 6,
    "disk_allocated_gb": 24,
    "ram_allocated_gb": 30
  },
  "released_at": "2026-10-15T09:02:15Z",
  "rows_released": 1,
  "status": "released"
}
[step 3] committed capacity after release: {'cpu_allocated': 0, 'ram_allocated_gb': 0, 'disk_allocated_gb': 0}

--- step 4 — SAME 3x4 GiB plan (capacity returned) -> HTTP 200 ---
{
  "allocations": [
    {
      "guardrail": {
        "clamped_fields": [],
        "reject_reasons": []
      },
      "index": 0,
      "node_id": "01M4ZCRFJW7B0PV128BHDNRA60",
      "realized_as": "vm",
      "resolved": {
        "count": 3,
        "disk_gb": 8,
        "passthrough_gpu": false,
        "ram_gb": 4,
        "target_node": "auto",
        "type": "agent_vm",
        "vcpus": 2
      },
      "status": "accepted",
      "vm_ids": []
    }
  ],
  "audit": {
    "actor": "human",
    "guardrail": "termini.intent.clamp_validate"
  },
  "created_at": "2026-10-15T09:02:15Z",
  "outcome": "accepted",
  "plan_id": "01M4ZCRFRTJNSNCQ19DBQXSTW6",
  "project_id": "01J0DEM0WARM000000000000AB",
  "request_id": "01M4ZCRFRVVD8MNNJW33RQRVNT"
}
[step 4] reject_reasons: None
[cycle 3] released the step-4 plan; pool back to {'cpu_allocated': 0, 'ram_allocated_gb': 0, 'disk_allocated_gb': 0}

########################################################################
# STEP 6 — capacity drift check (GiB RAM committed at each cycle boundary)
########################################################################
trace (start, after cycle 1/2/3): [0, 0, 0, 0, 0, 0]
[step 6] capacity did NOT drift monotonically upward — back to 0 after every cycle

########################################################################
# STEP 7 — audit trail survives the recovery (no DB wipe)
########################################################################

vm_allocations rows (6 total — soft-released, NOT deleted):
  plan=01M4ZCRFKMMX69XBDQPEMQM5SY ram=30GiB created=2026-10-15T09:02:15Z released_at=2026-10-15T09:02:15Z reason=stale_demo_state
  plan=01M4ZCRFMJ7A7Q11CPQQ2A4189 ram=12GiB created=2026-10-15T09:02:15Z released_at=2026-10-15T09:02:15Z reason=cycle_cleanup
  plan=01M4ZCRFNKG6N2J45PE7F58P59 ram=30GiB created=2026-10-15T09:02:15Z released_at=2026-10-15T09:02:15Z reason=stale_demo_state
  plan=01M4ZCRFPNEKBKJK1GA2V5HW4G ram=12GiB created=2026-10-15T09:02:15Z released_at=2026-10-15T09:02:15Z reason=cycle_cleanup
  plan=01M4ZCRFQJRS22NNE6NC82Z4MX ram=30GiB created=2026-10-15T09:02:15Z released_at=2026-10-15T09:02:15Z reason=stale_demo_state
  plan=01M4ZCRFRTJNSNCQ19DBQXSTW6 ram=12GiB created=2026-10-15T09:02:15Z released_at=2026-10-15T09:02:15Z reason=cycle_cleanup

[step 7] all 6 commit rows survived as audit records (released_at + release_reason set; nothing deleted)

operator_logs (6 rows; 6 release rows):
  [2026-10-15T09:02:15.173Z] info (human) released plan 01M4ZCRFKMMX69XBDQPEMQM5SY: 30 GiB RAM, 6 vCPU returned to the pool
  [2026-10-15T09:02:15.200Z] info (human) released plan 01M4ZCRFMJ7A7Q11CPQQ2A4189: 12 GiB RAM, 6 vCPU returned to the pool
  [2026-10-15T09:02:15.237Z] info (human) released plan 01M4ZCRFNKG6N2J45PE7F58P59: 30 GiB RAM, 6 vCPU returned to the pool
  [2026-10-15T09:02:15.266Z] info (human) released plan 01M4ZCRFPNEKBKJK1GA2V5HW4G: 12 GiB RAM, 6 vCPU returned to the pool
  [2026-10-15T09:02:15.302Z] info (human) released plan 01M4ZCRFQJRS22NNE6NC82Z4MX: 30 GiB RAM, 6 vCPU returned to the pool
  [2026-10-15T09:02:15.339Z] info (human) released plan 01M4ZCRFRTJNSNCQ19DBQXSTW6: 12 GiB RAM, 6 vCPU returned to the pool

[step 7] the release is as observable as the commit: 6 operator-log rows + release_reason on every surviving commit row

PART A complete: steps 1-4, 6, 7 all PASSED

========================================================================
PART B — step 5: startup reconcile self-heals a stale commit (no request)
========================================================================

[step 5] wrote a stale commit directly: plan=STALE000000000000000000001 ram=8GiB with recorded instance 'termini-stale-0-0' (absent from LXD)
[step 5] committed capacity BEFORE restart: {'cpu_allocated': 4, 'ram_allocated_gb': 8, 'disk_allocated_gb': 16}
[step 5] controller restarted as subprocess pid=3109304 on 127.0.0.1:35681

--- controller startup log (stdout+stderr) ---
2026-10-15 09:02:16,128 WARNING termini.controller no LXD client identity — operator LXD calls will be unauthenticated
2026-10-15 09:02:16,130 INFO termini.controller inventory loaded: 3 node(s), ssh=ubuntu@:22
2026-10-15 09:02:16,130 INFO termini.controller SSH CA not configured (POST /ssh-key and the SSH terminal backend disabled)
2026-10-15 09:02:16,131 INFO termini.controller controller listening on 127.0.0.1:35681 (db=/home/ubuntu/.hermes/profiles/controller/cache/scratch/termini-startup-reconcile-jlwigr2w/controller.db)
2026-10-15 09:02:16,145 INFO termini.controller startup reconcile released 1 stale allocation(s): STALE000000000000000000001(instances_gone)

--- end controller log ---

[step 5] committed capacity AFTER restart: {'cpu_allocated': 0, 'ram_allocated_gb': 0, 'disk_allocated_gb': 0}
[step 5] released_at/release_reason on the stale row: ('2026-10-15T09:02:16Z', 'instances_gone')
[step 5] live allocations remaining: 0
[step 5] startup reconcile self-healed the stale commit with NO release request

PART B complete: step 5 PASSED

========================================================================
ALL 7 ACCEPTANCE STEPS PASSED (GCP-free)
========================================================================
```

## Audit / decision row for the release

The release is as observable as the commit:

- **Commit** writes a `vm_allocations` row (`created_at`, committed cpu/ram/disk).
- **Release** (soft) writes `released_at` + `release_reason` on that same
  surviving row — the row is never deleted — and appends an operator-log entry
  (`"released plan <id>: 30 GiB RAM, 6 vCPU returned to the pool"`).

Both are visible in the transcript above (step 7 dumps all six surviving
`vm_allocations` rows and all six `operator_logs` release rows). Deleting
`/data/controller.db` is therefore not the recovery path and not used: recovery
is a soft release that preserves the decision history.

## Companion verification

- `python3 -m unittest tests.test_allocation_release -v` -> `Ran 15 tests ... OK`
  (the landed PR #134 regression suite).
- `python3 -m unittest discover -s tests` -> `Ran 624 tests in 90.734s ... OK`
  (full suite green on `origin/main`).

## No live node required

No GCP resource, LXD daemon, or live node was used. Step 5's reconcile existence
probe targets a local `StubLxd` HTTP server (the same stub the regression suite
uses) so absence of the stale instances can be *proven* rather than assumed. The
decommission route and the phantom-OFFLINE-node self-heal are intentionally NOT
claimed here; they remain on t_33c7e847 pending live hardware.
