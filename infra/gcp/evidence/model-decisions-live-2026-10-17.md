# The model's answer is kept: 6/6 consecutive live fires decide on the model (t_77f26781)

**Card:** t_77f26781 — *Model decisions fall back with schema_violation on ~half of live
fires (raw model output is not captured).*
**Measured:** 2026-10-17T20:52:21–21:16:37Z on the standing fleet (deploy 20:52:21,
acceptance run 21:00–21:06, raw-capture reproduction 21:11–21:13, post-restore
re-check 21:16:37Z).
**Deployed tree:** `9430db15d1a362a2c160fe5657714c091f0974d8` (branch
`wt/t_77f26781-model-decision-robust` on top of merged `main` `0d995f1`), read back
from `/opt/termini/DEPLOYED_COMMIT` after the deploy at 20:52:21Z — the tree the
acceptance run above executed on. It was redeployed from branch commit
`4a982b00ac841994c513da3633c706c0f0beef17` at 21:17:55Z; between the two,
`git diff 9430db1..4a982b0 --stat` touches only `infra/gcp/evidence/` (this doc and the
three driver/evidence tools), and the five runtime files this card changes hash
identically to the commit:

```
$ sha256sum /opt/termini/controller/app/operator/decision.py …      # on the controller
70c466d9…  controller/app/operator/decision.py
929382ea…  controller/app/operator/external_call.py
e97978b8…  termini/decision/adapter.py
29abcf72…  termini/decision/config.py
6bf6d504…  termini/decision/types.py
$ for f in …; do git show HEAD:$f | sha256sum; done                  # same values
```
**Node:** `termini-node-a` / `01M4TGD31WXH97JAA5S6A4R10D`, `lxd_endpoint=https://10.10.0.11:8443`.
**Model surface:** `https://…` loopback provider proxy `:8085` → `gemini-2.5-flash`
(trace `answered_by=forwarded-managed-model`).

Everything below is raw output from the live controller's journal, API and SQLite
file; commands are in [Reproduce](#reproduce).

**Result: 6 consecutive live fires produced `decision_source=model` with a model
rationale and an executed, audited action — 6/6, not ~half.** The reply that used to
be rejected is now diagnosed as what it was (a truncation) and quoted in the journal;
the same reply shape that used to read `schema_violation: action_id must be a non-empty
string, got None` reads `parse_error: reply truncated mid-JSON after 575 chars
(finish_reason='length')` instead.

## 1. What was wrong — measured, not inferred

`termini/decision/config.py` had `DEFAULT_MAX_TOKENS = 512`, copied from the intent
extractor. The operator-decision v2 reply is the decision **plus** the §4.2 incident
record (evidence array, explanation, actions), so its completion length varies with the
incident. Over the last 60 live model decisions on the standing controller 22 needed
**more than 512 completion tokens** (min 350, median 463, max 704):

```
$ python3 token-stats.py            # reads the model_call step of each decision trace
model decisions with a completion count: 60
  2026-10-17T20:59:19.074Z 669
  2026-10-17T20:59:10.666Z 454
  2026-10-17T20:59:01.391Z 678
  2026-10-17T20:58:23.182Z 413
  2026-10-17T20:58:17.979Z 435
  2026-10-17T20:58:07.020Z 687
  …
replies > 512 completion tokens: 22 of 60
min/median/max: 350 463 704
```

That is the card's "roughly half". Every one of those was cut mid-JSON
(`finish_reason=length`), and the old extraction — "decode the first JSON object
anywhere in the text" — found the **params fragment** of the cut-off decision and
reported a *missing field*:

```
# pre-fix journal line, from the card
model decision fell back (reason=schema_violation): action_id must be a non-empty string, got None
```

The reply it was describing had `"action_id":"instance.restart"` at character 90. The
diagnosis pointed at the wrong thing, and the raw text was not retained anywhere, so it
could not be checked.

## 2. The fix (this card's PR; branch commit `c09ba68`)

1. **Budget.** `DEFAULT_MAX_TOKENS = 2048`, overridable with
   `TERMINI_INFERENCE_MAX_TOKENS` (`termini/decision/config.py`). The transport
   returns a `Completion` (a `str` subclass) carrying `finish_reason` and `usage`;
   `usage` is recorded on the `model_call` trace step.
2. **Honest extraction.** Only the *first* structural opener is tried, so a truncated
   reply can never be mistaken for a fragment; a reply that stops mid-JSON is reported
   as a truncation with its length and `finish_reason`, never as a missing field.
3. **One bounded repair retry** for mechanical shapes (cut off at the budget, an
   incident record with no action, a missing rationale): the same model is asked once
   more, continuing the same conversation, and a second failure returns a
   `DecisionError` naming both attempts. It never fabricates a decision.
4. **Shape repair within one reply.** `action_id` / `instance_name` may be read from
   the reply's own `actions[0]` (ACTION_SPACE-checked); the guardrail still authorizes
   G3/G5/G6, so nothing is loosened downstream. Recorded as `DecisionResponse.repairs`
   and a `reply_repaired` trace step.
5. **Capture.** The fallback journal line carries a bounded, C0-safe raw excerpt plus a
   shape summary; the trace records `reply_shape` even when the text itself is dropped
   as C2; the external-call audit row names the typed reason (`DecisionError:
   parse_error`) instead of only the class name.
6. **A fallback stays visible.** `decision_source=fallback` with `fallback_reason`
   (and `incident_generated=fallback`), the §10.2 repeated-failure cooldown unchanged.
   Nothing in this change can make a deterministic/fallback answer look like a model
   one; the tests assert the opposite (see §5).

## 3. Live acceptance: 6 consecutive fires, 6 model decisions

`infra/gcp/evidence/tools/model-decisions-live.py run --node 01M4TGD31WXH97JAA5S6A4R10D
--cycles 6 --target real --duration 30`, on the controller, 21:00:31→21:06:21Z. Each
cycle injects the OOM signature on the **real** node, waits for the decision, clears the
injection, waits for the rule to re-arm and (this is required — a back-to-back injection
is refused with `remediation_in_flight`, which is what stopped the first attempt at
20:53) for the console's 60 s settle window.

```
=== run: node=01M4TGD31WXH97JAA5S6A4R10D scenario=oom cycles=6 at 2026-10-17T21:00:31Z ===

--- cycle 1/6: inject at 2026-10-17T21:00:31Z ---
  HTTP 201 {"schema_version": "1.0", "injection_id": "01M55TN3VBXZ44N0N9P2F6AK5Q", …
  decision: {"id": "01M55TN93K6NCMZ2A5FWBZFZ04", … "decided_at": "2026-10-17T21:00:33.813Z",
             "decision_source": "model", "confidence": 0.9, "fallback_reason": null,
             "action_kind": "instance.restart", …}
  trace: threshold_fired … detection_latency_ms=1719
  trace: prompt_built   state=ok       template=operator-decision v2 … C0_assertion=ok
  trace: model_call     state=ok       endpoint=external model=gemini-2.5-flash
        answered_by=forwarded-managed-model latency_ms=2992
        tokens(prompt=37703,completion=463,total=38166) finish=stop request_id=01M55TN65BASK6BG84H9A2JZAM
  trace: reply_received state=ok       {"schema_version":1,"request_id":"01M55TN65BASK6BG84H9A2JZAM",
        "action_id":"instance.restart","params":{…},"confidence":0.9,"rationale":"Host RAM is at 96.99% …
  trace: gates          state=ok       rule=G7 field=rationale decision=accept (C0-safe, within bounds)
  trace: gates          state=ok       rule=G9 decision=accept (record clean or absent)
  trace: action_decided state=ok       action_kind=instance.restart target=demo-workload
  clear HTTP 200 …
  oom-guard-01 armed before the next fire
  console settle window: clear (>=65 s since the last audit)
… (cycles 2–6 identical in shape; full transcript in the run — summary below)
=== summary ===
{"cycle": 1, "injection": "01M55TN3VBXZ44N0N9P2F6AK5Q", "decision": "01M55TN93K6NCMZ2A5FWBZFZ04", "source": "model", "confidence": 0.9,  "fallback_reason": null, "action": "instance.restart"}
{"cycle": 2, "injection": "01M55TQ6FD5WE6HTDJ5W2K9HNM", "decision": "01M55TQBG7ZNGCANKGG59QFA43", "source": "model", "confidence": 0.9,  "fallback_reason": null, "action": "instance.restart"}
{"cycle": 3, "injection": "01M55TS83NPM2NWEP06HFJRX78", "decision": "01M55TSDX8XEWR8MREHA397PDX", "source": "model", "confidence": 0.9,  "fallback_reason": null, "action": "instance.restart"}
{"cycle": 4, "injection": "01M55TVAQCM6GVY9DH5ES5EVG7", "decision": "01M55TVP7A845HSXFWRPPZ2H55", "source": "model", "confidence": 0.9,  "fallback_reason": null, "action": "instance.restart"}
{"cycle": 5, "injection": "01M55TXMBP1GC3ZWKFR8VN42XB", "decision": "01M55TXT6N7TRW1WHMHME58ZTQ", "source": "model", "confidence": 0.9,  "fallback_reason": null, "action": "instance.restart"}
{"cycle": 6, "injection": "01M55TZPZE00046WZAHPMJZ11Q", "decision": "01M55TZVVTPR461KDKK015GA45", "source": "model", "confidence": 0.95, "fallback_reason": null, "action": "instance.restart"}

6/6 fires produced decision_source=model
```

The six replies, their budgets and their targets (`finish=stop` on every one):

```
tokens(prompt=37703,completion=463) finish=stop   target=demo-workload
tokens(prompt=37561,completion=477) finish=stop   target=demo-workload
tokens(prompt=38204,completion=434) finish=stop   target=demo-workload
tokens(prompt=36752,completion=412) finish=stop   target=termini-01m4wjhwqr3b8pcav2tvqbgg-0-0
tokens(prompt=37426,completion=438) finish=stop   target=demo-workload
tokens(prompt=38098,completion=468) finish=stop   target=demo-workload
```

Raw decision rows and their audits (from `/data/controller.db`, `report` command):

```
$ python3 model-decisions-live.py report --node 01M4TGD31WXH97JAA5S6A4R10D --since 2026-10-17T21:00:00Z
-- 01M55TN93K6NCMZ2A5FWBZFZ04 2026-10-17T21:00:33.813Z oom-guard-01
  decision: {… "decision_source": "model", "confidence": 0.9, "fallback_reason": null,
             "action_kind": "instance.restart",
             "reason": "Host RAM is at 96.99% of total, and instance NNB9287WRHGFDPEZ995TKCXDNW is
                        consuming 85% of the total RAM. Restarting it will reclaim memory."}
  audit:    {"result": "ok", "detail": "operation Success: …; action=restart; state Running -> Running",
             "lxd_method": "PUT", "lxd_path": "/1.0/instances/demo-workload/state"}
-- 01M55TQBG7ZNGCANKGG59QFA43 2026-10-17T21:01:41.745Z
  reason: "Host RAM is at 96.99% of total, and instance NNB9287WRHGFDPEZ995TKCXDNW is consuming 85%
           of the total RAM, indicating it is the primary cause of the OOM event."
  audit:  result=ok PUT /1.0/instances/demo-workload/state (Running -> Running)
-- 01M55TSDX8XEWR8MREHA397PDX 2026-10-17T21:02:49.714Z
  reason: "Host RAM is at 96.99% of total, with instance NNB9287WRHGFDPEZ995TKCXDNW consuming 85% of
           the total RAM. Restarting this instance will reclaim its memory."
  audit:  result=ok PUT /1.0/instances/demo-workload/state (Running -> Running)
-- 01M55TVP7A845HSXFWRPPZ2H55 2026-10-17T21:04:03.969Z
  reason: "Host RAM is at 96.99% of total, and instance 7NM4JV8BSD1P8KYBYP8B7BT7CY is the heaviest
           consumer, so restarting it will reclaim memory."
  audit:  result=ok PUT /1.0/instances/termini-01m4wjhwqr3b8pcav2tvqbgg-0-0/state (Running -> Running)
-- 01M55TXT6N7TRW1WHMHME58ZTQ 2026-10-17T21:05:13.560Z
  reason: "Host RAM is at 96.99% of total, and instance NNB9287WRHGFDPEZ995TKCXDNW is consuming 85% of
           the total RAM. Restarting this instance will reclaim its memory."
  audit:  result=ok PUT /1.0/instances/demo-workload/state (Running -> Running)
-- 01M55TZVVTPR461KDKK015GA45 2026-10-17T21:06:21.011Z  confidence 0.95
  reason: "Host RAM is at 96.99% of total, and instance NNB9287WRHGFDPEZ995TKCXDNW is consuming 85% of
           the total RAM. Restarting it will reclaim memory."
  audit:  result=ok PUT /1.0/instances/demo-workload/state (action=start; Stopped -> Running)
```

Six of six decided by the model, with the model's own rationale on the row, and six
audits `result=ok` against the node's real LXD API. (Cycle 4's target is a different
instance because the model picked the largest consumer in its window that time; the
executor restarted that real instance and the audit recorded it.)

## 4. A rejected violation, quoted — and the raw text it was rejected on

To capture a violation *on the deployed tree* the completion budget was forced low for
one fire (200 tokens instead of the pre-fix 512 — see §4.1 for why), while the loop kept
running normally. The journal now carries the reply it rejected (first 400 chars,
C0-scanned, shape appended):

```
$ sudo journalctl -u termini-controller --since "-6min" | grep "fell back (reason=parse_error)"
… 21:12:15,947 WARNING termini.operator.decision model decision fell back (reason=parse_error):
  reply truncated mid-JSON after 531 chars (finish_reason='length'): no complete JSON object or
  array found [repair retry after truncated; first attempt: reply truncated mid-JSON after 551
  chars (finish_reason='length'): no complete JSON object or array found; attempt lengths 551 /
  531 chars] | raw[531]=['{"schema_version":1,"request_id":"01M55VAG05SM5VTEYDCNPBGR7M",
  "action_id":"instance.restart","params":{"instance_name":"NNB9287WRHGFDPEZ995TKCXDNW",
  "force":true,"timeout_s":30},"confidence":0.95,"rationale":"Host RAM is at 96% of total, and
  instance NNB9287WRHGFDPEZ995TKCXDNW uses 16.5GB RAM with a 4.29GB limit, indicating a memory
  leak or misconfiguration. Restarting it will reclaim memory.","inc'…]
  shape=[chars=531 json_object=no truncated=yes]
… 21:12:25,942 WARNING termini.operator.decision model decision fell back (reason=parse_error):
  reply truncated mid-JSON after 575 chars … [attempt lengths 578 / 575 chars] |
  raw[575]=['{"schema_version":1,"request_id":"01M55VASWV7STVYVNRKS0N7D9S","action_id":"instance.stop",
  "params":{"instance_name":"NNB9287WRHGFDPEZ995TKCXDNW","force":true},"confidence":0.9,
  "rationale":"Disk write latency on /dev/sda is 120ms, indicating severe I/O degradation; stopping
  the heaviest I/O instance will shed load.","incident_title":"Disk I/O degradation on /dev/sda",
  "explanation":"The disk.write_l'…] shape=[chars=575 json_object=no truncated=yes]
… 21:12:34,651 WARNING … raw[537]=['…"action_id":"instance.restart"…  shape=[chars=537
  json_object=no truncated=yes]
```

That is the whole diagnosis in one line: **the reply carried a valid `action_id` and a
rationale, and it was cut mid-JSON** — `json_object=no truncated=yes` — exactly the
shape the old code reported as "action_id must be a non-empty string, got None". The
decision rows for those fires are honest fallbacks:

```
decision 01M55VAKV8WEBCA4GJFK2EG7QN 2026-10-17T21:12:11.995Z decision_source=fallback fallback_reason=parse_error
decision 01M55VAXKQW2VPGZCSG6VKH0VQ 2026-10-17T21:12:22.154Z decision_source=fallback fallback_reason=parse_error
decision 01M55VB63WTCYAVPR6TWMHJBJ0 2026-10-17T21:12:31.170Z decision_source=fallback fallback_reason=parse_error
```

### 4.1 Why the budget was lowered for the capture, and what that does and does not show

The pre-fix default was 512, so the first reproduction attempt ran the **unmodified**
budget of 512 from 21:07:35 to 21:12:05. Sixteen fires in that window all decided on the
model (`decision_source=model`, no fallback) with completion around 460–500 tokens: those
replies fit. The failure is length-dependent — which is exactly why the card saw it
"roughly half" of the time, and why it cannot be reproduced on demand at 512 without
waiting for an incident whose reply is longer. Lowering the cap to 200 for one fire
makes the truncation deterministic and produces the *identical mechanical shape* (valid
`action_id`, cut mid-string) at the identical place in the parser. What this run shows is
the capture and the diagnosis; the prevalence is shown by the 22-of-60 token measurement
in §1.

The forced fallbacks (three, each after its bounded repair retry, across two seeder
nodes) then tripped the §10.2 repeated-failure guard, which is the designed behaviour
and is visible exactly as it should be — the affected `(node, rule)` went to
`deterministic` rows with `forced_until=2026-10-17T21:18:05.840Z` rather than pretending
to be model decisions:

```
('01M4NZK474BVARFBXYQBMVA8GV', 'oom-guard-01', 0, '2026-10-17T21:18:05.840Z', '2026-10-17T21:13:05.840Z')
```

The drop-in that set the small budget was then removed, `daemon-reload` + restart
(`MainPID 318795`), and `systemctl show … Environment` contains no `MAX_TOKENS` entry.
The next fire on the real node decided on the model again:

```
--- cycle 1/1: inject at 2026-10-17T21:16:35Z ---
  decision: {"id": "01M55VJPW89KP7P8YVF25XJ0KN", … "decision_source": "model", "confidence": 0.95,
             "fallback_reason": null, "action_kind": "instance.restart", …}
  trace: model_call … tokens(prompt=37737,completion=481,total=38218) finish=stop
1/1 fires produced decision_source=model
```

## 5. Tests

The malformed shapes that used to fall back are unit-tested, including the exact live
truncated reply from the earlier capture (fixture `LIVE_TRUNCATED_REPLY`, kept verbatim
in `tests/test_decision_adapter.py`):

```
$ python3 -m unittest tests.test_decision_adapter tests.test_external_model_call \
      tests.test_operator_decision_fallback tests.test_operator_decision_guardrail
Ran 115 tests in 99.563s
OK
```

The tests that pin this card:

* `TestTruncatedReply.test_truncation_is_reported_as_truncation_not_a_missing_field` —
  the captured live reply is a truncation, and the old
  `action_id must be a non-empty string` sentence is **absent** from the detail.
* `…test_finish_reason_length_is_honoured_and_recorded`,
  `…test_a_complete_reply_that_ends_in_a_string_is_not_truncated`.
* `…test_repair_retry_recovers_the_decision`, `…test_repair_retry_is_bounded_to_one_attempt`,
  `…test_retry_transport_failure_is_reported_as_such`.
* `…test_action_id_and_target_are_read_from_actions`,
  `…test_an_invented_action_is_still_a_schema_violation`,
  `…test_an_incident_with_no_action_at_all_still_falls_back`.
* `tests/test_operator_decision_fallback.py` — the fallback stays visible
  (`decision_source=fallback`), the journal raw excerpt is bounded and C2-safe.
* `tests/test_external_model_call.py` — the audit row names the typed reason.

Full suite on this host: `Ran 1108 tests`, `FAILED (errors=17)` — all 17 are
`tests/test_agent_tasks.py` failing in `setUp` with
`ModuleNotFoundError: No module named 'yaml'` (the agent-vm package imports PyYAML,
which the host's Python 3.14 does not have). Pre-existing and unrelated to this change;
the four operator-decision modules above are green.

## 6. Reproduce

```bash
# on the controller (stdlib only; loopback API + the controller's own SQLite file).
# The three tools below are in infra/gcp/evidence/tools/ — copy them to the
# controller first (gcloud compute scp …).
python3 model-decisions-live.py run --node 01M4TGD31WXH97JAA5S6A4R10D --cycles 6 \
    --target real --duration 30
python3 model-decisions-live.py report --node 01M4TGD31WXH97JAA5S6A4R10D \
    --since 2026-10-17T21:00:00Z

# the token distribution behind "roughly half"
python3 model-decision-token-stats.py

# force the truncation (writes a drop-in, restarts, and then remove it again)
bash max-tokens-repro-set.sh        # TERMINI_INFERENCE_MAX_TOKENS=200; pass 512 for the pre-fix value
#   … fire once, read the journal …
bash max-tokens-repro-unset.sh      # rm the drop-in, daemon-reload, restart
sudo journalctl -u termini-controller --since "-6min" | grep "fell back (reason=parse_error)"
```

## 7. Costs, and what this does not claim

* Six real restarts of real instances on node-a (`demo-workload`, plus one
  `termini-…-0-0` the model selected once). No instance was created or deleted, no
  GCP resource changed, nothing in terraform.
* The fault *series* is the demo console's simulation (as the card allows); the target,
  the decision, the executor call and the LXD restart are real.
* This does not claim the model never fails: a transport error, a low-confidence answer
  or a genuinely malformed reply still falls back, visibly, with the reason on the row
  and the raw excerpt in the journal. It claims the truncation that caused *this* card's
  fallbacks is fixed, is reported honestly when it does happen, and that six
  consecutive live fires now decide on the model.
* After the redeploy from `4a982b0` (21:17:55Z), one more fire decided on the model —
  `01M55VPTRHFGTM03XHXBPRRS9D` at 21:18:53.613Z, confidence 0.9, completion 371 tokens,
  `finish=stop` — so the tree that is live now is the tree that was measured.
