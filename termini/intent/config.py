"""Model/provider configuration for intent extraction.

Reuses the frozen on-prem inference config (agent-runtime.md §8/§14.1): the
OpenAI-compatible llama.cpp router on Node C, model alias ``deepseek-flash``,
temperature 0, JSON-object output. Values may be overridden via environment
variables; the defaults are the frozen demo values.

Env vars:
  TERMINI_INFERENCE_BASE_URL   (default http://10.10.0.13:8080)
  TERMINI_INFERENCE_MODEL      (default deepseek-flash)
  TERMINI_INFERENCE_TOKEN      (default empty — on-prem router bearer)
  TERMINI_INFERENCE_TIMEOUT_S  (default 30)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

DEFAULT_BASE_URL = "http://10.10.0.13:8080"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 512


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    token: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/v1/chat/completions"

    @classmethod
    def from_env(cls, environ=None) -> "ModelConfig":
        env = os.environ if environ is None else environ
        return cls(
            base_url=env.get("TERMINI_INFERENCE_BASE_URL", DEFAULT_BASE_URL),
            model=env.get("TERMINI_INFERENCE_MODEL", DEFAULT_MODEL),
            token=env.get("TERMINI_INFERENCE_TOKEN", ""),
            timeout_s=float(
                env.get("TERMINI_INFERENCE_TIMEOUT_S", str(DEFAULT_TIMEOUT_S))
            ),
            temperature=DEFAULT_TEMPERATURE,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
