# Evidence — agent VM image (t_d1960fd6)

Everything here was produced by running the committed scripts on this host's
LXD 5.21.8 (container variant). Reproduce with:

```bash
TERMINI_OUT_DIR=/var/lib/termini/build image/agent-vm/verify-agent-image.sh
```

| File | Produced by | Shows |
|------|-------------|-------|
| `build.log` / `verify.log` | `build-agent-image.sh --build` / `verify-agent-image.sh` | the full clean bake and the boot verdict |
| `offline-acceptance.json` | `tools/offline-acceptance.sh` inside the booted VM | per-check acceptance verdict (`verdict: pass`) |
| `first-boot.json` | `first-boot-preflight.sh` at boot | `verdict: pass`, the **measured** `network` block, `network_downloads_at_boot: 0`, per-module import status |
| `offline-acceptance.net-download-negative.json` | `tools/offline-acceptance.sh` on a VM given a NIC + a real 2×2 MiB fetch | the acceptance **fails**: `network_downloads_at_boot` reports the received bytes (4,264,377) — the counter is real, so a boot that downloads cannot pass |
| `selfcheck.json` | `termini-agent selfcheck` | every runtime + framework import `ok`; `/etc/termini/agent.yaml` valid |
| `status.json` | `curl 127.0.0.1:9101/status` | the runtime is live and `governance.egress: deny` |
| `boot-journal.txt` | `journalctl -b -u termini-agent` | boot-time timeline (no fetch, no failure) |
| `image-manifest.json` | `build-agent-image.sh` | alias, immutable tag, artifact sha256/size, all pins — **refreshed 2026-10-16 by `t_94303203`** (`agent-vm-2026.10.0+g68763f60ef6d`, sha256 `eb7806…`); the per-check files above still describe the 2026-10-12 bake (`…+gb9f934d8f544`, sha256 `c3d039…`) |

The key acceptance facts: the VM was launched with a profile that has **no
network interface**, `termini-agent` reached `active`, `termini-agent selfcheck`
imported every agent dependency offline, and `network_downloads_at_boot: 0` was
**measured** from the guest's `/proc/net/dev` (zero non-loopback RX bytes), not
asserted. The negative evidence shows the same check failing once the VM is
given a NIC and performs a real fetch.
