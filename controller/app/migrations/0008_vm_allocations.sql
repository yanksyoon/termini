-- 0008_vm_allocations — committed agent_vm capacity per node (controller-api.md §5.2 step 7).
CREATE TABLE IF NOT EXISTS vm_allocations (
    id                TEXT PRIMARY KEY,             -- ULID
    project_id        TEXT NOT NULL REFERENCES projects(id),
    plan_id           TEXT NOT NULL,
    node_id           TEXT NOT NULL REFERENCES nodes(id),
    cpu_allocated     INTEGER NOT NULL DEFAULT 0,
    ram_allocated_gb  INTEGER NOT NULL DEFAULT 0,
    disk_allocated_gb INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vm_allocations_node ON vm_allocations (node_id);
