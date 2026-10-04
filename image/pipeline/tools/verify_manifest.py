#!/usr/bin/env python3
"""verify_manifest.py — validate the Termini image pins + build manifest.

Runs with only the Python standard library (no jsonschema dependency), so it
works on any clean checkout and inside CI.

What it checks:
  1. models.lock.json: aliases are exactly {deepseek-flash, deepseek-v4-pro},
     every sha256 is 64 hex chars, sizes are positive ints, urls are on
     huggingface.co, the llama.cpp commit is a full 40-hex sha.
  2. image-manifest.schema.json is well formed and declares the same top-level
     required keys the builder emits.
  3. (optional) a produced image-manifest.json validates against that schema and
     its pins agree with models.lock.json.

Exit code 0 = all good, 1 = a real validation failure.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_ALIASES = {"deepseek-flash", "deepseek-v4-pro"}
EXPECTED_MANIFEST_REQUIRED = {
    "schema_version", "alias", "version", "immutable_tag", "variant", "gitsha",
    "built_at", "base_image", "artifacts", "pins", "contents", "first_boot",
}


def load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --- minimal JSON-Schema subset validator ------------------------------------
def _type_ok(value, tname: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(tname, True)


def validate(instance, schema, path="") -> list[str]:
    errs: list[str] = []
    if not isinstance(schema, dict):
        return errs

    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(instance, x) for x in types):
            errs.append(f"{path or '$'}: expected type {t}, got {type(instance).__name__}")
            return errs

    if "const" in schema and instance != schema["const"]:
        errs.append(f"{path or '$'}: expected const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errs.append(f"{path or '$'}: {instance!r} not in enum {schema['enum']}")

    if isinstance(instance, str) and "pattern" in schema:
        if not re.search(schema["pattern"], instance):
            errs.append(f"{path or '$'}: {instance!r} does not match {schema['pattern']}")

    if isinstance(instance, int) and not isinstance(instance, bool) and "minimum" in schema:
        if instance < schema["minimum"]:
            errs.append(f"{path or '$'}: {instance} < minimum {schema['minimum']}")

    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errs.append(f"{path or '$'}: missing required key {req!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(props)
            for key in sorted(extra):
                errs.append(f"{path or '$'}: unexpected key {key!r}")
        for key, subschema in props.items():
            if key in instance:
                errs += validate(instance[key], subschema, f"{path}.{key}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errs.append(f"{path or '$'}: needs >= {schema['minItems']} items")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(instance):
                errs += validate(item, items, f"{path}[{i}]")
    return errs


# --- checks -------------------------------------------------------------------
def check_lock(lock: dict) -> list[str]:
    errs: list[str] = []
    aliases = lock.get("aliases")
    if not isinstance(aliases, dict):
        return ["models.lock.json: 'aliases' must be an object"]
    if set(aliases) != REQUIRED_ALIASES:
        errs.append(f"models.lock.json: aliases must be exactly {sorted(REQUIRED_ALIASES)}, got {sorted(aliases)}")
    for name, spec in aliases.items():
        for field in ("repo", "file", "url", "sha256", "size_bytes", "residency"):
            if field not in spec:
                errs.append(f"models.lock.json: {name} missing {field}")
        if not HEX64.match(str(spec.get("sha256", ""))):
            errs.append(f"models.lock.json: {name}.sha256 is not 64-hex")
        if not isinstance(spec.get("size_bytes"), int) or spec.get("size_bytes", 0) <= 0:
            errs.append(f"models.lock.json: {name}.size_bytes must be a positive int")
        if not str(spec.get("url", "")).startswith("https://huggingface.co/"):
            errs.append(f"models.lock.json: {name}.url must be on huggingface.co")
        if not str(spec.get("file", "")).endswith(".gguf"):
            errs.append(f"models.lock.json: {name}.file must be a .gguf")
    cpp = lock.get("llamacpp", {}).get("commit", "")
    if not HEX40.match(str(cpp)):
        errs.append("models.lock.json: llamacpp.commit must be a full 40-hex sha")
    smoke = lock.get("harness_smoke_model", {})
    if smoke and not HEX64.match(str(smoke.get("sha256", ""))):
        errs.append("models.lock.json: harness_smoke_model.sha256 is not 64-hex")
    return errs


def check_schema(schema: dict) -> list[str]:
    errs: list[str] = []
    required = set(schema.get("required", []))
    if required != EXPECTED_MANIFEST_REQUIRED:
        errs.append(
            "image-manifest.schema.json: required keys drifted from the builder "
            f"contract; missing={sorted(EXPECTED_MANIFEST_REQUIRED - required)} "
            f"extra={sorted(required - EXPECTED_MANIFEST_REQUIRED)}"
        )
    if schema.get("properties", {}).get("alias", {}).get("enum") != ["agentic-os/w3"]:
        errs.append("image-manifest.schema.json: alias enum must be ['agentic-os/w3']")
    fb = schema.get("properties", {}).get("first_boot", {}).get("properties", {})
    if fb.get("network_required", {}).get("const") is not False:
        errs.append("image-manifest.schema.json: first_boot.network_required must be const false")
    return errs


def check_manifest(manifest: dict, schema: dict, lock: dict) -> list[str]:
    errs = validate(manifest, schema)
    pins = manifest.get("pins", {})
    if pins.get("llamacpp_commit") != lock.get("llamacpp", {}).get("commit"):
        errs.append("manifest.pins.llamacpp_commit != models.lock.json llamacpp.commit")
    for alias in REQUIRED_ALIASES:
        want = lock["aliases"][alias]["sha256"]
        got = pins.get("models", {}).get(alias)
        if got != want:
            errs.append(f"manifest.pins.models.{alias}={got!r} != lock {want!r}")
    if manifest.get("first_boot", {}).get("network_required") is not False:
        errs.append("manifest.first_boot.network_required must be false")
    return errs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate Termini image pins + manifest")
    ap.add_argument("--lock", required=True)
    ap.add_argument("--schema", required=True)
    ap.add_argument("--manifest", default=None)
    args = ap.parse_args(argv)

    errors: list[str] = []
    lock = load_json(args.lock)
    schema = load_json(args.schema)
    errors += check_lock(lock)
    errors += check_schema(schema)

    if args.manifest:
        manifest = load_json(args.manifest)
        errors += check_manifest(manifest, schema, lock)
        print(f"[verify] manifest validated: {args.manifest}")

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"[verify] OK: {args.lock} + {args.schema}"
          + (f" + {args.manifest}" if args.manifest else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
