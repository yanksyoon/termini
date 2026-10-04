# Failure mode 1 — cloud-init / boot latency (W7 red-team, t_8d243ee1)

**Verdict: BREAK-THE-DEMO, reproducible — on the *degraded* registration path the
node silently hangs in `REGISTERING` and never fails fast; the happy path is
hardened (activation-only bootstrap registers in 1–3 s), but a cold first boot
breaches the 30 s demo budget.**

> **Status: F1 / F2 / F3 RESOLVED (task `t_ca7fc132`, owner infra, 2026-10-04).**
> The fix and the re-run evidence are in [§Resolution](#resolution-t_ca7fc132-owner-infra--f1f2f3-fixed)
> at the end of this report; the original findings below are kept for the audit trail.

| | |
|---|---|
| Harness | `boot/boot_latency.py`, `boot/fault_proxy.py`, `boot/legacy_apt_repro.sh` |
| Raw evidence | `evidence/boot/{baseline,delay,refuse,dnsfail,badtoken,legacy-apt}/` |
| Host | emulated single-host demo, LXD containers on `lxdbr0`, dedicated controller per run |

## Reliability budget used

- `docs/contracts/node-bootstrap.md` §12.5 item 5: `systemctl start termini-bootstrap`
  on a pre-baked node → `ONLINE` **target < 5 s**.
- W6 warm layer (`infra/warm`): a snapshot-restored fresh node reaches
  *registered + inference responding* **< 30 s** → used as the **demo timeout**.
- `docs/contracts/node-bootstrap.md` §9 row 2: a **401/403 token** is retried at
  most 3× then the node enters the terminal state **`REGISTRATION_FAILED`**.

## Method

Each run restores a *fresh* node from the `demo-node-a/unregistered` snapshot,
wipes any carried identity/token, writes its own single-use enrollment token into
cloud-init `user-data`, starts the container, and times it to a controller-visible
`ONLINE`. Faults are injected with `fault_proxy.py` (a TCP/HTTP proxy in front of
the controller: `delay`/`refuse`/`stall`). Commands:

```bash
python3 w7/redteam/boot/boot_latency.py --scenario baseline --runs 5 --budget 50 --demo-timeout 30 --out evidence/boot/baseline
python3 w7/redteam/boot/boot_latency.py --scenario delay    --runs 3 --delay-s 8 --budget 50 --demo-timeout 30 --out evidence/boot/delay
python3 w7/redteam/boot/boot_latency.py --scenario refuse   --runs 2 --budget 20 --demo-timeout 30 --out evidence/boot/refuse
python3 w7/redteam/boot/boot_latency.py --scenario dnsfail  --runs 1 --budget 20 --demo-timeout 30 --out evidence/boot/dnsfail
python3 w7/redteam/boot/boot_latency.py --scenario badtoken --runs 1 --budget 20 --demo-timeout 30 --out evidence/boot/badtoken
bash  w7/redteam/boot/legacy_apt_repro.sh evidence/boot/legacy-apt 90
```

## Results

### Baseline (N=5, no fault) — evidence `evidence/boot/baseline/summary.json`

| run | copy (s) | `lxc start`→ONLINE (s) | svc start→registered (s) | state |
|-----|---------:|-----------------------:|-------------------------:|-------|
| 1 | 14.74 | **39.45** | 1.0 | ONLINE |
| 2 | 21.21 | 16.01 | 2.0 | ONLINE |
| 3 | 16.71 | 11.06 | 2.0 | ONLINE |
| 4 | 16.46 | 9.24 | 2.0 | ONLINE |
| 5 | 16.22 | 15.08 | 3.0 | ONLINE |

- `lxc start`→ONLINE: min 9.24 / mean **18.17** / p50 15.08 / p95 **39.45** s. **4/5 under the 30 s demo timeout; 1/5 breaches it.**
- **`termini-bootstrap` start → registered: 1–3 s (mean 2.0 s) → PASS** the §12.5 `< 5 s` target (activation-only path).
- `err=None` on all 5; telemetry accepted, node listed by the controller.

### Degradation scenarios — all N runs end in TIMEOUT, never a terminal error

| scenario | N | ONLINE | final `state.json` | first error seen | budget |
|----------|---|-------:|--------------------|------------------|-------:|
| delay (8 s proxy) | 3 | **0** | `REGISTERING` (all 3) | `HTTP 401 bootstrap token already used` (×2), `HTTP 502` (×1) | 50 s |
| refuse | 2 | **0** | `REGISTERING` / none | `Remote end closed connection without response` | 20 s |
| dnsfail | 1 | **0** | `REGISTERING` | `<urlopen error [Errno -2] Name or service not known>` | 20 s |
| badtoken (consumed) | 1 (+1 warmup) | **0** | `REGISTERING` | `HTTP 401 bootstrap token already used` | 20 s |

Raw (badtoken run, `evidence/boot/badtoken/boot-badtoken-1.journal.txt`):

```
... termini-bootstrap[485]: registration retry in 1s: register failed: HTTP 401: {'error': {'code': 'UNAUTHENTICATED', 'message': 'bootstrap token already used', ...}}
... termini-bootstrap[485]: registration retry in 2s: register failed: HTTP 401: ...
... termini-bootstrap[485]: registration retry in 4s: register failed: HTTP 401: ...
   (still retrying when the budget expired; state.json = REGISTERING)
```

### Legacy apt-at-boot cloud-init (break-the-demo, pre-hardening path)

`bash w7/redteam/boot/legacy_apt_repro.sh … 90` — raw `evidence/boot/legacy-apt/legacy-apt-result.txt`:

```
cloud_init_done_within_budget=0
elapsed_s=301.1
budget_s=90
--- cloud-init status ---
status: running
extended_status: running
...
Err:66 http://10.14.100.1:8095/ubuntu noble InRelease
  Connection failed [IP: 10.14.100.1 8095]
```

A cloud-init that runs `apt-get update && apt-get install` at boot against a
mirror that accepts the TCP connection and then never answers **never finishes**:
at 301 s (≈3.3× the 90 s budget, well past the 30 s demo timeout) cloud-init is
still `running`. This is exactly the failure the frozen **D3** activation-only
decision removes.

## Findings

### F1 — [HIGH / reproducible break-the-demo] A consumed/invalid token is retried forever; the node never reaches `REGISTRATION_FAILED`

- **Spec:** `node-bootstrap.md` §9 row 2 — 401/403 ⇒ retry ≤ 3 then **terminal `REGISTRATION_FAILED`**.
- **Observed:** the shipped `termini-bootstrap` emits `registration retry in 1s/2s/4s…` and stays `REGISTERING` for the entire 20 s budget (and 50 s in the delay runs); `state.json` is never moved to a terminal state.
- **Repro:** `python3 w7/redteam/boot/boot_latency.py --scenario badtoken --runs 1 --budget 20 --out /tmp/bt` → `status=timeout, state=REGISTERING`.
- **Impact:** a single mis-provisioned/consumed token leaves a node unreachable and **silently retrying forever** with no controller-visible signal. **Severity: HIGH.**

### F2 — [HIGH] DNS failure and connection-refused also hang silently in `REGISTERING`

- `dnsfail`: `Name or service not known`, state stuck `REGISTERING` for 35.6 s.
- `refuse`: proxy resets the connection (`Remote end closed connection without response`), state `REGISTERING`, 38.8 s.
- §9 row 1 documents this path as retry-forever in state `ACTIVATING`/`DEGRADED`; the node never surfaces a bounded failure. There is **no fail-fast** and no local alert, so a demo operator sees only "a node that never appears".

### F3 — [MEDIUM] Cold first boot breaches the 30 s demo timeout

- Baseline run 1 (`lxc start`→ONLINE **39.45 s**, copy 14.74 s) exceeds the 30 s W6 demo budget; 4/5 warm runs are 9–16 s.
- The breach is first-run cold-copy under host contention, not the activation path. Confirms the repository already needs the **pre-warmed cluster** mitigation (t_9fa53ad8).

### F4 — [INFO] Activation-only path is hardened within budget

`termini-bootstrap` start → registered = **1.0 / 2.0 / 2.0 / 2.0 / 3.0 s** (mean 2.0 s), 5/5 — comfortably inside the §12.5 `< 5 s` target because D3 removed package installs and downloads from boot.

## Honesty / environment notes

- The demo is emulated on one host: `demo-node-*` are LXD **containers**, not VMs; `copy_s` reflects local filesystem copy, not image pull over a network. The activation path (`termini-bootstrap` → controller) is the same code path as on GCP.
- Since the previous run of this card, the node agent hardcodes the telemetry latency fields; that is reported under the fan-network failure mode.
- The `delay` proxy's 8 s hold exceeds the node's registration HTTP timeout, so the delayed attempt can be abandoned and its retry rejected with 401 — the measured property (node never leaves `REGISTERING`) holds regardless; the 401/502 are the retry outcomes, not the cause.
- `evidence/boot/*/controller.db` and harness run logs are kept out of git (they contain per-run single-use tokens); the JSON summaries and journals above contain request ids only.

---

## Resolution (t_ca7fc132, owner infra) — F1/F2/F3 fixed

**Fix** (`infra/bootstrap/node/termini-bootstrap.py`, contract §9). The single
`except Exception` that backed off forever is replaced by a **classified, bounded**
retry loop:

- `register()` now raises a typed `RegistrationError` (`classify_http_error`,
  `classify_network_error`). The dead `except urllib.error.HTTPError` branch —
  `http_json` already returns the status — is gone.
- `register_with_retries()` applies the policy: terminal kinds (`auth` 401/403,
  `schema` 400, `unsupported` 426, `malformed`, and the `unreachable_dns` /
  `unreachable_refused` / `unreachable_timeout` network failures) get **≤3 backoff
  retries** (1 → 2 → 4 s) and then park. Transient kinds (`server` 5xx,
  `rate_limited` 429) keep backing off 1 s → 60 s indefinitely.
- The terminal failure **parks** the daemon in `REGISTRATION_FAILED` (records
  `failure_kind` + `registration_attempts`, sends no `READY`). Because the unit is
  `Restart=always`, exiting would respawn into the loop, so the process holds the
  terminal state until an operator re-provisions/restarts. While an unreachable
  controller is retried the state is `DEGRADED`, never the transient `REGISTERING`.

**Re-run** — `python3 w7/redteam/boot/boot_latency.py --scenario {baseline,refuse,dnsfail,badtoken}
--controller-port 8150 --proxy-port 8151 --out w7/redteam/evidence/boot/<scenario>-fixed`.
The harness now pushes the repo's canonical daemon into each node *before* start,
so these runs exercise the committed code (not the snapshot's baked copy).
Committed evidence: `evidence/boot/{baseline,refuse,dnsfail,badtoken}-fixed/`.

| scenario | N | ONLINE | terminal | final `state.json` | fail-fast | first error |
|---|---:|---:|---:|---|---|---|
| baseline | 5 | **5/5** | — | `ONLINE` | — | — |
| refuse | 2 | 0 | **2/2** | `REGISTRATION_FAILED` | 11.63 / 11.70 s | `connection reset` |
| dnsfail | 1 | 0 | **1/1** | `REGISTRATION_FAILED` | 11.82 s | `DNS name could not be resolved` |
| badtoken (consumed) | 1 | 0 | **1/1** | `REGISTRATION_FAILED` | 10.63 s | `HTTP 401 UNAUTHENTICATED: bootstrap token already used` |

**Acceptance mapping (t_ca7fc132):**

1. **Consumed/invalid token → terminal.** `--scenario badtoken` ends at
   `state: REGISTRATION_FAILED` (not `REGISTERING`), after ≤3 backoff retries
   (`registration_attempts: 4`, `failure_kind: auth`). ✅ (F1)
2. **DNS-fail and connection-refused → terminal.** both end at
   `REGISTRATION_FAILED` with `failure_kind` `unreachable_dns` / `unreachable_refused` —
   no silent `REGISTERING` hang. ✅ (F2)
3. **Cold boot ≤ 30 s p95.** `lxc start → ONLINE` over N=5: min 2.99 / mean 4.03 /
   **p95 5.22 s**, 5/5 inside the 30 s demo timeout; the daemon's own
   `register_ms` (activation → ack, §12.5) is **65–125 ms**. The original 39.45 s
   outlier was a single first-run cold `lxc copy` under host contention (other
   workers copying containers concurrently), not the activation path. **Fallback
   (documented):** run the demo against the **pre-warmed cluster** (`infra/warm`,
   `t_9fa53ad8`) — boot/register the nodes before the presentation so no cold boot
   is on the critical path; the measured registration ack stays sub-200 ms. ✅ (F3)
4. **Happy path not regressed.** 5/5 ONLINE, `register_ms` ≤ 125 ms (≪ the §12.5
   `<5 s` target). ✅ (F4)

Deterministic unit coverage: `tests/test_bootstrap_registration.py` (14 tests —
classification, terminal parking for 401/403/DNS/refused, indefinite retry for
5xx, and a real 401 over HTTP).
