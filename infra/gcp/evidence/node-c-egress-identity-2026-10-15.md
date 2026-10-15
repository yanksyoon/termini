# Node C egress + least-privilege identity for Google AI (task `t_5a4a0f73`)

Date: 2026-10-15 · Owner: `infra` · Status: **PASS** (one environmental finding, §7)

This is the hard prerequisite for the simulated-GPU inference proxy
(`t_f466824c`): before it, every node in the footprint had **no path to any
Google API at all**, so the first thing an inference call would have hit was a
DNS/TCP failure — not an IAM failure, and not a credential failure.

Everything below is raw tool output, captured on 2026-10-15 against the live
project `termini-510609` (region `asia-south1`, zone `asia-south1-a`).

---

## 1. What was actually wrong

| Layer | State before | Consequence |
|-------|--------------|-------------|
| Data path | subnet `termini-underlay` had `privateIpGoogleAccess=False`; nodes run `--no-address`; project has no Cloud Router/Cloud NAT | no route to any Google API from any node |
| Identity | `termini-node-c` ran as the **default compute SA** `667736937184-compute@developer.gserviceaccount.com` (project role `roles/editor`) | far more privilege than an inference host needs |
| OAuth scope | that instance carried 7 scopes: `devstorage.read_only`, `logging.write`, `monitoring.write`, `pubsub`, `service.management.readonly`, `servicecontrol`, `trace.append` — **no `cloud-platform`** | the metadata-server token was structurally incapable of calling Vertex AI regardless of IAM |

Both halves of the identity problem had to be fixed at once: IAM grants
permission, the **scope** is what lets the metadata server mint a token that can
exercise it. Fixing only the SA would have produced a confusing 403 from Vertex
AI; fixing only the scope would have left `roles/editor` attached to a node that
only needs to call one API.

Raw "before" state:

```
$ gcloud compute networks subnets describe termini-underlay --region=asia-south1 \
    --format='value(privateIpGoogleAccess,privateIpv6GoogleAccess)'
False	DISABLE_GOOGLE_ACCESS

$ gcloud compute routers list
Listed 0 items.

$ gcloud compute instances describe termini-node-c --zone=asia-south1-a \
    --format='yaml(name,status,serviceAccounts,networkInterfaces)'
name: termini-node-c
networkInterfaces:
- fingerprint: KDi5Y9mGG4U=
  kind: compute#networkInterface
  name: nic0
  network: https://www.googleapis.com/compute/v1/projects/termini-510609/global/networks/termini-vpc
  networkIP: 10.10.0.13
  stackType: IPV4_ONLY
  subnetwork: https://www.googleapis.com/compute/v1/projects/termini-510609/regions/asia-south1/subnetworks/termini-underlay
serviceAccounts:
- email: 667736937184-compute@developer.gserviceaccount.com
  scopes:
  - https://www.googleapis.com/auth/devstorage.read_only
  - https://www.googleapis.com/auth/logging.write
  - https://www.googleapis.com/auth/monitoring.write
  - https://www.googleapis.com/auth/pubsub
  - https://www.googleapis.com/auth/service.management.readonly
  - https://www.googleapis.com/auth/servicecontrol
  - https://www.googleapis.com/auth/trace.append
status: TERMINATED
```

---

## 2. Fix 1 — Private Google Access on the underlay subnet (free)

```
$ gcloud compute networks subnets update termini-underlay --region=asia-south1 \
    --enable-private-ip-google-access
Updated [https://www.googleapis.com/compute/v1/projects/termini-510609/regions/asia-south1/subnetworks/termini-underlay].

$ gcloud compute networks subnets describe termini-underlay --region=asia-south1 \
    --format='value(privateIpGoogleAccess,privateIpv6GoogleAccess)'
True	DISABLE_GOOGLE_ACCESS
```

`privateIpv6GoogleAccess` is deliberately left at `DISABLE_GOOGLE_ACCESS`: the
underlay is `IPV4_ONLY`, so the IPv6 knob changes nothing today and flipping it
would widen the egress surface for no gain.

### The choice: Private Google Access, **not** Cloud NAT

Recorded here because the task asked for the decision *and* the cost comparison.

| Option | Cost | What it does for Google API traffic |
|--------|------|-------------------------------------|
| **Private Google Access** (chosen) | **$0/h, $0/GiB** | routes Google API VIP traffic over Google's network from the node's private address — exactly the requirement |
| Cloud Router + Cloud NAT (rejected) | ≤32 instances: **$0.0014 × instances/hour** + **$0.005/hour** for the NAT's external IP + **$0.045/GiB processed** (both directions). The commonly quoted `$0.044/h` is only the >32-instance **cap** | Google-API traffic reached through PGA **never touches the NAT meter** — [Google's own pricing page](https://cloud.google.com/nat/pricing) states the per-GiB data-processing charge does not apply to traffic that goes via Private Google Access or Private Service Connect |

So for this footprint NAT is not a cheaper alternative to PGA, it is **pure
overhead on top of it**: the 3-node demo would pay ~$0.0092/h (~$6.7/month)
always-on plus $0.045/GiB for the *non-Google* internet egress it does not need,
and it would still have to enable PGA to reach Google APIs without an external
IP. No Cloud Router and no Cloud NAT were created; confirmed after the change:

```
$ gcloud compute routers list
Listed 0 items.
```

### IaC: the flag is now encoded, not just flipped

`infra/gcp/main.tf` (`google_compute_subnetwork.underlay`) now carries
`private_ip_google_access = true` with the rationale in a comment, so a future
`terraform apply` cannot silently revert the live setting — the failure mode
this card exists to prevent.

---

## 3. Fix 2 — least-privilege identity for the inference node

```
$ gcloud iam service-accounts create termini-node-inference \
    --display-name="Termini inference node (Node C)" \
    --description="Least-privilege identity for the Node C inference host (Vertex AI user + log/metric writer). Created by t_5a4a0f73."
Created service account [termini-node-inference].
Service account email: termini-node-inference@termini-510609.iam.gserviceaccount.com

$ gcloud projects get-iam-policy termini-510609 --flatten='bindings[].members' \
    --filter='bindings.members:termini-node-inference@' --format='table(bindings.role,bindings.members)'
ROLE                           MEMBERS
roles/aiplatform.user          serviceAccount:termini-node-inference@termini-510609.iam.gserviceaccount.com
roles/logging.logWriter        serviceAccount:termini-node-inference@termini-510609.iam.gserviceaccount.com
roles/monitoring.metricWriter  serviceAccount:termini-node-inference@termini-510609.iam.gserviceaccount.com
```

Attached to node-c, replacing the project-default `roles/editor` SA:

```
$ gcloud compute instances set-service-account termini-node-c --zone=asia-south1-a \
    --service-account=termini-node-inference@termini-510609.iam.gserviceaccount.com \
    --scopes=cloud-platform
Updated [https://www.googleapis.com/compute/v1/projects/termini-510609/zones/asia-south1-a/instances/termini-node-c].

$ gcloud compute instances describe termini-node-c --zone=asia-south1-a \
    --format='yaml(name,status,serviceAccounts)'
name: termini-node-c
serviceAccounts:
- email: termini-node-inference@termini-510609.iam.gserviceaccount.com
  scopes:
  - https://www.googleapis.com/auth/cloud-platform
status: TERMINATED
```

### "It works while TERMINATED, and the boot disk is not rebuilt" — verified, not assumed

`set-service-account` only rewrites instance metadata, so it does not require the
instance to be recreated. Proved by comparing the instance and disk identity
before and after the change:

```
$ gcloud compute instances describe termini-node-c --zone=asia-south1-a --format='value(id,machineType.basename(),status)'
584752554767002517	n2-standard-8	TERMINATED          # before the identity change
584752554767002517	e2-standard-8	RUNNING             # after (see §7 for the machine-type note)

$ gcloud compute disks describe termini-node-c --zone=asia-south1-a --format='value(id,name,sizeGb,creationTimestamp)'
4704597953751526293	termini-node-c	60	2026-10-13T04:26:50.791-07:00    # identical before and after
```

The instance `id` and the boot disk `id` + `creationTimestamp` are unchanged: no
recreate, no image re-bake, no lost local state (the model weights and llama.cpp
build on node-c survive).

### A recreate now keeps the identity

`termini-node-c` in the live footprint is created by
`infra/gcp/tools/raise-standing-fleet.sh`, not by Terraform. Left alone, the next
recreate would have handed node-c back the `roles/editor` default SA and the
old scope list. That script now passes
`--service-account termini-node-inference@… --scopes cloud-platform` for node-c
(configurable via `INFERENCE_SERVICE_ACCOUNT` / `INFERENCE_SCOPES`; node-a keeps
its previous behaviour).

---

## 4. Firewall / egress path

There is no egress rule in the project at all, so GCE's implied allow-egress is
what governs. Verified rather than reasoned about, and then proved with a real
call from the node (§5):

```
$ gcloud compute firewall-rules list --project=termini-510609 --filter='direction=EGRESS' --format='value(name)'
(no output — no egress rules exist)

$ gcloud compute firewall-rules list --project=termini-510609 \
    --format='table(name,direction,priority,sourceRanges.list(),allowed[].map().firewall_rule().list(),disabled)'
NAME                               DIRECTION  PRIORITY  SOURCE_RANGES  ALLOW                         DISABLED
default-allow-icmp                 INGRESS    65534     0.0.0.0/0      icmp                          False
default-allow-internal             INGRESS    65534     10.128.0.0/9   tcp:0-65535,udp:0-65535,icmp  False
default-allow-rdp                  INGRESS    65534     0.0.0.0/0      tcp:3389                      False
default-allow-ssh                  INGRESS    65534     0.0.0.0/0      tcp:22                        False
termini-allow-controller-api       INGRESS    1000      10.10.0.0/24   tcp:443                       False
termini-allow-controller-public    INGRESS    1000      0.0.0.0/0      tcp:22,tcp:80,tcp:443         False
termini-allow-controller-underlay  INGRESS    1000      10.10.0.0/24   tcp:8080                      False
termini-allow-inference            INGRESS    1000      10.10.0.0/24   tcp:8080                      False
termini-allow-inference-intent     INGRESS    1000      10.10.0.0/24   tcp:8090                      False
termini-allow-lxd-cluster          INGRESS    1000      10.10.0.0/24   tcp:8443                      False
termini-allow-ssh                  INGRESS    1000      0.0.0.0/0      tcp:22                        False
termini-allow-vxlan                INGRESS    1000      10.10.0.0/24   udp:4789                      False

$ gcloud compute routes list --project=termini-510609 --filter='network:termini-vpc' \
    --format='table(name,destRange,nextHopGateway.basename(),priority)'
NAME                              DEST_RANGE    NEXT_HOP_GATEWAY          PRIORITY
default-route-bedaa047076bdfc4    0.0.0.0/0     default-internet-gateway  1000
default-route-r-a3998790bb4571c5  10.10.0.0/24                            0
```

All 12 firewall rules are `INGRESS`; nothing filters node egress, and the VPC has
no custom default route (no NAT-instance hop). `privateIpGoogleAccess=True` is
the only thing that turned "a default route exists" into "Google APIs are
actually reachable".

---

## 5. The proof — from the node, not from the laptop

`infra/gcp/tools/verify-node-c-egress.sh` is the re-runnable acceptance probe. It
asserts the path (PGA on), the identity (least-privilege SA + `cloud-platform`),
opens a **temporary** SSH external IP only because the node is `--no-address` by
design, runs the token + Vertex AI call **on the node**, and removes the
temporary IP again on exit.

Full run, verbatim:

```
$ ./infra/gcp/tools/verify-node-c-egress.sh
[verify-node-c-egress] 1/4 subnet Private Google Access
[verify-node-c-egress]     privateIpGoogleAccess=True
[verify-node-c-egress]     cloud routers (expect none — this is deliberately NOT a Cloud NAT design):
[verify-node-c-egress] 2/4 node identity (least privilege + scope)
[verify-node-c-egress]     service account: termini-node-inference@termini-510609.iam.gserviceaccount.com
[verify-node-c-egress]     scope[0]       : https://www.googleapis.com/auth/cloud-platform
[verify-node-c-egress]     role bindings on termini-node-inference@termini-510609.iam.gserviceaccount.com:
      role: roles/aiplatform.user
      role: roles/logging.logWriter
      role: roles/monitoring.metricWriter
[verify-node-c-egress]     termini-node-c has no external IP (by design) — adding a TEMPORARY one for SSH only
Updated [https://www.googleapis.com/compute/v1/projects/termini-510609/zones/asia-south1-a/instances/termini-node-c].
[verify-node-c-egress] 3/4 firewall: GCE implied egress — proving it with a real call instead of reasoning
[verify-node-c-egress]     egress DENY rules in the project (expect none):
[verify-node-c-egress] 4/4 token from the node's metadata server -> Vertex AI gemini-2.5-flash (global)
    token_len=1024
    resolved_vip=2404:6800:4000:1027::5f
    {
      "candidates": [
        {
          "content": {
            "role": "model",
            "parts": [
              {
                "text": "PROXY_OK"
              }
            ]
          },
          "finishReason": "STOP"
        }
      ],
      "usageMetadata": {
        "promptTokenCount": 8,
        "candidatesTokenCount": 3,
        "totalTokenCount": 33,
        "trafficType": "ON_DEMAND",
        "promptTokensDetails": [ { "modality": "TEXT", "tokenCount": 8 } ],
        "candidatesTokensDetails": [ { "modality": "TEXT", "tokenCount": 3 } ],
        "thoughtsTokenCount": 22
      },
      "modelVersion": "gemini-2.5-flash",
      "createTime": "2026-10-15T09:20:49.861189Z",
      "responseId": "8ZrQaoXINKq57PkP59zm4Qc"
    }

    round_trip_s=1.095738820
[verify-node-c-egress] PASS — node-c reaches Vertex AI over Private Google Access with its own least-privilege identity
[verify-node-c-egress] removing the temporary external IP (node-c must stay --no-address)
[verify-node-c-egress] external IP removed
```

Reproduced three times from the node with the metadata-server token (only):
`PROXY_OK`, 32–33 total tokens, round trip 0.88–1.16 s. Model and endpoint as
directed: `gemini-2.5-flash` on the **GLOBAL** endpoint; `gemini-2.0-flash-001`
was not attempted (documented 404).

Sanity check on the token itself — it is minted by the node, for the node's own
identity:

```
    sa_email=termini-node-inference@termini-510609.iam.gserviceaccount.com
    ipv4_addr=10.10.0.13/32
    default_route=default via 10.10.0.1 dev ens4 proto dhcp src 10.10.0.13 metric 100
    route_to_vip: 142.250.207.138 via 10.10.0.1 dev ens4 src 10.10.0.13 uid 1000
```

---

## 6. What this costs the story (governance restatement)

**Private Google Access removes the "no egress path at all" property.** Before
this change the nodes had no route to *any* Google API; from now on, the node's
private address (and anything NAT'd behind the node host, e.g. the LXD guest
VMs' bridged traffic) can reach Google API VIPs. That is the exact trade the
governance card — the sibling of the proxy card — must restate, and it is
recorded here so nobody has to notice it from the diff:

- The claim that survives: **no external IP, no Cloud NAT, no public inbound
  surface, and the traffic never leaves Google's network** — which is the
  property PGA was chosen to preserve.
- The claim that does **not** survive verbatim: "the nodes cannot reach Google
  at all." Any judge-facing text asserting a total egress block, including the
  `AR-11`-style statement that a node/agent VM has no route to GCP, is now
  inaccurate **for the node host** and needs the governance card's review. This
  card deliberately did not edit `docs/contracts/` (those documents are frozen
  and owned elsewhere); it is flagged here instead.
- `PGA` makes the egress path **Google-scoped by construction**: reaching
  Google APIs required the destination to be an API VIP that Google advertises
  internally. It does not by itself open the general internet, and no NAT was
  added for that purpose.

---

## 7. Findings and caveats (read these — one is environmental)

1. **`n2-standard-8` was unavailable in `asia-south1-a` for the whole window.**
   `gcloud compute instances start termini-node-c` failed with
   `ZONE_RESOURCE_POOL_EXHAUSTED` ("A n2-standard-8 VM instance is currently
   unavailable in the asia-south1-a zone") on nine consecutive attempts over
   ~9 minutes, and fresh probe creates of `n2-standard-8`, `n2-standard-4`,
   `n2-standard-2` and `n2-highmem-8` all failed the same way — so it is zone
   capacity, not a quota. `e2-standard-8` (same 8 vCPU / 32 GiB shape) was
   available and was used **temporarily** to bring the node up for the proof:
   `set-machine-type n2-standard-8 → e2-standard-8`, then restored.
   *This is orthogonal to the card's deliverable* — identity and egress are
   machine-type independent — but whoever next raises the standing demo should
   know that a `n2-standard-8` create in `asia-south1-a` can fail on capacity
   alone; `raise-standing-fleet.sh` will surface the raw gcloud error.
2. **The node keeps `--no-address` in the end state.** The proof needs an SSH
   transport, and with no external IP there is none (IAP is not enabled for this
   project). A temporary ephemeral external IP was therefore added around each
   probe and **removed afterwards** — verified empty at the end of every run,
   including at the end of this one. Nothing about the design property changed.
3. **Attribution of the successful call to PGA.** The first probe call ran while
   the temporary SSH external IP was attached, so on that run alone the call
   cannot be *uniquely* attributed to Private Google Access. A follow-up
   experiment therefore pinned the sampling window and the call to an absolute
   clock and removed the external IP for the whole window; see §8. Read §8's
   conclusion before quoting "the call went over PGA".
4. **The `--no-address` instance's access config is not exclusive.** The
   `metadata` view of `access-configs/0/external-ip` inside the guest does not
   track a control-plane `delete-access-config` promptly (it kept returning the
   previous address), so the in-guest metadata path is **not** a reliable
   instrument for "does this VM have a public address right now". The
   control-plane (`gcloud compute instances describe`) view is authoritative.
   Recorded because the proxy/verification work will be tempted to use it.

---

## 8. PGA vs. the temporary SSH IP — attribution experiment

Because the probe in §5 ran with the temporary SSH external IP attached, that run
alone does not *uniquely* attribute the successful call to Private Google Access.
A second experiment pins every step to an **absolute clock** (the node's own
clock, verified against Google's `Date:` header to within a second) and removes
the SSH external IP for the whole sampling window, so no ssh latency can slide
the measurement out of the no-external-IP interval. Two destination classes are
probed at the same instants:

- `public_1.1.1.1` — a non-Google public endpoint, addressed by **bare IP** so no
  DNS is involved (result shown as `http_code/curl_exit_code`).
- `google_aiplatform` — `https://aiplatform.googleapis.com/`, a Google API VIP.
  404 is the *expected* unauthenticated response for the API root: it proves the
  TLS+HTTP round trip completed, i.e. the destination is reachable.

```
job_started_utc=2026-10-15T10:12:12Z T=1792059424 (T is 2026-10-15T10:17:04Z)
--- sampling starts 2026-10-15T10:17:05Z, external IP should be gone ---
2026-10-15T10:17:05Z t+1s public_1.1.1.1=301/0 google_aiplatform=404/0 ext_ip_meta='8.231.98.148'
2026-10-15T10:17:11Z t+7s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:17:21Z t+17s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:17:32Z t+28s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:17:43Z t+39s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:17:54Z t+50s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:18:04Z t+60s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:18:15Z t+71s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:18:26Z t+82s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:18:36Z t+92s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:18:47Z t+103s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:18:58Z t+114s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:19:08Z t+124s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:19:19Z t+135s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
2026-10-15T10:19:30Z t+146s public_1.1.1.1=000/28 google_aiplatform=404/0 ext_ip_meta=''
--- vertex call at 2026-10-15T10:19:55Z (T+170) ---
metadata_token_len=1024
{ "candidates": [ { "content": { "role": "model", "parts": [ { "text": "PROXY_OK" } ] },
  "finishReason": "STOP" } ],
  "usageMetadata": { "promptTokenCount": 8, "candidatesTokenCount": 3, "totalTokenCount": 32, … },
  "modelVersion": "gemini-2.5-flash",
  "createTime": "2026-10-15T10:19:55.424138Z",
  "responseId": "y6jQasrxGYyBoLAPq5zXWA" }

vertex_round_trip_s=.523400443
vertex_call_window_end_utc=2026-10-15T10:19:55Z
JOB_DONE
```

Read the two columns at the same timestamp and the conclusion is forced:

- **7 seconds after the external IP was removed, the general internet path is
  gone** — `curl` exit `28` (operation timed out) for a bare-IP non-Google host,
  for all 14 samples across 2½ minutes, and the instance's own metadata view of
  its access config is empty (`ext_ip_meta=''`) for the whole window.
- **Google's API VIP stays reachable throughout** (`404`, i.e. a completed HTTP
  round trip), and the Vertex AI `generateContent` call executed at T+170 s in
  that same window returned **`PROXY_OK`** on a token minted by the node's
  metadata server for `termini-node-inference@…`, in 0.52 s.

⇒ **The successful call cannot have used an internet path. Private Google Access
is the only thing that carried it.** (This also answers §7.4: the in-guest
metadata view *does* clear, ~6 s behind the control-plane delete.)

## 8b. End state (restored to the pre-task shape)

```
$ gcloud compute instances describe termini-node-c --zone=asia-south1-a --format='yaml(name,status,id,machineType.basename(),serviceAccounts,networkInterfaces)'
id: '584752554767002517'
machineType: n2-standard-8
name: termini-node-c
networkInterfaces:
- fingerprint: KDi5Y9mGG4U=
  name: nic0
  networkIP: 10.10.0.13
  stackType: IPV4_ONLY
  subnetwork: …/subnetworks/termini-underlay
serviceAccounts:
- email: termini-node-inference@termini-510609.iam.gserviceaccount.com
  scopes:
  - https://www.googleapis.com/auth/cloud-platform
status: TERMINATED

$ gcloud compute disks describe termini-node-c --zone=asia-south1-a --format='value(id,name,sizeGb,creationTimestamp)'
4704597953751526293	termini-node-c	60	2026-10-13T04:26:50.791-07:00

$ gcloud compute networks subnets describe termini-underlay --region=asia-south1 --format='value(name,privateIpGoogleAccess,privateIpv6GoogleAccess)'
termini-underlay	True	DISABLE_GOOGLE_ACCESS

$ gcloud compute routers list
Listed 0 items.
```

TERMINATED (as found), `n2-standard-8` (as found), **no external IP**, same
instance id and boot-disk id/creation timestamp (no rebuild), the new
least-privilege identity, PGA on, no NAT.

---

## 9. Reproduce

```
export PATH=$PATH:~/google-cloud-sdk/bin
home=$(git rev-parse --show-toplevel)

# 1. path (free, idempotent)
gcloud compute networks subnets update termini-underlay --region=asia-south1 \
  --enable-private-ip-google-access

# 2. identity (idempotent — the SA and bindings may already exist)
gcloud iam service-accounts create termini-node-inference \
  --display-name="Termini inference node (Node C)"
gcloud projects add-iam-policy-binding termini-510609 \
  --member="serviceAccount:termini-node-inference@termini-510609.iam.gserviceaccount.com" \
  --role=roles/aiplatform.user --condition=None
# …+ roles/logging.logWriter, roles/monitoring.metricWriter
gcloud compute instances set-service-account termini-node-c --zone=asia-south1-a \
  --service-account=termini-node-inference@termini-510609.iam.gserviceaccount.com \
  --scopes=cloud-platform

# 3. prove it from the node (adds and removes the temporary SSH IP itself)
"$home"/infra/gcp/tools/verify-node-c-egress.sh
```
