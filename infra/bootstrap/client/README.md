# Installing a Termini node — customer guide

This guide is for the **customer** (the operator of the machines), not for the
Termini team. It takes about two minutes and one command per machine.

You need three things, all of which your Termini provider gives you:

1. the **controller URL** of your deployment (for example
   `https://34-93-190-64.sslip.io`);
2. an **enrollment credential** — a one-time code, one per node;
3. a machine to install on: any x86-64 Linux box with `python3`, `curl` (or
   `wget`) and systemd. A machine booted from the Termini node image already
   carries the node runtime, `python3` and `curl`; any other Linux machine gets
   the runtime fetched for you.

You do **not** need — and will never be asked for — the controller's project
token. That credential stays with the provider; the whole install works on your
own credential. If anything tells you otherwise, it is a bug in the install, not
something you should work around.

## What you do — the whole flow, in five steps

1. **Prerequisites.** You have the controller URL, one enrollment credential per
   machine, and a machine that can reach the controller (see the three things
   above).
2. **Run the one-liner** on the machine that will become the node (§1). It does
   the rest, including waiting for the controller to acknowledge the node.
3. **Read the confirmation it prints** — `YOUR NODE IS REGISTERED`, the node id,
   and the node's first health sample (§2).
4. **Open the dashboard** your provider gave you and use its **Your node**
   panel: paste the `node_id` and the `node_token` from
   `/var/lib/termini/identity/node.json` on your node. The panel shows the node
   as registered, online, and its health refreshing in place (§3).
5. **Re-run safely at any time.** Re-running the same command on the same machine
   reuses the node's identity and consumes no credential — do **not** ask your
   provider for a new one unless you are enrolling a *different* machine (§4).

If a step fails, the message names the cause and comes with a matching exit code;
§5 is the table.

---

## 1. The command

Run this **on the machine that will become the node, as root**:

```sh
curl -fsSL https://<your-controller>/install/termini-node.sh | sudo sh -s -- \
    --controller https://<your-controller> --token <YOUR-ENROLLMENT-CREDENTIAL>
```

Both URLs are the same value: `<your-controller>` is the controller URL your
provider gave you. `--token` is your one-time credential. Add `--name node-01` if
you want the name the controller will show to be something other than the
machine's hostname.

What the command does, in order:

1. installs the node runtime if the machine does not already have it;
2. installs `termini-node-status` — your own view of the node;
3. writes `/etc/termini/config.yaml` (which controller this node belongs to) and
   stores your credential at `/var/lib/termini/enrollment.token`, mode `0600`;
4. starts `termini-bootstrap.service` and **waits for the controller to
   acknowledge the registration** — the machine is a node only once the
   controller says so;
5. prints your node's id and its first health sample.

## 2. What success looks like

```
[termini-node-install] controller:  https://<your-controller>
[termini-node-install] node name:   node-01   role: auto
[termini-node-install] node runtime: already present on this machine (nothing to install)
[termini-node-install] client status helper: installed (/usr/local/bin/termini-node-status)
[termini-node-install] enrollment credential stored (0600) at /var/lib/termini/enrollment.token
[termini-node-install] termini-bootstrap.service started; waiting up to 90s for the controller to acknowledge the registration
YOUR NODE IS REGISTERED
  node_id        01M4TGD31WXH97JAA5S6A4R10D
  role           compute
  project        01J0DEM0WARM000000000000AB
  registered_at  2026-10-15T17:04:11Z
  register_ms    41 (controller acknowledgement)
HEALTH (your node, read with your node's own credential)
  seq=1 ts=2026-10-15T17:04:12Z cpu=1.4% ram=8.1% load1=0.0 disk_free=88.4% ctrl_rtt=0.8ms
[termini-node-install] done — this node is part of your Termini deployment.
```

## 3. Your own view of the node

Two ways, both using **your** credential and nothing else.

**On the node, in the terminal:**

```sh
termini-node-status            # registration details + the latest health samples
termini-node-status --watch    # stream its health, one line per sample
termini-node-status --json     # the same data, machine readable
```

This reads your node back through `GET /v1/nodes/<node_id>/telemetry/recent`
using **the node's own credential** (`/var/lib/termini/identity/node.json`, mode
`0600`, written by the controller at registration). It cannot see any other node,
and it needs nothing from your provider.

**In the dashboard your provider gave you — the "Your node" panel:**

Open that dashboard URL (it carries only non-secret identifiers: the API base and
the project id — never a credential), then paste into **Your node**:

| field | value | where it comes from |
|---|---|---|
| `node_id` | e.g. `01M4TGD31WXH97JAA5S6A4R10D` | printed by the installer, and in `/var/lib/termini/identity/node.json` |
| `node_token` | your node's own credential | `/var/lib/termini/identity/node.json` (root-only) |

The panel then shows your node as **registered · online** with its last-seen time
and live telemetry — heartbeat sequence, CPU, memory, uptime, load, runtime and
services — refreshing in place every few seconds. The panel's read is
`GET /v1/nodes/<node_id>/telemetry/recent` with your node's credential in the
`Authorization` header; nothing you paste is put in the URL, and the values stay
in that browser tab only.

What you will **not** get from the dashboard is a fleet list: under the
provider-issued single-use model you hold one credential per node, so there is
nothing that can list other nodes — yours or anyone else's. If you want a
fleet-wide view, ask your provider to open theirs. And whatever you are shown,
the dashboard never asks you for the provider's project token.

## 4. Installing a second node, and re-running the command

- **A second machine needs its own credential.** Credentials are single-use; the
  first node that registers with one consumes it. Ask your provider for one per
  machine.
- **Re-running the command on the same machine is safe and needs no credential.**
  The machine keeps its identity in `/var/lib/termini/identity/node.json`, so a
  re-run reconnects as the same node and does **not** consume another credential.
  Re-running with a credential supplied also leaves that credential untouched.

## 5. When it fails

The install stops with a specific message and a matching exit code. Nothing else
on the machine is modified, and the fix is always "address the message, then
re-run the same command".

| Exit | Message | What it means and what to do |
|---|---|---|
| `1` | *cannot download `<artifact>` from `<url>`* | The installer could not fetch the node runtime or your status helper from the controller. Check the controller URL is exact (scheme, host, port, no trailing path), that this machine has outbound access (443/80) and that no proxy strips the request; `curl -fsS <your-controller>/healthz` should answer. Nothing on this machine was changed. |
| `3` | *your enrollment credential was rejected by the controller* | The controller does not know that credential. Check the whole code was copied, and that it is the one issued **for this machine**. Ask your provider for a fresh credential. |
| `4` | *this enrollment credential has already been used* | Credentials are single-use and this one is spent. If this machine is already registered, re-run **without** `--token`. Otherwise ask your provider for a new credential. |
| `5` | *the controller could not be reached* | Check the controller URL is exact, that this machine has outbound access to it (port 443/80) and that no proxy strips the request. The node daemon keeps retrying while the service runs; re-run the installer once the controller answers. |
| `6` | *registration failed (…)* | The controller rejected this node's registration — usually a version mismatch between the node runtime and the controller. Send the message to your provider. |
| `7` | *the node is not registered yet* | Still connecting when the wait ended. `termini-node-status` shows the live state; re-run the installer to keep waiting. |

A rejection never burns your machine: a node that failed to register has no
identity, so a re-run with a good credential registers it normally.

The installer starts the node service in the background and then waits for the
controller's own acknowledgement, so a failing registration can never park the
install: you get the message and the exit code above. If a run ever *appears* to
sit at "starting the service", press `Ctrl-C` and run `sudo termini-node-status`
— that prints the same diagnosis and the same exit code.

## 6. Removing a node

```sh
sudo systemctl disable --now termini-bootstrap.service
sudo rm -f /etc/termini/config.yaml /var/lib/termini/enrollment.token
```

That stops the node and stops it reporting; ask your provider to retire the
node's record on the controller too (they own the controller-side lifecycle).

---

## For the provider (not the customer)

Everything below is provider-side and needs the controller's project token,
which is why it is not part of the customer flow.

```sh
# ON the controller host — the mint route is loopback-only by design:
sudo python3 infra/bootstrap/client/issue-enrollment.py \
    --public-url https://<controller-host> --name node-01 --count 1
```

It reads the project token from the running controller's unit (or from
`--project-token-file`), mints fresh single-use tokens over the loopback mint
route (`POST /v1/bootstrap-tokens`), **preflights** the public install surface so
the handover cannot name an artifact your customer cannot fetch, prints the exact
command from §1 for each credential, writes the handover to a `0600` file, and
re-reads its own output to refuse the handover if the project token appears
anywhere in it.

| File | Role |
|---|---|
| `termini-node-install.sh` | the installer served at `/install/termini-node.sh` |
| `termini-node-status.py` | the client status helper, served at `/install/termini-node-status.py` |
| `issue-enrollment.py` | provider-side mint + handover printer |
| `evidence/` | raw runs of this flow on real machines |

The install surface and its contract are documented in
`../../../docs/contracts/controller-api.md` §4.8; the node-side bootstrap flow it
configures is `../README.md`.
