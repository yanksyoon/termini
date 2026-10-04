"""NL resource-intent extractor (LLM side).

Maps free text such as ``"deploy 3 agents + 1 fast LLM"`` to a raw list of
allocation dicts via a model call (on-prem DeepSeek-flash by default, per
agent-runtime.md §14.1). The raw output is **not** safe to execute: route it
through :func:`termini.intent.validator.clamp_validate` via
:func:`extract_and_validate`, which is the only public path that returns an
LXD-safe spec.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Union

from . import prompt as prompt_module
from .config import ModelConfig
from .types import REASON_EXTRACTION_FAILED, Rejection, ValidationResult
from .validator import clamp_validate

# A model client receives the built messages and returns the assistant's text
# content (a JSON string). Injectable so tests never touch the network.
ModelClient = Callable[..., str]


class ExtractionError(Exception):
    """The model did not return a usable allocation JSON."""


def _default_client(config: ModelConfig) -> ModelClient:
    """Build an HTTP client against the on-prem OpenAI-compatible router."""

    def _call(messages: List[Dict[str, str]], **kwargs: Any) -> str:
        body = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if config.token:
            headers["Authorization"] = "Bearer " + config.token
        req = urllib.request.Request(
            config.chat_url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=config.timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExtractionError(f"unexpected model response shape: {exc}") from exc

    return _call


def _parse_allocations(content: str) -> List[dict]:
    """Parse the model's JSON text into a list of raw component dicts.

    Accepts a bare list, or an ``{"allocations": [...]}`` /
    ``{"components": [...]}`` envelope. Raises :class:`ExtractionError` on
    anything that is not a JSON list of objects.
    """
    text = content.strip()
    # Tolerate stray markdown fences the model sometimes adds.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"model output is not valid JSON: {exc}") from exc

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if "allocations" in data:
            items = data["allocations"]
        elif "components" in data:
            items = data["components"]
        else:
            raise ExtractionError("model JSON missing 'allocations' key")
    else:
        raise ExtractionError(f"model output is {type(data).__name__}, expected a list")

    if not isinstance(items, list):
        raise ExtractionError("'allocations' is not a list")
    if items and not all(isinstance(i, dict) for i in items):
        raise ExtractionError("'allocations' contains non-object entries")
    return items


def extract(
    prompt: str,
    client: Optional[ModelClient] = None,
    config: Optional[ModelConfig] = None,
) -> List[dict]:
    """Run extraction and return the RAW component dicts (unvalidated).

    Callers that intend to execute must use :func:`extract_and_validate`
    instead; this function exists for introspection and is deliberately named
    to signal the output is not yet safe.
    """
    cfg = config or ModelConfig.from_env()
    client = client or _default_client(cfg)
    messages = prompt_module.build_messages(prompt)
    content = client(messages)
    return _parse_allocations(content)


def extract_and_validate(
    prompt: str,
    host_caps: Any,
    client: Optional[ModelClient] = None,
    config: Optional[ModelConfig] = None,
) -> ValidationResult:
    """Extract an intent and clamp it into an LXD-safe spec.

    This is the single public entry point an operator/LXD layer may call. The
    raw LLM output never leaves this function: only the validator's
    :class:`ValidationResult` is returned, and a model failure produces a
    rejected (empty, safe) result rather than an exception.
    """
    try:
        raw = extract(prompt, client=client, config=config)
    except (ExtractionError, ValueError, TypeError, OSError) as exc:
        result = ValidationResult(outcome="rejected")
        result.rejections.append(
            Rejection(index=None, reason=REASON_EXTRACTION_FAILED, detail=str(exc))
        )
        return result
    return clamp_validate(raw, host_caps)
