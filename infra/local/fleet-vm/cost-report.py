#!/usr/bin/env python3
"""cost-report.py — what the Termini footprint costs in GCP, from live inventory.

Two questions, one report:

  1. what the GCE fleet costs while it is RUNNING (the thing the LXD-VM fleet
     replaces), and
  2. what still incurs cost after those VMs are stopped or deleted — the disks,
     addresses, images, buckets and managed services that do not go away with
     `gcloud compute instances stop`.

Prices are NOT hardcoded: they are read from the public Cloud Billing Catalog API
(`cloudbilling.googleapis.com/v1/services/<id>/skus`), which needs no billing-account
role, and matched by SKU description + region. The project's own billing account is
NOT readable by the deployment service account (`billing.accounts.get` is denied),
so this is a list-price estimate of provable inventory, not an invoice.

Usage:
    python3 cost-report.py [--project termini-510609] [--json]
    python3 cost-report.py --hours 730            # monthly run rate (default)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

GCLOUD = "gcloud"
ENV = {
    "PATH": "/home/ubuntu/google-cloud-sdk/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": os.path.expanduser("~"),
}

COMPUTE_SERVICE = "6F81-5844-456A"
STORAGE_SERVICE = "95FF-2EF5-5EA1"


def gcloud(*args: str) -> str:
    return subprocess.check_output([GCLOUD, *args], text=True, env=ENV)


def gjson(*args: str):
    return json.loads(gcloud(*args, "--format=json"))


def access_token() -> str:
    return gcloud("auth", "print-access-token").strip()


def fetch_skus(service: str, token: str) -> list[dict]:
    out: list[dict] = []
    page = None
    while True:
        url = (f"https://cloudbilling.googleapis.com/v1/services/{service}/skus"
               f"?pageSize=5000&currencyCode=USD")
        if page:
            url += "&pageToken=" + page
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
        out += data.get("skus", [])
        page = data.get("nextPageToken")
        if not page:
            return out


def price(sku: dict, tier: int = 0) -> float:
    pe = sku["pricingInfo"][0]["pricingExpression"]
    rates = pe.get("tieredRates") or []
    if not rates:
        return 0.0
    r = rates[min(tier, len(rates) - 1)]["unitPrice"]
    return int(r.get("units", 0)) + r.get("nanos", 0) / 1e9


def unit(sku: dict) -> str:
    return sku["pricingInfo"][0]["pricingExpression"].get("usageUnit", "")


def find_sku(skus: list[dict], region: str, desc_exact: str) -> dict | None:
    """Exact description match.

    Substring matching is a trap here: "E2 Instance Ram running in Mumbai" is a
    substring of "Spot Preemptible E2 Instance Ram running in Mumbai", so a
    `in` test silently prices the fleet at spot rates.
    """
    for s in skus:
        if s["description"] == desc_exact and region in (s.get("serviceRegions") or []):
            return s
    return None


def parse_machine(inst: dict) -> tuple[int, float]:
    """(vCPU, GiB) for an instance, custom shapes included."""
    mt = inst["machineType"].rsplit("/", 1)[-1]
    m = re.match(r"custom-(\d+)-(\d+)", mt)
    if m:
        return int(m.group(1)), int(m.group(2)) / 1024
    zone = inst["zone"].rsplit("/", 1)[-1]
    info = gjson("compute", "machine-types", "describe", mt, "--zone", zone)
    return int(info["guestCpus"]), int(info["memoryMb"]) / 1024


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="termini-510609")
    ap.add_argument("--hours", type=float, default=730.0, help="hours per month")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    token = access_token()
    compute_skus = fetch_skus(COMPUTE_SERVICE, token)
    storage_skus = fetch_skus(STORAGE_SERVICE, token)

    rows: list[dict] = []

    # ---- instances ------------------------------------------------------- #
    instances = gjson("compute", "instances", "list", "--project", args.project)
    for inst in instances:
        zone = inst["zone"].rsplit("/", 1)[-1]
        region = zone.rsplit("-", 1)[0]
        cpu, gib = parse_machine(inst)
        core = find_sku(compute_skus, region, f"E2 Instance Core running in "
                        + _city(region))
        ram = find_sku(compute_skus, region, f"E2 Instance Ram running in "
                       + _city(region))
        running = inst["status"] == "RUNNING"
        usd = (cpu * price(core) + gib * price(ram)) * args.hours if (running and core and ram) else 0.0
        rows.append({
            "kind": "instance", "name": inst["name"], "state": inst["status"],
            "detail": f"{cpu:g} vCPU / {gib:g} GiB ({inst['machineType'].rsplit('/',1)[-1]})",
            "usd_month": round(usd, 2),
            "note": "compute stops when STOPPED" if running else "no compute charge",
        })

    # ---- disks ------------------------------------------------------------ #
    for d in gjson("compute", "disks", "list", "--project", args.project):
        region = d["zone"].rsplit("/", 1)[-1].rsplit("-", 1)[0]
        dtype = d["type"].rsplit("/", 1)[-1]
        desc = "Balanced PD Capacity" if "balanced" in dtype else "Storage PD Capacity"
        sku = find_sku(compute_skus, region, desc + " in " + _city(region))
        gb = int(d["sizeGb"])
        usd = gb * price(sku) if sku else 0.0
        rows.append({
            "kind": "disk", "name": d["name"], "state": d["status"],
            "detail": f"{gb} GiB {dtype}", "usd_month": round(usd, 2),
            "note": "billed while it exists, running or stopped",
        })

    # ---- addresses -------------------------------------------------------- #
    # A reserved address only escapes the charge while it is attached to a
    # RUNNING resource. GCE still reports `status: IN_USE` for an address bound to
    # a TERMINATED instance, so billing is decided from the instance map, not from
    # the address status.
    running_links = {i["selfLink"] for i in instances if i["status"] == "RUNNING"}
    for a in gjson("compute", "addresses", "list", "--project", args.project):
        if a.get("addressType") != "EXTERNAL":
            continue
        region = a["region"].rsplit("/", 1)[-1]
        sku = find_sku(compute_skus, region, "Static Ip Charge in " + _city(region))
        in_use = any(u in running_links for u in (a.get("users") or []))
        # Tier 0 is the first hour free; an unattached reservation bills from
        # hour 1 at tier 1.
        usd = 0.0
        if not in_use and sku:
            usd = price(sku, tier=1) * max(args.hours - 1, 0)
        rows.append({
            "kind": "external-ip", "name": a["name"], "state": a.get("status", ""),
            "detail": a["address"], "usd_month": round(usd, 2),
            "note": "free while attached; billed when reserved and unused",
        })

    # ---- custom images ---------------------------------------------------- #
    img_sku = None
    for s in compute_skus:
        if "Image" in s["description"] and "Mumbai" in s["description"] \
                and unit(s) == "GiBy.mo":
            img_sku = s
            break
    for im in gjson("compute", "images", "list", "--project", args.project,
                    "--no-standard-images"):
        gb = int(im.get("diskSizeGb") or 0)
        usd = gb * price(img_sku) if img_sku else 0.0
        rows.append({
            "kind": "image", "name": im["name"], "state": im["status"],
            "detail": f"{gb} GiB", "usd_month": round(usd, 2),
            "note": "image storage is billed whether or not a VM uses it",
        })

    # ---- buckets ---------------------------------------------------------- #
    for b in gjson("storage", "buckets", "list", "--project", args.project):
        loc = (b.get("location") or "").lower()
        city = _city(loc)
        sku = None
        for s in storage_skus:
            if (s["description"] == f"Standard Storage {city}"
                    and loc in [r.lower() for r in (s.get("serviceRegions") or [])]):
                sku = s
                break
        size = 0.0
        try:
            listing = gcloud("storage", "ls", "-l", "--recursive", f"gs://{b['name']}")
            for line in listing.splitlines():
                parts = line.split()
                if parts and parts[0].isdigit():
                    size += int(parts[0])
            size /= 1024 ** 3
        except Exception:
            size = 0.0
        usd = size * price(sku) if sku else 0.0
        rows.append({
            "kind": "bucket", "name": b["name"], "state": loc,
            "detail": f"{size:.2f} GiB", "usd_month": round(usd, 2),
            "note": "model weights; storage keeps billing",
        })

    # ---- Cloud Run -------------------------------------------------------- #
    try:
        runs = gjson("run", "services", "list", "--region", "asia-southeast1",
                     "--project", args.project)
    except subprocess.CalledProcessError:
        runs = []
    for svc in runs:
        rows.append({
            "kind": "cloud-run", "name": svc["metadata"]["name"], "state": "configured",
            "detail": "no instances by default (min-instances unset)",
            "usd_month": 0.0,
            "note": "request-billed only; the GPU config cannot start (GPU quota 0)",
        })

    # ---- report ----------------------------------------------------------- #
    total_up = round(sum(r["usd_month"] for r in rows), 2)
    instance_up = round(sum(r["usd_month"] for r in rows if r["kind"] == "instance"), 2)
    after_stop = round(total_up - instance_up, 2)

    if args.json:
        print(json.dumps({
            "project": args.project, "hours": args.hours,
            "rows": rows, "total_running_usd_month": total_up,
            "instances_usd_month": instance_up,
            "residual_after_stop_usd_month": after_stop,
        }, indent=2))
        return 0

    print(f"Termini GCP footprint — project {args.project}, "
          f"{args.hours:.0f} h/month, USD list prices\n")
    print(f"{'RESOURCE':<20} {'KIND':<12} {'STATE':<12} {'DETAIL':<34} {'$/mo':>8}  NOTE")
    print("-" * 132)
    for r in rows:
        print(f"{r['name'][:20]:<20} {r['kind']:<12} {r['state'][:12]:<12} "
              f"{r['detail'][:34]:<34} {r['usd_month']:>8.2f}  {r['note']}")
    print("-" * 132)
    print(f"{'TOTAL NOW':<20} {'':<12} {'':<12} {'':<34} {total_up:>8.2f}")
    print(f"{'  of which compute':<20} {'':<12} {'':<12} {'':<34} {instance_up:>8.2f}")
    print(f"{'RESIDUAL ATTACHED':<20} {'':<12} {'':<12} {'':<34} {after_stop:>8.2f}")
    print("\nNot priced here (usage-based, and the billing account is not readable by "
          "the deployment SA):\n  * Vertex AI Gemini tokens served through the "
          "inference front door (measured per call, not a fixed monthly line)\n"
          "  * Cloud Logging ingestion from the GCE instances (agent-dependent)\n"
          "  * Container image storage in Artifact Registry / Container Registry")
    return 0


def _city(region: str) -> str:
    return {
        "asia-south1": "Mumbai",
        "asia-southeast1": "Singapore",
        "asia-east1": "Taiwan",
        "us-central1": "Iowa",
        "us-east1": "South Carolina",
        "europe-west1": "Belgium",
    }.get(region, "Mumbai")


if __name__ == "__main__":
    raise SystemExit(main())
