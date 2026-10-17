#!/usr/bin/env python3
"""live-window.py — the live-fleet window lease (one owner at a time, loudly).

WHY THIS EXISTS
    Several `infra` workers share ONE standing fleet (controller + node-a + node-c).
    On 2026-10-17 three of them ran live experiments at once: one worker's clear-all
    ended another's 60 s OOM injection 31 s in (`cleared_by=human` is the default
    attribution for ANY caller), and two different trees were deployed to
    `/opt/termini` within ~4 minutes, each restart flipping the controller and
    swinging `expire_stale_on_boot` at the other worker's open injection
    (kanban t_9c1362f6). Nothing in the system said "this fleet is busy".

WHAT IT IS
    A lease file on the controller VM:

        <state-dir>/live-window.json     the lease ({owner, purpose, taken_at, expires_at})
        <state-dir>/live-window.lock     flock target guarding read-modify-write

    Take it BEFORE an inject/deploy/restart and release it after. Ownership is
    exclusive: `flock(LOCK_EX)` serialises the read-check-write, so two concurrent
    `take` calls can never both win — the loser is REFUSED (exit 3) with the holder's
    task id, purpose and remaining time. It lives on the VM (not in this checkout and
    not in the controller DB) so it is shared by every worker on every workstation,
    survives controller restarts, and is readable during a migration pause.

USAGE
    live-window.py take --owner t_9c1362f6 --ttl 45m --purpose "live acceptance: t_c37aa0dc"
    live-window.py status
    live-window.py check  --owner t_9c1362f6     # exit 0 owned, 3 held by other, 4 none
    live-window.py release --owner t_9c1362f6

    Run it on the controller VM through `infra/gcp/tools/live-window.sh` (which
    installs it at /usr/local/bin/termini-live-window and ssh's in). `--state-dir`
    exists so the same logic is testable on any host (see tests/test_live_window.py).

EXIT CODES
    0  taken / released / free / owned by the caller
    2  usage error (bad owner, bad ttl, missing purpose)
    3  REFUSED — another owner holds a live lease
    4  check: no live lease at all
    5  release: the lease is held by a different owner (refused, untouched)
    6  the lease file exists but is unreadable (refused — inspect it by hand)

    There is deliberately NO --force. Only a human may delete the lease file, and
    doing so should be recorded on the board: stealing a window silently is exactly
    the failure this tool exists to prevent.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone

DEFAULT_STATE_DIR = "/var/lib/termini/live-window"
LEASE_NAME = "live-window.json"
LOCK_NAME = "live-window.lock"
DEFAULT_TTL_S = float(os.environ.get("TERMINI_LIVE_WINDOW_TTL_S", 45 * 60))
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{2,63}$")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_HELD = 3
EXIT_NO_LEASE = 4
EXIT_NOT_OWNER = 5
EXIT_UNREADABLE = 6


class Refused(Exception):
    """A refusal that must reach the caller as a loud, specific message."""

    def __init__(self, message: str, code: int = EXIT_HELD):
        super().__init__(message)
        self.code = code


def now_iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch,
                                  tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_ttl(text: str) -> float:
    """`90s` / `45m` / `2h` / `2700` (seconds). Bounded to [60 s, 6 h]."""
    raw = str(text).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|m|h)?", raw)
    if not m:
        raise Refused(f"--ttl must look like 90s, 45m, 2h or a plain number of seconds "
                      f"(got {text!r})", EXIT_USAGE)
    value = float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, None: 1}[m.group(2)]
    if not (60 <= value <= 6 * 3600):
        raise Refused(f"--ttl must be between 60s and 6h (got {text!r} = {value:.0f}s)",
                      EXIT_USAGE)
    return value


def validate_owner(owner: str) -> str:
    owner = (owner or "").strip()
    if not OWNER_RE.match(owner):
        raise Refused(
            "--owner must be the claimer's card id or role "
            "(e.g. t_9c1362f6, human:ops, deploy:release) — 3..64 chars of "
            f"[A-Za-z0-9_.:@-] (got {owner!r})", EXIT_USAGE)
    return owner


def _remaining(lease: dict, now_epoch: float) -> float:
    return float(lease["expires_at_epoch"]) - now_epoch


def _describe(lease: dict, now_epoch: float) -> str:
    rem = _remaining(lease, now_epoch)
    state = "EXPIRED" if rem <= 0 else f"expires in {rem:.0f}s"
    return (f"owner={lease.get('owner')} purpose={lease.get('purpose')!r} "
            f"taken={lease.get('taken_at')} expires={lease.get('expires_at')} ({state})")


class LiveWindow:
    """The lease, on a state directory. One instance per call site."""

    def __init__(self, state_dir: str = DEFAULT_STATE_DIR, *, clock=time.time,
                 host: str | None = None):
        self.state_dir = os.path.abspath(os.path.expanduser(state_dir))
        self.lease_path = os.path.join(self.state_dir, LEASE_NAME)
        self.lock_path = os.path.join(self.state_dir, LOCK_NAME)
        self._clock = clock
        self._host = host or socket.gethostname()

    # ------------------------------------------------------------- internals
    @contextlib.contextmanager
    def _locked(self):
        os.makedirs(self.state_dir, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o664)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read(self) -> dict | None:
        try:
            with open(self.lease_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise Refused(f"cannot read {self.lease_path}: {exc}", EXIT_UNREADABLE)
        if not raw.strip():
            return None
        try:
            lease = json.loads(raw)
            float(lease["expires_at_epoch"])
            lease["owner"]
            return lease
        except (ValueError, KeyError, TypeError) as exc:
            raise Refused(
                f"{self.lease_path} exists but is not a lease ({exc}). Refusing to "
                f"trample it — inspect or delete it by hand (a human decision).",
                EXIT_UNREADABLE)

    def _write(self, lease: dict) -> None:
        tmp = f"{self.lease_path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(lease, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.lease_path)

    def _unlink(self) -> None:
        try:
            os.unlink(self.lease_path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------ verbs
    def take(self, owner: str, purpose: str, ttl_s: float) -> dict:
        """Acquire the window. Raises Refused (exit 3) if another owner holds it."""
        owner = validate_owner(owner)
        purpose = (purpose or "").strip()
        if not purpose:
            raise Refused("--purpose is required: whoever waits on this window needs to "
                          "know what it is for and which card to ask", EXIT_USAGE)
        if len(purpose) > 200:
            raise Refused("--purpose must be at most 200 characters", EXIT_USAGE)
        with self._locked():
            # Test-only: widen the critical section so tests/test_live_window.py can
            # prove the lease is atomic (without the lock, two takers would both read
            # "free" and both win). Unset in every real invocation.
            _delay = float(os.environ.get("TERMINI_LIVE_WINDOW_TEST_DELAY_S", "0") or 0)
            if _delay > 0:
                time.sleep(_delay)
            now = self._clock()
            current = self._read()
            reclaimed = None
            if current is not None:
                if current["owner"] == owner:
                    renewed = dict(current)
                    renewed.update({
                        "purpose": purpose,
                        "expires_at_epoch": now + ttl_s,
                        "expires_at": now_iso(now + ttl_s),
                        "ttl_s": ttl_s,
                        "renewed_at": now_iso(now),
                    })
                    self._write(renewed)
                    return {"acquired": True, "renewed": True, "lease": renewed,
                            "reclaimed": None}
                if _remaining(current, now) > 0:
                    raise Refused(
                        "REFUSED: the live fleet is already claimed.\n  "
                        + _describe(current, now)
                        + "\n  You are " + owner + ". Do not inject, deploy or restart "
                        "until this window is released or expires; if it is abandoned, "
                        "the holder releases it (`live-window.sh release --owner "
                        + str(current['owner']) + "`).", EXIT_HELD)
                reclaimed = current
            lease = {
                "schema_version": "1.0",
                "owner": owner,
                "purpose": purpose,
                "taken_at": now_iso(now),
                "taken_at_epoch": now,
                "expires_at": now_iso(now + ttl_s),
                "expires_at_epoch": now + ttl_s,
                "ttl_s": ttl_s,
                "host": self._host,
                "pid": os.getpid(),
            }
            self._write(lease)
            return {"acquired": True, "renewed": False, "lease": lease,
                    "reclaimed": reclaimed}

    def status(self) -> dict:
        with self._locked():
            now = self._clock()
            lease = self._read()
            if lease is None:
                return {"held": False, "lease": None, "expired": False,
                        "remaining_s": None, "now": now_iso(now)}
            rem = _remaining(lease, now)
            return {"held": rem > 0, "expired": rem <= 0, "lease": lease,
                    "remaining_s": max(0.0, rem), "now": now_iso(now)}

    def release(self, owner: str) -> dict:
        owner = validate_owner(owner)
        with self._locked():
            now = self._clock()
            lease = self._read()
            if lease is None:
                return {"released": False, "reason": "no lease held", "lease": None}
            if lease["owner"] != owner:
                raise Refused(
                    "REFUSED: this window belongs to someone else — not releasing it.\n  "
                    + _describe(lease, now) + f"\n  You are {owner}.",
                    EXIT_NOT_OWNER)
            self._unlink()
            return {"released": True, "lease": lease}

    def check(self, owner: str) -> tuple[int, dict]:
        owner = validate_owner(owner)
        with self._locked():
            now = self._clock()
            lease = self._read()
            if lease is None:
                return EXIT_NO_LEASE, {"ok": False, "reason": "no live window is claimed",
                                       "lease": None, "now": now_iso(now)}
            if lease["owner"] == owner and _remaining(lease, now) > 0:
                return EXIT_OK, {"ok": True, "reason": "window owned by caller",
                                 "lease": lease, "remaining_s": _remaining(lease, now)}
            if lease["owner"] == owner:
                return EXIT_NO_LEASE, {"ok": False,
                                       "reason": "caller's window has expired",
                                       "lease": lease, "now": now_iso(now)}
            return EXIT_HELD, {"ok": False, "reason": "another owner holds the window",
                               "lease": lease, "remaining_s": _remaining(lease, now)}


# --------------------------------------------------------------------- CLI
def _print(obj: dict, as_json: bool, *, now: float | None = None) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
        return
    if "lease" in obj and obj.get("lease"):
        print(f"  {_describe(obj['lease'], time.time() if now is None else now)}"
              f" host={obj['lease'].get('host')}")
    for key in ("acquired", "renewed", "released", "held", "expired", "ok", "reason"):
        if key in obj and obj[key] is not None:
            print(f"  {key}: {obj[key]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Take/release the shared live-fleet window (one owner at a time).")
    ap.add_argument("--state-dir", default=os.environ.get("TERMINI_LIVE_WINDOW_DIR",
                                                          DEFAULT_STATE_DIR))
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="verb", required=True)

    p_take = sub.add_parser("take", help="claim the window (fails loudly if another owner holds it)")
    p_take.add_argument("--owner", required=True)
    p_take.add_argument("--ttl", default=f"{DEFAULT_TTL_S / 60:.0f}m")
    p_take.add_argument("--purpose", required=True)

    p_status = sub.add_parser("status", help="who holds the window, and until when")

    p_check = sub.add_parser("check", help="exit 0 only if --owner holds a live window")
    p_check.add_argument("--owner", required=True)

    p_release = sub.add_parser("release", help="release your window")
    p_release.add_argument("--owner", required=True)

    args = ap.parse_args(argv)
    win = LiveWindow(args.state_dir)
    try:
        if args.verb == "take":
            out = win.take(args.owner, args.purpose, parse_ttl(args.ttl))
            if out["renewed"]:
                print(f"renewed: the live window is yours again ({args.owner})")
            elif out["reclaimed"]:
                print(f"reclaimed an EXPIRED window from {out['reclaimed']['owner']} "
                      f"(expired {out['reclaimed']['expires_at']}, purpose "
                      f"{out['reclaimed'].get('purpose')!r})")
            else:
                print(f"taken: {args.owner} holds the live fleet")
            _print(out, args.json)
            return EXIT_OK
        if args.verb == "status":
            out = win.status()
            _print(out, args.json)
            if out["held"]:
                print(f"HELD by {out['lease']['owner']} — do not inject/deploy/restart")
            elif out["expired"]:
                print(f"EXPIRED (last held by {out['lease']['owner']}) — reclaimable")
            else:
                print("free — no live window is claimed")
            return EXIT_OK
        if args.verb == "check":
            code, out = win.check(args.owner)
            _print(out, args.json)
            if code == EXIT_HELD:
                print("REFUSED: another owner holds the live window — wait for its release")
            elif code == EXIT_NO_LEASE:
                print(f"REFUSED: {out['reason']} for {args.owner} — take one first")
            return code
        if args.verb == "release":
            out = win.release(args.owner)
            _print(out, args.json)
            print("released" if out["released"] else "nothing to release")
            return EXIT_OK
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
