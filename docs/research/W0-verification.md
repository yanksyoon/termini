# Termini — W0 Verification (tech-landscape.md + four source streams)

- Verifier: `verifier` (kanban task `t_1651d8a5`)
- Target: `docs/research/tech-landscape.md` + `01/02/03/04-*.md`, as committed in PR #3 (squash-merge `8fbcde7`, CI `sanity` SUCCESS).
- Method: read every verdict row and citation at commit `8fbcde7`; re-ran citation resolution (DNS + HTTP + redirect + title) against 142 unique URLs; cross-checked §-references against the committed contracts (`governance.md`, `controller-api.md`, `node-telemetry.md`, `infra-decisions.md`, `agent-runtime.md`); verified load-bearing factual claims (TGI archive status, AutoGen maintenance, Robusta repo identity, cloud-init canonical repo) against primary GitHub sources.
- Date: 2026-10-04.

## Verdict: FAIL — blocks W1 until the gaps below are resolved

The synthesis is structurally sound and its decisions are well-formed, but it contains
one fabricated citation domain backing the single riskiest infrastructure assumption,
plus three broken (404) links and two wrong-target citations. Per the acceptance
criterion ("any fail lists exact missing citation/verdict and blocks W1"), this is a
FAIL. W1 (root `t_6df74d95` and its contract children) must not proceed until G1–G6
are fixed.

## Check 1 — every area has a verdict + a working citation

- Verdict coverage: **PASS.** `tech-landscape.md` §1 has exactly 57 areas
  (8 bootstrap + 7 clustering + 7 networking + 8 serving + 7 agents + 13 observability
  + 7 prior art), every one with a single primary verdict. Count verified by reading the
  table.
- Citation coverage: **FAIL.** 56 areas carry an external URL; the 1 BUILD area
  (telemetry daemon + ring-buffer TSDB + rules engine + operator + command channel)
  correctly cites the frozen contracts instead of a third-party component. However:

  - **G1 (BLOCKING).** The "GPU presentation" area's sole citation is a fabricated
    domain. `https://www.lxdubuntu.com/docs/reference/devices_gpu` does not resolve
    (DNS NXDOMAIN — the host does not exist). It appears in `tech-landscape.md` §1.4
    ("GPU presentation" row), and in `02-model-serving-agent-runtime.md` §2, §7.2, §8
    (verdict table), and §11 (citations). This area therefore has **zero** working
    citations, and it is the decision ("host or LXD container, NOT nested VM") that
    stream 2 itself calls the single riskiest infra assumption. Correct source:
    `https://canonical.com/lxd/docs/latest/reference/devices_gpu/` (verified HTTP 200).

  - **G2 (broken link).** `01-bootstrap-cluster-network.md` ref [1]
    `https://canonical.com/maas/docs/what-maas-can-do` → HTTP 404 ("Page not found").
    Replace with the live MAAS docs page (e.g.
    `https://canonical.com/maas/docs/latest/explanation/...`).

  - **G3 (broken link).** `01-bootstrap-cluster-network.md` ref [28]
    `https://linuxcontainers.org/incus/news/2024_04_04_03_04.html` → HTTP 404.
    Replace with the correct Incus 6.0 LTS announcement URL.

  - **G4 (broken link).** `01-bootstrap-cluster-network.md` ref [39] and
    `tech-landscape.md` §1.3 ("LXD fan networking" row) cite
    `https://wiki.ubuntu.com/FanNetworking` → HTTP 404. Replace with a live fan/OVN
    reference (e.g. the LXD networking docs). Note the fan *regression* evidence in
    ref [41] (`discourse.ubuntu.com/...`) and ref [40] (`github.com/canonical/lxd/releases`)
    both resolve, so the underlying claim is not citation-less — only the listed link
    is dead.

## Check 2 — maturity / cost / licensing / GPU / integration-risk claims

- **PASS** on the load-bearing claims I could verify against primary sources:
  - TGI "archived Mar 2026" — CONFIRMED: `huggingface/text-generation-inference` is
    `archived: true`, last push `2026-03-21`.
  - `governance.md` §7.1 wording — CONFIRMED it literally wires "DeepSeek flash" and
    "deepseek-v4-pro" to on-prem inference, exactly as BQ-1 and contradiction #1 state.
  - `node-telemetry.md` §5 — CONFIRMED the four frozen rules
    `oom-guard-01 / disk-fail-01 / disk-degrade-01 / gpu-oos-01` exist verbatim.
  - `controller-api.md` §6 allowlist — CONFIRMED the 8 verbs
    (`vm.resize|migrate|stop|start|destroy|node.drain|node.reboot|ssh.inject_key`)
    match; the 3 extra verbs (`vm.launch`, `agent.dispatch_task`, `agent.cancel_task`)
    are correctly described as "proposed additions" in `agent-runtime.md` §17.
  - GPU sizing: DeepSeek-V4-Flash (284B MoE) / V4-Pro (1.6T) cannot fit a single
    L4/T4 — the arithmetic is correct and the doc flags it (BQ-1) rather than
    overclaiming.
- **FLAG (uncertain, non-blocking).** `02` claims AutoGen is in "maintenance mode".
  The repo `microsoft/autogen` is **not** archived (last push `2026-04-15`). "Maintenance
  mode" is softer than "archived" and may be correct per the README, but it is not
  evidenced by repo status; cite the README/maintainer notice or soften to "low activity".
- **Citation-quality note (non-blocking).** A cluster of third-party aggregator/blog
  URLs back licensing/cost/benchmark claims that should cite primary sources
  (Open WebUI license, Mistral licensing, GCP GPU pricing). These resolve (so they are
  not "broken"), but they are weak evidence. See G7.

## Check 3 — recommendations map onto W1 and are 5-day feasible

- **PASS.** §4 mapping onto the six W1 surfaces is accurate against the committed
  contracts: node registration (`node-bootstrap.md` §4-5, `controller-api.md` §4.1),
  telemetry (`node-telemetry.md`), serving API + agent image (`agent-runtime.md`
  §3-4/§8-12/§17 — see N1), remediation guardrails (`governance.md` §7.3/§9,
  `controller-api.md` §5-6, `vm-allocation.schema.json`), networking/SSH
  (`infra-decisions.md` D2/D4, `node-bootstrap.md` §7). All referenced sections exist.
- **PASS.** The shortlist leans "adopt" (LXD, cloud-init, OpenTofu, Packer, llama.cpp,
  ADK) with only two builds (governance boundary + operator loop) — feasible for a
  5-day GCP + single-GPU-VM demo. GPU choice (G2/L4 24 GB, T4 fallback) and the
  host-exposed-`llama.cpp` (not nested-VM passthrough) decision are the right call and
  match frozen `infra-decisions.md` D5.

## Check 4 — hidden assumptions / missing areas

- **N1 (non-blocking).** `tech-landscape.md` §4 ("Serving API", "Agent image") and §2
  net-guidance reference `docs/contracts/agent-runtime.md`, which was **not** present at
  commit `8fbcde7`; it landed separately in PR #4 (commit `43c3cb3`). The reference now
  resolves in `origin/main`, and every cited §-number (§3, §4, §8–§12, §17) checks out.
  No content error — a cross-PR timing note only.
- **N2 (cosmetic).** `tech-landscape.md` header cites the requirements source as
  `~/termini/project.md`; the file is now committed in-repo as `termini/project.md`
  (PR #6). Update the path reference when convenient.
- No missing W0-brief research area was found: all seven brief areas (bootstrapping,
  VM/cluster, networking overlays, model serving, agent runtimes, AIOps/observability,
  prior art) are covered.

## Gap list (severity → required fix)

| # | Severity | File(s) | Exact defect | Required fix |
|---|----------|---------|--------------|--------------|
| G1 | BLOCKING | `tech-landscape.md` §1.4; `02` §2/§7.2/§8/§11 | `lxdubuntu.com` is a fabricated domain (DNS NXDOMAIN); sole citation for GPU-presentation area | Replace with `https://canonical.com/lxd/docs/latest/reference/devices_gpu/` (200) |
| G2 | HIGH | `01` ref [1] | `canonical.com/maas/docs/what-maas-can-do` → 404 | Point at the live MAAS docs page |
| G3 | HIGH | `01` ref [28] | `linuxcontainers.org/incus/news/2024_04_04_03_04.html` → 404 | Point at the correct Incus 6.0 LTS announcement |
| G4 | HIGH | `01` ref [39]; `tech-landscape.md` §1.3 | `wiki.ubuntu.com/FanNetworking` → 404 | Point at a live LXD fan/OVN networking reference |
| G5 | MED | `01` ref [16] | cloud-init LICENSE cited via random fork `SadeghHayeri/cloud-init` | Cite `https://github.com/canonical/cloud-init/blob/main/LICENSE` |
| G6 | MED | `04` §2 ("Robusta" row) | `github.com/bucketeer-io/robusta` is a fork (parent `robusta-dev/robusta`) | Cite `https://github.com/robusta-dev/robusta` |
| G7 | LOW | `02`, `04`, `01` | Licensing/cost/benchmark claims cited via aggregators (Open WebUI license, Mistral license, AutoGen "maintenance", GPU price trackers) | Replace/supplement with primary sources, or mark the claim "uncertain" |

## Verified-correct (for the record)

- PR #3 CI (`sanity`) was SUCCESS; merge SHA `8fbcde7` matches `origin/main` history.
- 57/57 areas carry an explicit verdict.
- 4/4 contradiction resolutions and BQ-1 are correctly identified, owned, and proposed.
- Contract cross-references resolve; the `for:`-rule and command-allowlist mappings are
  exact; the two "proposed additions" are correctly marked as not-yet-in-`controller-api.md`.
