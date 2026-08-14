#!/usr/bin/env python3
"""
Tests for the ONE write path — the priced Sales Order envelope.

This file exists because its absence cost a live day. The importer shipped
quantity-only, every amount zero, and this Tally build refuses zero-value
vouchers: the only path that writes to the books could not produce a voucher
the books would accept, and nothing caught it until an order was sent for
real. Everything below is about the money surviving the trip.

No Tally, no Frappe, no network — the envelope is built and read back.

Run:  python test_orders.py
"""

from __future__ import annotations

import re
import sys

from order_importer import (
    NET_FACTOR,
    OrderDataError,
    _assert_sales_order,
    build_envelope,
    load_order_settings,
    normalise_order,
)

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"  ok    {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}\n        got  {got!r}\n        want {want!r}")


def check_raises(label: str, fn, *, exc=Exception, contains: str = "") -> None:
    global FAILED
    try:
        fn()
    except exc as e:  # noqa: BLE001 — the point is to catch what was declared
        if contains and contains not in str(e):
            FAILED += 1
            print(f"  FAIL  {label}\n        raised {e!r}, wanted {contains!r}")
        else:
            print(f"  ok    {label}")
        return
    FAILED += 1
    print(f"  FAIL  {label} — nothing raised")


def amounts(xml: str, tag: str = "AMOUNT") -> list[float]:
    return [float(a) for a in re.findall(rf"<{tag}>([^<]*)</{tag}>", xml)]


def queue_rows(rate=1740, unit="Box", second: dict | None = None) -> dict:
    """The shape pending_sales_orders actually returns: one row per size.

    `second` overrides fields on the SECOND size row only — that is how a
    queue ends up with two rows of one item disagreeing about its rate.
    """
    row = {"item_name": "VATIKA TOP S-XL-(Doz)", "unit": unit, "rate": rate,
           "discount": 50}
    return {
        "order_key": "TEST-1", "order_no": "1205",
        "company": "SN JAIN INDUSTRIES PVT LTD - (26-27)",
        "party_ledger": "SAMRAT HOSIERY-AURANGABAD",
        "order_date": "2026-08-13",
        "lines": [dict(row, size_batch="S", qty=3),
                  dict(row, size_batch="M", qty=6, **(second or {}))],
    }


def envelope(raw: dict) -> str:
    return build_envelope(normalise_order(raw), load_order_settings(),
                          "27AWRPS7219L1ZL")


print("queue shape")
o = normalise_order(queue_rows())
check("flat size rows regroup to one item line", len(o["lines"]), 1)
check("quantities sum", o["lines"][0]["qty"], 9.0)
check("rate rides through", o["lines"][0]["rate"], 1740.0)
check_raises(
    "rows disagreeing on rate are refused",
    lambda: normalise_order(queue_rows(second={"rate": 1800})),
    exc=OrderDataError, contains="disagree on rate")

print("priced arithmetic")
xml = envelope(queue_rows())
line = 1740 * 9 * NET_FACTOR
check("line amount is rate x qty x chain",
      amounts(xml)[0], round(line, 2))
check("batches sum to their line",
      round(sum(amounts(xml)[1:3]), 2), round(line, 2))
check("accounting allocation carries the line value",
      amounts(xml)[3], round(line, 2))
check("party is debited the total (negative)",
      amounts(xml)[-1], -round(line, 2))
check("rate is written with its unit",
      re.findall(r"<RATE>([^<]*)</RATE>", xml), ["1740.00/Box"])
check("only the first discount step is written",
      re.findall(r"<DISCOUNT>([^<]*)</DISCOUNT>", xml), ["50"])

print("tamper detection")
check_raises(
    "a doctored line amount is caught on the outgoing bytes",
    lambda: _assert_sales_order(xml.replace("<AMOUNT>6264.00</AMOUNT>",
                                            "<AMOUNT>6000.00</AMOUNT>", 1)),
    exc=RuntimeError, contains="does not equal rate")
check_raises(
    "a party line that is not minus the total is caught",
    lambda: _assert_sales_order(xml.replace("<AMOUNT>-6264.00</AMOUNT>",
                                            "<AMOUNT>-1.00</AMOUNT>", 1)),
    exc=RuntimeError, contains="minus the inventory total")
check_raises(
    "MRP is never written",
    lambda: _assert_sales_order(xml.replace("<RATE>", "<MRPRATE>x</MRPRATE><RATE>", 1)),
    exc=RuntimeError, contains="MRP")
check_raises(
    "any voucher type but Sales Order is refused",
    lambda: _assert_sales_order(xml.replace("Sales Order", "Journal")),
    exc=RuntimeError, contains="not exactly one")

print("unpriced orders")
check_raises(
    "a non-positive rate is refused at normalisation",
    lambda: normalise_order(queue_rows(rate=0)),
    exc=OrderDataError, contains="not positive")

print()
if FAILED:
    print(f"{FAILED} check(s) FAILED.")
    sys.exit(1)
print("All order-import tests passed.")
