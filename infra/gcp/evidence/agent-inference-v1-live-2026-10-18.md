# Agent inference base URL — live proof on the standing fleet (2026-10-18)

Card: `t_af463463` — "Agent inference: base_url has no /v1, so every agent LLM call 404s on
the front door". Contract: `docs/contracts/agent-runtime.md` §4.4, §8.2, §19.1/§19.2.

Everything below is raw output from an executed command. Nothing was deployed and no GCP
compute was started, stopped or created: the fleet was already RUNNING and this card only
reads from the front door (one model call per config form).

## Live window

| thing | value |
| --- | --- |
| window owner | `t_af463463` |
| taken | `2026-10-18T07:41:32.519Z` |
| released | `~2026-10-18T07:42Z` — released immediately after the run; `live-window.sh status` read "free — no live window is claimed" at `07:43:10Z` |
| purpose | live proof the agent `InferenceClient` reaches the node-C front door on `/v1` |

## Environment

| thing | value |
| --- | --- |
| front door | `termini-node-c` (asia-south1-a), `http://10.10.0.13:8080`, `backend=gemini-proxy` |
| transport | `gcloud compute ssh termini-node-c -- -N -L 18080:127.0.0.1:8080` (no public IP involved; nothing in the VM's config was changed) |
| code under test | the `t_af463463` branch tip — `image/agent-vm/agent/src/termini_agent/{config,inference}.py` |
| config used | `inference.base_url` written **both ways**: `http://127.0.0.1:18080` (the old shipped form) and `http://127.0.0.1:18080/v1`; loaded through `load_config(dev_override=True)` so the loopback tunnel passes the on-prem allow-list |

## The defect, reproduced on the live door

A raw `openai` client with the old `base_url` (no `/v1`) still 404s — that is the bug the
runtime now normalises away:

```
raw SDK without /v1 (base_url=http://127.0.0.1:18080): 404 NotFoundError -> Error code: 404 - {'error': {'message': "no route '/chat/completions' on the gemini-proxy backend", 'type': 'termini_err
```

## The fix, through the agent's own `InferenceClient`

`InferenceClient.complete_with_provenance()` (non-streaming, one call per config form):

```
configured base_url : http://127.0.0.1:18080
normalised base_url : http://127.0.0.1:18080/v1
request path        : /v1/chat/completions
answer (0.89s)  : 'The first letter of the Greek alphabet is Alpha.'
provenance          : {'backend': 'gemini-proxy', 'answered_by': 'forwarded-managed-model', 'model': 'gemini-2.5-flash', 'alias': 'gemma', 'egress': 'external', 'destination': 'aiplatform.googleapis.com', 'forwarded': True, 'note': 'answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node', 'source': 'endpoint-in-band'}
usage               : {'prompt_tokens': 13, 'completion_tokens': 10, 'total_tokens': 23}

configured base_url : http://127.0.0.1:18080/v1
normalised base_url : http://127.0.0.1:18080/v1
request path        : /v1/chat/completions
answer (1.19s)  : 'The first letter of the Greek alphabet is Alpha.'
provenance          : {'backend': 'gemini-proxy', 'answered_by': 'forwarded-managed-model', 'model': 'gemini-2.5-flash', 'alias': 'gemma', 'egress': 'external', 'destination': 'aiplatform.googleapis.com', 'forwarded': True, 'note': 'answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node', 'source': 'endpoint-in-band'}
usage               : {'prompt_tokens': 13, 'completion_tokens': 10, 'total_tokens': 23}

/health             : {'reachable': True, 'body': {'status': 'ok', 'model': 'gemma', 'backend': 'gemini-proxy', 'answered_by': 'forwarded-managed-model', 'egress': 'external', 'served_model': 'gemini-2.5-flash', 'local_path': {'available': True, 'upstream': 'http://127.0.0.1:8081', 'switch': 'TERMINI_INFERENCE_BACKEND=local'}, 'destination': 'aiplatform.googleapis.com', 'note': 'forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights'}}

RESULT: PASS
```

Both spellings produce the identical request path `/v1/chat/completions`, and a
non-empty answer comes back in ~1 s (the expected seconds-scale shape), labelled
in-band by the front door. `InferenceClient.health()` still probes `/health` at the
root, not `/v1/health`.

## What this does not cover

The image was **not** rebuilt or redeployed (a rebuild is a separate infra decision).
So the VM that shipped `agent.yaml.template` with the un-suffixed base URL keeps that
file until it is re-baked; the fix makes such a VM work anyway, because the runtime
normalises the configured value.
