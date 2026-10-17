"""Regression + negative tests for the G9 phone-number false positive (t_220a468a).

Live finding (operator's fleet, 2026-10-17): the model answered, G7 accepted the
rationale, then G9 rejected the incident ``explanation`` for a "phone-number
signature" — every time, on a real node. Root cause: the prompt renders the
evidence window with **raw integers** (``termini/decision/prompt.py::compact_json``)
and a real node's window is full of 10-15 digit byte counts (host
``ram_used_bytes`` 4119142400, ``ram_total_bytes`` 8319729664, ``disk.total_bytes``
61285326848, per-instance ``ram_used_bytes``/``ram_limit_bytes`` in
``lxd_instances[]``). ``governance.scan_c2`` reads *any* bare 10-15 digit run as a
phone number, so a model that quotes a metric is rejected for quoting the evidence.

The fix renders byte-magnitude integers thousands-grouped in the prompt (the same
shape the controller's own ``safe_int_display`` produces), so a model that quotes a
metric quotes ``4,119,142,400`` — which is C2-safe — while a genuine phone number
is still a bare digit run and is still caught. The payload values are never changed
(the C0 class assertion runs on the raw ``DecisionInput``), only the rendered string.

The model is always a stub that quotes the metric **as the prompt renders it** — the
behaviour gemini-2.5-flash exhibited live. No network, no model call.
"""

from __future__ import annotations

import pathlib
import re
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

from app import governance  # noqa: E402
from app.operator import decision  # noqa: E402
from app.operator.models import HealthEvent  # noqa: E402
from termini.decision.prompt import compact_json, render_user  # noqa: E402
from termini.decision.types import DecisionResponse  # noqa: E402

VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"
NODE_ID = "01M4349RPTT29RNTC6VBCXQ0RC"

# Real byte magnitudes from the live node (t_8b66366d §6.4).
RAM_USED = 4_119_142_400
RAM_TOTAL = 8_319_729_664
DISK_TOTAL = 61_285_326_848

# The phone-signature regex G9 uses (governance.py::_PHONE), kept here so the
# "no bare 10-15 digit run" assertion can never drift from the scanner itself.
_PHONE = re.compile(r"(?<!\d)\+?[0-9]{10,15}(?!\d)")


def make_event() -> HealthEvent:
    return HealthEvent(
        event_id="01M4349RQ09HMAX6RC9YPYQH4W",
        node_id=NODE_ID,
        rule_id="oom-guard-01",
        type="oom",
        metric="ram_used_bytes",
        value=96.0,
        threshold=">= 95% of ram_total_bytes for 2 consecutive samples",
        fired_at="2026-10-17T02:45:02.000Z",
        evidence=[{"seq": 26389, "ram_used_bytes": RAM_USED,
                   "ram_total_bytes": RAM_TOTAL}],
    )


def make_context() -> decision.DecisionContext:
    insts = [{"vm_id": VM_ID, "state": "running",
              "ram_used_bytes": RAM_USED, "ram_limit_bytes": RAM_TOTAL}]
    samples = [
        {"seq": s, "ram_used_bytes": RAM_USED, "ram_total_bytes": RAM_TOTAL,
         "disk": [{"path": "/dev/sda", "total_bytes": DISK_TOTAL,
                   "free_bytes": 10_000_000_000, "write_latency_ms": 3.0,
                   "mount_ro": False}],
         "lxd_instances": insts}
        for s in range(26370, 26390)
    ]
    return decision.DecisionContext(
        event=make_event(),
        state=samples[-1],
        evidence=samples,
        instances=insts,
        instance_names={VM_ID: "demo-workload"},
        default_instance_name="demo-workload",
        allowed_actions=list(decision.ACTION_IDS),
        project_id="01M4349RPSZFTB60AVJEG0ZZY9",
    )


def _quote_metric_stub(decision_input):
    """A stub model that quotes ``ram_used_bytes`` exactly as the prompt renders it.

    This is the live failure mode: the model reasons from the number it was shown
    and quotes it back in its free-text ``explanation``. It renders the prompt the
    same way the adapter does, so on ``main`` it quotes the raw digit run and on the
    fixed branch it quotes the grouped form.
    """
    user_msg = render_user(decision_input)
    match = re.search(r'"ram_used_bytes":("?[0-9,]+"?)', user_msg)
    rendered = match.group(1).strip('"') if match else "0"
    return DecisionResponse(
        action_id="instance.restart",
        confidence=0.87,
        rationale="host RAM exhausted; restart the heaviest instance to reclaim its memory",
        params={"instance_name": "heaviest", "force": True, "timeout_s": 30},
        incident_title="Host memory exhaustion from one runaway instance",
        explanation=(
            f"host RAM reached {rendered} of ram_total_bytes and the heaviest "
            "instance holds most of it, so the stall is memory-bound; restarting "
            "it reclaims its memory and drops the host below the OOM threshold."
        ),
        evidence=[{"field": "ram_used_bytes", "value": rendered, "seq": 26389}],
        actions=[{"action_kind": "instance.restart", "target": "heaviest",
                  "note": "reclaim the heaviest instance's resident memory"}],
    )


def _provider(stub):
    det = decision.DeterministicDecisionProvider(
        playbooks={"oom": {"type": "oom", "rule_id": "oom-guard-01",
                           "action": {"kind": "instance.restart",
                                      "target": "heaviest", "params": {}},
                           "reason": "oom", "description": "oom"}},
        instance_names={VM_ID: "demo-workload"},
        default_instance_name="demo-workload")
    return decision.ModelDecisionProvider(
        deterministic=det, decide_fn=stub,
        instance_names={VM_ID: "demo-workload"},
        default_instance_name="demo-workload")


class PromptRenderingGroupsMetricsTest(unittest.TestCase):
    """The prompt renders byte-magnitude integers grouped (the fix itself)."""

    def test_compact_json_groups_large_integers(self):
        out = compact_json({
            "ram_used_bytes": RAM_USED, "ram_total_bytes": RAM_TOTAL,
            "seq": 26389, "cpu_util_pct": 91.7, "mount_ro": False,
        })
        self.assertIn('"ram_used_bytes":"4,119,142,400"', out)
        self.assertIn('"ram_total_bytes":"8,319,729,664"', out)
        self.assertIn('"seq":26389', out)      # small int untouched
        self.assertIn('"cpu_util_pct":91.7', out)  # small float untouched
        self.assertIn('"mount_ro":false', out)

    def test_rendered_evidence_window_has_no_phone_shaped_run(self):
        user = render_user(decision.build_decision_input(make_context()))
        self.assertIn("4,119,142,400", user)
        self.assertIn("8,319,729,664", user)
        self.assertIn("61,285,326,848", user)
        # No bare 10-15 digit run survives rendering — a phone signature would.
        self.assertIsNone(_PHONE.search(user))


class MetricQuoteG9RegressionTest(unittest.TestCase):
    """A model that quotes a metric must NOT be rejected by G9 (the live bug)."""

    def test_model_decision_survives_when_quoting_a_metric(self):
        result = _provider(_quote_metric_stub).decide(make_event(), make_context())
        self.assertEqual(result.source, "model", result.guardrail)
        self.assertIsNone(result.fallback_reason)
        self.assertIsNotNone(result.incident)
        self.assertIsNotNone(result.trace)
        # The stored explanation quotes the grouped metric and is C2-clean.
        explanation = result.incident["explanation"]
        self.assertIn("4,119,142,400", explanation)
        self.assertEqual(governance.scan_c2(explanation), [])
        # No G9 reject anywhere in the guardrail/trace drops.
        rejects = [d for d in result.guardrail if d.get("decision") == "reject"]
        self.assertEqual(rejects, [])


class MetricQuoteG9NegativeTest(unittest.TestCase):
    """Fail-closed preserved: a genuinely phone-shaped explanation still rejects."""

    def test_phone_shaped_explanation_is_still_rejected(self):
        def stub(decision_input):
            resp = _quote_metric_stub(decision_input)
            resp.explanation = (
                "oncall engineer is at +15551234567, page them before restarting "
                "the instance"
            )
            return resp

        result = _provider(stub).decide(make_event(), make_context())
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "schema_violation")
        self.assertTrue(any(
            d.get("rule") == "G9" and d.get("decision") == "reject"
            and "phone" in str(d.get("detail", ""))
            for d in result.guardrail
        ), result.guardrail)

    def test_email_shaped_explanation_is_still_rejected(self):
        def stub(decision_input):
            resp = _quote_metric_stub(decision_input)
            resp.explanation = "email admin@example.com for the restart approval"
            return resp

        result = _provider(stub).decide(make_event(), make_context())
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "schema_violation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
