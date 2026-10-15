"""Tests for the model-endpoint egress identity and its audit row.

The operator's decision step calls ONE address (the node's underlay endpoint) in
both configurations, so the audit row is the only place that can say whether the
tokens came from local weights or from a model that left the premises. These
tests pin that:

  * the destination class is derived from evidence (the endpoint's ``/health``
    self-description, or the URL itself) and is never assumed to be in-VPC;
  * a forwarded call is recorded with its external destination and
    ``answered_by=forwarded-managed-model``;
  * the §7.2 class assertion is untouched — an unlabelled endpoint still cannot
    smuggle C2, and a violation still sends nothing.
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

from app.operator import egress  # noqa: E402
from app.operator.decision import ModelDecisionProvider, build_decision_input  # noqa: E402
from app.operator.external_call import (  # noqa: E402
    ExternalCallViolation,
    external_model_call,
)
from termini.decision.types import DecisionResponse  # noqa: E402

from tests.test_operator_decision_fallback import make_context  # noqa: E402


def ctx_input(ctx):
    """The §3 request object for a context (what the guard inspects + audits)."""
    return build_decision_input(ctx)


def health_hits(server) -> list:
    """Paths the fake health endpoint has been asked for."""
    return server.RequestHandlerClass.hits  # type: ignore[attr-defined]

FORWARDED_HEALTH = {
    "status": "ok",
    "model": "gemma",
    "backend": "gemini-proxy",
    "answered_by": "forwarded-managed-model",
    "egress": "external",
    "served_model": "gemini-2.5-flash",
    "destination": "aiplatform.googleapis.com",
}
LOCAL_HEALTH = {
    "status": "ok",
    "model": "gemma",
    "backend": "local",
    "answered_by": "local-weights",
    "egress": "in-vpc",
    "served_model": "gemma",
    "upstream": "http://127.0.0.1:8081",
    "destination": "http://127.0.0.1:8081",
}


class _HealthHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    body: dict = {}
    hits: list = []

    def log_message(self, format, *args):  # noqa: A002
        return

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        data = json.dumps(type(self).body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestClassifyDestination(unittest.TestCase):
    def test_private_and_loopback_are_in_vpc(self):
        for url in ("http://10.10.0.13:8080", "10.10.0.13", "http://127.0.0.1:8081",
                    "http://localhost:8080", "http://metadata.google.internal",
                    "http://10.10.0.2:8080/v1/chat/completions"):
            self.assertEqual(egress.classify_destination(url), egress.CLASS_IN_VPC, url)

    def test_public_endpoints_are_external(self):
        for url in ("https://aiplatform.googleapis.com",
                    "https://aiplatform.googleapis.com/v1/projects/p/locations/global",
                    "https://termini-gemma-tylgzp65ma-as.a.run.app",
                    "https://api.openai.com"):
            self.assertEqual(egress.classify_destination(url), egress.CLASS_EXTERNAL, url)

    def test_empty_or_unusable_is_unknown_not_in_vpc(self):
        for value in ("", "   ", None, "not a url at all"):
            self.assertEqual(
                egress.classify_destination(value),  # type: ignore[arg-type]
                egress.CLASS_UNKNOWN, repr(value))


class TestIdentityFromHealth(unittest.TestCase):
    def test_forwarded_endpoint_is_labelled_external(self):
        identity = egress.identity_from_health("http://10.10.0.13:8080", FORWARDED_HEALTH)
        self.assertEqual(identity.backend, "gemini-proxy")
        self.assertTrue(identity.forwarded)
        self.assertEqual(identity.model, "gemini-2.5-flash")
        self.assertEqual(identity.destination, "aiplatform.googleapis.com")
        self.assertEqual(identity.destination_class, egress.CLASS_EXTERNAL)
        self.assertEqual(identity.answered_by, "forwarded-managed-model")

    def test_local_endpoint_is_labelled_in_vpc(self):
        identity = egress.identity_from_health("http://10.10.0.13:8080", LOCAL_HEALTH)
        self.assertEqual(identity.backend, "local")
        self.assertFalse(identity.forwarded)
        self.assertEqual(identity.destination_class, egress.CLASS_IN_VPC)
        self.assertEqual(identity.answered_by, "local-weights")

    def test_unlabelled_endpoint_is_unknown_not_local(self):
        """A stock llama.cpp answers `{"status":"ok"}` — that is not proof of local."""
        identity = egress.identity_from_health("http://10.10.0.13:8080", {"status": "ok"})
        self.assertEqual(identity.backend, "unknown")
        self.assertEqual(identity.destination_class, egress.CLASS_UNKNOWN)
        self.assertEqual(identity.answered_by, "unknown")

    def test_forwarded_backend_with_a_private_destination_is_not_external(self):
        identity = egress.identity_from_health(
            "http://10.10.0.13:8080",
            dict(FORWARDED_HEALTH, destination="http://10.10.0.9:9000"),
        )
        self.assertEqual(identity.destination_class, egress.CLASS_IN_VPC)


class TestProbe(unittest.TestCase):
    def setUp(self):
        handler = type("Bound", (_HealthHandler,), {"body": FORWARDED_HEALTH, "hits": []})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_probe_reads_the_endpoint_self_description(self):
        identity = egress.probe(self.endpoint)
        self.assertEqual(identity.backend, "gemini-proxy")
        self.assertEqual(identity.destination_class, egress.CLASS_EXTERNAL)
        self.assertEqual(identity.source, "health-probe")

    def test_probe_failure_is_explicit_and_never_raises(self):
        identity = egress.probe("http://127.0.0.1:9")
        self.assertEqual(identity.backend, "unknown")
        self.assertEqual(identity.destination_class, egress.CLASS_UNKNOWN)
        self.assertEqual(identity.source, "unavailable")
        self.assertTrue(identity.detail)

    def test_cache_avoids_a_probe_per_call(self):
        cached = egress.CachedEgressProbe(self.endpoint, ttl_s=60.0)
        first = cached()
        second = cached()
        self.assertEqual(first["backend"], second["backend"])
        self.assertEqual(len(health_hits(self.server)), 1)

    def test_cache_can_be_refreshed(self):
        cached = egress.CachedEgressProbe(self.endpoint, ttl_s=0.0)
        cached()
        time.sleep(0.01)
        cached()
        self.assertEqual(len(health_hits(self.server)), 2)


class TestAuditRowCarriesTheDestination(unittest.TestCase):
    def test_forwarded_egress_is_recorded_on_both_records(self):
        ctx = make_context()
        seen = []
        external_model_call(
            ctx_input(ctx), lambda: "OK",
            destination="http://10.10.0.13:8080",
            audit_sink=seen.append,
            egress=egress.identity_from_health("http://10.10.0.13:8080", FORWARDED_HEALTH).as_dict(),
        )
        self.assertEqual([r["phase"] for r in seen], ["before", "after"])
        for record in seen:
            self.assertEqual(record["destination"], "http://10.10.0.13:8080")
            self.assertEqual(record["destination_class"], "external")
            self.assertEqual(record["external_destination"], "aiplatform.googleapis.com")
            self.assertEqual(record["egress"]["backend"], "gemini-proxy")
            self.assertEqual(record["egress"]["model"], "gemini-2.5-flash")
            self.assertEqual(record["egress"]["answered_by"], "forwarded-managed-model")
            self.assertTrue(record["egress"]["forwarded"])

    def test_local_egress_is_recorded_as_in_vpc(self):
        ctx = make_context()
        seen = []
        external_model_call(
            ctx_input(ctx), lambda: "OK", destination="http://10.10.0.13:8080",
            audit_sink=seen.append,
            egress=egress.identity_from_health("http://10.10.0.13:8080", LOCAL_HEALTH).as_dict(),
        )
        self.assertEqual(seen[0]["destination_class"], "in-vpc")
        self.assertEqual(seen[0]["external_destination"], "")
        self.assertEqual(seen[0]["egress"]["answered_by"], "local-weights")

    def test_without_a_probe_the_public_destination_is_still_external(self):
        ctx = make_context()
        seen = []
        external_model_call(
            ctx_input(ctx), lambda: "OK",
            destination="https://aiplatform.googleapis.com",
            audit_sink=seen.append,
        )
        self.assertEqual(seen[0]["destination_class"], "external")
        self.assertEqual(seen[0]["external_destination"], "aiplatform.googleapis.com")

    def test_without_a_probe_the_private_destination_is_in_vpc(self):
        ctx = make_context()
        seen = []
        external_model_call(
            ctx_input(ctx), lambda: "OK",
            destination="http://10.10.0.13:8080", audit_sink=seen.append,
        )
        self.assertEqual(seen[0]["destination_class"], "in-vpc")
        self.assertEqual(seen[0]["external_destination"], "")

    def test_class_assertion_still_fails_closed(self):
        """Extending the audit must not weaken the §7.2 class assertion."""
        ctx = make_context()
        ctx.state = dict(ctx.state, app_logs=["raw customer text"])
        seen = []
        sent = []
        with self.assertRaises(ExternalCallViolation):
            external_model_call(
                ctx_input(ctx), lambda: sent.append(1),
                destination="http://10.10.0.13:8080", audit_sink=seen.append,
                egress=egress.identity_from_health("http://10.10.0.13:8080",
                                                   FORWARDED_HEALTH).as_dict(),
            )
        self.assertEqual(sent, [])                       # nothing was sent
        self.assertEqual(seen[0]["outcome"], "rejected")
        self.assertEqual(seen[0]["destination_class"], "external")  # still labelled


class TestAuditMessage(unittest.TestCase):
    def test_forwarded_line_names_the_destination_and_never_gpu(self):
        record = {
            "phase": "after", "outcome": "returned", "request_id": "01REQ",
            "destination": "http://10.10.0.13:8080",
            "egress": egress.identity_from_health("http://10.10.0.13:8080",
                                                  FORWARDED_HEALTH).as_dict(),
        }
        message = egress.audit_message(record)
        self.assertIn("destination=aiplatform.googleapis.com", message)
        self.assertIn("backend=gemini-proxy", message)
        self.assertIn("egress=external", message)
        self.assertIn("answered_by=forwarded-managed-model", message)
        self.assertNotIn("gpu", message.lower())
        self.assertLessEqual(len(message), 1000)

    def test_local_line_says_local(self):
        record = {
            "phase": "before", "outcome": "ok", "request_id": "01REQ",
            "destination": "http://10.10.0.13:8080",
            "egress": egress.identity_from_health("http://10.10.0.13:8080",
                                                  LOCAL_HEALTH).as_dict(),
        }
        message = egress.audit_message(record)
        self.assertIn("backend=local", message)
        self.assertIn("egress=in-vpc", message)
        self.assertIn("answered_by=local-weights", message)
        self.assertNotIn("destination=", message.replace("endpoint=", ""))

    def test_unknown_egress_says_unknown(self):
        record = {
            "phase": "before", "outcome": "ok", "request_id": "01REQ",
            "destination": "http://10.10.0.13:8080",
            "egress": egress.classify_and_describe("http://10.10.0.13:8080"),
        }
        message = egress.audit_message(record)
        self.assertIn("backend=unknown", message)
        self.assertIn("egress=in-vpc", message)


class TestDecisionProviderLabelsTheCall(unittest.TestCase):
    """The real decision path must produce a labelled audit row."""

    def test_model_decision_records_the_forwarded_destination(self):
        ctx = make_context()
        seen = []

        def decide_fn(_input):
            return DecisionResponse(
                action_id="instance.restart", params={"instance_name": "heaviest"},
                confidence=0.9, rationale="oom on the worker",
            )

        provider = ModelDecisionProvider(
            decide_fn=decide_fn, audit_sink=seen.append,
            egress_endpoint="http://10.10.0.13:8080",
            egress_probe=lambda: egress.identity_from_health(
                "http://10.10.0.13:8080", FORWARDED_HEALTH).as_dict(),
        )
        result = provider.decide(ctx.event, ctx)
        self.assertEqual(result.source, "model")
        self.assertEqual([r["phase"] for r in seen], ["before", "after"])
        self.assertEqual(seen[1]["destination_class"], "external")
        self.assertEqual(seen[1]["external_destination"], "aiplatform.googleapis.com")

    def test_a_broken_probe_does_not_break_the_decision(self):
        ctx = make_context()

        def boom():
            raise RuntimeError("no health")

        provider = ModelDecisionProvider(
            decide_fn=lambda _input: DecisionResponse(
                action_id="instance.restart", params={"instance_name": "heaviest"},
                confidence=0.9, rationale="r"),
            egress_probe=boom,
        )
        result = provider.decide(ctx.event, ctx)
        self.assertEqual(result.source, "model")


if __name__ == "__main__":
    unittest.main()

