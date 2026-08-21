#!/usr/bin/env python3
"""Send ONE priced Sales Order to Tally, built from a hold file + a rate file.

The importer is the standing path for queued orders; this is the same
envelope builder driven by hand, for the case where an order has been
settled in chat and needs to go in now. It talks to Tally only — it does not
touch the Frappe queue, so a row queued for the same order must be cleared
by hand or it would import a second time.

    python send_priced_order.py --hold ../order_console/hold_1205.json \\
        --rates /path/rates_1205.json --config /path/cfg.toml --dry-run

Refuses to send unless EVERY line has a rate: a partial order in the books is
worse than no order.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import sync
from order_importer import build_envelope, load_order_settings, normalise_order
from tally_client import TallyError, post_write


def build_order(hold: dict, rates: dict) -> tuple[dict, list[str]]:
    """Hold lines + rates -> the shape normalise_order expects."""
    lines, missing = [], []
    for l in hold["lines"]:
        rate = (rates.get(l["item"]) or {}).get("rate")
        if not rate:
            missing.append(l["item"])
            continue
        # Rates read back from Tally look like "1740.00/Box".
        rate_num = float(str(rate).split("/")[0])
        sizes = ([{"size": s, "qty": q} for s, q in l["batches"].items()]
                 if l["batches"] else
                 [{"size": l["item"].rsplit("-(", 1)[0].rsplit(" ", 1)[-1],
                   "qty": l["qty"]}])
        lines.append({"item": l["item"], "unit": l["unit"],
                      "rate": rate_num, "dealer": l.get("dealer", ""),
                      "sizes": sizes})
    return {
        "order_key": hold.get("order_key") or f"PAD-{hold['order_no']}",
        "order_no": hold["order_no"],
        "company": hold["company"],
        "party": hold["party"],
        "order_date": hold["order_date"],
        "narration": hold.get("narration", ""),
        "items": lines,
    }, missing


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hold", required=True, type=Path)
    p.add_argument("--rates", required=True, type=Path)
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    hold = json.loads(a.hold.read_text())
    rates = json.loads(a.rates.read_text())
    raw, missing = build_order(hold, rates)
    if missing:
        print("No rate for these items — nothing sent:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        return 2

    order = normalise_order(raw)
    st = sync.load_settings(a.config)
    ocfg = load_order_settings(a.config)
    cfg = st.tally
    cfg.company = order["company"]

    gstin = ""
    from order_importer import _masters
    parties, items, gstins, details = _masters(cfg, order["company"], {})
    if order["party"] not in parties:
        print(f"Party {order['party']!r} is not a ledger in "
              f"{order['company']!r}", file=sys.stderr)
        return 2
    unknown = [l["item"] for l in order["lines"] if l["item"] not in items]
    if unknown:
        print("Unknown stock items — nothing sent:", *unknown, sep="\n  ",
              file=sys.stderr)
        return 2
    gstin = gstins.get(order["party"], "")

    xml = build_envelope(order, ocfg, gstin, party=details.get(order["party"]))
    total = sum(float(x) for x in __import__("re").findall(
        r"<ACCOUNTINGALLOCATIONS\.LIST>(?:(?!</ACCOUNTING).)*?"
        r"<AMOUNT>([^<]*)</AMOUNT>", xml, __import__("re").S))
    print(f"{len(order['lines'])} lines, order value {total:,.2f}",
          file=sys.stderr)

    if a.dry_run:
        print(xml)
        return 0

    # Both sides of the exchange are kept — they are the evidence of what was
    # sent — but under sent_orders/, which is gitignored: they carry the
    # party, its GSTIN, rates and amounts, and this repo is public.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(__file__).resolve().parent / "sent_orders"
    out.mkdir(exist_ok=True)
    (out / f"sent-{order['order_key']}-{stamp}.xml").write_text(xml)
    try:
        resp = post_write(cfg, xml)
    except TallyError as exc:
        print(f"SEND FAILED (may or may not have imported): {exc}",
              file=sys.stderr)
        return 1
    (out / f"resp-{order['order_key']}-{stamp}.xml").write_text(resp)
    print(resp[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
