"""Unit tests for the Node C inference proxy (`infra/inference/proxy.py`).

What is under test is the DROP-IN property and the in-band labelling:

  * `local` mode is byte-transparent — the caller's body reaches llama.cpp and
    llama.cpp's answer comes back unchanged (only labels are added);
  * `gemini-proxy` mode answers the SAME OpenAI envelope from Vertex AI,
    honouring `max_tokens`/`temperature`, including `usage`/`timings`;
  * every response and every log line names the forwarded backend, its model and
    the external destination — and a forwarded call is never logged as "local"
    or as "GPU";
  * a failed forwarded call fails LOUDLY and FAST with a distinct code, so the
    controller's decision path can fall back deterministically.

Both upstreams are faked in-process, so nothing here needs a network.
"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROXY_PATH = REPO_ROOT / "infra" / "inference" / "proxy.py"


def _load_proxy():
    spec = importlib.util.spec_from_file_location("termini_inference_proxy", PROXY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


proxy = _load_proxy()


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    behaviour: dict = {}
    seen: list = []

    def log_message(self, format, *args):  # noqa: A002
        return

    def _reply(self, status: int, body: bytes, content_type: str,
               headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        type(self).seen.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        payload = type(self).behaviour.get("get_body", {"status": "ok"})
        self._reply(200, json.dumps(payload).encode(), "application/json")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        type(self).seen.append({
            "method": "POST", "path": self.path, "headers": dict(self.headers),
            "body": json.loads(raw or b"{}"),
        })
        behaviour = type(self).behaviour
        delay = behaviour.get("delay_s")
        if delay:
            time.sleep(delay)
        status = behaviour.get("status", 200)
        if status != 200:
            error = json.dumps({"error": {"message": behaviour.get("error_message", "nope")}})
            headers = {}
            if behaviour.get("retry_after"):
                headers["Retry-After"] = str(behaviour["retry_after"])
            self._reply(status, error.encode(), "application/json", headers)
            return
        if behaviour.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for frame in behaviour.get("frames", []):
                data = ("data: " + json.dumps(frame) + "\n\n").encode()
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
                time.sleep(behaviour.get("frame_delay_s", 0.0))
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        self._reply(200, json.dumps(behaviour.get("body", {})).encode(), "application/json")


class _Upstream:
    """A fake llama.cpp worker and a fake Vertex AI endpoint."""

    def __init__(self) -> None:
        self._servers: list = []

    def start(self, behaviour: dict) -> str:
        handler = type("Bound", (_Fake,), {"behaviour": behaviour, "seen": []})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers = []


def _vertex_body(text: str = "PROXY_OK", prompt: int = 8, completion: int = 2) -> dict:
    return {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": text}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": completion,
            "totalTokenCount": prompt + completion,
        },
    }


def _sse_frame(text: str = "", finish: str | None = None, usage: dict | None = None) -> dict:
    frame: dict = {"candidates": []}
    if text or finish:
        frame["candidates"] = [{
            "content": {"role": "model", "parts": [{"text": text}]},
            **({"finishReason": finish} if finish else {}),
        }]
    if usage:
        frame["usageMetadata"] = usage
    return frame


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

class ProxyTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream = _Upstream()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.upstream.stop()

    def setUp(self) -> None:
        self.upstream.stop()
        self._servers: list = []
        self.cfg = proxy.Config(
            backend=proxy.BACKEND_GEMINI,
            host="127.0.0.1",
            port=0,
            alias="gemma",
            upstream="http://127.0.0.1:9",  # deliberately dead unless replaced
            gemini_host="http://127.0.0.1:9",
            gemini_project="termini-510609",
            gemini_model="gemini-2.5-flash",
            access_token="test-token",
            state_file="/tmp/termini-test-state.json",
        )

    def tearDown(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers = []

    def start_proxy(self, cfg=None) -> str:
        cfg = cfg or self.cfg
        server = proxy.make_server(cfg, host="127.0.0.1", port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._servers.append(server)
        self.log = server.RequestHandlerClass.request_log
        self.cfg = cfg
        return f"http://127.0.0.1:{server.server_address[1]}"

    def post(self, base: str, payload: dict, headers: dict | None = None):
        req = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def get(self, base: str, path: str):
        try:
            with urllib.request.urlopen(base + path, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def sse(self, base: str, payload: dict) -> tuple[int, list, list, bool]:
        payload = dict(payload, stream=True)
        req = urllib.request.Request(
            base + "/v1/chat/completions", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        deltas: list = []
        finals: list = []
        done = False
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                chunk = json.loads(data)
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    deltas.append(delta["content"])
                if choice.get("finish_reason") or chunk.get("usage"):
                    finals.append(chunk)
        return status, deltas, finals, done


# --------------------------------------------------------------------------- #
# Local backend — the drop-in property
# --------------------------------------------------------------------------- #

class TestLocalBackend(ProxyTestCase):
    def _cfg(self, upstream: str):
        return proxy.Config(
            backend=proxy.BACKEND_LOCAL, host="127.0.0.1", port=0, alias="gemma",
            upstream=upstream, state_file="/tmp/termini-test-state.json",
        )

    def test_health_and_models_advertise_the_alias(self):
        upstream = self.upstream.start({"get_body": {"status": "ok"}})
        base = self.start_proxy(self._cfg(upstream))

        status, headers, body = self.get(base, "/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["backend"], "local")
        self.assertEqual(payload["answered_by"], "local-weights")
        self.assertEqual(payload["egress"], "in-vpc")
        self.assertEqual(headers["X-Termini-Backend"], "local")

        status, _, body = self.get(base, "/v1/models")
        self.assertEqual(status, 200)
        models = json.loads(body)
        self.assertEqual([m["id"] for m in models["data"]], ["gemma"])
        self.assertEqual(models["data"][0]["object"], "model")

    def test_non_streaming_body_is_relayed_byte_for_byte(self):
        llama_body = {
            "id": "chatcmpl-from-llamacpp", "object": "chat.completion",
            "created": 123, "model": "gemma",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "local answer"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            "timings": {"prompt_n": 5, "predicted_n": 3},
        }
        upstream = self.upstream.start({"body": llama_body})
        base = self.start_proxy(self._cfg(upstream))

        status, headers, body = self.post(base, {"model": "gemma", "messages": [
            {"role": "user", "content": "hello"}], "max_tokens": 8})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), llama_body)  # untouched
        self.assertEqual(headers["X-Termini-Backend"], "local")
        self.assertEqual(headers["X-Termini-Answered-By"], "local-weights")

    def test_worker_sees_the_request_unchanged(self):
        upstream = self.upstream.start({"body": {"choices": []}})
        base = self.start_proxy(self._cfg(upstream))
        sent = {"model": "gemma", "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 8, "temperature": 0.7}
        self.post(base, sent)
        worker_seen = self._last_seen()
        self.assertEqual(worker_seen["path"], "/v1/chat/completions")
        self.assertEqual(worker_seen["body"], sent)

    def _last_seen(self) -> dict:
        # The handler class is created per server by _Upstream.start.
        for server in self.upstream._servers:
            seen = server.RequestHandlerClass.seen
            if seen:
                return seen[-1]
        raise AssertionError("nothing reached the fake worker")

    def test_streaming_is_relayed(self):
        behaviour = {"stream": True, "frames": [
            {"choices": [{"delta": {"content": "one "}}]},
            {"choices": [{"delta": {"content": "two"}}]},
        ]}
        upstream = self.upstream.start(behaviour)
        base = self.start_proxy(self._cfg(upstream))
        status, deltas, _, done = self.sse(base, {"model": "gemma", "messages": [
            {"role": "user", "content": "count"}], "max_tokens": 8})
        self.assertEqual(status, 200)
        self.assertEqual(deltas, ["one ", "two"])
        self.assertTrue(done)

    def test_worker_down_fails_loudly(self):
        base = self.start_proxy(self._cfg("http://127.0.0.1:9"))
        status, headers, body = self.post(base, {"model": "gemma", "messages": [
            {"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "MODEL_UNAVAILABLE")
        self.assertEqual(error["distinct"], "local_unreachable")
        self.assertEqual(error["backend"], "local")
        self.assertEqual(headers["X-Termini-Backend"], "local")


# --------------------------------------------------------------------------- #
# Forwarded backend — translation, labelling, failure
# --------------------------------------------------------------------------- #

class TestGeminiBackend(ProxyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.vertex = self.upstream.start({"body": _vertex_body()})
        self.cfg.gemini_host = self.vertex

    def test_request_is_translated_and_parameters_are_honoured(self):
        base = self.start_proxy()
        status, headers, body = self.post(base, {
            "model": "gemma",
            "messages": [{"role": "system", "content": "be terse"},
                         {"role": "user", "content": "Reply with exactly: PROXY_OK"}],
            "max_tokens": 32, "temperature": 0.0,
            "response_format": {"type": "json_object"},
        })
        self.assertEqual(status, 200)
        seen = self._vertex_seen()
        self.assertIn(":generateContent", seen["path"])
        self.assertIn("/projects/termini-510609/locations/global/", seen["path"])
        self.assertIn("gemini-2.5-flash", seen["path"])
        self.assertEqual(seen["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(seen["body"]["contents"],
                         [{"role": "user", "parts": [{"text": "Reply with exactly: PROXY_OK"}]}])
        self.assertEqual(seen["body"]["systemInstruction"], {"parts": [{"text": "be terse"}]})
        self.assertEqual(seen["body"]["generationConfig"]["maxOutputTokens"], 32)
        self.assertEqual(seen["body"]["generationConfig"]["temperature"], 0.0)
        self.assertEqual(seen["body"]["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(seen["body"]["generationConfig"]["thinkingConfig"],
                         {"thinkingBudget": 0})

    def test_default_max_tokens_and_temperature_follow_the_contract(self):
        base = self.start_proxy()
        self.post(base, {"model": "gemma", "messages": [{"role": "user", "content": "hi"}]})
        config = self._vertex_seen()["body"]["generationConfig"]
        self.assertEqual(config["maxOutputTokens"], 1024)   # contract §10.1 default
        self.assertEqual(config["temperature"], 0.2)

    def test_response_envelope_is_openai_shaped_and_labelled(self):
        base = self.start_proxy()
        status, headers, body = self.post(base, {"model": "gemma", "messages": [
            {"role": "user", "content": "Reply with exactly: PROXY_OK"}], "max_tokens": 32})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["model"], "gemma")           # the alias the caller knows
        self.assertTrue(payload["id"].startswith("chatcmpl-"))
        self.assertEqual(payload["choices"][0]["message"]["content"], "PROXY_OK")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertEqual(payload["usage"],
                         {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10})
        self.assertIn("timings", payload)

        label = payload["termini_backend"]
        self.assertEqual(label["backend"], "gemini-proxy")
        self.assertEqual(label["answered_by"], "forwarded-managed-model")
        self.assertEqual(label["model"], "gemini-2.5-flash")
        self.assertEqual(label["destination"], "127.0.0.1")
        self.assertEqual(label["egress"], "external")
        self.assertTrue(label["forwarded"])

        self.assertEqual(headers["X-Termini-Backend"], "gemini-proxy")
        self.assertEqual(headers["X-Termini-Model-Id"], "gemini-2.5-flash")
        self.assertEqual(headers["X-Termini-Egress"], "external")
        self.assertEqual(headers["X-Termini-Answered-By"], "forwarded-managed-model")
        self.assertIn("X-Termini-External-Destination", headers)
        self.assertEqual(headers["X-Termini-Model"], "gemma")

    def test_streaming_emits_deltas_finish_and_done(self):
        frames = [
            _sse_frame("PROXY"),
            _sse_frame("_OK", finish="STOP",
                       usage={"promptTokenCount": 8, "candidatesTokenCount": 2,
                              "totalTokenCount": 10}),
        ]
        self.stop_all()
        self.vertex = self.upstream.start({"stream": True, "frames": frames})
        self.cfg.gemini_host = self.vertex
        base = self.start_proxy()

        status, deltas, finals, done = self.sse(base, {
            "model": "gemma", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16, "stream_options": {"include_usage": True}})
        self.assertEqual(status, 200)
        self.assertEqual("".join(deltas), "PROXY_OK")
        self.assertEqual(len(deltas), 2)
        self.assertTrue(done)
        last = finals[-1]
        self.assertEqual(last["choices"][0]["finish_reason"], "stop")
        self.assertEqual(last["usage"]["total_tokens"], 10)
        self.assertEqual(last["termini_backend"]["backend"], "gemini-proxy")

    def test_request_id_is_echoed(self):
        base = self.start_proxy()
        _, headers, _ = self.post(base, {"model": "gemma", "messages": [
            {"role": "user", "content": "hi"}]}, headers={"X-Termini-Request-Id": "01REQ"})
        self.assertEqual(headers["X-Termini-Request-Id"], "01REQ")

    def test_rate_limit_maps_to_inference_busy_with_retry_after(self):
        self.stop_all()
        self.vertex = self.upstream.start({"status": 429, "retry_after": 7,
                                           "error_message": "quota exceeded"})
        self.cfg.gemini_host = self.vertex
        base = self.start_proxy()
        status, headers, body = self.post(base, {"model": "gemma", "messages": [
            {"role": "user", "content": "hi"}]})
        self.assertEqual(status, 429)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "INFERENCE_BUSY")
        self.assertEqual(error["distinct"], "vertex_rate_limited")
        self.assertEqual(error["destination"], "127.0.0.1")
        self.assertEqual(headers["Retry-After"], "7")
        self.assertEqual(headers["X-Termini-Backend"], "gemini-proxy")

    def test_model_rejected_maps_to_model_unavailable(self):
        """The forced-failure path: a bogus model id must fail loudly, not silently."""
        self.stop_all()
        self.vertex = self.upstream.start({"status": 404, "error_message":
                                           "Publisher Model gemini-nope was not found"})
        self.cfg.gemini_host = self.vertex
        base = self.start_proxy()
        status, _, body = self.post(base, {"model": "gemma", "messages": [
            {"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "MODEL_UNAVAILABLE")
        self.assertEqual(error["distinct"], "vertex_rejected")
        self.assertIn("gemini-nope", error["detail"])
        self.assertNotIn("local", error["message"])

    def test_auth_refusal_is_distinct(self):
        self.stop_all()
        self.vertex = self.upstream.start({"status": 403,
                                           "error_message": "Permission denied on resource"})
        self.cfg.gemini_host = self.vertex
        base = self.start_proxy()
        status, _, body = self.post(base, {"model": "gemma", "messages": [
            {"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"]["distinct"], "vertex_auth")

    def test_unreachable_destination_is_loud(self):
        self.cfg.gemini_host = "http://127.0.0.1:9"
        base = self.start_proxy()
        status, _, body = self.post(base, {"model": "gemma", "messages": [
            {"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        error = json.loads(body)["error"]
        self.assertEqual(error["distinct"], "vertex_unreachable")
        self.assertIn("unreachable", error["message"])

    def test_metadata_token_failure_is_loud(self):
        """No key file, no token — the node's own identity is the only credential."""
        cfg = proxy.Config(
            backend=proxy.BACKEND_GEMINI, host="127.0.0.1", port=0, alias="gemma",
            gemini_host=self.vertex, gemini_project="termini-510609",
            access_token="",  # forces the metadata-server path
            state_file="/tmp/termini-test-state.json",
        )
        original = proxy._METADATA_TOKEN_URL
        proxy._METADATA_TOKEN_URL = "http://127.0.0.1:9/token"  # type: ignore[attr-defined]
        try:
            base = self.start_proxy(cfg)
            status, _, body = self.post(base, {"model": "gemma", "messages": [
                {"role": "user", "content": "hi"}]})
        finally:
            proxy._METADATA_TOKEN_URL = original  # type: ignore[attr-defined]
        self.assertEqual(status, 503)
        error = json.loads(body)["error"]
        self.assertEqual(error["distinct"], "metadata_token")
        self.assertIn("metadata server", error["message"])

    # -- helpers ----------------------------------------------------------- #
    def _vertex_seen(self) -> dict:
        for server in self.upstream._servers:
            seen = server.RequestHandlerClass.seen
            if seen:
                return seen[-1]
        raise AssertionError("nothing reached the fake Vertex endpoint")

    def stop_all(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers = []
        self.upstream.stop()


# --------------------------------------------------------------------------- #
# Logging and configuration
# --------------------------------------------------------------------------- #

class TestLoggingAndConfig(ProxyTestCase):
    def _capture(self) -> io.StringIO:
        return io.StringIO()

    def test_forwarded_log_line_names_backend_model_and_destination(self):
        vertex = self.upstream.start({"body": _vertex_body()})
        cfg = proxy.Config(
            backend=proxy.BACKEND_GEMINI, host="127.0.0.1", port=0, alias="gemma",
            gemini_host=vertex, gemini_project="termini-510609", access_token="t",
            state_file="/tmp/termini-test-state.json",
        )
        base = self.start_proxy(cfg)
        buffer = self._capture()
        original = sys.stderr
        sys.stderr = buffer
        try:
            self.post(base, {"model": "gemma", "messages": [
                {"role": "user", "content": "hi"}], "max_tokens": 32})
        finally:
            sys.stderr = original

        lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
        request_lines = [line for line in lines if line["event"] == "inference_request"]
        self.assertEqual(len(request_lines), 1)
        record = request_lines[0]
        self.assertEqual(record["backend"], "gemini-proxy")
        self.assertEqual(record["answered_by"], "forwarded-managed-model")
        self.assertEqual(record["model"], "gemini-2.5-flash")
        self.assertEqual(record["egress"], "external")
        self.assertIn("destination", record)
        self.assertEqual(record["requested_max_tokens"], 32)
        # No log line may claim "GPU" or local weights for a forwarded call.
        blob = json.dumps(record)
        self.assertNotIn("gpu", blob.lower())
        self.assertNotIn("local-weights", blob)
        self.assertEqual(record["backend"], "gemini-proxy")
        self.assertEqual(record["answered_by"], "forwarded-managed-model")
        self.assertEqual(record["forwarded"], True)

    def test_local_log_line_is_labelled_local(self):
        upstream = self.upstream.start({"body": {"choices": []}})
        cfg = proxy.Config(
            backend=proxy.BACKEND_LOCAL, host="127.0.0.1", port=0, alias="gemma",
            upstream=upstream, state_file="/tmp/termini-test-state.json",
        )
        base = self.start_proxy(cfg)
        buffer = self._capture()
        original = sys.stderr
        sys.stderr = buffer
        try:
            self.post(base, {"model": "gemma", "messages": [
                {"role": "user", "content": "hi"}]})
        finally:
            sys.stderr = original
        lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
        record = [line for line in lines if line["event"] == "inference_request"][0]
        self.assertEqual(record["backend"], "local")
        self.assertEqual(record["answered_by"], "local-weights")
        self.assertFalse(record["forwarded"])

    def test_failure_log_line_is_an_error_with_a_distinct_reason(self):
        cfg = proxy.Config(
            backend=proxy.BACKEND_GEMINI, host="127.0.0.1", port=0, alias="gemma",
            gemini_host="http://127.0.0.1:9", gemini_project="termini-510609",
            access_token="t", state_file="/tmp/termini-test-state.json",
        )
        base = self.start_proxy(cfg)
        buffer = self._capture()
        original = sys.stderr
        sys.stderr = buffer
        try:
            self.post(base, {"model": "gemma", "messages": [{"role": "user", "content": "hi"}]})
        finally:
            sys.stderr = original
        lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
        failures = [line for line in lines if line["event"] == "upstream_failure"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["severity"], "error")
        self.assertEqual(failures[0]["distinct"], "vertex_unreachable")
        self.assertEqual(failures[0]["backend"], "gemini-proxy")

    def test_backend_comes_from_configuration_only(self):
        self.assertEqual(
            proxy.Config.from_env({"TERMINI_INFERENCE_BACKEND": "local"}).backend, "local")
        self.assertEqual(
            proxy.Config.from_env({"TERMINI_INFERENCE_BACKEND": "gemini-proxy"}).backend,
            "gemini-proxy")
        self.assertEqual(proxy.Config.from_env({}).backend, "local")
        with self.assertRaises(ValueError):
            proxy.Config.from_env({"TERMINI_INFERENCE_BACKEND": "nonsense"})

    def test_env_overrides(self):
        cfg = proxy.Config.from_env({
            "TERMINI_INFERENCE_BACKEND": "gemini-proxy",
            "TERMINI_INFERENCE_PORT": "8080",
            "TERMINI_INFERENCE_ALIAS": "gemma",
            "TERMINI_GEMINI_MODEL": "gemini-2.5-flash",
            "TERMINI_GEMINI_LOCATION": "global",
            "TERMINI_GEMINI_PROJECT": "termini-510609",
            "TERMINI_INFERENCE_UPSTREAM": "http://127.0.0.1:8081",
        })
        self.assertEqual(cfg.port, 8080)
        self.assertEqual(cfg.alias, "gemma")
        self.assertEqual(cfg.served_model, "gemini-2.5-flash")
        self.assertEqual(cfg.upstream_url("/v1/models"), "/v1/models")
        self.assertEqual(cfg.upstream_host_port(), ("127.0.0.1", 8081))
        self.assertEqual(cfg.external_destination(), "aiplatform.googleapis.com")

    def test_state_file_records_the_active_backend(self):
        for backend, answered in ((proxy.BACKEND_GEMINI, "forwarded-managed-model"),
                                  (proxy.BACKEND_LOCAL, "local-weights")):
            cfg = proxy.Config(
                backend=backend, host="127.0.0.1", port=8080, alias="gemma",
                gemini_project="termini-510609",
                state_file=f"/tmp/termini-state-{backend}.json",
            )
            path = proxy.write_state(cfg, "termini-inference-proxy.service")
            state = json.loads(pathlib.Path(path).read_text())
            self.assertEqual(state["backend"], backend)
            self.assertEqual(state["answered_by"], answered)
            self.assertEqual(state["active_unit"], "termini-inference-proxy.service")
            self.assertFalse(state["is_gpu_inference"])
            if backend == proxy.BACKEND_GEMINI:
                self.assertEqual(state["destination"], "aiplatform.googleapis.com")
                self.assertEqual(state["egress"], "external")


if __name__ == "__main__":
    unittest.main()
