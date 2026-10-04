# Termini node bootstrap — cloud-init + systemd (W3)

Owner: kanban task **`t_066b6e3d`** — *Cloud-init bootstrap daemon: SSH keys +
controller registration <5s*. Parent epic `t_5783da13` (W3 bare-metal bring-up).

This directory is the node-side bootstrap flow: the cloud-init user-data that
turns a freshly provisioned node from the pre-baked image into a **registered**
member of the Termini fleet, the systemd unit that runs the bootstrap daemon, and
the harness that proves the card's acceptance criteria on a real node.

It implements the node half of `docs/contracts/node-bootstrap.md`
(registration §5, SSH key injection §7, telemetry §8, idempotency §10) against the
real controller core in `controller/`.

---

## 1. The flow (boot → registered)

```
  operator / IaC                     node (booted from alias agentic-os/w3)          controller (GCP VM)
       │                                        │                                        │
       │ provision-node.sh renders the          │                                        │
       │ cloud-init user-data (team SSH key,    │                                        │
       │ config, single-use enrollment token)   │                                        │
       ├───────────────────────────────────────►│ cloud-init config stage:               │
       │                                        │   write /etc/termini/config.yaml (+token)
       │                                        │   install team key into authorized_keys│
       │                                        │   harden sshd, TrustedUserCAKeys       │
       │                                        │                                        │
       │                                        │ systemd starts                         │
       │                                        │ termini-bootstrap.service (Type=notify)│
       │                                        │   gather facts + machine_id            │
       │                                        │ 1. POST /v1/nodes/register ───────────►│ validate token, upsert by machine_id,
       │                                        │                                        │ assign role
       │                                        │ 2. ◄── 201 node_id + node_token ───────│
       │                                        │   persist identity (idempotent reboot) │
       │                                        │   sd_notify(READY=1)  ← ONLY now       │
       │                                        │ 3. telemetry @ configured cadence ────►│
       │                                        │                                        │
       │ SSH with the injected team key (no password)                                    │
       └─────────────────────────────────────────────────────────────────────────────────►
```

Key properties:

* **No live download.** The image is pre-baked (alias `agentic-os/w3`,
  `image/`); cloud-init performs no apt/network fetch — it only writes files.
* **Success is reported only after acknowledgement.** The unit is `Type=notify`
  and the daemon sends `READY=1` **only** after the controller returns 2xx to
  `/v1/nodes/register`. A node still connecting stays `activating`; a terminal
  enrollment failure never reports ready (`TimeoutStartSec=infinity` keeps the
  contract's ACTIVATING/wait state legal).
* **Idempotent.** cloud-init's key installer de-dups the key line; a reboot
  reuses the persisted identity and reconnects instead of re-enrolling.

---

## 2. Files

| Path | What |
|---|---|
| `node/termini-bootstrap.py` | **Canonical bootstrap daemon.** Reads config + token, gathers facts, registers, then streams telemetry. Emits the objective timing line (see §4). |
| `node/termini-bootstrap.service` | systemd unit (`Type=notify`, `Restart=always`, activation-only via `ConditionPathExists=/etc/termini/config.yaml`). |
| `cloud-init/node-bootstrap.yaml.tftpl` | cloud-init user-data: injects the team SSH key, writes config + token + CA + sshd policy, starts the unit. |
| `render-cloud-init.sh` | Renders the template with `envsubst` (documented variable list only). |
| `provision-node.sh` | Creates/configures a node from the image alias with the rendered user-data (idempotent). |
| `verify-bootstrap.sh` | Acceptance harness — provisions a fresh node, times boot→registered, tests passwordless SSH with the injected key, and checks idempotency. |
| `evidence/` | Captured raw evidence (see §5). |

`infra/warm/node/termini-bootstrap.py` and `.service` are symlinks to the two
canonical files here, so the demo warm layer (`infra/warm`) and this flow share a
single implementation (no drift).

---

## 3. Acceptance, mapped to commands

| Card acceptance criterion | How it is met | Evidence |
|---|---|---|
| Fresh node reaches *registered* with the controller in **<5 s from boot** | `verify-bootstrap.sh` provisions a fresh node from `agentic-os/w3`, boots it, and measures boot→ack from the node's own boot clock. | `evidence/timing-distribution.txt`: **2.93 / 3.11 / 3.31 / 3.54 s** |
| SSH login using the injected key succeeds **without a password** | Team ed25519 key generated per run; public half injected by cloud-init; harness logs in as `root@<node>` with `BatchMode=yes`; password auth asserted refused. | `evidence/bootstrap-run.txt` §SSH |
| Timing evidence (log excerpt / timestamp delta) attached | Daemon `REGISTERED ... register_ms=… boot_to_registered_ms=…` log line + harness timing block + `/proc/uptime` at ack. | `evidence/bootstrap-run.txt` |

The registration step itself (service start → 2xx ack, the metric fixed by
`docs/contracts/node-bootstrap.md` §12.5) is **69–124 ms**: the <5 s budget is
about registration not being slowed by downloads, and it never is.

---

## 4. How the timing is measured (objectively)

Two independent intervals are logged, so neither depends on trusting a single
clock:

* `register_ms` — monotonic ms from the daemon process starting to the moment the
  controller's 2xx registration response is parsed. This is the contract §12.5
  metric (*`systemctl start termini-bootstrap` → ONLINE*), and it **excludes OS
  boot**, which is not something the bootstrap flow controls.
* `boot_to_registered_ms` — `/proc/uptime` read **at the instant of the ack**. In
  an LXD instance LXD virtualises `/proc/uptime` to the instance's own boot clock,
  so this is a genuine *boot timestamp → ack* value. This is the card's headline
  metric.

The harness additionally measures `lxc start` → controller has the node row on the
**host's single clock**, to cross-check the node-side value without any clock-skew
assumption between host and node.

---

## 5. Evidence (`evidence/`)

| File | Shows |
|---|---|
| `bootstrap-run.txt` | A full `verify-bootstrap.sh` transcript: rendered cloud-init, timing block, systemd unit state, the controller's node record, passwordless SSH + perms, password-auth refusal, key-installer idempotency, and reboot reconnect with a stable node_id. `RESULT: PASS`. |
| `timing-distribution.txt` | Boot→ack across four independent fresh-node provisions (all PASS, all <5 s), plus the one contention-spike outlier. |

Regenerate with:

```bash
CONTROLLER_URL=... TEAM_SSH_PUBKEY_FILE=/path/team.pub \
  infra/bootstrap/provision-node.sh bsp-verify compute <enrollment-token>
PORT=18099 NODE=bsp-verify infra/bootstrap/verify-bootstrap.sh | tee evidence/bootstrap-run.txt
```

---

## 6. Honest scope and deviations

* **Node runtime is baked into the image alias.** The canonical daemon + unit are
  installed by `image/pipeline/provision/60-node-bootstrap.sh` (0755 daemon,
  enabled unit), so `agentic-os/w3` contains them directly; a node created from
  the alias needs no provision-time push. `provision-node.sh` defaults
  `REFRESH_RUNTIME=0`; setting it to `1` is a legacy escape hatch that copies the
  repo's canonical files into the fresh node (a local file copy, no network). The
  node-side and image pipelines share one implementation (`infra/warm/node/*` are
  symlinks to `infra/bootstrap/node/*`).
* **Instances are LXD containers, not VMs.** This GCP footprint has no `/dev/kvm`
  (inherited from `t_58a1fed1`); the bootstrap flow is instance-type agnostic. On a
  real VM the OS boot is slower and `boot_to_registered_ms` grows by the VM's boot
  time — the *registration* interval and the no-download property are unchanged.
* **The controller core returns `identity: null` / `cluster: null`.** The daemon
  therefore runs over plain HTTP on the demo underlay and does not perform the LXD
  cluster join or mTLS channel (contract §5/§6); those are the controller
  workstream's surface. Registration + telemetry are exercised for real.
* **Referenced alias.** The cloud-init template and `provision-node.sh` default to
  the image task's alias `agentic-os/w3` (`image/`, version `2026.10.0`).

---

## 7. Follow-ups (not done here)

1. ~~**Bake the daemon + unit into the image alias.**~~ **Done (t_d8203794):**
   `image/pipeline/provision/60-node-bootstrap.sh` installs the canonical daemon +
   unit into the alias; `provision-node.sh` defaults `REFRESH_RUNTIME=0`.
2. **Unify the GCP cloud-init** — `infra/gcp/cloud-init/node.yaml.tftpl` should render
   from `cloud-init/node-bootstrap.yaml.tftpl` so the GCP path gets the same SSH key
   injection + sshd hardening instead of a placeholder CA line.
