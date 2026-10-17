#!/usr/bin/env bash
# CMD: (node-c) curl :8080/v1/chat/completions (forwarded) vs curl :8081/v1/chat/completions (local CPU Gemma) — same prompt, both timed
# Step 12 — the honest comparison: the SAME prompt answered by the CPU/Gemma path and
# by the forwarded path, side by side, with both latencies and the explicit label.
#
# What each side is:
#   local      = llama.cpp serving Gemma 3 4B IT on this node's CPU cores (no GPU)
#   forwarded  = the same alias `gemma` served by the proxy, answered by
#                gemini-2.5-flash at aiplatform.googleapis.com (a managed model
#                standing in for a GPU node — labelled as such everywhere)
# The 402.1 s figure quoted at the end is the CPU path answering the REAL 20-sample
# operator decision prompt (12,748 prompt tokens), not this short probe.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
echo "step 12 — CPU/Gemma vs forwarded, same prompt   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
PROMPT='{"messages":[{"role":"user","content":"A node reports disk write latency 120ms and rising. Reply with one JSON object: {\"action_kind\":\"...\",\"confidence\":0.0}. No other text."}],"max_tokens":48,"temperature":0}'

note "FORWARDED path — alias gemma via the proxy on :8080 (managed model, external egress)"
on_node termini-node-c "curl -sS -X POST http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d '$PROMPT' -w '\\n--- forwarded: http=%{http_code} total_s=%{time_total}\\n'"

note "CPU path — the same prompt straight to llama.cpp on 127.0.0.1:8081 (no GPU on this box)"
on_node termini-node-c "curl -sS -X POST http://127.0.0.1:8081/v1/chat/completions -H 'Content-Type: application/json' -d '$PROMPT' -w '\\n--- local-cpu: http=%{http_code} total_s=%{time_total}\\n'"

note "the proxy's record of the forwarded side (backend label, destination, latency)"
on_node termini-node-c "sudo journalctl -u termini-inference-proxy --since '-3 min' --no-pager | grep inference_request | tail -2"

echo
echo "READ THIS WITH THE LABEL: the local path above is Gemma on CPU (no GPU present on"
echo "node-c); the forwarded path is a MANAGED MODEL (gemini-2.5-flash at"
echo "aiplatform.googleapis.com) standing in for a GPU node. The forwarded side is not"
echo "local inference and nothing here claims GPU compute."
echo
echo "For the REAL operator decision prompt (20 telemetry samples, 12,748 prompt tokens):"
echo "  CPU Gemma path      : 402.1 s   (infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md)"
echo "  forwarded path      : ~1-3 s    (step 07/08 decision_trace model_call latency_ms, this run)"
echo "The ~400x gap is the reason the demo forwards instead of running weights on CPU."
