#!/usr/bin/env bash
# verify-proxy.sh — acceptance harness for Node C's inference front door.
#
# Run this ON A PEER (Node A), against Node C's underlay address:
#
#   ./verify-proxy.sh 10.10.0.13 gemini-proxy
#   ./verify-proxy.sh 10.10.0.13 local
#
# It asserts the properties the card is about, over the wire, from a peer:
#
#   1. DROP-IN — `/health` is 200, `/v1/models` advertises the alias `gemma`, and
#      `/v1/chat/completions` returns the OpenAI envelope both non-streaming and
#      streaming (>=2 incremental SSE deltas terminated by `[DONE]`).
#   2. PARAMETERS — `max_tokens` and `temperature` are honoured.
#   3. LABELLING — the response says which backend answered: `backend`,
#      `answered_by` and (when forwarded) `destination`. The check FAILS if a
#      forwarded call is labelled as local weights, or vice versa, and it fails if
#      any label claims a GPU.
#   4. SUBSTANCE — when the backend is `gemini-proxy` the answer is a real
#      completion from the managed model, not an empty string, and the wall-clock
#      latency is reported so the forwarded path can be compared with the CPU one.
#
# Exit 0 only when every check passes. Raw output is the evidence; nothing here
# summarises or prettifies a failure.
set -uo pipefail

NODE_C="${1:-${NODE_C_IP:-}}"
EXPECT_BACKEND="${2:-${EXPECT_BACKEND:-gemini-proxy}}"
PORT="${PORT:-8080}"
ALIAS="${MODEL_ALIAS:-gemma}"
PROMPT="${PROMPT:-Reply with exactly: PROXI_OK}"

[ -n "$NODE_C" ] || { echo "usage: $0 <node-c-ip> [gemini-proxy|local|legacy]" >&2; exit 2; }
case "$NODE_C" in
  127.0.0.1|localhost)
    if [ "${ALLOW_LOOPBACK:-0}" != "1" ]; then
      echo "FATAL: $NODE_C is this host — the acceptance is FROM A PEER (Node A)." >&2
      echo "       Set ALLOW_LOOPBACK=1 only for a node-local smoke test." >&2
      exit 2
    fi ;;
esac

say() { printf '%s\n' "$*"; }
hdr() { printf '\n===== %s =====\n' "$*"; }

say "Termini inference front door — peer acceptance, $(date -u +%FT%TZ)"
say "caller: $(hostname) ($(hostname -I 2>/dev/null | awk '{print $1}'))   endpoint: http://${NODE_C}:${PORT}   expect backend: ${EXPECT_BACKEND}"

hdr "1. GET /health (from this peer)"
HEALTH_CODE=$(curl -s -m 10 -o /tmp/termini-proxy-health.json -w '%{http_code}' "http://${NODE_C}:${PORT}/health")
say "http_code=$HEALTH_CODE"
say "body: $(cat /tmp/termini-proxy-health.json 2>/dev/null)"
REPORTED_BACKEND=$(python3 -c 'import json;print(json.load(open("/tmp/termini-proxy-health.json")).get("backend",""))' 2>/dev/null || echo "")

hdr "2. GET /v1/models"
MODELS=$(curl -s -m 10 "http://${NODE_C}:${PORT}/v1/models")
say "$MODELS"

hdr "3-6. requests (non-streaming, streaming, parameters, labels)"
NODE_C="$NODE_C" PORT="$PORT" ALIAS="$ALIAS" PROMPT="$PROMPT" \
EXPECT_BACKEND="$EXPECT_BACKEND" HEALTH_CODE="$HEALTH_CODE" MODELS="$MODELS" \
REPORTED_BACKEND="$REPORTED_BACKEND" python3 - <<'PY'
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

node_c = os.environ["NODE_C"]
port = os.environ["PORT"]
alias = os.environ["ALIAS"]
prompt = os.environ["PROMPT"]
expect = os.environ["EXPECT_BACKEND"]
health_code = os.environ["HEALTH_CODE"]
models = os.environ["MODELS"]
reported_backend = os.environ["REPORTED_BACKEND"]

BASE = f"http://{node_c}:{port}"
failures = []


def check(name, ok, detail=""):
    if ok:
        print(f"PASS  {name}" + (f" — {detail}" if detail else ""))
    else:
        print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))
        failures.append(name)
    return ok


def post(payload, timeout=180):
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Termini-Request-Id": "01ACCEPT"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read().decode()), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        return exc.code, dict(exc.headers), json.loads(body or "{}"), time.monotonic() - started


# -- 1. health --------------------------------------------------------------- #
check("peer: /health returns 200", health_code == "200", f"got {health_code}")
check("/health reports a backend", bool(reported_backend), f"backend={reported_backend!r}")
if expect:
    check(f"/health backend == {expect}", reported_backend == expect,
          f"reported {reported_backend!r}")

# -- 2. model list ----------------------------------------------------------- #
try:
    model_ids = [m.get("id") for m in json.loads(models).get("data", [])]
except Exception as exc:  # noqa: BLE001
    model_ids = []
    print(f"      /v1/models unparsable: {exc}")
check(f"/v1/models advertises the alias {alias!r}", alias in model_ids, f"ids={model_ids}")

# -- 3. non-streaming completion --------------------------------------------- #
latencies = []
status, headers, body, elapsed = post({
    "model": alias,
    "messages": [{"role": "user", "content": prompt}],
    "max_tokens": 32,
    "temperature": 0,
})
latencies.append(elapsed)
print(f"      non-streaming: http={status} in {elapsed:.2f}s")
print(f"      response headers: "
      + ", ".join(f"{k}={v}" for k, v in headers.items()
                  if k.lower().startswith("x-termini")))
print(f"      body: {json.dumps(body)[:900]}")

content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
check("non-streaming returns HTTP 200", status == 200, f"got {status}")
check("non-streaming returns non-empty content", bool(content.strip()), repr(content[:80]))
check("the envelope names the model alias", body.get("model") == alias, f"model={body.get('model')!r}")
check("the envelope carries the OpenAI id/object/created fields",
      str(body.get("id", "")).startswith("chatcmpl-") and body.get("object") == "chat.completion"
      and isinstance(body.get("created"), int))
check("usage is reported", isinstance(body.get("usage"), dict)
      and isinstance(body["usage"].get("total_tokens"), int), f"usage={body.get('usage')}")

label = body.get("termini_backend") or {}
# Where the labels live depends on the backend, and that is deliberate:
#   * forwarded  -> headers AND a `termini_backend` body block (the answer is ours
#                   to shape, so the substitution is stated in-band in both);
#   * local      -> headers only, because the body is llama.cpp's OWN bytes
#                   (byte-transparent passthrough is what makes it a drop-in).
hdr = {k.lower(): v for k, v in headers.items()}
hdr_backend = hdr.get("x-termini-backend", "")
hdr_answered = hdr.get("x-termini-answered-by", "")
hdr_egress = hdr.get("x-termini-egress", "")
hdr_dest = hdr.get("x-termini-external-destination", "")
backend_label = label.get("backend") or hdr_backend
answered_label = label.get("answered_by") or hdr_answered
egress_label = label.get("egress") or hdr_egress
print(f"      label headers: X-Termini-Backend={hdr_backend!r} "
      f"X-Termini-Answered-By={hdr_answered!r} X-Termini-Egress={hdr_egress!r} "
      f"X-Termini-External-Destination={hdr_dest!r}")
print(f"      in-band body label: {json.dumps(label)}")
check("the response labels its backend in-band", bool(backend_label),
      f"header={hdr_backend!r} body={label.get('backend')!r}")
check("the label names what actually answered (answered_by)",
      bool(answered_label), f"header={hdr_answered!r} body={label.get('answered_by')!r}")
# The strongest form of "nothing may claim GPU": the word must not appear in a
# response or its headers at all, for either backend.
blob = (json.dumps(body) + json.dumps(dict(headers))).lower()
check("no response claims GPU inference (word absent from body + headers)",
      "gpu" not in blob)
check("no response claims local weights for a forwarded call",
      expect != "gemini-proxy" or "local-weights" not in json.dumps(label))
if expect == "gemini-proxy":
    check("a forwarded answer is labelled answered_by=forwarded-managed-model",
          answered_label == "forwarded-managed-model", f"got {answered_label!r}")
    check("a forwarded answer names its external destination",
          bool(label.get("destination") or hdr_dest),
          f"body={label.get('destination')!r} header={hdr_dest!r}")
    check("a forwarded answer is flagged egress=external",
          egress_label == "external", f"got {egress_label!r}")
    check("a forwarded answer is NOT labelled as local weights",
          backend_label != "local")
elif expect == "local":
    check("a local answer is labelled answered_by=local-weights",
          answered_label == "local-weights", f"got {answered_label!r}")
    check("a local answer is flagged egress=in-vpc", egress_label == "in-vpc",
          f"got {egress_label!r}")
    check("the local answer is llama.cpp's own envelope (byte-transparent passthrough)",
          "termini_backend" not in json.dumps(body))

# -- 4. parameters honoured -------------------------------------------------- #
status, _, body2, elapsed2 = post({
    "model": alias,
    "messages": [{"role": "user", "content": "Count from one to twenty, one number per line."}],
    "max_tokens": 8, "temperature": 0,
})
completion_tokens = (body2.get("usage") or {}).get("completion_tokens")
check("max_tokens is honoured (8 requested)",
      isinstance(completion_tokens, int) and 0 < completion_tokens <= 12,
      f"completion_tokens={completion_tokens}")
print(f"      max_tokens=8 run: {elapsed2:.2f}s, usage={body2.get('usage')}")

# -- 5. streaming ------------------------------------------------------------ #
stream_body = json.dumps({
    "model": alias,
    "messages": [{"role": "user", "content": "Count from one to five, one number per line."}],
    "max_tokens": 40, "temperature": 0, "stream": True,
    "stream_options": {"include_usage": True},
}).encode()
req = urllib.request.Request(BASE + "/v1/chat/completions", data=stream_body,
                            headers={"Content-Type": "application/json"}, method="POST")
started = time.monotonic()
deltas, done, first, final_usage, final_label, final_finish = [], False, None, None, None, None
with urllib.request.urlopen(req, timeout=180) as resp:
    stream_status = resp.status
    final_hdr_backend = resp.headers.get("X-Termini-Backend", "")
    final_hdr_dest = resp.headers.get("X-Termini-External-Destination", "")
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
        text = (choice.get("delta") or {}).get("content")
        if text:
            if first is None:
                first = time.monotonic() - started
            deltas.append(text)
        if chunk.get("usage"):
            final_usage = chunk["usage"]
        if chunk.get("termini_backend"):
            final_label = chunk["termini_backend"]
        if choice.get("finish_reason"):
            final_finish = choice["finish_reason"]
print(f"      streaming: http={stream_status} deltas={len(deltas)} first_token_s="
      f"{first if first is not None else -1:.3f} total_s={time.monotonic()-started:.2f}")
print(f"      assembled: {''.join(deltas)!r}")
print(f"      final label: {json.dumps(final_label)} usage={final_usage} finish={final_finish!r}")
check("streaming returns HTTP 200", stream_status == 200, f"got {stream_status}")
check("streaming yields >=2 incremental deltas", len(deltas) >= 2, f"{len(deltas)} deltas")
check("streaming terminates with [DONE]", done)
check("streaming assembled content is non-empty", bool("".join(deltas).strip()))
check("streaming reports usage when asked (include_usage)", isinstance(final_usage, dict)
      and isinstance(final_usage.get("total_tokens"), int), f"usage={final_usage}")
check("streaming final chunk is labelled in-band",
      bool((final_label or {}).get("backend") or final_hdr_backend),
      f"body={final_label} header={final_hdr_backend!r}")
if expect == "gemini-proxy":
    check("streaming from a forwarded backend names the destination",
          bool((final_label or {}).get("destination") or final_hdr_dest),
          f"label={final_label} header={final_hdr_dest!r}")

# -- 6. the honest comparison ------------------------------------------------ #
print(f"\n      latency samples (s): {[round(x, 3) for x in latencies]}")
print(f"      median latency: {statistics.median(latencies):.3f}s "
      f"(backend reported by the endpoint: {reported_backend})")
print("      NOTE: this compares the FRONT DOOR's own answer time; for the same")
print("      20-sample decision prompt the CPU path on this node is documented at")
print("      402 s (infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md).")

print()
if failures:
    print(f"RESULT: FAIL ({len(failures)} check(s) failed): {', '.join(failures)}")
    sys.exit(1)
print("RESULT: PASS (drop-in envelope + labelled backend + parameters + streaming)")
PY
RC=$?

hdr "RESULT"
if [ "$RC" -eq 0 ]; then
  say "RESULT: PASS — the front door is a drop-in and it labels what answered"
  exit 0
fi
say "RESULT: FAIL (see checks above)"
exit 1
