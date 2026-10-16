"""Controller-issued ``vm_id`` for an LXD instance — the missing link.

`lxd_instances[].vm_id` is a C0 field and the ingest validator requires it to be
a 26-character Crockford ULID (``app/fields.py``: ``"ulid": True``). An LXD
instance name can never be that: names are lowercase/DNS-ish (``demo-workload``,
``termini-01m4...-0-0``) while a ULID is uppercase Crockford. So the node and the
controller need a *shared* rule that turns an instance name into a C0 id, and
back again.

That rule is the whole reason the demo's remediation used to 404. The seeder
fabricated its own ULIDs (``01M4N827979BD9D2762EFE24A0``), the guardrail resolved
the model's target to that fabricated string, and the executor PUT it at a real
node's LXD, where no instance of that name exists. Nothing in the loop could tell
a controller-issued id from an invented one.

`vm_id_for_instance` is that rule:

    vm_id = 26 Crockford base32 characters of SHA-256("termini-vm-id:" + <name>)

Properties that matter:

* **C0-preserving.** The id is a one-way hash: the instance name (which may carry
  customer data) never appears in telemetry, in the model prompt, or in an audit
  row. Governance.md §4.1 is unchanged.
* **Deterministic and shared.** The node derives it from its own LXD inventory;
  the controller derives the same id from the inventory it reads back over the
  LXD REST API (`GET /1.0/instances?recursion=1`), so no per-instance config
  table has to be kept in sync. `infra/bootstrap/node/termini-bootstrap.py`
  carries the byte-identical implementation; ``tests/test_vm_id_derivation.py``
  fails the build if the two ever drift.
* **Invertible-by-lookup only.** The controller resolves a vm_id back to a real
  instance name by hashing the names that actually exist on the node it is about
  to dial. A vm_id that hashes from *nothing* on that node is, by construction,
  not a target — which is exactly the state the fail-closed guard reacts to
  (`executor.TARGET_MISSING`).

ULID-*shaped* is deliberate: the wire contract (node-telemetry.md §4.6) and the
ingest validator keep their C0 id format, and neither has to change.
"""
from __future__ import annotations

import hashlib

# Crockford base32 — the alphabet the controller's ULID validator accepts
# (`app/fields.py: ULID_PATTERN = ^[0-9A-HJKMNP-TV-Z]{26}$`). I, L, O and U are
# excluded so a printed id cannot be mistyped into another valid one.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

VM_ID_LEN = 26
VM_ID_PREFIX = "termini-vm-id:"


def vm_id_for_instance(name: str) -> str:
    """The controller-issued `vm_id` for LXD instance `name` (see module docstring).

    Byte-for-byte compatible with the node agent's copy: the digest is taken over
    UTF-8 bytes, the top 130 bits of a 17-byte prefix are used (17*8 = 136, so 6
    bits are shifted off), and they are emitted MSB-first.
    """
    digest = hashlib.sha256((VM_ID_PREFIX + str(name)).encode("utf-8")).digest()
    value = int.from_bytes(digest[:17], "big") >> 6
    out = []
    for _ in range(VM_ID_LEN):
        out.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def instance_names_for_vm_ids(names: list[str]) -> dict[str, str]:
    """``{vm_id -> instance name}`` for the instance names present on a node.

    This is the mapping the guardrail resolves a model-chosen target through
    (`DecisionContext.instance_names`), built from the *live* inventory rather
    than from a config table: a name that exists on the node is resolvable, and
    nothing else is.
    """
    return {vm_id_for_instance(n): n for n in names if isinstance(n, str) and n}
