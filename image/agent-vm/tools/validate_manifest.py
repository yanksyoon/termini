#!/usr/bin/env python3
"""Validate an agent image manifest against manifests/agent-image-manifest.schema.json.

Uses jsonschema when available, otherwise a structural fallback that enforces the
fields the pipeline actually depends on (alias/version/digest/artifact/pins).
"""

from __future__ import annotations

import json
import sys

REQUIRED_TOP = ["schema_version", "alias", "version", "immutable_tag", "gitsha",
                "image_digest", "artifacts", "pins", "contents", "first_boot"]


def fallback_validate(doc: dict) -> list[str]:
    errors = [f"missing top-level key: {k}" for k in REQUIRED_TOP if k not in doc]
    if not doc.get("artifacts"):
        errors.append("artifacts[] is empty")
    else:
        art = doc["artifacts"][0]
        for k in ("name", "format", "size_bytes", "sha256"):
            if k not in art:
                errors.append(f"artifacts[0] missing {k}")
        if not str(art.get("sha256", "")).startswith(("",)) or len(str(art.get("sha256", ""))) != 64:
            errors.append("artifacts[0].sha256 must be a 64-hex digest")
    pins = doc.get("pins", {})
    for k in ("python", "node_version", "runtime", "frameworks"):
        if k not in pins:
            errors.append(f"pins missing {k}")
    if doc.get("first_boot", {}).get("network_required") is not False:
        errors.append("first_boot.network_required must be false")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: validate_manifest.py <manifest.json> <schema.json>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        doc = json.load(fh)
    with open(argv[2], encoding="utf-8") as fh:
        schema = json.load(fh)
    try:
        import jsonschema  # type: ignore import-not-found

        jsonschema.validate(doc, schema)
        print(f"[manifest] valid against {argv[2]} (jsonschema)")
        return 0
    except ImportError:
        errors = fallback_validate(doc)
        if errors:
            print("[manifest] INVALID:\n  " + "\n  ".join(errors), file=sys.stderr)
            return 1
        print(f"[manifest] valid against {argv[2]} (structural fallback)")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[manifest] INVALID: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
