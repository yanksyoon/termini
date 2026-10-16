# Verification tools — how to re-run

These are the verifier's own adversarial harnesses (not the implementers' tests).
Each was run from a fresh detached worktree of `origin/main` @ `e706b08`; the
scripts hardcode that worktree's path as `WT` and the run's capture dir as
`LIVE`. To reproduce on a fresh checkout, edit the two path constants at the top
of each script to point at your worktree and re-run:

```bash
git worktree add --detach /tmp/mv origin/main
# edit WT= in each script to /tmp/mv, and LIVE= to a fresh run_metric_diagnosis.py out dir
python3 verify_c2.py            # C2 fail-closed plants (80/80)
python3 verify_real_metrics.py  # /proc,/sys,cgroup vs collector (all MATCH)
python3 verify_bounds_cost.py   # over-long arrays, worst-case bytes
python3 measure_bytes.py        # 744-byte worst-case confirmation
python3 measure_token_delta2.py # token delta v1->v2 from git history
# diagnosis falsification + assembled-request C0 audit:
python3 docs/evidence/2026-10-16-metric-expansion/tools/run_metric_diagnosis.py /tmp/mv-live
python3 verify_diagnosis.py     # point LIVE= at /tmp/mv-live/live
```

`verify_diagnosis.py` and `measure_token_delta2.py` read the assembled request
from the `live/stub-model-requests.jsonl` that `run_metric_diagnosis.py` writes;
run the latter first and set `LIVE=` accordingly.
