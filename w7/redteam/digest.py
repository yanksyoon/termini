#!/usr/bin/env python3
"""digest.py — print the headline evidence numbers for the report."""
import json
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
B = ROOT / "boot"
N = ROOT / "net"


def j(p):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


print("========== BOOT ==========")
for s in ["baseline", "delay", "refuse", "dnsfail", "badtoken"]:
    d = j(B / s / "summary.json")
    if not d:
        print(f"{s}: (no summary)")
        continue
    row = d.get("by_scenario", {}).get(s, {})
    print(f"{s}: attempts={row.get('attempts')} online={row.get('online')} "
          f"start_to_online={row.get('start_to_online_s')} "
          f"svc_to_register={row.get('service_start_to_register_s')}")

print("\n--- legacy-apt ---")
p = B / "legacy-apt" / "legacy-apt-result.txt"
print(p.read_text()[:1500] if p.exists() else "(missing)")

print("\n========== NET ==========")
for s in ["baseline", "latency", "loss", "kill"]:
    d = j(N / s / "summary.json") or {}
    sc = d.get(s, {})
    print(f"\n--- {s} (elapsed {sc.get('elapsed_s')}) ---")
    if s == "baseline":
        print("ping a->b:", sc.get("ping_a_to_b", {}).get("loss_pct"), "% loss,", sc.get("ping_a_to_b", {}).get("rtt_avg_ms"), "ms")
        print("ping a->c:", sc.get("ping_a_to_c", {}).get("loss_pct"), "% loss,", sc.get("ping_a_to_c", {}).get("rtt_avg_ms"), "ms")
        print("tcp a->c:", sc.get("tcp_a_to_c", {}))
        ch = sc.get("chat_baseline", {})
        print("chat:", {k: ch.get(k) for k in ("n", "ok", "failed", "retried_calls", "latency_s")})
    if s == "latency":
        print("netem:", sc.get("netem"))
        print("ping a->c:", sc.get("ping_a_to_c", {}).get("loss_pct"), "% loss,", sc.get("ping_a_to_c", {}).get("rtt_avg_ms"), "ms")
        print("telemetry net latency:", sc.get("telemetry_net_latency_ms"))
        ev = sc.get("operator_events_a", {})
        print("operator events(a):", json.dumps(ev)[:400])
        ch = sc.get("chat_under_latency", {})
        print("chat:", {k: ch.get(k) for k in ("n", "ok", "failed", "retried_calls", "latency_s")})
    if s == "loss":
        print("netem:", sc.get("netem"))
        print("ping a->c:", sc.get("ping_a_to_c", {}).get("loss_pct"), "% loss,", sc.get("ping_a_to_c", {}).get("rtt_avg_ms"), "ms")
        print("tcp a->c:", sc.get("tcp_a_to_c", {}))
        ch = sc.get("chat_under_loss", {})
        print("chat:", {k: ch.get(k) for k in ("n", "ok", "failed", "retried_calls", "latency_s")})
        print("telemetry net lat a:", sc.get("telemetry_net_latency_ms_a"))
    if s == "kill":
        print("stream_result:", json.dumps(sc.get("stream_result"))[:400])
        print("exit_after_kill_s:", sc.get("stream_client_exit_s_after_kill"))
        print("time_to_degraded_s:", sc.get("time_to_degraded_s"), "time_to_offline_s:", sc.get("time_to_offline_s"))
        print("recover_online_s:", sc.get("time_to_recover_online_s"), "recover_inference_s:", sc.get("time_to_recover_inference_s"))
        print("transitions:", json.dumps(sc.get("status_transitions"))[:500])
