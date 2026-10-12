# Baked-node registration on the deployed footprint — RAW EVIDENCE

This file exists because the `register_ms` figure in the deck, README, SUBMISSION and
`w7/verdict.md` was originally asserted **without a committed artifact** — an
independent re-verification flagged exactly that. This is the archived run.

## Provenance

| | |
|---|---|
| Date | 2026-10-12 |
| Image | `termini-node-os-1` (family `termini-node-os`, 20 GB, `READY`) |
| Instance | `termini-baked-archive`, `asia-south1-a` (`n2-standard-2`), created with `--image-family termini-node-os` |
| Startup | **activation-only**: writes `/etc/termini/config.yaml` + the enrollment token, then `systemctl enable --now termini-bootstrap.service`. Nothing installed, nothing downloaded. |
| Controller | `https://34-93-190-64.sslip.io` (project `01J0DEM0WARM000000000000AB`) |
| node_id | `01M4R11VHGEG8N3JB59F90X7T3` |
| Cost | a 10-minute `n2-standard-2` in `asia-south1` ≈ **$0.02** |

## Corroboration — independent of the log below

Read back from the **deployed controller's own database**, not from the node:

```
id=01M4R11VHGEG8N3JB59F90X7T3  role=compute  status=online
registered_at=2026-10-12T12:22:58Z
```

The timestamp matches the journal line to the second. The controller recorded this
independently of the node's own logging.

And on the node, the daemon was **timestamped at bake time, not boot time**
(`/usr/local/bin/termini-bootstrap` = `Oct 11 19:51`), with the `/var/lib/termini/.baked-offline`
sentinel present — which is what "no installs at boot" means in practice.

## Raw journal

`journalctl -u termini-bootstrap.service --no-pager -o short-iso`

```
2026-10-12T12:22:50+00:00 termini-baked-archive systemd[1]: termini-bootstrap.service - Termini node bootstrap (register + telemetry) was skipped because of an unmet condition check (ConditionPathExists=/etc/termini/config.yaml).
2026-10-12T12:22:58+00:00 termini-baked-archive.asia-south1-a.c.termini-510609.internal systemd[1]: Starting termini-bootstrap.service - Termini node bootstrap (register + telemetry)...
2026-10-12T12:22:59+00:00 termini-baked-archive.asia-south1-a.c.termini-510609.internal termini-bootstrap[1113]: 2026-10-12T12:22:58Z [termini-bootstrap] REGISTERED node_id=01M4R11VHGEG8N3JB59F90X7T3 role=compute register_ms=150 boot_to_registered_ms=22840
2026-10-12T12:22:59+00:00 termini-baked-archive.asia-south1-a.c.termini-510609.internal systemd[1]: Started termini-bootstrap.service - Termini node bootstrap (register + telemetry).
```

## Reading it

- **`register_ms=150`** — daemon start → controller acknowledgement. This is the
  contract §12.5 metric and the deck's "<5 s" claim, met with ~33× margin. (An earlier
  manual run observed 291 ms; both are far inside budget. This is the archived one.)
- **`boot_to_registered_ms=22840`** — VM power-on → registered. Dominated by
  firmware/OS/cloud-init, **not** by registration, so it is not the claim.
- The first line is the baked unit **waiting** for the config the startup script
  writes (`ConditionPathExists`) — evidence the unit was already installed in the
  image rather than created at boot.
