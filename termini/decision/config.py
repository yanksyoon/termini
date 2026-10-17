"""Model/provider configuration for the operator decision adapter.

Reuses the frozen on-prem inference config (``agent-runtime.md`` §8/§14.1,
``docs/contracts/operator-decision.md`` §6.1/§13): the OpenAI-compatible
llama.cpp router on Node C, model alias ``gemma`` (Gemma 3 4B IT, Google open
weights — the model the shipped single-model node-c deployment serves),
temperature 0, JSON-object output. No new endpoint and no new service — this is
the exact same surface ``termini.intent`` already calls.

Env vars (all pre-existing; the adapter adds none of its own):

  TERMINI_INFERENCE_BASE_URL   (default http://10.10.0.13:8080)
  TERMINI_INFERENCE_MODEL      (default gemma)
  TERMINI_INFERENCE_TOKEN      (default empty — on-prem router bearer)
  TERMINI_INFERENCE_TIMEOUT_S  (default 30)

The controller-side decision knobs (``TERMINI_OPERATOR_DECISION_*`` — mode,
timeout, min-confidence, fallback cap) belong to the controller, not to this
package; the controller passes its own timeout through when it constructs a
:class:`DecisionModelConfig` for the proxy call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Optional, Mapping

# The request-correlation header the adapter sets on every model call (§6.1).
REQUEST_ID_HEADER = "X-Termini-Request-Id"

DEFAULT_BASE_URL = "http://10.10.0.13:8080"
DEFAULT_MODEL = "gemma"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_TEMPERATURE = 0.0
# The decision response carries the §4.2 incident record (title + explanation +
# up to 12 evidence entries + up to 8 action entries). With the F01 leak
# signature the evidence window is dense, and a model that faithfully quotes the
# fields it used produces a reply longer than 512 tokens — which truncated the
# JSON mid-array and turned a correct `instance.restart` into a schema_violation
# fallback (t_979fa31b). 1024 leaves headroom; override with
# TERMINI_INFERENCE_MAX_TOKENS when a longer record is needed.
DEFAULT_MAX_TOKENS = 1024


@dataclass(frozen=True)
class DecisionModelConfig:
    """Frozen on-prem inference settings for the decision adapter.

    Mirrors :class:`termini.intent.config.ModelConfig` field-for-field so the
    two model-facing packages share one transport contract.
    """

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    token: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/v1/chat/completions"

    def with_timeout(self, timeout_s: float) -> "DecisionModelConfig":
        """Return a copy with a different call timeout.

        The controller's ``TERMINI_OPERATOR_DECISION_TIMEOUT_S`` (§13) overrides
        the generic inference timeout for the decision call specifically:
        ``decide(input, config=cfg.with_timeout(cfg_decision_timeout_s))``.
        """
        return replace(self, timeout_s=timeout_s)

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "DecisionModelConfig":
        env = os.environ if environ is None else environ
        return cls(
            base_url=env.get("TERMINI_INFERENCE_BASE_URL", DEFAULT_BASE_URL),
            model=env.get("TERMINI_INFERENCE_MODEL", DEFAULT_MODEL),
            token=env.get("TERMINI_INFERENCE_TOKEN", ""),
            timeout_s=float(env.get("TERMINI_INFERENCE_TIMEOUT_S", str(DEFAULT_TIMEOUT_S))),
            temperature=DEFAULT_TEMPERATURE,
            max_tokens=int(env.get("TERMINI_INFERENCE_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))),
        )
