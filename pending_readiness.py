#!/usr/bin/env python3
"""Morning dispatch report: every pending Sales Order, ranked by readiness.

    python3 pending_readiness.py --host <tally-host> [--days 45] [--out FILE]

Pending per (order, item, size) = ordered qty minus qty already invoiced
against that order number (Sales invoice batch lines carry ORDERNO).
Stock per (item, size) = freshest BlncQty from the same voucher pulls.

Readiness is ALLOCATION-AWARE: orders are walked oldest first and each
takes stock before later orders see it — so two orders never count the
same dozen. INTERNAL ONLY: this report never goes to the portal
(party-facing rule).
"""
import argparse, json, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tally_client import TallyConfig
from distributor_fetch import fetch_sales_orders, fetch_invoices, harvest_size_balances
from readiness_html import render as render_html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--company", default="SN JAIN INDUSTRIES PVT LTD - (26-27)")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--out", type=Path, default=None,
                    help="write the text report here")
    ap.add_argument("--html", type=Path, default=None,
                    help="also write the browsable dashboard here")
    ap.add_argument("--json", type=Path, default=None,
                    help="also write the structured report here")
    a = ap.parse_args()

    cfg = TallyConfig(host=a.host, port=a.port, timeout=280)
    cfg.company = a.company
    today = date.today()
    frm = today - timedelta(days=a.days)

    sos = fetch_sales_orders(cfg, frm, today)
    invs = fetch_invoices(cfg, frm, today)

    delivered = defaultdict(float)          # (order_no, item, size) -> qty
    for v in invs:
        if v.get("is_cancelled"):
            continue
        for l in v["lines"]:
            if l["order_no"]:
                delivered[(l["order_no"], l["item_name"], l["size_batch"])] += l["qty"]

    pool = {(b["item_name"], b["batch_name"]): max(b["closing_qty"], 0.0)
            for b in harvest_size_balances(sos + invs)}

    orders = []
    for v in sorted(sos, key=lambda x: x["date"]):
        # operators retire orders by renaming them "...(CANCEL)" rather than
        # cancelling the voucher — both spellings mean not pending
        vn = v["voucher_number"].upper()
        if v.get("is_cancelled") or "CANCEL" in vn or "CLOSE" in vn:
            continue
        pend = []
        for l in v["lines"]:
            if not l["size_batch"]:
                continue
            left = l["qty"] - delivered.get(
                (v["voucher_number"], l["item_name"], l["size_batch"]), 0.0)
            if left > 0.004:
                pend.append((l["item_name"], l["size_batch"], left))
        if pend:
            orders.append((v, pend))

    lines_out, rank = [], []
    blocked_short = defaultdict(lambda: {"qty": 0.0, "orders": set()})
    blocked_unknown = defaultdict(lambda: {"qty": 0.0, "orders": set()})
    for v, pend in orders:                       # oldest first: takes stock first
        got = need = unknown = 0.0
        shorts = defaultdict(float)
        for item, size, left in pend:
            key = (item, size)
            if key not in pool:
                unknown += left
                continue
            take = min(left, pool[key])
            pool[key] -= take
            got += take; need += left
            if take < left:
                shorts[item] += left - take
                blocked_short[key]["qty"] += left - take
                blocked_short[key]["orders"].add(v["voucher_number"])
        pct = 100.0 * got / need if need else 0.0
        rank.append((pct, v, got, need, unknown, dict(shorts)))
        for item, size, left in pend:
            key = (item, size)
            if key not in pool:
                blocked_unknown[key]["qty"] += left
                blocked_unknown[key]["orders"].add(v["voucher_number"])
    # blocked_short is filled inside the allocation walk below

    rank.sort(key=lambda r: -r[0])
    W = lines_out.append
    W(f"DISPATCH READINESS — {today:%d-%b-%Y} (orders {frm:%d-%b} onward, "
      f"oldest order takes stock first)")
    W(f"{len(rank)} pending orders\n")
    for pct, v, got, need, unknown, shorts in rank:
        flag = "READY" if pct >= 99.95 else (f"{pct:.0f}%")
        W(f"{flag:>6}  {v['voucher_number']:<16} {v['date']}  "
          f"{v['party'][:38]:<38} {got:g}/{need:g}"
          + (f" (+{unknown:g} unknown)" if unknown else ""))
        for item, s in sorted(shorts.items(), key=lambda x: -x[1])[:4]:
            W(f"        short {s:g}: {item}")
    W("\n" + "=" * 72)
    W("ITEMS BLOCKING DISPATCH — pending demand the stock cannot cover")
    W("(short qty across all pending orders after oldest-first allocation)\n")
    by_item = defaultdict(lambda: {"qty": 0.0, "orders": set(), "sizes": {}})
    for (item, size), rec in blocked_short.items():
        d = by_item[item]
        d["qty"] += rec["qty"]; d["orders"] |= rec["orders"]
        d["sizes"][size] = rec["qty"]
    for item, d in sorted(by_item.items(),
                          key=lambda x: -len(x[1]["orders"]))[:60]:
        sizes = " ".join(f"{sz}:{q:g}" for sz, q in
                         sorted(d["sizes"].items(), key=lambda x: -x[1]))
        W(f"  blocks {len(d['orders']):3d} orders  short {d['qty']:8g}   {item}")
        W(f"        {sizes}")
    by_u = defaultdict(lambda: {"qty": 0.0, "orders": set()})
    for (item, size), rec in blocked_unknown.items():
        by_u[item]["qty"] += rec["qty"]; by_u[item]["orders"] |= rec["orders"]
    if blocked_unknown:
        W("\nNO RECENT STOCK SIGHTING (excluded from percentages — count "
          "them by hand or move the item once, and BlncQty will report it):")
        for item, d in sorted(by_u.items(), key=lambda x: -len(x[1]["orders"]))[:40]:
            W(f"  {len(d['orders']):3d} orders  qty {d['qty']:8g}   {item}")
    text = "\n".join(lines_out) + "\n"
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text)

    if a.html or a.json:
        # The text report truncates its two tail sections for readability; the
        # structured report does not, because the page has a search box and
        # nothing there has to be cut to fit.
        report = {
            "company": a.company,
            "as_of": f"{today:%d-%b-%Y}",
            "window_from": f"{frm:%d-%b}",
            "order_count": len(rank),
            "orders": [{
                "voucher": v["voucher_number"], "date": str(v["date"]),
                "party": v["party"], "got": got, "need": need,
                "unknown": unknown, "pct": pct, "ready": pct >= 99.95,
                "shorts": [{"item": i, "qty": q} for i, q in
                           sorted(shorts.items(), key=lambda x: -x[1])],
            } for pct, v, got, need, unknown, shorts in rank],
            "blocking": [{
                "item": item, "orders": len(d["orders"]), "qty": d["qty"],
                "sizes": d["sizes"],
            } for item, d in sorted(by_item.items(),
                                    key=lambda x: -len(x[1]["orders"]))],
            "unknown": [{
                "item": item, "orders": len(d["orders"]), "qty": d["qty"],
            } for item, d in sorted(by_u.items(),
                                    key=lambda x: -len(x[1]["orders"]))],
        }
        for path, body in ((a.json, lambda: json.dumps(report, indent=1)),
                           (a.html, lambda: render_html(report))):
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body())
                print(f"wrote {path}")
    print(text)


if __name__ == "__main__":
    main()
