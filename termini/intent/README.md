# termini.intent — NL resource-intent extractor + clamp validator

Two importable modules and a thin CLI that turn a free-text resource request
into an allocation spec that is provably safe to hand to LXD.

```
termini/intent/
  types.py      frozen bounds + LXD-facing dataclasses (AllocationSpec, HostCaps,
                ValidationResult, ClampDecision, Rejection)
  validator.py  deterministic clamp validator (PURE, LLM-free — the security
                boundary)
  extractor.py  LLM extraction + extract_and_validate() pipeline
  prompt.py     static, versioned extraction template + few-shot examples
  config.py     on-prem inference config (agent-runtime.md §14.1), env-overridable
  __main__.py   CLI
```

## Security boundary

`validator.clamp_validate` is pure and LLM-free (no network, no model, no I/O).
It is the **only** path from LLM output to LXD. For every numeric field it
applies `min(requested, host_max)` from a supplied host-capacity table, plus
floor/positive bounds, integer coercion, a max component count, and
unknown-component rejection (all-or-nothing). Every alteration is reported as
a `ClampDecision` (requested vs applied). The raw LLM dicts are never returned;
only freshly constructed `AllocationSpec` objects are.

## Run it

```bash
# full pipeline (LLM extraction + clamp) — needs the on-prem router reachable
python -m termini.intent "deploy 3 agents + 1 fast LLM"

# validator-only, constrained host (deterministic — no model call)
python -m termini.intent --raw \
  '[{"type":"agent_vm","count":3,"vcpus":1000,"ram_gb":512,"disk_gb":8},
    {"type":"inference_vm","count":1,"vcpus":128,"ram_gb":512,"disk_gb":64,"passthrough_gpu":false}]' \
  --host-caps '{"agent_vm":{"vcpus":2,"ram_gb":4,"disk_gb":16},
                "inference_vm":{"vcpus":4,"ram_gb":8,"disk_gb":32}}'
```

## As a library

```python
from termini.intent import extract_and_validate, clamp_validate

result = extract_and_validate(
    "deploy 3 agents + 1 fast LLM",
    {"agent_vm": {"vcpus": 4, "ram_gb": 8, "disk_gb": 32},
     "inference_vm": {"vcpus": 8, "ram_gb": 16, "disk_gb": 64}},
    client=my_model_client,   # injectable; default hits the on-prem router
)
assert result.safe
for spec in result.allocations:      # LXD-facing specs, all fields clamped
    ...
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```
