# Termini — Regulatory Landscape (region-ranked)

Status: draft — EU section complete; non-EU rows and synthesis sections are appended by sibling tasks.
Owner: `pitch` (EU rows — kanban task `t_9fae6ae9`). Non-EU rows: `pitch` (`t_0269f452`). Design-control mapping + priority regions: `architect` (`t_9128b7b9`). Adversarial review: `verifier` (`t_d0635046`).
Consumed by: W9 root synthesis (`t_149e890a`), `docs/compliance/certifications.md`, and go-to-market positioning.
Posture under analysis: **customer data stays on customer hardware; Termini operates the fleet remotely** ("your hardware, your data, our operations").

Citation convention: every regulation links to its primary source on EUR-Lex (official text) or a regulator page. Every "why the on-prem design helps" claim names the specific control in `docs/contracts/governance.md` / `docs/architecture.md` that satisfies it. Where the design does NOT satisfy a requirement, it is listed in §4 "Open gaps", not smoothed over.

---

## 1. The critical question, answered

**When Termini performs remote "our operations" access from outside the customer's environment, does that create (a) a controller/processor ambiguity, (b) a transfer issue, or (c) an adequacy/SCC requirement?**

**Answer: (a) no ambiguity in the standard operating path — Termini is a processor; (b) the transfer question is real but is designed away for the standard path, and resurfaces for any access that could touch personal data; (c) SCCs (not adequacy) are the practical instrument, and they must ship as standard rather than optional.**

### (a) Controller vs processor — not ambiguous, and the architecture is what keeps it that way

The roles are **functional, not contractual**: they follow what each party actually decides, not what the contract says ([EDPB Guidelines 07/2020 on controller/processor concepts](https://www.edpb.europa.eu/system/files/2023-10/EDPB_guidelines_202007_controllerprocessor_final_en.pdf)). A controller "determines the purposes and means" — the why and how — of processing ([GDPR Art. 4(7)](https://eur-lex.europa.eu/eli/reg/2016/679/oj)); a processor processes "on behalf of" the controller and only on documented instructions ([Art. 4(8)](https://eur-lex.europa.eu/eli/reg/2016/679/oj), [Art. 28(3)(a)](https://eur-lex.europa.eu/eli/reg/2016/679/oj)). A processor that starts determining its own purposes/means becomes a controller for that processing ([EDPB 07/2020 §2.1.4](https://www.edpb.europa.eu/system/files/2023-10/EDPB_guidelines_202007_controllerprocessor_final_en.pdf), [Art. 28(10)](https://eur-lex.europa.eu/eli/reg/2016/679/oj)).

- The customer decides what workloads/agents run on their data → **controller**.
- Termini operates the infrastructure under instruction → **processor**, bound by a DPA ([Art. 28(3)](https://eur-lex.europa.eu/eli/reg/2016/679/oj)).

The reason this is *not* ambiguous is architectural, not just contractual: Termini's remote operating loop **never processes the customer's C2 data**. The AI operator sees only C0 hardware/OS telemetry (the external model call is gated by `externalModelCall()` to all-C0 input with a static, versioned template), and model output is **advisory** — it survives `guardrailClamp()` → `commandAllowlist()` before any action (`governance.md` §7, §7.2, §7.3). Because Termini neither sees nor decides over the customer's actual personal data, it does not "determine the purposes and means" of that processing — it stays in the processor lane.

**Residual risk (flagged, not glossed):** joint controllership ([Art. 26](https://eur-lex.europa.eu/eli/reg/2016/679/oj)) would arise if Termini ever processed C2 data to make substantive decisions about the customer's business. The design forbids this today (`G-AR-1`: node→controller never carries C2; `G-AR-2`: the controller carries task content only transiently, never persists or model-processes it — `governance.md` §12). This is a **design guarantee that must be continuously enforced**, not a legal given; see gap G1/G4.

### (b) The transfer issue — real, but engineered out of the standard path

GDPR does not define "transfer," and the EDPB has confirmed that **remote access from a third country to personal data located in the EEA is a transfer**: "it is sufficient for making available personal data … if, for example, personal data is accessed remotely from a third country" ([EDPB Guidelines 05/2021 on the interplay of Art. 3 and Chapter V, §13](https://www.edpb.europa.eu/system/files/2023-02/edpb_guidelines_05-2021_interplay_between_the_application_of_art3-chapter_v_of_the_gdpr_v2_en_0.pdf)). So an operator in a non-EEA country who remotely reads a customer's personal data is a transfer under [Art. 44](https://eur-lex.europa.eu/eli/reg/2016/679/oj).

Termini's answer is that the standard remote-ops path **carries no personal data**, so nothing is "made available":

- The egress allowlist admits only **C0** (hardware/OS metrics — not personal data) and transformed **C1** identifiers (hashed hostname, truncated IP, hashed MAC) (`governance.md` §4).
- **C2 — application logs, customer payloads, prompts, file contents, credentials — never egresses**, enforced fail-closed by the on-node redactor and the controller `governanceGate` (`governance.md` §4.3, §6, §9).
- The remote channel is an **outbound-only mTLS WebSocket** carrying typed commands (`vm.launch`, `node.drain`, …) selected from a closed `commandAllowlist()`, never free-form shell (`governance.md` §7.3; `architecture.md` §5). An operator cannot "pull" arbitrary customer data through it.

If the C2-never-egresses property holds (and it is testable — audit chain + `governance_chain_tip` reconciliation, `governance.md` §8.3), the standard operating path effects **no transfer of personal data**, and Chapter V is not engaged.

**This is the honest, conditional claim:** the transfer question is answered by the architecture *only for the standard path*. It does not cover break-glass access that could surface personal data — see (c) and gap G2.

### (c) Adequacy/SCC — SCCs are the default; adequacy is not required if SCCs are used

For any access path that could expose personal data to Termini staff outside the EEA (break-glass SSH into a customer VM, or any future feature that renders customer content to an operator), the GDPR transfer instruments are:

- **SCCs** — [Commission Implementing Decision (EU) 2021/914](https://eur-lex.europa.eu/eli/dec_impl/2021/914/oj), Module 2 (processor-to-processor), adopted under [Art. 46(2)(c)](https://eur-lex.europa.eu/eli/reg/2016/679/oj). This is the appropriate instrument because Termini is a processor; it should ship as a standard attachment to the DPA, not be negotiated ad hoc.
- **Adequacy decision** ([Art. 45](https://eur-lex.europa.eu/eli/reg/2016/679/oj)) — only if the operator's location enjoys one; **not needed where SCCs are executed**. Adequacy is therefore *not* a prerequisite of the design; SCCs are.
- **Derogations** ([Art. 49](https://eur-lex.europa.eu/eli/reg/2016/679/oj)) — narrow and case-by-case; unsuitable as a standing basis.

**Territorial scope (Art. 3), which binds the whole analysis.** GDPR applies to Termini's remote operations **regardless of where Termini is established**, because processing occurs "in the context of the activities of an establishment … in the Union" — the customer's establishment ([Art. 3(1)](https://eur-lex.europa.eu/eli/reg/2016/679/oj)). Termini cannot opt out of GDPR by operating offshore; conversely the customer stays accountable for Termini's processing ([Art. 24](https://eur-lex.europa.eu/eli/reg/2016/679/oj) accountability + [Art. 28](https://eur-lex.europa.eu/eli/reg/2016/679/oj) DPA). The EDPB notes that even where Chapter V does not formally apply, remote processing still carries residual risk (e.g. third-country government access) the controller must assess ([EDPB 05/2021 §12–13](https://www.edpb.europa.eu/system/files/2023-02/edpb_guidelines_05-2021_interplay_between_the_application_of_art3-chapter_v_of_the_gdpr_v2_en_0.pdf)).

---

## 2. Region-ranked table — EU (priority region)

Ordering within EU: GDPR first (universal gatekeeper for every EU buyer), then the AI Act (the product's core technology), then NIS2 (infrastructure buyers), then the Data Act (a positive differentiator, not a burden), then sector overlays DORA and MDR. Columns: region | regulation (article/section) | why the on-prem design helps (named control) | buyer/segment.

| Region | Regulation (article/section) | Why the on-prem design helps (named control) | Buyer / segment |
|---|---|---|---|
| EU | **GDPR** — territorial scope ([Art. 3](https://eur-lex.europa.eu/eli/reg/2016/679/oj)) | GDPR binds Termini via the customer's EU establishment, so the customer stays accountable and Termini is captured as processor; the on-prem posture means the customer retains physical control of the data's location (control: **data-at-rest on customer hardware**, `architecture.md` §4.5) rather than exporting accountability to a cloud. | Every EU data controller considering any AI/ops vendor. |
| EU | **GDPR** — processor & sub-processor flow-down ([Art. 28(2)–(4)](https://eur-lex.europa.eu/eli/reg/2016/679/oj)) | Termini's processing surface is shrunk to C0/C1 operational metadata (control: **on-node redactor + C0/C1 allowlist + C2-never-egresses**, `governance.md` §4/§6), and the only "sub-processor" that sees anything (the external operator model) is gated to **all-C0 input** (control: `externalModelCall()`, `governance.md` §7). A DPA (Art. 28(3)) and sub-processor disclosure (Art. 28(2)/(4)) are still required — see gap G1/G8. | Any EU controller; strongest for data-sensitive SMBs who cannot audit a cloud processor's sub-processor chain. |
| EU | **GDPR** — security of processing ([Art. 32(1)](https://eur-lex.europa.eu/eli/reg/2016/679/oj): encryption (a), confidentiality/integrity/availability/resilience (b), access restoration (c), testing (d)) | Named controls map 1:1 — (a) **mTLS encryption in transit** (D6 outbound-only channel) + **data-at-rest on customer hardware under the customer's keys** (`architecture.md` §5); (b) **fail-closed redactor** + **`governanceGate` re-verification** (`governance.md` §6/§9); (c) SSH access via **short-lived (8 h) principal-bound CA certs, private keys never transit the controller** (`architecture.md` §5, D4/R15); (d) **tamper-evident audit chain** (`governance_chain_tip`, `governance.md` §8.3). | Any EU controller; the Art. 32 measures are also exactly what a DPA auditor asks to see. |
| EU | **GDPR** — international transfers ([Art. 44–49](https://eur-lex.europa.eu/eli/reg/2016/679/oj)) | Standard remote-ops path carries only C0/C1, so **no personal data is "made available" to a third country and Chapter V is not triggered** (control: **C2-never-egresses + typed `commandAllowlist()`**, `governance.md` §4.3/§7.3). For residual break-glass access, **SCCs ([2021/914](https://eur-lex.europa.eu/eli/dec_impl/2021/914/oj), Module 2)** are the standing instrument (gap G2). | Any EU controller with operators/vendors outside the EEA; the decisive question their DPO asks of a remote-ops vendor. |
| EU | **EU AI Act** ([Reg. (EU) 2024/1689](https://eur-lex.europa.eu/eli/reg/2024/1689/oj)) — risk tiers: prohibited ([Art. 5](https://eur-lex.europa.eu/eli/reg/2024/1689/oj)), high-risk ([Art. 6](https://eur-lex.europa.eu/eli/reg/2024/1689/oj) + Annex III), limited/transparency ([Art. 50](https://eur-lex.europa.eu/eli/reg/2024/1689/oj)), GPAI ([Art. 51–55](https://eur-lex.europa.eu/eli/reg/2024/1689/oj)) | Termini's own AI (the operator loop) is **limited-risk**: it acts on C0 infra telemetry, is **advisory with a human-approvable allowlist** (`guardrailClamp` → `commandAllowlist`, `governance.md` §7.3) — not an Annex III high-risk use. The GPAI models run **on-prem via llama.cpp on the customer's GPU** (`architecture.md` §4.5), so Termini is a downstream deployer, **not the "provider" of a GPAI model** (Art. 3(3)/53 obligations fall on the model's provider, e.g. DeepSeek, not Termini). Residual obligations: **Art. 50 transparency** if agents interact with natural persons (gap G3); customer-side high-risk deployment may pull the platform into scope (gap G4). | AI-platform buyers; the "we are not the GPAI provider and the loop is advisory" story is the core AI-Act position. |
| EU | **NIS2** ([Directive (EU) 2022/2555](https://eur-lex.europa.eu/eli/dir/2022/2555/oj)) — risk-management ([Art. 21](https://eur-lex.europa.eu/eli/dir/2022/2555/oj)), incident reporting ([Art. 23](https://eur-lex.europa.eu/eli/dir/2022/2555/oj): 24 h early warning / 72 h notification / 1-month final), essential vs important ([Annex I/II](https://eur-lex.europa.eu/eli/dir/2022/2555/oj)) | The customer (essential/important entity) keeps its network & info systems on its own hardware and under its own governance (control: **data-at-rest on customer hardware + customer-retained key custody**, `architecture.md` §5 invariant #4). Termini's **telemetry + operator loop** is a detection signal that feeds the customer's Art. 23 incident reporting. Supply-chain duty (Art. 21(2)(d)) obliges the customer to assess Termini as a supplier — the design must *produce* security evidence (gap G5). | Critical-sector SMBs: energy, transport, health, digital infrastructure, managed-service providers (Annex I/II). |
| EU | **Data Act** ([Reg. (EU) 2023/2854](https://eur-lex.europa.eu/eli/reg/2023/2854/oj)) — cloud switching ([Chapter VI, Art. 23–31](https://eur-lex.europa.eu/eli/reg/2023/2854/oj)) | The switching mandate is satisfied **by construction**: because customer data and models live on the customer's hardware, there is no vendor-held data to extract and no egress lock-in — the design *is* the exit (control: **data-at-rest on customer hardware + standard substrate LXD/Ubuntu/GGUF, no proprietary format**, `architecture.md` §4.5, D1). This is a positive differentiator, not a compliance burden. | Any EU cloud/AI buyer; especially public-sector and SME buyers the Data Act explicitly wants to de-lock. |
| EU (finance) | **DORA** ([Reg. (EU) 2022/2554](https://eur-lex.europa.eu/eli/reg/2022/2554/oj)) — incident reporting ([Art. 19](https://eur-lex.europa.eu/eli/reg/2022/2554/oj)), ICT third-party risk ([Chapter V, Art. 28–30](https://eur-lex.europa.eu/eli/reg/2022/2554/oj)) | Financial entities face strict third-party risk + exit obligations; on-prem keeps their regulated data under their control and makes **audit, exit and data-return** (Art. 30(2)) straightforward to grant (control: **customer-retained data + key custody**, `architecture.md` §5). Termini must still accept Art. 30 contractual terms — audit rights, exit assistance, subcontracting disclosure (gap G6). | Banks, insurers, investment firms, payment/e-money institutions subject to DORA. |
| EU (health) | **MDR** ([Reg. (EU) 2017/745](https://eur-lex.europa.eu/eli/reg/2017/745/oj)) — Annex I General Safety & Performance Requirements, IT security ([Annex I §17.4](https://eur-lex.europa.eu/eli/reg/2017/745/oj)) | Termini is not a medical-device manufacturer; MDR obligations rest with the customer's software. On-prem supports the manufacturer's Annex I IT-security duty by keeping SaMD and its data on customer hardware under controlled access (control: **data-at-rest on customer hardware + SSH CA access control**, `architecture.md` §5). See gap G7. | Medical-device software (SaMD) manufacturers and hospitals running MDR-governed software. |

---

## 3. Named controls referenced above (one-line index)

| Control | Where defined | What it does for compliance |
|---|---|---|
| C0/C1/C2 data classification | `governance.md` §3 | Decides what may egress; C2 = customer data, never egresses. |
| On-node redactor + egress allowlist | `governance.md` §4–§5 | Structural allowlist + C2 scan + entity redaction before any socket opens. |
| Fail-closed egress | `governance.md` §6 | No guarantee ⇒ no egress ⇒ no transfer of personal data. |
| `governanceGate` (controller re-verification) | `governance.md` §9 | Independent cloud-side re-check of every inbound payload. |
| `externalModelCall()` (C0-only, static template) | `governance.md` §7/§7.2 | External model never sees C1/C2; sub-processor surface is non-personal. |
| `guardrailClamp()` + `commandAllowlist()` | `governance.md` §7.3 | Model output advisory; no free-form execution. |
| G-AR-1 / G-AR-2 | `governance.md` §12 | Node→controller never carries C2; controller holds task content only transiently. |
| mTLS outbound-only control channel (D6) | `architecture.md` §5 | Encrypted, no inbound surface on the premises. |
| SSH CA — short-lived, principal-bound certs | `architecture.md` §5 (D4/R15) | Scoped, time-bounded, revocable operator access; private keys never transit the controller. |
| Audit chain + `governance_chain_tip` | `governance.md` §8.3 | Tamper-evident traceability of every egress and every external model call. |
| Data-at-rest on customer hardware; key custody | `architecture.md` §4.5, §5 invariant #4 | Data and model weights stay under the customer's control; the Data Act "exit" is free. |

---

## 4. Open gaps (design does NOT yet satisfy these — flagged, not glossed)

- **G1 — DPA + SCCs are not yet a shipped artifact.** The processor role and transfer instruments are argued above, but `docs/contracts/` contains no DPA template or SCC (2021/914 Module 2) attachment. Owner: legal/commercial. Without a signed Art. 28(3) DPA and sub-processor list, the on-prem argument does not convert into GDPR compliance.
- **G2 — Break-glass operator access is an unresolved transfer.** Any operator access that could touch personal data (e.g. `lxc exec` into a customer VM via the terminal surface) is a Chapter V transfer per EDPB 05/2021. The design has the SSH CA control but **no operator-geolocation policy and no "no-personal-data-access" statement**; SCCs/adequacy must be in place for that path. Owner: product/legal.
- **G3 — AI Act Art. 50 transparency is unaddressed.** If Termini's agents interact with natural persons (chat, synthetic output), disclosure obligations apply and are not in the contracts. Owner: product.
- **G4 — High-risk (Annex III) customer deployments pull the platform into scope.** If a customer runs Termini in a high-risk context (e.g. critical digital infrastructure, employment, essential services), the customer's deployer obligations may require Termini to support documentation/conformity. Not designed for today. Owner: product/architect.
- **G5 — NIS2 supply-chain evidence (Art. 21(2)(d)).** Essential/important customers must assess Termini as a supplier; Termini must produce security assurance/evidence — this is the bridge to `docs/compliance/certifications.md`. Owner: security/certifications workstream.
- **G6 — DORA Art. 30 contractual provisions.** Audit rights, exit assistance, data return and subcontracting disclosure must be accepted in contracts for financial customers; on-prem makes them easier to grant but they are not yet drafted. Owner: legal/commercial.
- **G7 — MDR Annex I §17.4 is the customer's obligation, not Termini's.** The on-prem design *supports* it but does not itself satisfy it; Termini must not over-claim MDR coverage in health deals. Owner: product/marketing.
- **G8 — Sub-processor transparency.** Termini's own controller infra (GCP VM) and any cloud model used for the external operator loop must be disclosed as sub-processors under Art. 28(2)/(4) with flow-down terms. Owner: legal/infra.

---

## 5. Structure note for sibling tasks (do not remove)

This file is a shared deliverable. Sections are appended in task order: non-EU rows (`t_0269f452`) extend the §2 table and add a ranking-rationale paragraph; `t_9128b7b9` appends "Design Control Mapping" and "Priority Regions & Segments"; `t_d0635046` appends the adversarial review note. Each appender should keep the table's four columns (`region | regulation | why the on-prem design helps | buyer/segment`) and cite primary sources.
