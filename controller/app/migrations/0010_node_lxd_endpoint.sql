-- 0010_node_lxd_endpoint — per-node LXD REST endpoint (apply-plan materialisation).
--
-- Until now the controller addressed LXD through a single global
-- TERMINI_LXD_BASE_URL, which cannot name one node among a fleet: each node runs
-- its own LXD on its own underlay address (10.10.0.11-13:8443, infra-decisions.md
-- D2). A node now reports its own LXD API address at registration; the column is
-- NULL for an older node that reports none, and the executor then falls back to
-- the global base URL (the stub-demo path).
ALTER TABLE nodes ADD COLUMN lxd_endpoint TEXT;

-- Optional controller-side mTLS client credentials for talking to THIS node's LXD.
-- Populated out-of-band at provisioning, never from the registration wire: the
-- contract is explicit that no private key transits the controller
-- (node-bootstrap.md §11). NULL means "use the global TERMINI_LXD_CLIENT_CERT/KEY".
ALTER TABLE nodes ADD COLUMN lxd_client_cert TEXT;
ALTER TABLE nodes ADD COLUMN lxd_client_key TEXT;
