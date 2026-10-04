# Research Stream 3 — AIOps, Observability & Control-Loop Prior Art

Status: complete (feeds `tech-landscape.md` synthesis, task t_8550b53c)
Owner: controller
Consumes: `docs/contracts/node-telemetry.md`, `controller-api.md`, `governance.md`, `vm-allocation.schema.json`

## Purpose

Survey existing AIOps / observability / control-loop tooling so Termini does not
rebuild telemetry, dashboards, or remediation loops from scratch. For each tool we
record: what it is, maturity, license/cost, fit to the GCP Controller, an
adopt-vs-build verdict, integration risk, and a citation. The output is a verdict
table plus a concrete shortlist of tools/libraries to commit to for the 5-day demo,
mapped explicitly onto the frozen W1 contracts.

Reading key for verdicts:

- ADOPT — use the tool/library as-is (with a named integration point).
- BORROW — do not run the tool; take its design pattern / semantics into our own code.
- BUILD — implement ourselves; the frozen contracts already assume this.
- DO NOT ADOPT — rejected, with reason.

---

## 0. Executive summary

The GCP Controller already has a frozen telemetry schema, a health-state model, and
a guarded remediation command allowlist (`node-telemetry.md`, `controller-api.md`,
`governance.md`). The prior art confirms we do **not** need to run a full
observability stack (Prometheus/Grafana/Datadog) or a runbook engine
(Rundeck/StackStorm/Ansible) for the demo. What we genuinely adopt is **patterns**,
not services:

1. **BUILD** the controller-side time-series ring buffer + downsampling (already
   specified in `controller-api.md` §8), with VictoriaMetrics as the only
   "adopt if we need real retention" fallback.
2. **BUILD** a thin threshold rules engine with Prometheus-alerting `for:` semantics
   ("N consecutive samples") to drive the four frozen demo rules.
3. **BUILD** the AI operator as an idempotent **reconcile loop** (Kubernetes operator
   pattern) — advisory model output → `guardrailClamp()` → `commandAllowlist()`.
4. **BUILD** the node command channel (already frozen). Borrow Rundeck's
   execution-lifecycle audit shape; do not run Rundeck/Ansible/StackStorm.
5. **BORROW** the closest published "AI operator" guardrail design from **K8sGPT**
   (risk threshold + anonymization + auto-remediation toggle), and **Netdata's**
   per-metric ML anomaly detection as the post-demo roadmap for replacing static
   thresholds.

The single most important finding for Termini's value proposition: **SaaS
observability (Datadog, Netdata Cloud) is structurally incompatible with the
"your hardware, your data, our operations" promise.** `governance.md` classifies
application logs and customer payloads as C2 and forbids their egress (§3, §4.3,
§7). Any hosted observability product would be a C2 egress path. This converts the
observability question from "which vendor?" to "which on-prem pattern?," and the
answer is: our own telemetry daemon + redactor is the correct shape, and the prior
art is there to copy the semantics, not the binaries.

---

## 1. Observability prior art

### 1.1 Datadog

- **What**: closed-source SaaS observability (infra metrics, APM, logs, synthetics,
  RUM, security). The incumbent benchmark Termini is explicitly positioned against
  (`project.md` §Introduction: "benchmarking from already existing solutions like
  Datadog…").
- **Maturity**: very high (public company, de-facto enterprise standard).
- **License/cost**: proprietary; per-host pricing. Infrastructure $15–$23/host/mo,
  APM $31–$40/host/mo, log ingestion $0.10/GB, custom metrics $0.05/metric/host/mo.
  Bills scale with cardinality, not just hosts (skedler.com/blog/datadog-pricing;
  irinobservability.com/blog/datadog-vs-open-source).
- **Fit**: none for the controller. It is a competitor, not a component.
- **Verdict**: DO NOT ADOPT. Three independent reasons: (a) SaaS means telemetry
  leaves the customer premises — a direct violation of `governance.md` §4.3 (C2
  egress) and §7 (external-call boundary); (b) per-host + cardinality pricing is
  antithetical to a low-cost bootstrap story; (c) closed source precludes the
  transparency a governance-first product needs.
- **Integration risk**: high — would require a full C2 egress exception we are not
  willing to grant.
- **Citation**: https://www.datadoghq.com/pricing/ ; https://skedler.com/blog/datadog-pricing

### 1.2 Netdata

- **What**: open-source, real-time (per-second) monitoring agent with a built-in
  edge ML anomaly-detection engine; optional closed-source Netdata Cloud for
  cross-node aggregation/alerts.
- **Maturity**: high (76k+ GitHub stars, active, SOC 2 for the Cloud product).
- **License/cost**: Netdata Agent is **GPLv3+**; the UI and Netdata Cloud are
  closed-source (free tier + paid). Storage is highly efficient (~0.5 bytes/sample).
- **Fit**: partial. Netdata collects exactly the host/OS/GPU metrics our telemetry
  daemon collects, and its "data stays on your infrastructure" stance aligns with
  Termini. But it is a *second* agent that would run alongside our bootstrap daemon,
  and its collectors ingest process names, log data, and file paths — which
  `governance.md` classifies as C2 (§4.3).
- **Verdict**: DO NOT ADOPT the agent (redundant with our redacting telemetry
  daemon; GPLv3 copyleft; C2 exposure in default collectors). **BORROW** its two
  genuinely valuable ideas: (1) per-metric, unsupervised ML anomaly detection
  ("18 models per metric, 99% false-positive reduction") as the roadmap to replace
  our static thresholds after the demo; (2) sub-2-second anomaly detection latency,
  which our 1–2 s demo polling cadence already targets.
- **Integration risk**: low if we only borrow the pattern; medium if we run the
  agent (copyleft + C2 + duplicate collection).
- **Citation**: https://github.com/netdata/netdata ; https://www.netdata.cloud/ai

### 1.3 Prometheus + Grafana

- **What**: the de-facto OSS observability stack. Prometheus (metrics TSDB, pull
  model, PromQL) + Alertmanager (alert dedup/grouping/routing) + Grafana
  (dashboards).
- **Maturity**: very high. Prometheus is a CNCF graduated project; Grafana is the
  dominant visualization layer.
- **License/cost**: Prometheus and Alertmanager are **Apache 2.0**; Grafana is
  **AGPL-3.0**. Free to self-host.
- **Fit**: partial. Prometheus is pull-based (scrapes `/metrics` exporters), while
  Termini is push-based (nodes POST JSON batches over `node_token`-auth'd HTTPS,
  `node-telemetry.md` §1). Adopting Prometheus wholesale would require either a
  Pushgateway/remote-write adapter or renaming our canonical C0/C1 fields to
  Prometheus exposition format — both cost work and weaken the governance allowlist.
- **Verdict**: BUILD our own store/rules (the frozen contracts already assume a
  ring buffer + downsample in `controller-api.md` §8 and a threshold engine in
  `node-telemetry.md` §5). **BORROW** Prometheus/Alertmanager semantics: metric
  naming (snake_case, explicit units), and the "for N consecutive samples" alerting
  rule shape that our four frozen rules already use. Grafana is an OPTIONAL ADOPT
  only if we later expose a Prometheus `/metrics` scrape endpoint from the
  controller — not needed for the demo (the frontend renders charts from
  `GET /metrics`, `controller-api.md` §4.2).
- **Integration risk**: medium for the full stack (pull-model mismatch, extra
  services, AGPL on Grafana if modified and served); low for borrowing semantics.
- **Citation**: https://prometheus.io/ ; https://github.com/prometheus/alertmanager ;
  https://github.com/grafana/grafana

### 1.4 Zabbix

- **What**: enterprise-class OSS monitoring (network, servers, services), agent +
  server + web UI, proxies, discovery.
- **Maturity**: very high (20+ years, 300k+ deployments).
- **License/cost**: GPLv2 through 6.4; **AGPLv3 from 7.0** onward. Free to
  self-host; support sold separately.
- **Fit**: poor. Zabbix is agent-centric, heavyweight (server + DB + frontend), and
  oriented to classic IT estates rather than a programmatic AI fleet with a custom
  push schema. Its templating/trigger model duplicates what our threshold engine
  does in ~100 lines.
- **Verdict**: DO NOT ADOPT. No capability it offers is missing from the frozen
  contracts, and its AGPLv3 shift (7.0) adds network-copyleft friction if we ever
  modify and serve it.
- **Integration risk**: medium (operational weight, no programmatic push-model fit).
- **Citation**: https://www.zabbix.com/license ; https://github.com/zabbix/zabbix

### 1.5 VictoriaMetrics (candidate TSDB, surveyed as the "adopt" fallback)

- **What**: high-performance single-binary time-series database; drop-in for
  Prometheus, and accepts InfluxDB line protocol, OpenTSDB, Graphite, and OTLP.
- **Maturity**: high, production-proven at scale.
- **License/cost**: **Apache 2.0** (single-node is fully OSS).
- **Fit**: good — it is the only surveyed component whose ingestion model (push via
  InfluxDB line protocol / Prometheus remote-write) matches our push-telemetry
  design, and it would give us retention, PromQL queries, and downsampling for free
  if the demo grows beyond an in-memory ring buffer.
- **Verdict**: OPTIONAL ADOPT. Recommended as the designated fallback if W2 finds
  the ring buffer insufficient (persistence, >1 node, longer retention). Not
  required for the 5-day demo.
- **Integration risk**: low (single binary, Apache 2.0, our schema maps cleanly to
  InfluxDB line protocol).
- **Citation**: https://github.com/VictoriaMetrics/VictoriaMetrics ;
  https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/

---

## 2. Control-loop & runbook automation prior art

### 2.1 Ansible (core / AWX / Event-Driven Ansible)

- **What**: push-based (SSH) configuration automation; AWX is the web/API/task
  engine; Event-Driven Ansible (EDA) adds if-this-then-that "rulebooks" triggered by
  external event sources.
- **Maturity**: very high.
- **License/cost**: Ansible core GPLv3; AWX Apache 2.0 (upstream of commercial Red
  Hat Ansible Automation Platform); EDA rulebook engine ships in AAP.
- **Fit**: partial. Ansible's remediation strength is SSH push of idempotent
  playbooks. Termini's frozen design instead has the node bootstrap daemon execute
  LXD commands locally, driven by a `node_token`-auth'd command channel
  (`controller-api.md` §6) — no SSH push, no agentless inventory. Ansible would add
  a second execution path and a second credential surface.
- **Verdict**: DO NOT ADOPT for remediation execution. **BORROW** two ideas:
  (1) idempotent actions (every remediation command is safe to retry — our command
  envelope + ack already implies this); (2) EDA's rulebook "source → rule →
  condition → action" shape, which mirrors our frozen threshold rules
  (`node-telemetry.md` §5) almost exactly.
- **Integration risk**: medium if adopted (SSH trust, inventory, Python runtime on
  nodes); low if we borrow the pattern.
- **Citation**: https://github.com/ansible/awx ;
  https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.4/html/getting_started_with_event-driven_ansible_guide/ansible-rulebooks

### 2.2 Rundeck (PagerDuty Process Automation)

- **What**: open-core runbook automation — web console, CLI, WebAPI, job scheduling,
  RBAC, execution audit; executes scripts/workflows across nodes.
- **Maturity**: high (2010–present; acquired by PagerDuty 2020).
- **License/cost**: Community Edition **Apache 2.0** (free, self-managed); paid
  Enterprise / managed "Process Automation" (custom quote, ~$51k/yr reported).
  Java/Groovy codebase.
- **Fit**: partial. Rundeck's execution lifecycle — job queued → acked → running →
  succeeded/failed with result, all audit-logged — is exactly the model our command
  channel already implements (`controller-api.md` §6 ack endpoint). But Rundeck is a
  JVM service with its own DB, scheduler, and RBAC; adopting it would duplicate the
  command channel and the `guardrail_audit` ledger.
- **Verdict**: DO NOT ADOPT. **BORROW** the execution-lifecycle + audit shape (we
  already have it as `command_id` + `ack {status, detail, result}` +
  `actor: ai_operator` audit records).
- **Integration risk**: medium (operational weight, redundant audit store).
- **Citation**: https://github.com/rundeck/rundeck ; https://www.rundeck.com/

### 2.3 StackStorm ("IFTTT for Ops")

- **What**: event-driven automation — sensors (inbound events) → rules → actions →
  workflows; 160+ integration packs; auto-remediation and security response.
- **Maturity**: mature; Apache 2.0; actively maintained (commits within the last
  quarter), though its community momentum is smaller than Ansible/Rundeck.
- **License/cost**: **Apache 2.0**.
- **Fit**: partial. StackStorm is the closest full prior art for the "event →
  decide → act" pipeline that our AI operator implements. But it is a multi-service
  Python/MongoDB/RabbitMQ stack, and its rule engine is deterministic, not
  model-driven.
- **Verdict**: DO NOT ADOPT (heavyweight; our loop is ~one reconcile function).
  **BORROW** the sensor→rule→action→workflow mental model as the shape of the AI
  operator's pipeline.
- **Integration risk**: medium-high (deployment footprint).
- **Citation**: https://github.com/StackStorm/st2 ; https://stackstorm.com/

### 2.4 Event-driven remediation patterns (architectural prior art)

The strongest *architectural* prior art for Termini's AI operator is the
**Kubernetes controller / operator reconcile loop**:

> Observe actual state → compare to desired state → act → repeat, idempotently,
> reporting health via status conditions.

This is precisely the "observe → decide → act → verify" loop our operator needs,
and it is what the frozen contracts already describe (`node-telemetry.md` §5 trigger
linkage; `governance.md` §7.3 advisory-output; `controller-api.md` §6 command
allowlist). Key properties we adopt: idempotent reconciliation, status conditions
(`online/degraded/offline`, VM `state`), and "a controller manages one resource
kind" error-isolation guidance.

- **Verdict**: ADOPT THE PATTERN (no software dependency).
- **Citation**: https://kubernetes.io/docs/concepts/extend-kubernetes/operator/ ;
  https://book.kubebuilder.io/reference/good-practices.html

---

## 3. OSS "AI operator" prior art

### 3.1 K8sGPT (closest direct prior art)

- **What**: CNCF Sandbox project (accepted Dec 2023) that scans a Kubernetes cluster
  with SRE analyzers and uses an LLM backend to explain failures in plain English;
  runs in-cluster as an operator that can notify, record, and **fix**.
- **Maturity**: early but real (CNCF Sandbox, Apache 2.0, active community).
- **License/cost**: **Apache 2.0**.
- **Fit**: strong. Its operator CRD already encodes the exact three guardrails
  Termini needs: `ai.enabled`, `anonymized: true` (redact identifying data before it
  reaches the LLM — our `governance.md` redactor), and
  `autoRemediation.enabled + riskThreshold` (only auto-act when confidence exceeds a
  threshold — our deterministic `guardrailClamp()` is the stricter analog).
- **Verdict**: ADOPT AS REFERENCE / BORROW the guardrail design. We do not run it
  (it targets Kubernetes, not LXD), but it is the strongest published evidence that
  the "advisory LLM + deterministic guardrail + allowlisted actions" architecture is
  the right one, and it validates our anonymization-before-LLM boundary.
- **Integration risk**: n/a (reference only).
- **Citation**: https://k8sgpt.ai/ ; https://www.cncf.io/projects/k8sgpt/

### 3.2 Netdata edge ML (anomaly detection, covered in §1.2)

Netdata's per-metric, edge-trained, unsupervised ML anomaly detection is the
published reference for the *detection* half of the AI operator. For the demo we use
deterministic thresholds (frozen in `node-telemetry.md` §5); Netdata's approach is
the roadmap for learned baselines (e.g., detecting a *gradual* disk-latency climb
before it crosses the 50 ms hard threshold).

- **Verdict**: BORROW as post-demo roadmap.
- **Citation**: https://www.netdata.cloud/ai ; https://github.com/netdata/netdata

### 3.3 Rundeck / StackStorm "AI-assisted" features

Both vendors market AI-assisted runbook generation (Rundeck/PagerDuty) and
event-driven auto-remediation (StackStorm). These are marketing-adjacent features
rather than reusable libraries; the deterministic engine underneath is what we
borrow (§2.2, §2.3), not the LLM glue.

---

## 4. Fit to the GCP Controller (explicit mapping to frozen contracts)

| Controller need | Frozen contract | What this survey concludes |
|---|---|---|
| Node telemetry polling / ingest | `node-telemetry.md` §1–§7 | BUILD (already frozen). Push model, not Prometheus pull. |
| Health state (online/degraded/offline) | `controller-api.md` §8 cadence | BUILD; borrow Prometheus `up`+`for` semantics; no server needed. |
| Time-series storage + charts | `controller-api.md` §8 (raw→5s/1m/5m), `GET /metrics` | BUILD ring buffer; VictoriaMetrics = fallback. |
| Threshold rules (4 demo rules) | `node-telemetry.md` §5 | BUILD thin engine; borrow Alertmanager/EDA rule shape. |
| AI remediation loop | `governance.md` §7.3, `controller-api.md` §5 | BUILD as reconcile loop; advisory output + clamp + allowlist. |
| Remediation execution | `controller-api.md` §6 command allowlist | BUILD node command channel; borrow Rundeck lifecycle. |
| Audit/traceability | `governance.md` §8, §9 | BUILD (already frozen); do not import a second audit store. |

### 4.1 Mapping to W1 controller contracts

- **Telemetry schema** (`node-telemetry.md`): our store is the *consumer* of the
  canonical C0/C1 field names. No external tool imports. Adopting Prometheus would
  force a rename to exposition format and weaken the governance allowlist — rejected.
- **Remediation actions** (`controller-api.md` §6 allowlist —
  `vm.resize | vm.migrate | vm.stop | vm.start | vm.destroy | node.drain |
  node.reboot | ssh.inject_key`): the AI operator may only *select* among these; it
  never emits free-form shell. Ansible/Rundeck/StackStorm are **not** wired in — the
  node daemon executes these directly over the command channel. This is exactly the
  `commandAllowlist()` that `governance.md` §9 requires.
- **Guardrails** (`governance.md` §7.3 + `controller-api.md` §5
  `guardrailClamp()`): the model output is advisory and always passes the
  deterministic clamp validator + command allowlist before any action. K8sGPT's
  `riskThreshold` is the closest published analog; we adopt a stricter deterministic
  clamp rather than a probabilistic threshold, because the frozen contracts require
  an immutable, replayable audit record for every autonomous change.

---

## 5. Concrete shortlist (commit-to for the demo)

**Build (primary):**

1. Controller telemetry ingest + in-memory ring-buffer TSDB with raw→5s/1m/5m
   downsampling (satisfies `controller-api.md` §8 and `GET /metrics`).
2. Deterministic threshold rules engine with "N consecutive samples" semantics
   (borrowed from Prometheus `for:` / EDA rulebooks); implements the four frozen
   rules `oom-guard-01`, `disk-fail-01`, `disk-degrade-01`, `gpu-oos-01`.
3. AI operator as an idempotent reconcile loop (Kubernetes operator pattern);
   advisory external-model call → `guardrailClamp()` → `commandAllowlist()`.
4. Node command channel + ack lifecycle (already frozen; borrow Rundeck's
   `acked → running → succeeded/failed` + result + audit shape).

**Optional adopt (only if needed, not for the demo):**

5. VictoriaMetrics single-binary (Apache 2.0) as the TSDB if persistence/retention
   or multi-node scale is needed — feed via InfluxDB line protocol or Prometheus
   remote-write from the controller.
6. Grafana (AGPL-3.0) dashboards, gated on exposing a Prometheus `/metrics` scrape
   endpoint from the controller — the built-in frontend already covers the demo.

**Borrow (patterns/prior art, no runtime dependency):**

7. K8sGPT guardrail design (`anonymized`, `autoRemediation` risk threshold) — the
   reference for our anonymization + clamp boundary.
8. Netdata per-metric edge ML anomaly detection — post-demo roadmap to replace
   static thresholds with learned baselines.
9. Prometheus/Alertmanager metric-naming and alert-rule semantics.

**Explicitly rejected (with reason):** Datadog (SaaS = C2 egress + cost), Netdata
agent (redundant + GPLv3 + C2 collectors), Zabbix (heavyweight + AGPLv3 + no
push-model fit), Ansible/AWX (SSH push conflicts with command channel), Rundeck
(redundant audit/scheduler), StackStorm (heavyweight, deterministic engine already
covered).

---

## 6. Security & audit implications

1. **SaaS observability is a C2 egress path.** Datadog and Netdata Cloud would
   transmit data that `governance.md` §4.3 classifies as C2 (application logs,
   payloads) or requires redaction (§4.2). Hosted observability is therefore
   rejected on governance grounds, not just cost. This keeps the "data stays
   on-prem" promise enforceable.
2. **A second agent = a second redaction surface.** Netdata/Zabbix/Prometheus
   node_exporter collect process argv, environment, log lines, and file paths — all
   C2. Running one alongside the bootstrap daemon would either leak C2 or require
   re-implementing the redactor in a second tool. The frozen `node-telemetry.md`
   design (one daemon, redactor as the single egress gate) is the correct shape.
3. **License posture.** Apache 2.0 (Prometheus, Alertmanager, VictoriaMetrics,
   Rundeck CE, StackStorm, K8sGPT) is safe to embed/serve. **AGPLv3** (Grafana,
   Zabbix ≥7.0) and **GPLv3** (Netdata agent, Ansible core) impose copyleft
   obligations if we modify and serve them — another reason to borrow rather than
   embed these. We do not ship any modified copyleft component.
4. **AI operator audit trail.** The `command_id` + ack + `guardrail_audit` +
   `governance_chain_tip` hash chain (`governance.md` §8, `controller-api.md` §5.4)
   already provide the traceability that Rundeck/StackStorm audit logs provide. Do
   not import a second audit store; keep one canonical ledger. Every autonomous
   action must remain reconstructable as `telemetry trigger → advisory model output
   → clamp → allowlisted command → ack result` (`node-telemetry.md` §5).
5. **External model call boundary.** Any "AI observability" feature must respect
   `governance.md` §7: model input is C0 telemetry only, via the static
   `operator-prompt` template, with `externalModelCall()` asserting all-C0 fields.
   K8sGPT's `anonymized: true` is the reference pattern; we enforce it
   deterministically.

---

## 7. Open items (handed to synthesis, none blocking)

- **TSDB choice (ring buffer vs VictoriaMetrics)** is a W2 decision, not W0. This
  doc recommends the ring buffer for the demo; flag for `tech-landscape.md` that
  VictoriaMetrics is the designated upgrade path. Owner: architect.
- **Grafana integration** is explicitly deferred; the built-in frontend satisfies
  the demo. If dashboards are a judged criterion, expose a Prometheus `/metrics`
  adapter as a stretch goal. Owner: controller + frontend.

---

## Appendix A — Verdict table (one row per tool/area)

| Tool / area | Maturity | License / cost | Fit to Termini | Verdict | Integration risk |
|---|---|---|---|---|---|
| Datadog | very high | closed SaaS; $15–40/host/mo + per-GB | none (competitor; C2 egress) | DO NOT ADOPT | high |
| Netdata agent | high | GPLv3+ agent; closed Cloud | partial (overlaps telemetry daemon) | DO NOT ADOPT; BORROW ML detection | medium |
| Prometheus | very high | Apache 2.0 | partial (pull vs push) | BORROW naming/rules; BUILD store | medium (full stack) |
| Alertmanager | very high | Apache 2.0 | partial (dedup/group/routing) | BORROW semantics | low |
| Grafana | very high | AGPL-3.0 | optional (dashboards) | OPTIONAL ADOPT (stretch) | low–medium |
| VictoriaMetrics | high | Apache 2.0 | good (push protocols) | OPTIONAL ADOPT (TSDB fallback) | low |
| Zabbix | very high | GPLv2→AGPLv3 (7.0) | poor (heavy, agent-centric) | DO NOT ADOPT | medium |
| Ansible / AWX / EDA | very high | GPLv3 / Apache 2.0 (AWX) | partial (SSH push conflicts) | DO NOT ADOPT; BORROW idempotency + rulebooks | medium |
| Rundeck | high | Apache 2.0 CE; PA enterprise | partial (runbook + audit; JVM) | DO NOT ADOPT; BORROW lifecycle | medium |
| StackStorm | mature | Apache 2.0 | partial (event-driven; heavy) | DO NOT ADOPT; BORROW pipeline shape | medium–high |
| K8s operator / reconcile loop | mature pattern | Apache 2.0 (controller-runtime) | strong (our loop = reconcile) | ADOPT PATTERN | low |
| K8sGPT | early (CNCF Sandbox) | Apache 2.0 | strong (guardrail + anonymize) | ADOPT AS REFERENCE | n/a |
| Telemetry daemon + redactor + TSDB + rules + operator + command channel | — | ours | core | BUILD (frozen) | n/a |

## Appendix B — Citation index

- Datadog pricing: https://www.datadoghq.com/pricing/ ; https://skedler.com/blog/datadog-pricing ; https://irinobservability.com/blog/datadog-vs-open-source
- Netdata: https://github.com/netdata/netdata ; https://www.netdata.cloud/ai ; https://www.netdata.cloud/open-source
- Prometheus: https://prometheus.io/ ; Alertmanager license: https://github.com/prometheus/alertmanager
- Grafana: https://github.com/grafana/grafana
- Zabbix license: https://www.zabbix.com/license ; https://github.com/zabbix/zabbix
- VictoriaMetrics: https://github.com/VictoriaMetrics/VictoriaMetrics ; https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/
- Ansible/AWX/EDA: https://github.com/ansible/awx ; https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.4/html/getting_started_with_event-driven_ansible_guide/ansible-rulebooks
- Rundeck: https://github.com/rundeck/rundeck ; https://www.rundeck.com/
- StackStorm: https://github.com/StackStorm/st2 ; https://stackstorm.com/
- Kubernetes operator pattern: https://kubernetes.io/docs/concepts/extend-kubernetes/operator/ ; https://book.kubebuilder.io/reference/good-practices.html
- K8sGPT: https://k8sgpt.ai/ ; https://www.cncf.io/projects/k8sgpt/
