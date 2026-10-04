-- 0006_ssh_ca — SSH certificate-authority issuance + revocation ledger (D4).

-- One row per issued user certificate. This is the audit ledger for
-- POST /v1/projects/{pid}/ssh-key (controller-api.md §4.3, governance.md §8):
-- every issued certificate is recorded; revocation sets `revoked_at` without
-- deleting the row, so the full issuance history survives for reconciliation.
CREATE TABLE IF NOT EXISTS ssh_certificates (
    cert_id          TEXT PRIMARY KEY,        -- ULID
    project_id       TEXT NOT NULL REFERENCES projects(id),
    principal        TEXT NOT NULL,           -- root (demo)
    key_id           TEXT NOT NULL,           -- operator identity (ssh-keygen -I)
    key_algorithm    TEXT NOT NULL,           -- ed25519 | rsa | ecdsa
    key_fingerprint  TEXT NOT NULL,           -- SHA256:... of the uploaded public key
    valid_from       TEXT NOT NULL,           -- RFC3339 UTC
    valid_until      TEXT NOT NULL,           -- RFC3339 UTC
    issued_at        TEXT NOT NULL,           -- RFC3339 UTC
    revoked_at       TEXT                     -- RFC3339 UTC, set on revoke
);

CREATE INDEX IF NOT EXISTS idx_ssh_certs_project
    ON ssh_certificates (project_id, issued_at);
CREATE INDEX IF NOT EXISTS idx_ssh_certs_fingerprint
    ON ssh_certificates (key_fingerprint);
