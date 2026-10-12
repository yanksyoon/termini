-- 0013_allocation_release_node_decommission — release committed capacity, retire nodes.
--
-- Defect (t_33c7e847): `vm_allocations` was the only record of committed agent_vm
-- capacity and NOTHING ever decremented or deleted it once the instances it
-- claimed were gone. Committed capacity therefore only ever grew: a demo that
-- scaled a few times saturated a node's allocatable RAM and every later plan was
-- rejected 422 `exceeds_node_ram_ceiling`, unrecoverable without wiping the DB —
-- which destroys the audit trail. A live link that saturates mid-evaluation is
-- permanently dead.
--
-- Two release paths now exist (controller-api.md §4.2), both SOFT: the original
-- commit row is kept and marked, never deleted, so the audit trail survives.
--
-- `released_at` marks a row released (its capacity is back in the pool). A row is
-- LIVE while `released_at IS NULL`; `store.allocated_capacity` sums LIVE rows
-- only. Idempotent: a released row is still found (so a repeat release is a clean
-- `already_released`, not a double decrement or an unknown), but is no longer
-- counted as capacity.
ALTER TABLE vm_allocations ADD COLUMN released_at TEXT;
ALTER TABLE vm_allocations ADD COLUMN release_reason TEXT;

-- `vm_ids_json` is the JSON array of LXD instance names this commit actually
-- materialised on the node. It is NULL until the plan's apply succeeds. The
-- reconciler may only release a row whose instances were materialised AND are now
-- absent: a plan committed but never applied has materialised nothing, so its
-- reservation must stand until it is applied or explicitly released.
ALTER TABLE vm_allocations ADD COLUMN vm_ids_json TEXT;

CREATE INDEX IF NOT EXISTS idx_vm_allocations_live ON vm_allocations (node_id, released_at);

-- Node retirement (decommission). A retired node is dropped from the fleet
-- listing (`store.list_nodes`) but its row, telemetry and audit survive. Phantom
-- OFFLINE nodes (a red-team node, throwaway registration-test nodes) previously
-- accumulated forever, so the dashboard's fleet contradicted the deck's "2 nodes".
ALTER TABLE nodes ADD COLUMN decommissioned_at TEXT;
ALTER TABLE nodes ADD COLUMN decommission_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_nodes_live ON nodes (project_id, decommissioned_at);
