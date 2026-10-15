# Cleanup verification verdict — `teardown.sh --full` removes every billable resource

* **Kanban card**: `t_6208533e` (verifier) — "Adversarial verification: prove NO billable
  resource survives a full cleanup".
* **Parent**: `t_59831eb3` (infra) — full cleanup + explicit pause mode.
* **Revision under test**: `c305c272bdb96ef839904caf65d493ca9aedd951` (merge of PR #154,
  `origin/main`). All commands below were run from a throwaway worktree pinned to that
  commit (`git rev-parse HEAD` → `c305c27…`; the two scripts' blobs verified against the
  commit via `git ls-files -s`). Nothing was run against a working tree.
* **Project**: `termini-510609`, primary region `asia-south1` / zone `asia-south1-a`;
  Cloud Run + Artifact Registry + the model bucket live in `asia-southeast1` (the
  cross-region trap).
* **Runner**: `termini@termini-510609.iam.gserviceaccount.com`, gcloud SDK 588.0.0,
  bash 5.2.21, jq 1.7, shellcheck 0.9.0, 2026-10-15 (UTC).

This verdict was produced independently of the implementer's summary and the
implementer's own evidence file. Every enumerator run was executed by me; every
enumerator result was corroborated by an independent `gcloud`/`gcloud asset` read.

## 1. Instruments used (and which cross-check each other)

| # | Instrument | What it proves | Used? |
|---|---|---|---|
| 1 | `infra/gcp/tools/cleanup-verify.sh` (from the landed commit, run by me) | what is still enumerated, per class, with sizes | yes — pre + post |
| 2 | Cloud Asset inventory `gcloud asset search-all-resources` | independent project-wide census | yes — full census, counted by assetType |
| 3 | Per-service API enumeration (compute/storage/run/artifacts/logging/secretmanager/kms/pubsub/scheduler/firestore/vertex/firebase) | service-by-service ground truth | yes — all classes |
| 4 | THE BILL (Cloud Billing Reports / cost table / BigQuery export) | that no line item bills (usage 0) | **INACCESSIBLE — see §5** |

Instruments 1–3 agree with each other on every class below. Instrument 4 could not be
read; that is reported as a COULD NOT VERIFY, never as "clean".

## 2. Positive control — the enumerator reports hits, not a silent zero

Unscoped run against the current project (instrument 1) found the whole billable
surface and exited 1:

```
VERDICT: FAIL — 17 billable resource(s) `teardown.sh --full` must remove are still present
EXIT=1
```

Rows: 4 instances (controller + node-a/b/c), 4 disks, 1 image (`termini-node-os-5`,
family `termini-node-os`), 4 addresses (1 external `termini-controller-ip` IN_USE + 3
internal), 1 bucket `gs://termini-510609-models` (10.37 GiB, 17 objects), 1 run service
+ 1 revision, 1 empty Artifact Registry repo, plus 1 pending log bucket, 1 opt-in quota
preference, 3 non-billable info rows. The same surface is corroborated by the Cloud
Asset census (§3). A zero-hit result from an enumerator that was never seen to report a
hit proves nothing — this run shows the enumerator does report hits.

## 3. Cloud Asset census (instrument 2), counted by assetType

```
Address 4 · Disk 4 · Image 1 · Instance 4 · InstanceSettings 3 · FirebaseProject 1 ·
LogBucket 2 · LogSink 2 · Network 2 · Repository 1 · Revision 1 · ServiceAccount 4 ·
ServiceAccountKey 2 · Service(run) 1 · Bucket 1 · QuotaPreference 2 · Key(apikeys) 1 ·
Route 45 · Subnetwork 43 · Firewall 12 · Project 1 · ProjectBillingInfo 1 · Service 52
```

Notes: the 45 routes / 43 subnets / 12 firewalls are the auto-provisioned default VPC
in every region — free, retained by `--include-network` default OFF. The 2
ServiceAccountKeys and the 1 apikeys Key are **not enumerated by cleanup-verify.sh**
and not deleted by `teardown.sh`; they are not billable (see §6). The 1 FirebaseProject
exists but its billable stores (Firestore / Realtime Database) are **not enabled**
(`gcloud firestore databases list` → API disabled), so it contributes no billable line
item. The LogBucket census shows only `_Default` + `_Required`; the third bucket
(`termini-verify-logbucket`, DELETE_REQUESTED) appears in `gcloud logging buckets list`
but is no longer indexed by Cloud Asset — see §6.

## 4. Throwaway end-to-end — every adversarial case, `termini-verify-t62-*`

A fresh set was created to exercise the specific failure modes the card names, all
cheap (e2-micro, 10 GiB) and all deleted by the end of the run:

| Adversarial case | Resource created | Pre-check | After `--full` |
|---|---|---|---|
| TERMINATED VM never deleted + its disk | `termini-verify-t62-node-b` (stopped, IN_USE IP attached) | enumerated | deleted |
| RUNNING instance + boot disk | `termini-verify-t62-node-a` | enumerated | deleted (boot disk auto-deleted with instance) |
| disk orphaned / left behind (0 users, never attached) | `termini-verify-t62-orphan` | enumerated (0 user(s)) | deleted |
| image family that still resolves | `termini-verify-t62-os-1`, `-os-2` (family `termini-verify-t62-os`) | enumerated | deleted; `describe-from-family` → NOT_FOUND (rc 1) |
| reserved static IP with no user | `termini-verify-t62-ip-a` (RESERVED, 0 users) | enumerated | deleted |
| static IP still associated with a stopped instance | `termini-verify-t62-ip-b` (IN_USE, 1 user = node-b) | enumerated | deleted (instance first, then address) |
| Artifact Registry repo present-but-empty (0 B) | `termini-verify-t62-repo` (asia-southeast1) | enumerated (0.000MB) | deleted |
| cross-region GCS bucket + object | `gs://termini-verify-t62-models` (asia-southeast1, 1 object) | enumerated (16 B, 1 object) | deleted |
| Cloud Run revision outlives its service + container image behind it | not created (needs a built image; docker absent) | — | **not live-tested** — see §5 |

Full transcript: pre-check enumerated **11 in-remit resources, exit 1**; then

```
$ infra/gcp/teardown.sh --full --project termini-510609 --region asia-south1 --zone asia-south1-a --name-filter '^termini-verify-t62-'
  … deleted: instance node-a, instance node-b, disk orphan,
            address ip-a, address ip-b, image os-1, image os-2,
            artifacts.repository t62-repo, storage.bucket t62-models (+1 object)
  post-check: PASS — no removable billable resource of any enumerated class is present in filter scope
  FULL_EXIT=0
```

Independent raw counts (instrument 3, the enumerator is not allowed to grade its own
homework):

```
instances=0 disks=0 images=0 addresses=0 buckets=0 repos=0
family_rc=1   # '…/global/images/family/termini-verify-t62-os' was not found
```

Cloud Asset query for `termini-verify-t62` after the run returns nothing. The demo
footprint was verified untouched after every run (§7).

## 5. COULD NOT VERIFY — with the exact command and why

1. **THE BILL (usage 0).** The service account has no billing read access and there is
   no BigQuery billing-export dataset in this project:
   ```
   $ gcloud billing accounts list
   Listed 0 items.
   $ gcloud billing accounts describe 01571C-641C3D-631C4C
   ERROR: … does not have permission to access billingAccounts instance
          [01571C-641C3D-631C4C] (or it may not exist) …
   $ bq ls --project_id=termini-510609
   (no datasets listed)
   ```
   Therefore the "no billable line item remains / usage 0" claim is **UNVERIFIED**, not
   clean. Every other instrument proves "nothing is enumerated", which is necessary but
   not sufficient; only the bill proves "nothing bills", and I could not read it. The
   current project's idle spend ($35.37/mo by the parent's measurement: disks 210 GiB
   $25.20 + image 20 GiB $1.20 + static IP $8.76 + bucket $0.21) is the retained demo
   footprint, held **by decision** until 6 Nov 2026 — not a cleanup defect.

2. **Log-bucket sizes.** Still no size instrument. `gcloud logging buckets describe
   _Required --location=global --format=json` returns only `{name, retentionDays,
   locked, lifecycleState, description}` — no size field. `_Required` is 400-day
   retention, `locked: true`. The enumerator prints `size=unknown` (honest) rather than
   a fabricated 0. This is unchanged after the fix.

3. **Cloud Run service+revision live delete path.** Not exercised on a throwaway
   service (deploying one needs a container image build; docker is not installed on
   this host). The live `termini-gemma` service's container image is
   `us-docker.pkg.dev/vertex-ai/vertex-vision-model-garden-dockers/pytorch-vllm-serve:gemma4`
   — a Google-owned **public** image — and the project's only Artifact Registry repo
   `termini` is empty (0.000MB, `gcloud artifacts docker images list` → none). So there
   is no billable container image in this project; the revision itself is free. The
   service/revision delete path is covered by the real-project `--dry-run` (which names
   `run.service termini-gemma` + its revision as WOULD-delete) and by the live AR-repo
   delete path exercised in §4; the drain-if-non-empty branch (`gcloud artifacts docker
   images delete` loop) was reviewed but not exercised live against a non-empty repo.

## 6. Falsification of the script itself

* Static: `bash -n` clean on both scripts; `shellcheck -S warning` emits **no**
  findings. No `|| true` anywhere. Every `>/dev/null 2>&1` / `2>&1` is either a
  capture-then-check (stderr classified by `state()`) or an exit-code check
  (`if ! curl … ; then SURVIVORS+=…`), never a swallow. `state()` routes
  PERMISSION_DENIED / SERVICE_DISABLED / malformed-zone / quota / backend errors to
  `exit 1`, and only NOT_FOUND to "absent".
* The survivor detector firing on a real delete failure is on record from the parent's
  round-1 evidence (`cleanup-2026-10-15.md` §7): a log bucket whose delete left it
  present produced `SURVIVED … still present after delete` and `EXIT=2`. I reproduced
  the refusal arms live (below); I did not re-create a retained-log bucket to avoid
  leaving a second API-pinned DELETE_REQUESTED bucket (guardrail: leave no leftovers).

Falsification battery (run by me, on the landed commit):

```
F1 wrong zone inside right region (asia-south1-b)          → EXIT=1, names the real zone
F2 unreachable project (no-such-project-12345)             → EXIT=1, permission error (not "absent")
F3 REAL --full against the protected demo footprint        → EXIT=1, refuses, names all 14
                                                             protected resources, deletes nothing
```

F3 is the key "success path cannot be silent" proof: pointed at resources that DO
exist, `--full` exits non-zero and names them rather than reporting a clean teardown.

## 7. Guardrails

* Demo footprint verified intact after every run (raw `gcloud`): 4 instances
  (controller, node-a/b/c), 4 disks (30/60/60/60 GiB), image `termini-node-os-5`
  (family `termini-node-os`), address `termini-controller-ip` 34.93.190.64 IN_USE +
  3 internal addresses, `gs://termini-510609-models`, run service `termini-gemma`, AR
  repo `termini`. Nothing was deleted outside `^termini-verify-t62-`.
* No `termini-verify-t62-*` resource of any class remains (Cloud Asset query empty).
* `termini-node-c` was RUNNING (`e2-standard-8`) at the start of this run and
  TERMINATED at the end — a concurrent change by another workstream, not by this card
  (my only mutating command was name-filtered to `^termini-verify-t62-`; F1/F2/F3 all
  exited during preflight without mutating).

## 8. Per-class verdict table (throwaway set, filter `^termini-verify-t62-`)

| Resource class | Instruments | before | after | Verdict |
|---|---|---|---|---|
| compute.instance | enumerator + `gcloud compute instances list` | 2 | 0 | PASS |
| compute.disk | enumerator + `gcloud compute disks list` | 3 (2 attached, 1 orphan) | 0 | PASS |
| compute.image | enumerator + `gcloud compute images list` + `describe-from-family` | 2 (family) | 0, family NOT_FOUND | PASS |
| compute.address | enumerator + `gcloud compute addresses list` | 2 (RESERVED + IN_USE) | 0 | PASS |
| storage.bucket | enumerator + `gcloud storage buckets list` | 1 (+1 object) | 0 | PASS |
| artifacts.repository | enumerator + `gcloud artifacts repositories list` | 1 (empty) | 0 | PASS |
| compute.snapshot / machine-image / instance-template / reservation / forwarding-rule | enumerator + gcloud list | 0 | 0 | PASS (none existed) |
| run.service / run.revision | enumerator + `gcloud run … list` | 0 throwaway | 0 | NOT live-tested (see §5.3) |
| logging.bucket | enumerator + `gcloud logging buckets list` | 0 throwaway | 0 | PASS; size COULD NOT VERIFY (§5.2) |

## 9. What survives a real `--full`, and who owns it

* **Protected demo footprint** (the only billable resources a real `--full` would leave):
  4 disks (210 GiB), 1 image (20 GiB), 1 external IP (`termini-controller-ip`
  34.93.190.64), 1 bucket (10.37 GiB), 1 run service (public image, min=0), 1 empty AR
  repo. Owner: **user decision** — retained until 6 Nov 2026; `--full` refuses to
  delete them (F3). This is not a cleanup failure.
* **`termini-verify-logbucket`** (DELETE_REQUESTED, 1-day retention): leftover from
  `t_59831eb3`'s own round-1 testing. The Logging API pins a bucket with retained logs
  until its retention window closes; no API lever removes it sooner, and it accepts no
  further logs (bills $0 going forward).
* **Google-managed log buckets** `_Default` (30 d) and `_Required` (400 d, locked):
  not deletable via the API; bill only beyond the 50 GiB free tier.
* **Non-billable, not a cleanup concern**: 2 service-account keys, 2 quota preferences
  (opt-in `--delete-quota-preferences`), 1 Firebase project (Firestore disabled), 1 API
  key, the default VPC's routes/subnets/firewalls, 2 log sinks, 3 instance settings.

## 10. Verdict

**PASS — cleanup completeness, on the throwaway set.** Every billable class
`teardown.sh --full` claims to remove is corroborated by at least two independent
instruments at zero after the run (enumerator post-check exit 0 + raw `gcloud` counts 0
+ image-family NOT_FOUND), and the pre-check/positive-control run proves the enumerator
reports hits rather than a silent zero. The falsification battery (F1/F2/F3) shows the
script exits non-zero and names the defect whenever its success path cannot legitimately
run, and no swallowed error or `|| true` exists in the source.

**FAIL-PARTIAL — the "nothing bills" claim.** The bill instrument (Cloud Billing
Reports / cost table / BigQuery export) is inaccessible to the verifier service account
(permission denied; no BigQuery export dataset in the project — exact commands in §5.1),
so usage=0 cannot be shown. This is a measurement-access limitation, not evidence of
surviving usage; the idle spend that does exist is the retained demo footprint held by
decision. Log-bucket sizes remain unverifiable (no size instrument, §5.2), and the
Cloud Run service+revision delete path was not exercised live on a throwaway service
(§5.3).

*Claims not run by me:* the live demo raise+pause cycle (deferred by the parent's
freeze), a live non-empty Artifact Registry drain, and a live throwaway Cloud Run
service deletion. All are called out explicitly above rather than recorded as clean.
