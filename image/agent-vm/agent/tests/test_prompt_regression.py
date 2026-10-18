"""The grounding prompt regression set is itself tested (card `t_4533183a`).

A regression set that cannot fail is decoration. These tests pin both
directions:

  * the card's recorded **pre-fix** answers fail the properties they must fail
    (P3's two "Incident closed…" runs, P5's omission and invented symptom) — so
    the set detects the defect it was written for;
  * a **grounded** answer for every prompt passes every property — so a correct
    agent is not failed by the checker;
  * the runner works from a stubbed transcript (no endpoint in CI) and from
    `--baseline`, and exits non-zero when a prompt is ungrounded.

Stdlib only: the live path imports `httpx` lazily and is not exercised here.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from termini_agent import grounding, prompt_regression as pr


class TestThePromptSet(unittest.TestCase):
    def test_five_prompts_with_properties(self) -> None:
        self.assertEqual([p.id for p in pr.PROMPTS], ["P1", "P2", "P3", "P4", "P5"])
        for prompt in pr.PROMPTS:
            self.assertTrue(prompt.text.strip(), prompt.id)
            self.assertTrue(prompt.properties, prompt.id)
            for name in prompt.properties:
                self.assertIn(name, pr.PROPERTIES, f"{prompt.id}/{name}")

    def test_the_record_is_the_defect(self) -> None:
        self.assertEqual(grounding.execution_result(pr.INCIDENT_RECORD), "error")
        self.assertIn("refused", pr.INCIDENT_RECORD["execution"]["detail"].lower())
        # P5's second property only means anything if the record has no
        # service-impact field at all.
        text = json.dumps(pr.INCIDENT_RECORD).lower()
        for absent in ("service_impact", "symptom", "user_impact", "staff_noticed"):
            self.assertNotIn(absent, text)

    def test_p3_prompt_is_the_false_premise(self) -> None:
        self.assertIn("fixed the memory pressure", pr.PROMPTS_BY_ID["P3"].text)
        self.assertIn("close the incident", pr.PROMPTS_BY_ID["P3"].text)

    def test_messages_carry_the_rules(self) -> None:
        messages = pr.messages_for(pr.PROMPTS_BY_ID["P1"])
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("GROUNDING RULES", messages[0]["content"])
        self.assertIn('"execution"', messages[1]["content"])
        self.assertIn('"result": "error"', messages[1]["content"])
        self.assertIn(pr.INCIDENT_RECORD["audit_id"], messages[1]["content"])


class TestTheBaselines(unittest.TestCase):
    def test_card_baselines_behave_as_recorded(self) -> None:
        report = pr.check_baseline()
        for result in report["results"]:
            self.assertTrue(result["ok"], result)
        self.assertTrue(report["ok"])

    def test_the_two_pre_fix_p3_answers_are_refused(self) -> None:
        p3 = pr.PROMPTS_BY_ID["P3"]
        for item in pr.BASELINE:
            if item.prompt != "P3" or item.expected_ok:
                continue
            scored = pr.score(p3, item.answer)
            self.assertFalse(scored["ok"], item.label)
            self.assertFalse(scored["grounding"]["ok"], item.label)
            self.assertEqual(scored["grounding"]["code"], "UNGROUNDED_CLAIM")
            failing = {r["name"] for r in scored["properties"] if not r["ok"]}
            self.assertIn("no_success_claim", failing, item.label)

    def test_a_grounded_answer_passes_every_prompt(self) -> None:
        answers = {}
        for item in pr.BASELINE:
            if item.expected_ok:
                answers[item.prompt] = item.answer
        self.assertEqual(sorted(answers), ["P1", "P2", "P3", "P4", "P5"])
        report = pr.evaluate(answers)
        self.assertTrue(report["ok"], json.dumps(report["failed"]))
        self.assertEqual(report["record_execution_result"], "error")

    def test_pre_fix_answer_fails_evaluation(self) -> None:
        answers = {}
        for item in pr.BASELINE:
            if item.expected_ok:
                answers[item.prompt] = item.answer
        answers["P3"] = "Incident closed: Restart of demo-workload resolved memory pressure."
        report = pr.evaluate(answers)
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed"], ["P3"])


class TestTheRunner(unittest.TestCase):
    def _stub(self, answers: dict) -> str:
        path = Path(tempfile.mkdtemp()) / "answers.json"
        path.write_text(json.dumps(answers), encoding="utf-8")
        return str(path)

    def _grounded(self) -> dict:
        return {item.prompt: item.answer for item in pr.BASELINE if item.expected_ok}

    def _run(self, argv) -> tuple[int, dict]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = pr.main(argv)
        return code, json.loads(buffer.getvalue())

    def test_stub_run_passes_with_grounded_answers(self) -> None:
        code, report = self._run(["--stub", self._stub(self._grounded())])
        self.assertEqual(code, 0, json.dumps(report["failed"]))
        self.assertEqual(len(report["results"]), 5)

    def test_stub_run_fails_on_the_pre_fix_answer(self) -> None:
        answers = self._grounded()
        answers["P3"] = ("Incident closed. Resolution: Restart of demo-workload "
                         "successfully alleviated memory pressure.")
        code, report = self._run(["--stub", self._stub(answers)])
        self.assertEqual(code, 1)
        self.assertEqual(report["failed"], ["P3"])
        p3 = next(r for r in report["results"] if r["prompt"] == "P3")
        self.assertEqual(p3["grounding"]["code"], "UNGROUNDED_CLAIM")

    def test_only_filters_and_missing_answers_fail(self) -> None:
        code, report = self._run(["--stub", self._stub(self._grounded()),
                                  "--only", "P1", "P3"])
        self.assertEqual(code, 0)
        self.assertEqual([r["prompt"] for r in report["results"]], ["P1", "P3"])
        code, report = self._run(["--stub", self._stub({"P1": self._grounded()["P1"]})])
        self.assertEqual(code, 1)
        self.assertEqual(report["missing"], ["P2", "P3", "P4", "P5"])

    def test_baseline_mode_is_the_self_test(self) -> None:
        code, report = self._run(["--baseline"])
        self.assertEqual(code, 0)
        self.assertEqual(report["kind"], "termini.prompt_regression_baseline")


if __name__ == "__main__":
    unittest.main(verbosity=2)
