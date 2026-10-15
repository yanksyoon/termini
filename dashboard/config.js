/*
 * Termini dashboard runtime configuration.
 *
 * Precedence (highest first):
 *   1. URL query params      ?apiBase=&projectId=&token=&window=&pollMs=&mock=
 *   2. localStorage          (set once via the Settings dialog / ?token=)
 *   3. this file             (window.TERMINI_CONFIG)
 *   4. built-in defaults
 *
 * NOTE: the query-param names are the CONFIG KEYS (`apiBase`, `projectId`,
 * `token`, ...) — there are no short aliases. `?api=` and `?project=` are
 * silently ignored and the dashboard reports "No project configured", which is
 * an easy hour to lose. Authoritative list: `js/core/config.js` resolveConfig().
 *
 * `apiBase` is the controller REST root INCLUDING the version segment, e.g.
 *   same origin          -> ""            (dashboard served by the controller)
 *   local mock           -> "/v1"         (mock server serves both)
 *   remote controller    -> "https://10.10.0.10/v1"
 *
 * `token` is the project_token (controller-api.md §2). When the dashboard is
 * served by the controller itself this may be blank and injected server-side;
 * otherwise set it here, pass ?token=, or save it through the Settings dialog.
 */
window.TERMINI_CONFIG = {
  // REST root including "/v1". "" means "same origin".
  apiBase: "",

  // Project ULID to scope every request. Blank -> discovered from /v1/projects.
  projectId: "",

  // project_token (opaque bearer). Prefer ?token= or the Settings dialog.
  token: "",

  // Overview grid + metrics poll cadence (ms). Contract §1.5: 5 s.
  pollMs: 5000,

  // Operator-decisions poll cadence (ms). The decision surface has no SSE.
  decisionMs: 3000,

  // How many decisions/events/audits to fetch per poll (newest first).
  decisionLimit: 50,

  // Chart window buttons. Keys are labels, values are seconds.
  windows: { "5m": 300, "15m": 900, "1h": 3600, "6h": 21600 },
  defaultWindow: "15m",

  // Max log rows kept in the DOM (older rows are pruned client-side).
  logBufferMax: 2000,

  // When true the app never talks to the network and uses bundled fixtures.
  // Useful for a projector demo with no backend at all.
  offline: false,

  // Start with `?include=synthetic` (label demo/seeder nodes). Off by default:
  // the standing fleet a viewer sees is the real hardware.
  includeSynthetic: false,
};
