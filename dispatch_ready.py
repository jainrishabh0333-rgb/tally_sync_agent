#!/usr/bin/env python3
"""How much of a Sales Order can the warehouse dispatch today?

    python3 dispatch_ready.py --host <tally-host> --order WA-ADINATH \\
        [--order-date 2026-08-20] [--stock-days 14]

Ordered quantities come from the Sales Order's size batches. Available
stock per (item, size) is the freshest BlncQty figure harvested from the
last --stock-days of Sales + Sales Order vouchers — the only per-size
stock this build exports ([[distributor_fetch]]). Coverage is
min(stock, ordered) per size, summed; sizes with no BlncQty sighting in
the window are listed as UNKNOWN and excluded from the percentage.

The figure is stock-on-hand vs THIS order alone — it does not reserve
stock against other pending orders.
"""
import argparse, sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tally_client import TallyConfig
from distributor_fetch import fetch_sales_orders, fetch_invoices, harvest_size_balances


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--company", default="SN JAIN INDUSTRIES PVT LTD - (26-27)")
    ap.add_argument("--order", required=True)
    ap.add_argument("--order-date", default="")
    ap.add_argument("--stock-days", type=int, default=14)
    a = ap.parse_args()

    cfg = TallyConfig(host=a.host, port=a.port, timeout=280)
    cfg.company = a.company
    today = date.today()

    if a.order_date:
        d0 = d1 = date.fromisoformat(a.order_date)
    else:
        d0, d1 = today - timedelta(days=30), today
    sos = fetch_sales_orders(cfg, d0, d1)
    mine = [v for v in sos if v["voucher_number"] == a.order
            and not v.get("is_cancelled")]
    if not mine:
        sys.exit(f"Order {a.order!r} not found in {d0}..{d1}")
    order = mine[0]

    frm = today - timedelta(days=a.stock_days)
    payloads = sos if (d0 <= frm) else fetch_sales_orders(cfg, frm, today)
    payloads = payloads + fetch_invoices(cfg, frm, today)
    balances = {(b["item_name"], b["batch_name"]): b
                for b in harvest_size_balances(payloads)}

    print(f"\n{order['voucher_number']}  {order['party']}  dt {order['date']}")
    covered = short = unknown_qty = 0.0
    per_item = {}
    for l in order["lines"]:
        if not l["size_batch"]:
            continue
        per_item.setdefault(l["item_name"], []).append(l)
    for item, ls in per_item.items():
        parts, item_cov, item_ord = [], 0.0, 0.0
        for l in sorted(ls, key=lambda x: x["size_batch"]):
            need = l["qty"]
            b = balances.get((item, l["size_batch"]))
            if b is None:
                unknown_qty += need
                parts.append(f"{l['size_batch']}:{need:g}?")
                continue
            have = b["closing_qty"]
            ok = min(need, max(have, 0.0))
            covered += ok; short += need - ok
            item_cov += ok; item_ord += need
            mark = "" if ok >= need else f"(short {need-ok:g}, stock {have:g})"
            parts.append(f"{l['size_batch']}:{ok:g}/{need:g}{mark}")
        pct = f"{100*item_cov/item_ord:.0f}%" if item_ord else "?"
        print(f"  {pct:>4s}  {item}")
        print(f"        {' '.join(parts)}")
    known = covered + short
    print(f"\nREADY: {covered:g} of {known:g} known qty = "
          f"{100*covered/known:.1f}% dispatchable"
          + (f"  ({unknown_qty:g} qty has no size-stock sighting in "
             f"{a.stock_days}d — excluded)" if unknown_qty else ""))


if __name__ == "__main__":
    main()
