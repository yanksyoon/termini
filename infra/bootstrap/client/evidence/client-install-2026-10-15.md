# Client-facing node install — raw evidence (2026-10-15)

Task: kanban **`t_70bd0f2b`** — *Client-facing node installer: a customer bootstraps
their own node with their own credential*. Owner: infra.

The card's open question (REQUIRED WORK 2) was decided by the user on the card:
**option (a), provider-issued single-use enrollment tokens** — the provider mints a
single-use credential and hands the customer a command; the customer never holds the
controller's project token. Self-service enrollment (option b) is out of scope.

| | |
|---|---|
| Landed | PR **#165** → merge **`e8a18ebce907ed50b9671bde5354cef6dc94ca93`** on `main` (branch commit `383ca22`, branch `wt/t_70bd0f2b-client-installer`) |
| Consolidated | task `t_5ed0e1a3` (branch `wt/t_5ed0e1a3-consolidate`): the harness teardown fix below, the stale rows in this doc, and the one assumption it left open (§5.5) |
| Harness | `infra/bootstrap/client/verify-client-install.sh` — hermetic: a **real controller process** + the **real node daemon**, loopback, no root, no systemd, no cloud spend; 28 checks (~28 s on a warm host), including the script's own teardown guard |
| Raw transcript | `harness-run.txt` (verbatim) |
| Live half | **RUN on 2026-10-17** on the live fleet (controller + node-a/node-c): the documented one-liner on a fresh machine, the client's own view, the re-run, the three failure paths, the guardrail greps, and the off-host mint refusal — see [`client-install-2026-10-17.md`](client-install-2026-10-17.md) and its `raw-2026-10-17/`. That run also found and fixed a real defect in this flow (`systemctl restart` without `--no-block` hung the installer on every failure path). §6 below is kept as the historical recipe, superseded by that document. |

## 1. What now exists

| Piece | Where | What it does |
|---|---|---|
| Customer installer | `infra/bootstrap/client/termini-node-install.sh`, served at `GET /install/termini-node.sh` | the one-liner a customer pipes into `sh`: installs the runtime if the machine lacks it, installs the status helper, writes config + credential (`0600`), starts the unit, waits for the controller's **acknowledgement**, prints `YOUR NODE IS REGISTERED` + first telemetry |
| Client status helper | `infra/bootstrap/client/termini-node-status.py`, served at `GET /install/termini-node-status.py` | the customer's own view of the node (`termini-node-status [--watch] [--json]`), read back with the **node's own token**; also the single implementation of the three failure messages |
| Provider handover | `infra/bootstrap/client/issue-enrollment.py` | runs on the controller: reads the project token (unit env or file), mints single-use credentials over the loopback-only mint route, preflights the public install surface, prints the customer command, writes a `0600` handover file, and **refuses to hand over** if the project token appears in its own output |
| Public install surface | `controller/app/server.py` (`INSTALL_ARTIFACTS`, `_serve_install`) | `GET /install/<name>` — static, unauthenticated, secret-free, name-lookup only (no path from the request reaches the filesystem). Contract: `docs/contracts/controller-api.md` §4.8 |
| Deployment | `infra/gcp/tools/deploy-termini-tree.sh` | now ships `infra/bootstrap/` with the controller, so the artifacts the routes serve exist on the deployed host |
| Customer documentation | `infra/bootstrap/client/README.md` | written for the customer: what to run, what success looks like, how to read their node, what each failure means |

## 2. Acceptance, mapped to raw output

| Card acceptance | Result in this run |
|---|---|
| (a) from a clean machine, the documented one-liner registers a node using a client-scoped credential only, and the node appears in the client's view | **proven offline** (real controller process + real daemon): §3.1–§3.3 below. Not yet re-run on a GCP instance (see §6). |
| (b) a re-run on an already-registered node reuses its persisted identity and does not burn a second token | **proven offline, two ways**: the installer reports the credential is *not consumed*, and that same credential then registers a second node. §3.4 |
| (c) wrong/expired credential, unreachable controller and an already-used token each produce a distinct, actionable message | **proven offline**, exit codes 3 / 4 / 5 with three distinct messages (§3.5) |
| (d) evidence doc + PR through `scripts/land.sh` + commit/PR/merge_sha on the card | evidence doc = this file; PR/merge recorded on the card |
| GUARDRAIL: the client never holds the project token | §4 — the token appears in no client artifact, doc, URL or handover |

## 3. Raw output (`harness-run.txt`, 28/28 checks, exit 0)

The last check is the harness's own teardown guard — it asserts the controller it
started is gone and its port released, because the first version of this script
recorded the wrapper subshell's PID as the controller's and leaked a controller
per run (observed: a `python3 -m app.main` still holding the port after the
harness had exited). The launch now goes through `exec` so the recorded PID **is**
the process:

```
PASS  no leaked controller (port 45857 released)
```

### 3.1 the provider's handover

```
=== 1. provider handover: mint a single-use credential and print the customer command ===
[issue-enrollment] install surface OK: http://127.0.0.1:57423/install/termini-node.sh -> HTTP 200 (1 credential(s) to hand over)
--- Termini node enrollment 1/1 ---
credential (single-use, enrols ONE node; NOT the controller's project token):
  <REDACTED: throwaway credential minted against the harness's temporary controller>

Command for the customer — run it ON the machine that becomes the node,
as root (it installs the node and registers it):

  curl -fsSL http://127.0.0.1:57423/install/termini-node.sh | sudo sh -s -- \
      --controller http://127.0.0.1:57423 --token <REDACTED: throwaway credential> --name client-node

Re-running that command on the SAME machine reuses its node identity and
does not consume another credential. This credential is spent by the first
node that registers with it.

[issue-enrollment] handover written 0600 to …/handover.txt
[issue-enrollment] verified: the controller's project token appears nowhere in this handover.
PASS  handover preflighted the public install surface
PASS  handover printed the customer one-liner
PASS  the project token appears in neither the handover file nor its output
PASS  handover carries a single-use credential
```

### 3.2 the customer one-liner (the documented form: `curl … | sh -s --`)

```
=== 2. the customer one-liner: curl | sh -s -- (installs this machine, no provider shell) ===
[termini-node-install] controller:  http://127.0.0.1:57423
[termini-node-install] node name:   client-node   role: auto
[termini-node-install] node runtime: not present — installing it from the controller
[termini-node-install] node runtime: termini-bootstrap installed
[termini-node-install] client status helper: installed (/usr/local/bin/termini-node-status)
[termini-node-install] enrollment credential stored (0600) at /var/lib/termini/enrollment.token
PASS  the one-liner fetched the installer over HTTP and ran it
PASS  it installed the node runtime from the controller
PASS  it installed the client status helper
PASS  it stored the credential 0600
PASS  the installer never echoed the credential
PASS  runtime + status helper installed under the node root
```

The hermetic run passes `--root <dir> --no-start` (this host is not a node and must
not be reconfigured); the **byte-identical full path** — `sudo sh -s --` with no
`--root`, i.e. real `/etc`, real `systemd` — is what §6 runs on the real machine.

### 3.3 registration and the client's own view

```
=== 3. registration and the client's own view of the node ===
YOUR NODE IS REGISTERED
  node_id        01M507PHBFT6H2XG20QCFBHQNN
  role           compute
  project        01J0DEM0WARM000000000000AB
  registered_at  2026-10-15T16:53:02Z
  register_ms    51 (controller acknowledgement)
HEALTH (your node, read with your node's own credential)
  seq=1 ts=2026-10-15T16:53:02Z cpu=53.33% ram=84.8% load1=4.25927734375 disk_free=45.6% ctrl_rtt=0.095ms
PASS  termini-node-status confirms the registration
PASS  the client sees the node id
PASS  the client sees the assigned role
PASS  the client sees its first telemetry
PASS  the node acknowledged the registration
PASS  the controller lists the node (01M507PHBFT6H2XG20QCFBHQNN)
```

### 3.2b dry run: exactly what the installer writes (a placeholder credential)

The card's evidence list asks for a dry run that shows the rendered node
configuration with a placeholder credential. This path has no cloud-init (§8), so
the equivalent is the installer's own `--root <dir> --no-start` form, run offline
against a scratch root, with a placeholder credential and the runtime already
staged (the branch the node image family takes):

```
$ sh infra/bootstrap/client/termini-node-install.sh --root <root> --no-start \
      --controller https://controller.example.test --token <PLACEHOLDER-CREDENTIAL> \
      --name node-01 --status-helper infra/bootstrap/client/termini-node-status.py
[termini-node-install] controller:  https://controller.example.test
[termini-node-install] node name:   node-01   role: auto
[termini-node-install] node runtime: already present on this machine (nothing to install)
[termini-node-install] enrollment credential stored (0600) at <root>/var/lib/termini/enrollment.token
[termini-node-install] --no-start: wrote <root>/etc/termini/config.yaml; start termini-bootstrap.service to register.
[exit 0]

$ cat <root>/etc/termini/config.yaml
# Termini node configuration — written by termini-node-install.
# controller_url is the Termini deployment this node belongs to.
controller_url: https://controller.example.test
node_name: node-01
role_hint: auto
telemetry_interval_ms: 1000

$ find <root> -type f -printf '%M %p\n' | sort
-rw------- <root>/etc/termini/config.yaml
-rw------- <root>/var/lib/termini/enrollment.token
-rw-rw-r-- <root>/etc/systemd/system/termini-bootstrap.service
-rwxr-xr-x <root>/usr/local/bin/termini-bootstrap
-rwxr-xr-x <root>/usr/local/bin/termini-node-status
```

The credential lands `0600` and nothing else about the machine changes. On the
same scratch root the *fetch* step fails loudly if the controller is unreachable,
**before** any config is written:

```
$ sh … --controller https://controller.example.test --token <PLACEHOLDER-CREDENTIAL>
[termini-node-install] node runtime: already present on this machine (nothing to install)
curl: (6) Could not resolve host: controller.example.test
[termini-node-install] ERROR: cannot download termini-node-status.py from https://controller.example.test/install/termini-node-status.py
  * check the controller URL is exact (scheme, host, port, no trailing path);
  * check this machine has outbound network access to it, and that no proxy or
    firewall is stripping the request;
  * check the controller answers:      curl -fsS https://controller.example.test/healthz
  Nothing on this machine was changed.
[exit 1]
```

Read back with the node token only (`GET /v1/nodes/<id>/telemetry/recent`); the
project token appears in no step of this path (§3.3 and §3.4).

### 3.4 re-run: identity reused, no second credential burned (acceptance b)

```
=== 4. re-run on the registered node: identity reused, no second credential burned ===
[termini-node-install] this machine is already registered as node_id=01M507PHBFT6H2XG20QCFBHQNN — reusing its persisted identity
[termini-node-install] the enrollment credential you supplied is NOT used and is NOT consumed
[termini-node-install]   (enrollment credentials are single-use; this node never needs another)
PASS  the re-run recognised the persisted identity
PASS  the re-run did not consume the new credential
PASS  the unburned credential registered a second node
```

The last line is the end-to-end proof: the credential the re-run was given is then
used, unchanged, to register a *different* machine — so it was never burned.

### 3.5 the three failure modes (acceptance c)

```
=== 5a. a rejected credential ===
REGISTRATION FAILED: your enrollment credential was rejected by the controller
  The controller does not recognise the credential (single-use and
  scoped to one registration).
  * Check the whole token was copied — no missing characters and no
    shell quoting damage;
  * check it is the credential your provider issued for THIS node
    (every node needs its own);
  * then ask your provider for a fresh enrollment credential and
    re-run the installer with it.
  registration_attempts=4
  controller said: register failed: HTTP 401 UNAUTHENTICATED: … 'unknown bootstrap token' …
PASS  rejected credential exits 3
PASS  rejected credential message is actionable

=== 5b. an already-used credential (the same one that registered node1) ===
REGISTRATION FAILED: this enrollment credential has already been used
  Enrollment credentials are single-use: the first node that
  registers with one consumes it.
  * If THIS machine is already registered, re-run the installer
    WITHOUT a credential — it reuses the node's persisted
    identity and never needs one again.
  * If you are installing a DIFFERENT node, ask your provider
    for a new credential; this one cannot be reused.
  registration_attempts=4
  controller said: register failed: HTTP 401 UNAUTHENTICATED: … 'bootstrap token already used' …
PASS  used credential exits 4
PASS  used credential message is actionable

=== 5c. an unreachable controller (config points at a black hole) ===
REGISTRATION FAILED: the controller could not be reached
  * Check the controller URL is exact (scheme, host, port, no path);
  * check this machine has outbound access to it (port 443/80) and
    that no proxy or firewall strips the connection;
  * check the controller is running.
  The node daemon keeps retrying while its service runs; the
  installer can simply be re-run once the controller answers.
  registration_attempts=4
  controller said: register unreachable (connection refused): [Errno 111] Connection refused
PASS  unreachable controller exits 5
PASS  unreachable message is actionable
PASS  the three failure messages are distinct
```

The classification is the node daemon's own `failure_kind`
(`infra/bootstrap/node/termini-bootstrap.py`, contract §9); `termini-node-status`
translates it, so the installer's message and the client's own view can never
disagree. Unit-level coverage of the same mapping:
`tests/test_client_installer.py::FailureMessageTest`.

### 3.6 the repository suites (offline, on this branch)

```
$ python3 -m unittest tests.test_client_installer            # 20 tests  OK
$ python3 -m unittest discover -s tests                      # 769 tests OK (180 s)
$ shellcheck -S warning infra/bootstrap/client/*.sh infra/gcp/tools/deploy-termini-tree.sh
$ python3 scripts/check-readme-links.py                      # all README links resolve
$ python3 scripts/check-doc-ulids.py                         # all example ids are ULIDs
$ git ls-files '*.py' | xargs -n1 python3 -m py_compile      # syntax
```

`ClientEnrollmentEndToEndTest` was red on `origin/main` before this branch (it is
fixed here — see §8); 769 is the whole suite including it.

## 4. Guardrail — the project token in no client artifact

The credential classes in play (contract §2) are `bootstrap_token` (single-use, the
customer's), `node_token` (per node, the customer's read credential) and
`project_token` (the provider's, shared). The customer path may only ever touch the
first two. Two checks, because a repo-wide grep is the wrong instrument here:

```
$ grep -rn "demo-project-token" \
      infra/bootstrap/client/termini-node-install.sh \
      infra/bootstrap/client/termini-node-status.py \
      infra/bootstrap/client/README.md \
      docs/contracts/controller-api.md controller/app/server.py
(no match — every artifact on the customer path, the contract section and the route table)

$ python3 -m unittest tests.test_client_installer.InstallSurfaceTest -v
test_no_install_artifact_carries_the_project_token ... ok
```

The second is the stronger one: it fetches each artifact from a running controller
over `GET /install/<name>` and asserts the **served bytes** contain neither
`demo-project-token` nor `project_token`, so the property is enforced in CI rather
than asserted in a document.

The only mentions of a *project token* under `infra/bootstrap/client/` are in
`issue-enrollment.py` — the **provider-side** helper, which reads it, uses it for
the loopback mint and then re-reads its own output to refuse a leak. The four
artifacts served publicly (`/install/…`) contain no credential of any class. The
controller's route lookup is by name out of `INSTALL_ARTIFACTS`, so the public
surface cannot be walked into the filesystem.

One self-reference, named rather than hidden: **this evidence doc** quotes the
string `demo-project-token` in the command above, so a *repo-wide* grep does hit
this file — and it did before this run too, which is why the check above is scoped
to the artifacts instead of to the directory. Nothing in the evidence directory is
served by the controller or read by the installer.

## 5. What is NOT proven here

1. **The systemd start path on a real machine** (`systemctl daemon-reload &&
   systemctl restart termini-bootstrap.service`, then the installer's own wait
   loop). `--root` + `--no-start` stands in for it offline; §6 runs it for real.
2. **A machine booted from the `termini-node-os` image family**, where the runtime
   is already baked and the installer therefore does not fetch it. The offline run
   exercises the *fetch* branch instead (it installed the runtime from the controller).
3. **The public HTTPS path** (`https://<controller>/install/termini-node.sh`
   through Caddy). The offline run uses the controller directly on loopback; the
   Caddy config on the controller host is `reverse_proxy 127.0.0.1:8080` for every
   path, so the route is reachable, but that has not been observed end to end.
4. **A browser surface for a client-scoped credential.** The client's view today is
   the CLI (`termini-node-status`), which is what §3.3 shows; the dashboard remains
   project-token-scoped by design, because the client must not hold that token. Any
   "screenshot" of a client view would therefore be a screenshot of a terminal. No
   browser screenshot is claimed here.
5. ~~**That the node image ships `curl` (or `wget`).**~~ **Resolved from the image
   recipe, not from a boot.** The one-liner needs one of them. The route that
   produced `termini-node-os-5` is the golden-VM bake (`baked-node-os-5-verification.md`
   §1 → `image/pipeline/golden-vm/bake-node-os.sh`), and its step 0 is
   `image/pipeline/golden-vm/bake-node-os.sh:85`:

   ```
   apt-get install -y -q --no-install-recommends python3 jq curl ca-certificates cloud-init
   ```

   So the family ships `curl` **and** `python3` (which the installer also requires),
   and the other two bake routes agree (`image/pipeline/provision/00-base.sh:12`,
   `image/pipeline/distrobuilder/termini-node.yaml:37`). What is still unproven is
   the boot itself, not the recipe: `curl -fsSL https://<controller>/install/termini-node.sh`
   executed **on** a machine booted out of the family — §6 step 4, owned by task
   `t_7a98a110`. If a future bake ever drops `curl`, the installer's explicit
   "neither curl nor wget is installed" message is what a customer sees.

## 6. The live window: exact commands (needs operator approval)

The card requires a real machine; the operator's cost guard stops the fleet. Run
this when a window is approved (a few minutes, ~$0.02-0.05):

```sh
# 1. raise the controller and deploy THIS commit's tree (adds /install/ + the artifacts)
gcloud compute instances start termini-controller --zone asia-south1-a
infra/gcp/tools/deploy-termini-tree.sh --ref <landed-merge-sha>

# 2. mint the customer's credential ON the controller and print their command
gcloud compute ssh termini-controller --zone asia-south1-a --command \
  'sudo python3 /opt/termini/infra/bootstrap/client/issue-enrollment.py \
     --public-url https://34-93-190-64.sslip.io --name termini-client-proof --out /tmp/handover.txt'

# 3. boot a fresh node from the image family (no cloud-init token: the CUSTOMER's
#    credential is the only one it gets)
gcloud compute instances create termini-client-proof \
  --project termini-510609 --zone asia-south1-a --machine-type e2-standard-2 \
  --image-family termini-node-os --image-project termini-510609 \
  --boot-disk-size 30GB --boot-disk-type pd-balanced \
  --network termini-vpc --subnet termini-underlay \
  --tags termini-node --no-shielded-secure-boot --quiet

# 4. ON that machine — the documented one-liner, unmodified (real /etc, real systemd)
curl -fsSL https://34-93-190-64.sslip.io/install/termini-node.sh | sudo sh -s -- \
  --controller https://34-93-190-64.sslip.io --token <THE CREDENTIAL FROM STEP 2>
termini-node-status --watch        # what the customer sees

# 5. re-run on the same machine -> identity reused, no second credential spent;
#    then the three failure paths (bad credential / same credential again /
#    a bogus --controller URL), each capturing its message and exit code

# 6. clean up: delete the instance, decommission the proof node on the controller
gcloud compute instances delete termini-client-proof --zone asia-south1-a --quiet
curl -sS -X POST -H "Authorization: Bearer <project token>" \
  https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WARM000000000000AB/nodes/<nid>/decommission
```

Expected: step 4 prints `YOUR NODE IS REGISTERED` with a fresh `node_id`, and the
same node appears in `GET /v1/projects/<pid>/nodes`; the credential supplied at
step 2 is spent exactly once.

## 7. Reproducing the offline half

```sh
infra/bootstrap/client/verify-client-install.sh            # 28 checks, ~30 s, exit 0
python3 -m unittest tests.test_client_installer -v         # 20 tests, OK
python3 -m unittest discover -s tests                      # the CI suite, OK
```

## 8. Conformance to this card's REQUIRED WORK (and where it deviates)

The card was written against a sketch (`curl <controller>/install.sh | sudo bash -s
-- --enroll <cred>` + generated cloud-init). The frozen contract from the parent
decision card (`t_074b1a14`, option (a)) is what shipped, so the sketch's shapes are
recorded here as named deviations rather than silently dropped.

| Card item | Where it is satisfied | Deviation, named |
|---|---|---|
| 1. one-liner served from the controller, controller URL from flag/env, never hardcoded to an internal host | `GET /install/termini-node.sh` (`controller/app/server.py::INSTALL_ARTIFACTS`); `--controller` or `TERMINI_CONTROLLER_URL` | **there is no default value, on purpose**: any default would be a host, i.e. exactly the hardcoded internal host the card forbids. The installer refuses to guess and says which flag is missing; the frozen contract also writes `--controller` as required. |
| 2. client credential only; zero project/demo-token hits | §4, enforced by `InstallSurfaceTest` | none |
| 3. idempotent re-run reusing the persisted identity, no second token consumed | installer identity check (`/var/lib/termini/identity/node.json`); §3.4 — the un-burned credential then registers a *second* node; `InstallerScriptTest`, `ClientEnrollmentEndToEndTest` | none |
| 4. three distinct, actionable failures with distinct non-zero exit codes | `termini-node-status.py::classify` → `3` rejected, `4` already used, `5` unreachable; §3.5 asserts the messages are distinct | none |
| 5. customer-facing doc (prereqs, exact command, expected output, how to tell it registered, safe re-run, uninstall) | `infra/bootstrap/client/README.md` §1–§6 — including the exit-code table and the decommission steps | none |
| image/ISO handoff as an alternative path | the node image family carries the runtime, `python3` and `curl` (§5.5), and the installer detects it (`node runtime: already present`) instead of fetching | the image is a *runtime* shortcut, not a second enrollment path: customers boot it and still run the same documented one-liner with their own credential. |
| 6. hand the client onward to a dashboard view, state the URL | **not available under the frozen contract, and deliberately not faked** — under option (a) the client's only credential is per node, so there is no client-authenticated node list and the dashboard stays provider/project-token scoped (`t_074b1a14`) | the client's confirmation surface is `termini-node-status [--watch] [--json]` on the node; README §3 now says outright that there is **no dashboard page for the customer to open** and why, so nobody hands a customer a URL carrying a project token. |
| evidence: shellcheck-clean | §3.6 | none |
| evidence: dry run showing the rendered node config with a placeholder credential | §3.2b | the card said "rendered cloud-init"; **there is no cloud-init on this path** — the frozen contract configures the customer's machine directly (`/etc/termini/config.yaml` + the credential, both `0600`). cloud-init remains the provider/IaC path (`infra/bootstrap/render-cloud-init.sh`), untouched. |
| evidence: grep proving no project/demo token | §4 | none |
| evidence: the doc rendered/committed | `README.md` in this PR | none |

One more main-branch defect was fixed here because it was red inside **this
deliverable's own test file**:
`ClientEnrollmentEndToEndTest::test_credential_registers_the_node_and_the_client_reads_it_back`
had been failing since #166 (`26280b8`) on any host that actually has LXD. It read
the client's snapshot the instant the daemon flipped to `ONLINE`, and #166's LXD
inventory + service probes pushed the first telemetry sample past that instant
(measured: sample readable `+546 ms` after `ONLINE` on `origin/main` vs `+227 ms`
before #166; the test's immediate read lands at about `+148 ms`). It is a race in
the test, not in the product — the product's own path waits (`--wait`), which is
what the installer runs. The test now takes the same path the customer does.

