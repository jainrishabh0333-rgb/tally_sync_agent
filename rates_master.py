#!/usr/bin/env python3
"""Build/refresh the persistent rate master from 26-27 voucher history.

The MD's standing rule (2026-08-20): price orders from THIS year's history
only — an item with no 26-27 sale has no current rate and gets no line.

  Build (full Apr->today):   python3 rates_master.py --host <tally-host> --build
  Refresh (last N days):     python3 rates_master.py --host <tally-host> --days 14

Output: rates_master.json next to this script — per item the most-supported
(rate, discount) from Sales Orders first, Sales invoices as fallback, with
support counts and last-seen date. Refresh merges: a newer window's quotes
ADD to the counters, so a price revision takes over once it has more support;
last_seen always advances.
"""
import argparse, json, sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tally_client import TallyConfig
from distributor_fetch import fetch_sales_orders, fetch_invoices

HERE = Path(__file__).resolve().parent
OUT = HERE / "rates_master.json"

def month_windows(frm: date, to: date):
    cur = frm
    while cur <= to:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(to, nxt - timedelta(days=1))
        cur = nxt

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--company", default="SN JAIN INDUSTRIES PVT LTD - (26-27)")
    ap.add_argument("--build", action="store_true", help="full FY rebuild")
    ap.add_argument("--days", type=int, default=14, help="refresh window")
    a = ap.parse_args()

    cfg = TallyConfig(host=a.host, port=a.port, timeout=300)
    cfg.company = a.company
    today = date.today()
    frm = date(today.year if today.month >= 4 else today.year - 1, 4, 1) \
        if a.build else today - timedelta(days=a.days)

    master = {} if a.build or not OUT.exists() else json.loads(OUT.read_text())
    so = defaultdict(Counter); sale = defaultdict(Counter); last = {}
    for w0, w1 in month_windows(frm, today):
        for tag, fn, bag in (("SO", fetch_sales_orders, so),
                             ("Sales", lambda c, f, t: fetch_invoices(c, f, t), sale)):
            try:
                vs = fn(cfg, w0, w1)
            except Exception as e:
                print(f"{tag} {w0}: FAILED {e}", file=sys.stderr)
                continue
            for v in vs:
                seen = set()
                for l in v["lines"]:
                    it = l["item_name"]
                    k = (it, l["rate"], l["rate_unit"], l["discount"])
                    if l["rate"] and k not in seen:
                        seen.add(k)
                        bag[it][(l["rate"], l["rate_unit"], l["discount"])] += 1
                        if v["date"] > last.get(it, ""):
                            last[it] = v["date"]
            print(f"{tag} {w0}..{w1}: {len(vs)} vouchers", file=sys.stderr)

    for it in set(so) | set(sale):
        prev = master.get(it, {})
        merged_so = Counter({tuple(json.loads(k)): v for k, v in
                             prev.get("so_counts", {}).items()}) + so[it]
        merged_sa = Counter({tuple(json.loads(k)): v for k, v in
                             prev.get("sale_counts", {}).items()}) + sale[it]
        src = merged_so or merged_sa
        (rate, unit, disc), n = src.most_common(1)[0]
        master[it] = {
            "rate": f"{rate:.2f}/{unit}", "discount": str(int(disc or 0)),
            "seen": n, "variants": len(src),
            "source": "SO" if merged_so else "Sales",
            "last_seen": max(last.get(it, ""), prev.get("last_seen", "")),
            "so_counts": {json.dumps(list(k)): v for k, v in merged_so.items()},
            "sale_counts": {json.dumps(list(k)): v for k, v in merged_sa.items()},
        }
    OUT.write_text(json.dumps(master, indent=1, sort_keys=True))
    print(f"{len(master)} items in {OUT}")

if __name__ == "__main__":
    main()
