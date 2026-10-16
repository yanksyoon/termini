"""Recompute the prompt-token delta v1->v2 independently from git history.

Each rev is loaded in its OWN subprocess (clean import) to avoid module-cache
aliasing, then the static (system + few-shot) lengths are diffed. The dynamic
delta is re-measured from MY captured request.
"""
import json, subprocess, sys, tempfile, os, shutil

WT = "/home/ubuntu/.hermes/profiles/verifier/cache/scratch/metrics-verify-1792189188"
LOADER = ('import sys\n'
          'sys.path.insert(0, sys.argv[1])\n'
          'import termini.decision.prompt as p\n'
          'print(len(p.SYSTEM_PROMPT), sum(len(v) for m in p.FEW_SHOT for v in m.values() if isinstance(v, str)))\n')

tmp = tempfile.mkdtemp(prefix="tokendelta-")

def static(rev):
    pkg = os.path.join(tmp, rev)
    os.makedirs(pkg, exist_ok=True)
    tar_path = os.path.join(tmp, rev + ".tar")
    with open(tar_path, "wb") as fh:
        subprocess.run(["git", "archive", rev, "termini"], cwd=WT, check=True, stdout=fh)
    subprocess.run(["tar", "-xf", tar_path, "-C", pkg], check=True)
    out = subprocess.run([sys.executable, "-c", LOADER, pkg],
                         capture_output=True, text=True, check=True)
    sp, fs = out.stdout.strip().split()
    return int(sp), int(fs)

v2_sp, v2_fs = static("e706b08")
v1_sp, v1_fs = static("028e756")
static_delta = (v2_sp - v1_sp) + (v2_fs - v1_fs)
print(f"system prompt: v1={v1_sp} v2={v2_sp} (delta {v2_sp - v1_sp})")
print(f"few-shot:      v1={v1_fs} v2={v2_fs} (delta {v2_fs - v1_fs})")
print(f"STATIC delta (system + few-shot): {static_delta} chars = {static_delta/4:.0f} tokens @4c/tok")

# Dynamic delta from MY captured request (3-sample window).
LIVE = "/home/ubuntu/.hermes/profiles/verifier/cache/scratch/verify-diagnosis-1792189616/live"
reqs = [json.loads(l) for l in open(LIVE + "/stub-model-requests.jsonl")]
user_content = reqs[-1]["body"]["messages"][-1]["content"]
decision_json = user_content.split("input:\n\n", 1)[1].split("\n\nReturn", 1)[0]
payload = json.loads(decision_json)
din_len = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))

NEW_SCALARS = {"oom_kill_count", "pgmajfault_count", "mem_available_bytes",
               "tcp_retransmit_segs", "conntrack_count", "conntrack_max"}
NEW_PRESSURE = {"cpu_some_avg10", "io_some_avg10", "memory_some_avg10", "memory_full_avg10"}
NEW_DISK = {"io_ticks_ms", "io_in_flight", "inode_used", "inode_total"}
NEW_NET = {"rx_errors", "tx_errors", "rx_dropped", "tx_dropped"}
NEW_LXD = {"oom_kill_count", "pgmajfault_count"}

def strip(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in NEW_SCALARS or k in NEW_PRESSURE:
                continue
            if k == "disk" and isinstance(v, list):
                out[k] = [{kk: vv for kk, vv in e.items() if kk not in NEW_DISK} for e in v]
            elif k == "net" and isinstance(v, list):
                out[k] = [{kk: vv for kk, vv in e.items() if kk not in NEW_NET} for e in v]
            elif k == "lxd_instances" and isinstance(v, list):
                out[k] = [{kk: vv for kk, vv in e.items() if kk not in NEW_LXD} for e in v]
            else:
                out[k] = strip(v)
        return out
    if isinstance(obj, list):
        return [strip(e) for e in obj]
    return obj

stripped = strip(payload)
stripped.pop("derived", None)
din_stripped = len(json.dumps(stripped, separators=(",", ":"), ensure_ascii=False))
dynamic_delta = din_len - din_stripped
print(f"\ndynamic DecisionInput: with-new={din_len} without-new={din_stripped}")
print(f"DYNAMIC delta ({payload['derived']['window_samples']}-sample window): "
      f"{dynamic_delta} chars = {dynamic_delta/4:.0f} tokens @4c/tok")

total = static_delta + dynamic_delta
print(f"\nTOTAL delta per diagnosis (my measurement, 3-sample window): "
      f"{total} chars = {total/4:.0f} tokens @4c/tok")
print(f"daily @100 diagnoses/day = {total/4*100:,.0f} tokens/day")
shutil.rmtree(tmp, ignore_errors=True)
