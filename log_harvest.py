#!/usr/bin/env python3
"""
Harvest + Time Logger — Maynooth Homestead Digital Twin
Usage:
  python log_harvest.py harvest "San Marzano" --kg 0.45 --zone GH2N --quality 5
  python log_harvest.py harvest "Jalapeño Ruben" --count 12 --zone GH6N
  python log_harvest.py time --category claude_session --minutes 90 --activity "VPD dashboard"
  python log_harvest.py summary
  python log_harvest.py import-to-influx  (requires INFLUX_* env vars)

Phase 1: writes to CSV files in Season/
Phase 2: writes directly to InfluxDB (when Docker stack is running)
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent  # -> Gardening/
HARVEST_CSV  = PROJECT_ROOT / "Season" / "HARVEST_LOG_2026.csv"
TIME_CSV     = PROJECT_ROOT / "Season" / "TIME_LOG_2026.csv"

# Tesco-equivalent prices for ROI calculation (€/kg unless noted)
PRODUCE_VALUE = {
    "San Marzano":        3.00,
    "Black Krim":         4.50,
    "Marmande":           3.50,
    "Sungold F1":         8.00,   # per kg cherry
    "Tigerella":          3.50,
    "Smarald":            3.50,
    "Prima Bella":        3.00,
    "Passandra F1":       4.00,   # per kg cucumber (~3 per kg)
    "Jalapeño Ruben":     12.00,  # per kg fresh chilli
    "Yolo Wonder":        5.00,
    "Pantos":             5.00,
    "Tsaksoniki Aubergine": 4.00,
    "Kelvedon Wonder":    6.00,   # per kg peas
    "Aquadulce Claudia":  5.00,   # per kg broad beans
    "Defender F1":        3.00,   # courgette
    "Uchiki Kuri":        2.50,   # squash per kg
    "Spaghetti Heirloom": 2.00,
    "Kale":               3.00,
    "Spinach Matador":    4.00,
    "Lettuce":            2.00,
    "Dalmaziano":         8.00,   # per kg dry beans
    "Cobra":              6.00,   # per kg climbing beans
    "Default":            3.00,
}


def append_csv(filepath: Path, row: dict, fieldnames: list):
    """Append a row to CSV, creating header if file is new/empty."""
    write_header = not filepath.exists() or filepath.stat().st_size < 50
    with open(filepath, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def cmd_harvest(args):
    today = datetime.now().strftime("%Y-%m-%d")
    row = {
        "date":      today,
        "variety":   args.variety,
        "zone":      args.zone or "",
        "weight_kg": args.kg or "",
        "count":     args.count or "",
        "quality":   args.quality or "",
        "notes":     args.notes or "",
    }
    fields = ["date", "variety", "zone", "weight_kg", "count", "quality", "notes"]
    append_csv(HARVEST_CSV, row, fields)

    # Quick value estimate
    price = PRODUCE_VALUE.get(args.variety, PRODUCE_VALUE["Default"])
    value = (float(args.kg) * price) if args.kg else 0
    print(f"✅ Logged: {args.variety} | {args.zone} | {args.kg or args.count} | est. €{value:.2f}")
    if args.influx:
        write_harvest_to_influx(row)


def cmd_time(args):
    today = datetime.now().strftime("%Y-%m-%d")
    row = {
        "date":         today,
        "category":     args.category,
        "duration_min": args.minutes,
        "activity":     args.activity or "",
        "notes":        args.notes or "",
    }
    fields = ["date", "category", "duration_min", "activity", "notes"]
    append_csv(TIME_CSV, row, fields)
    print(f"✅ Time logged: {args.category} | {args.minutes} min | {args.activity or ''}")


def cmd_summary():
    print("\n── HARVEST SUMMARY 2026 ──────────────────────────────")
    totals = {}
    total_value = 0.0
    try:
        with open(HARVEST_CSV) as f:
            for row in csv.DictReader(f):
                v = row["variety"]
                if not v or v.startswith("#"):
                    continue
                kg = float(row["weight_kg"]) if row["weight_kg"] else 0
                price = PRODUCE_VALUE.get(v, PRODUCE_VALUE["Default"])
                value = kg * price
                totals[v] = totals.get(v, {"kg": 0, "value": 0})
                totals[v]["kg"] += kg
                totals[v]["value"] += value
                total_value += value
    except FileNotFoundError:
        print("  No harvest data yet.")
        return

    for variety, data in sorted(totals.items(), key=lambda x: -x[1]["kg"]):
        print(f"  {variety:<25} {data['kg']:.2f} kg   est. €{data['value']:.2f}")
    print(f"\n  TOTAL estimated value: €{total_value:.2f}")

    print("\n── TIME SUMMARY 2026 ──────────────────────────────────")
    time_by_cat = {}
    try:
        with open(TIME_CSV) as f:
            for row in csv.DictReader(f):
                cat = row.get("category", "")
                if not cat or cat.startswith("#"):
                    continue
                mins = int(row["duration_min"]) if row["duration_min"] else 0
                time_by_cat[cat] = time_by_cat.get(cat, 0) + mins
    except FileNotFoundError:
        print("  No time data yet.")
        return

    total_mins = sum(time_by_cat.values())
    for cat, mins in sorted(time_by_cat.items(), key=lambda x: -x[1]):
        print(f"  {cat:<25} {mins//60}h {mins%60:02d}m")
    print(f"\n  TOTAL: {total_mins//60}h {total_mins%60:02d}m")

    if totals and total_mins > 0:
        hourly = total_value / (total_mins / 60)
        print(f"\n  Effective hourly return: €{hourly:.2f}/hr  (target: >€15/hr)")


def write_harvest_to_influx(row):
    """Write harvest entry to InfluxDB (requires influxdb-client + env vars)."""
    try:
        from influxdb_client import InfluxDBClient, Point, WritePrecision
        from influxdb_client.client.write_api import SYNCHRONOUS
        client = InfluxDBClient(
            url=os.environ["INFLUX_URL"],
            token=os.environ["INFLUX_TOKEN"],
            org=os.environ["INFLUX_ORG"],
        )
        write_api = client.write_api(write_options=SYNCHRONOUS)
        p = (
            Point("harvest")
            .tag("variety", row["variety"])
            .tag("zone", row.get("zone", "unknown"))
        )
        if row.get("weight_kg"):
            p = p.field("weight_kg", float(row["weight_kg"]))
        if row.get("count"):
            p = p.field("count", int(row["count"]))
        if row.get("quality"):
            p = p.field("quality", int(row["quality"]))
        write_api.write(
            bucket=os.environ["INFLUX_BUCKET"],
            org=os.environ["INFLUX_ORG"],
            record=p
        )
        print("  → Written to InfluxDB ✅")
    except Exception as e:
        print(f"  ⚠️  InfluxDB write failed (stack not running?): {e}")


def main():
    parser = argparse.ArgumentParser(description="Maynooth Homestead — Harvest + Time Logger")
    sub = parser.add_subparsers(dest="cmd")

    # harvest command
    ph = sub.add_parser("harvest", help="Log a harvest event")
    ph.add_argument("variety", help='Crop variety e.g. "San Marzano"')
    ph.add_argument("--kg", type=float, help="Weight in kg")
    ph.add_argument("--count", type=int, help="Item count (if not weighed)")
    ph.add_argument("--zone", help="GH zone or outdoor bay e.g. GH2N, Bay3")
    ph.add_argument("--quality", type=int, choices=range(1, 6), help="Quality 1-5")
    ph.add_argument("--notes", help="Optional notes")
    ph.add_argument("--influx", action="store_true", help="Also write to InfluxDB")

    # time command
    pt = sub.add_parser("time", help="Log time spent")
    pt.add_argument("--category", required=True,
        choices=["claude_session","physical_garden","planning","infrastructure","research"],
        help="Activity category")
    pt.add_argument("--minutes", required=True, type=int, help="Duration in minutes")
    pt.add_argument("--activity", help="Short description")
    pt.add_argument("--notes", help="Optional notes")

    # summary command
    sub.add_parser("summary", help="Show season summary (harvest value + time + ROI)")

    # import to influx
    sub.add_parser("import-to-influx", help="Import CSV files to InfluxDB (requires running stack)")

    args = parser.parse_args()

    if args.cmd == "harvest":
        cmd_harvest(args)
    elif args.cmd == "time":
        cmd_time(args)
    elif args.cmd == "summary":
        cmd_summary()
    elif args.cmd == "import-to-influx":
        print("Import feature: read HARVEST_LOG_2026.csv and write all rows to InfluxDB")
        print("Implement when Docker stack is running (May/Jun).")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
