# Demo build check — does the DEPLOYED demo serve the §4.3 thinking log?

- **Task:** `t_6c5e9925` (Demo: put the diagnosis thinking log into the build, deck and runbook)
- **Date:** 2026-10-16 (UTC), executed against the live project `termini-510609`
- **Question (card item 3):** which artifact the demo actually runs, whether the
  running revision predates the trace, the exact command to redeploy it, and
  whether "deploying a revision costs nothing when it is not serving traffic"
  holds — checked against the provider's own output, not asserted.
- **Verdict: NO — the deployed demo build predates the thinking log, and the
  controller half cannot be updated without live compute.**

Everything below is either a provider/API response or a hash comparison against
git; the raw outputs are committed beside this file under `raw/`.

---

## 1. What the demo runs

| Half | Artifact | Where it runs | Deployed version |
|---|---|---|---|
| Dashboard | the static bundle in `dashboard/` | **Firebase Hosting** (`termini-510609.web.app`) — static CDN, no compute | `dashboard/` of commit **`6fb58d0`** |
| Controller + seeder | the runtime tree (`controller/ termini/ console/ chaos/ infra/bootstrap/`) at `/opt/termini` | **GCE VM `termini-controller`** (`asia-south1-a`, `e2-standard-2`, reserved IP `34.93.190.64`) | **`6fb58d0df0554740c4db04c1043de3b775ced226`** |
| Managed reference model | Cloud Run service `termini-gemma` (1× NVIDIA L4) | Cloud Run `asia-southeast1` | revision `termini-gemma-00001-ql5` (2026-10-11) |

The controller's own record of its version is `/opt/termini/DEPLOYED_COMMIT`
(written by `infra/gcp/tools/deploy-termini-tree.sh`). The refresh task
`t_4c293d8d` deployed **`6fb58d0`** to both halves and left the fleet RUNNING for
the owner's inspection; its run metadata records the read-back
(`deployed_commit_readback = 6fb58d0…`, 128 files, byte-identical to a fresh
archive; 34 hosting files sha256-equal to the committed `dashboard/`).

`termini-gemma` is **not on the demo's request path at all** — the diagnosis is a
managed-model call made by the controller (provider-only listener), and the
agent-facing front door is Node C's own surface. It is the parked GPU reference,
not the decider.

## 2. Does the running build serve the trace? No — and it is two merges behind

The thinking log shipped in two commits, **both after `6fb58d0`**:

| Commit | PR | What it added |
|---|---|---|
| `6f9defe` | #189 | the decision trace: capture, persist, serve `decision_trace` |
| `5e6bed9` | #191 | the thinking log in the AI panel (§4.3) |

`git merge-base --is-ancestor 6f9defe 6fb58d0` → **false**; the same for
`5e6bed9`. Both are descendants of the deployed build.

Re-verified independently in this run, from the two serving surfaces themselves:

1. **The deployed dashboard bundle is byte-identical to the pre-trace source.**
   Six assets were fetched over HTTPS from `https://termini-510609.web.app` and
   sha256-compared with `git show 6fb58d0:dashboard/<asset>` and
   `git show origin/main:dashboard/<asset>` — every one matches **the deployed
   build**, none matches `main`
   (`raw/deployed-vs-repo-dashboard-assets.txt`). The trace UI is measurably
   absent from the deployed build:

   | file | deployed | `6fb58d0` | `origin/main` |
   |---|---|---|---|
   | `js/ui/decisions.js` | 0768a33756523f1d | 0768a33756523f1d | 91251aa2ebd12cb6 |
   | `js/core/decisions.js` | 221e159bb82ebffe | 221e159bb82ebffe | 798d99d89665d173 |
   | `assets/styles.css` | a58b413822f889e9 | a58b413822f889e9 | 20eebf0b889fa6b6 |
   | `index.html` | ce199a0970bd50e2 | ce199a0970bd50e2 | ce199a0970bd50e2 |
   | `js/offline.js` | 771a23a34e0f4e20 | 771a23a34e0f4e20 | e1d819327bf9a2d7 |
   | `js/main.js` | df0d548996c0e959 | df0d548996c0e959 | 72492a46719e74f4 |

   Occurrences of the trace vocabulary in `dashboard/js/{ui,core}/decisions.js`:
   at `6fb58d0` **0 / 0**; at `origin/main` **4 / 20** (`decided_by`,
   `decision_trace`, `thinking`).

2. **The deployed controller does not serve the trace either.** `GET
   /v1/operator/decisions?limit=5` against `https://34-93-190-64.sslip.io/v1`
   returned rows whose keys are
   `[action_kind, action_params, confidence, decided_at, decision_source, event_id,
   fallback_reason, id, incident_actions, incident_evidence, incident_explanation,
   incident_generated, incident_title, node_id, playbook, reason, rule_id]` —
   `incident_*` is present (that is #186, merged as `6fb58d0`), `fallback_reason`
   is present, and **`decision_trace` and `decided_by` are absent**
   (`raw/deployed-operator-decisions.json`).

So the demo build's own behaviour matches its deployed sha: the incident card
renders, the thinking log cannot.

## 3. The exact commands to redeploy it

Five surfaces exist and fail independently (`deploy-termini-tree.sh` ships the
**controller only**). For the thinking log, two of them matter, and they must
both land — the panel renders "no decision trace" for every row an old
controller serves, so the bundle alone is not enough.

```bash
# 0. the fleet must be UP (see §4 — this is the part that is not free)
gcloud compute instances start termini-controller --zone asia-south1-a
#    poll until the API answers: a 404 on a known route means up-but-no-such-route

# 1. controller tree — API + the decision_trace column's migration (migrations run at startup)
bash infra/gcp/tools/deploy-termini-tree.sh --ref <sha-with-6f9defe-and-5e6bed9>

# 2. dashboard bundle, from the REPO ROOT (firebase.json sets hosting.public: dashboard)
firebase deploy --only hosting --project termini-510609 --non-interactive

# 3. verify per surface (the deploy's exit code is not evidence)
#    controller: decision rows now carry the trace
curl -sS -H "Authorization: Bearer ***" \
  "https://34-93-190-64.sslip.io/v1/operator/decisions?limit=1" | grep -c decision_trace   # expect >= 1
#    dashboard: the fetched asset now differs from the pre-trace hash
curl -sS https://termini-510609.web.app/js/core/decisions.js | sha256sum
#    (expect the `origin/main` hash from §2, e.g. 798d99d89665d173…, not 221e159bb82ebffe…)

# 4. Node C's own daemon and the controller inference surfaces are unchanged by
#    this feature, but if the whole tree moves, deploy those too — see
#    `.hermes/skills/.../references/fleet-deploy.md` for the five-surface check.
```

After step 1 the controller restarts; accepted plans held in memory are dropped,
so do any intent → apply probe after the last restart.

## 4. Does "deploying a revision costs nothing when it is not serving traffic" hold?

**It holds for the dashboard half and NOT for the controller half** — and the
demo needs both.

- **Dashboard (Firebase Hosting) — free.** `firebase deploy --only hosting`
  publishes static assets to a CDN; it provisions no compute. Provider output:
  no Cloud Run service and no VM exists for the site — it is a static host —
  and (documented in the runbook, not re-measured here) a static host answers
  `200` for the whole bundle even with the backend down, which is why a `200`
  there is never evidence the demo is up.
- **Controller (GCE) — NOT free.** The tree is copied to `/opt/termini` on
  `termini-controller` and the unit is restarted there, so the VM must be
  **RUNNING** to receive it. With the fleet TERMINATED (the standing cost guard),
  the controller half **cannot be redeployed without starting compute** — a
  window, not a free revision.
- **Cloud Run (the only Cloud Run service, `termini-gemma`) — not part of the
  demo path.** Its own service spec declares `autoscaling.knative.dev/maxScale:
  '1'` and **no `minScale`**, i.e. Cloud Run's default minimum of zero, and 100%
  of traffic on the latest revision (`raw/gcloud-run-termini-gemma-export.yaml`).
  A revision that receives no traffic therefore raises no instance. I could
  **not** corroborate that with a measured instance count:
  `run.googleapis.com/container/instance_count` returns **0 series for the whole
  project** over the window, while the positive control
  (`compute.googleapis.com/instance/cpu/utilization`) returns **3 series** — so
  the metric is unpopulated here and the scale-to-zero statement rests on the
  service configuration, not on a measurement
  (`raw/cloud-monitoring-cloudrun-instance-count.txt`,
  `raw/cloud-monitoring-positive-control.txt`). Stated as such rather than as a
  measured fact.

**Finding (the one item 3 is meant to surface):** on a TERMINATED fleet the demo
build cannot be made to serve the thinking log by a free deploy. The dashboard
half is free; the controller half needs the VM up. The honest options are (a) a
compute window that starts `termini-controller` (plus the nodes, if the demo is
to run end to end), or (b) demo the trace from the local stack, which is what the
verification task `t_e0d8077f` did and which this card's §4/§5 are gated on.

## 5. Fleet state observed during this check (provider output, not the card's premise)

The card's release condition says the fleet is TERMINATED by the standing cost
guard. It was **not** while this check ran:

```
NAME                ZONE           MACHINE_TYPE   STATUS      LAST_START_TIMESTAMP
termini-controller  asia-south1-a  e2-standard-2  RUNNING     2026-10-16T14:04:30.697-07:00
termini-node-a      asia-south1-a  e2-standard-2  RUNNING     2026-10-16T14:04:30.741-07:00
termini-node-c      asia-south1-a  e2-standard-8  RUNNING     2026-10-16T14:04:29.548-07:00
termini-node-b      asia-south1-a  n2-standard-2  TERMINATED  2026-10-04T04:05:23.560-07:00
```

That is not an accident: the demo refresh task `t_4c293d8d` deliberately left the
fleet RUNNING for the owner's own inspection, and `t_f3e25715` exists to pause it
afterwards. It matters for this card's release condition in one direction only —
the *premise* was stale, but there is **still no approved window recorded on
`t_6c5e9925`**, so items 4–5 were not run (and deploying the trace onto a live
demo the owner may be looking at was not done either). The running state is
reported here because it is a cost fact the operator should see, not because it
unlocks the card.

## 6. Raw evidence index

| File | What it is |
|---|---|
| `raw/gcloud-compute-instances-list.txt` | `gcloud compute instances list` — the fleet's status + last start |
| `raw/deployed-vs-repo-dashboard-assets.txt` | deployed Firebase assets vs `git show 6fb58d0` / `origin/main` |
| `raw/deployed-operator-decisions.json` | the deployed controller's decision rows (no `decision_trace`) |
| `raw/gcloud-run-services-list.txt` | the project's Cloud Run services (one: `termini-gemma`) |
| `raw/gcloud-run-termini-gemma-export.yaml` | `termini-gemma`'s own exported spec (maxScale 1, no minScale, GPU) |
| `raw/gcloud-run-termini-gemma-revisions.txt` | its revisions and image |
| `raw/cloud-monitoring-cloudrun-instance-count.txt` | the Cloud Run instance-count probe: 0 series (inconclusive) |
| `raw/cloud-monitoring-positive-control.txt` | the same probe against a metric that does have data (3 series) |
