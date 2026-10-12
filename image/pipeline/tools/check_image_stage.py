#!/usr/bin/env python3
"""check_image_stage.py — validate a node-image workload stage directory.

The node image bake imports the workload image (the guest `agent-vm/<version>`
the controller launches) from a staging directory. Historically the import step
was a `if [ -d "$STAGE" ]` guard keyed off the tarball's file name, so an
unpopulated or mis-named stage made the bake import ZERO images and still report
success. This checker makes that gap visible *before* a multi-GB bake and in the
pipeline's own `--check` gate.

It validates the stage against the same manifest contract the import step uses
(see image/pipeline/provision/import-staged-images.sh):

    $STAGE/images.json
      {"kind": "termini.image_stage",
       "images": [{"file": "<tarball>", "aliases": ["agent-vm/2026.10.0", ...],
                   "version": "2026.10.0", "sha256": "<hex>"}]}

Checks (each a hard failure):
  * the stage directory exists;
  * images.json exists, parses, and has kind=termini.image_stage;
  * images is a non-empty list with a valid file / non-empty aliases per entry;
  * every referenced tarball exists on disk;
  * every tarball's sha256 matches its manifest digest (when one is given);
  * every required alias (--require-alias) is present across the entry aliases.

Usage:
    check_image_stage.py --stage DIR [--require-alias A ...]
    check_image_stage.py --selftest          # prove the checks above are real

--selftest builds a throwaway stage in a temp dir and asserts the validator
PASSES a good stage and FAILS an empty stage, a digest mismatch, an empty image
list, and a missing required alias. Exit 0 only if all assertions hold.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sys
import tempfile

MANIFEST_NAME = "images.json"
KIND = "termini.image_stage"
DEFAULT_REQUIRED_ALIAS = "agent-vm/2026.10.0"


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate(stage: pathlib.Path, required_aliases: list[str]) -> tuple[list[str], dict]:
    """Return (problems, summary). problems is empty iff the stage is valid."""
    problems: list[str] = []
    stage = pathlib.Path(stage)
    manifest = stage / MANIFEST_NAME

    if not stage.is_dir():
        return ([f"stage directory does not exist: {stage}"], {})
    if not manifest.is_file():
        return ([f"stage manifest missing: {manifest}"], {})

    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return ([f"{manifest}: not valid JSON: {exc}"], {})

    if not isinstance(doc, dict) or doc.get("kind") != KIND:
        return ([f"{manifest}: 'kind' must be {KIND!r}"], {})

    images = doc.get("images")
    if not isinstance(images, list) or not images:
        return ([f"{manifest}: 'images' must be a non-empty list"], {})

    seen_aliases: set[str] = set()
    summary: dict = {"images": [], "aliases": set()}
    for idx, item in enumerate(images):
        if not isinstance(item, dict):
            problems.append(f"{manifest}: images[{idx}] must be an object")
            continue
        name = item.get("file")
        aliases = item.get("aliases")
        digest = item.get("sha256", "")
        if not isinstance(name, str) or not name:
            problems.append(f"{manifest}: images[{idx}].file is required")
            continue
        if not isinstance(aliases, list) or not aliases or not all(
            isinstance(a, str) and a for a in aliases
        ):
            problems.append(f"{manifest}: images[{idx}].aliases must be a non-empty list of strings")
            continue
        seen_aliases.update(aliases)

        tarball = stage / name
        if not tarball.is_file():
            problems.append(f"staged tarball missing: {tarball}")
        elif digest:
            got = sha256_file(tarball)
            if got != digest:
                problems.append(
                    f"digest mismatch for {name}: on-disk {got[:12]} != manifest {str(digest)[:12]}"
                )
            else:
                summary["images"].append({"file": name, "sha256": got, "aliases": aliases})
        else:
            summary["images"].append({"file": name, "sha256": "", "aliases": aliases})

    for want in required_aliases:
        if want not in seen_aliases:
            problems.append(f"required alias {want!r} is not registered by any staged image")

    summary["aliases"] = sorted(seen_aliases)
    return problems, summary


def _write_stage(stage: pathlib.Path, *, aliases: list[str], digest: str | None,
                 tarball: bytes = b"fake-lxd-image") -> pathlib.Path:
    stage.mkdir(parents=True, exist_ok=True)
    blob = stage / "agent-vm-2026.10.0-deadbeefcafe.tar.gz"
    blob.write_bytes(tarball)
    real = sha256_file(blob)
    img = {"file": blob.name, "aliases": aliases, "version": "2026.10.0",
           "sha256": real if digest is None else digest}
    (stage / MANIFEST_NAME).write_text(
        json.dumps({"kind": KIND, "images": [img]}, indent=2) + "\n", encoding="utf-8")
    return stage


def selftest() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} selftest: {name}{(' — ' + detail) if detail and not ok else ''}")
        if not ok:
            failures.append(name)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="termini-stage-selftest-"))
    try:
        # 1. a good stage with the contract alias PASSES.
        good = _write_stage(tmp / "good", aliases=["agent-vm/2026.10.0", "agent-os/w4"],
                            digest=None)
        probs, summ = validate(good, [DEFAULT_REQUIRED_ALIAS])
        check("good stage passes", not probs, "; ".join(probs))
        check("good stage reports both aliases",
              summ.get("aliases") == ["agent-os/w4", "agent-vm/2026.10.0"],
              str(summ.get("aliases")))

        # 2. an EMPTY stage dir FAILS (the exact bug class: zero images, exit 0).
        empty = tmp / "empty"
        empty.mkdir()
        probs, _ = validate(empty, [DEFAULT_REQUIRED_ALIAS])
        check("empty stage fails", bool(probs), "validator accepted an empty stage")

        # 3. a missing manifest FAILS.
        noman = tmp / "noman"
        noman.mkdir()
        (noman / "stray.tar.gz").write_bytes(b"x")
        probs, _ = validate(noman, [DEFAULT_REQUIRED_ALIAS])
        check("missing manifest fails", bool(probs), "validator accepted a stage with no manifest")

        # 4. a digest mismatch FAILS.
        bad = _write_stage(tmp / "bad", aliases=["agent-vm/2026.10.0"],
                           digest="0" * 64)
        probs, _ = validate(bad, [DEFAULT_REQUIRED_ALIAS])
        check("digest mismatch fails", any("digest mismatch" in p for p in probs),
              "; ".join(probs))

        # 5. an empty image list FAILS.
        emptylist = tmp / "emptylist"
        emptylist.mkdir()
        (emptylist / MANIFEST_NAME).write_text(
            json.dumps({"kind": KIND, "images": []}), encoding="utf-8")
        probs, _ = validate(emptylist, [DEFAULT_REQUIRED_ALIAS])
        check("empty image list fails", bool(probs), "validator accepted images=[]")

        # 6. a missing required alias FAILS.
        wrong = _write_stage(tmp / "wrong", aliases=["agent-os/w4"], digest=None)
        probs, _ = validate(wrong, [DEFAULT_REQUIRED_ALIAS])
        check("missing required alias fails",
              any("required alias" in p for p in probs), "; ".join(probs))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"SELFTEST FAILED: {len(failures)} assertion(s) failed", file=sys.stderr)
        return 1
    print("SELFTEST OK: stage validator passes a good stage and fails empty/corrupt stages")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate a node-image workload stage directory")
    ap.add_argument("--stage", help="stage directory to validate")
    ap.add_argument("--require-alias", action="append", default=[],
                    help=f"alias that must be registered (repeatable; default {DEFAULT_REQUIRED_ALIAS})")
    ap.add_argument("--selftest", action="store_true",
                    help="prove the validator is real (fixtures pass/fail)")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.stage:
        ap.error("--stage is required (or use --selftest)")

    required = args.require_alias or [DEFAULT_REQUIRED_ALIAS]
    problems, summary = validate(pathlib.Path(args.stage), required)
    if problems:
        print(f"image stage INVALID: {args.stage}", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"image stage OK: {args.stage}")
    for img in summary["images"]:
        print(f"  {img['file']} sha256={img['sha256'][:12]} aliases={img['aliases']}")
    print(f"  registered aliases: {summary['aliases']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
