# Direct-route lab evidence

Captured output of `infra/net/lab/overlay-vs-direct-lab.sh` on a Linux host with
root (network namespaces, veth, VXLAN, iptables). Regenerate with:

```bash
sudo infra/net/lab/overlay-vs-direct-lab.sh --out infra/net/evidence
```

One timestamped set is kept per run:

| File | What it shows |
|------|---------------|
| `lab-run-<ts>.txt` | Full transcript: interface MTUs (direct 1500 / overlay 1450, 50-byte encap), the DF-bit probes, the PMTU sweeps, the load tests, and the final `LAB RESULT`. |
| `sweep-<ts>.txt` | Binary-search path-MTU sweep per path: `1500` on the direct path, `1450` on the overlay. |
| `loadtest-direct-<ts>.json` | Sustained streaming load over the direct path — acceptance requires `dropped = 0`. |
| `loadtest-overlay-<ts>.json` | The same load over the (correctly-sized) overlay path, for comparison. |
| `loadtest-direct-control-<ts>.json` | Direct-path control run alongside the black-hole scenario. |
| `loadtest-blackhole-<ts>.json` | The failure mode: an overlay path-MTU mismatch with ICMP filtered — `dropped > 0`, which is why the direct route exists. |
| `mock-server-<ts>.txt` | Mock `llama-server` log for the run. |

The load test targets `lab/mock_llama_server.py` (OpenAI SSE) because no
`llama-server`/GGUF is present on this host; pointing `inference-loadtest.sh` at the
real Node C endpoint needs no code change.
