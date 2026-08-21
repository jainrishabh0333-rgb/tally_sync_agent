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
    sales_ledger_for,
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


# What _masters/fast_send hand over for one party: the ledger master's own
# address lines, state, pincode and mailing name.
MAHARASHTRA_PARTY = {
    "address_lines": ["Shop 4, Cloth Market", "Aurangabad, Maharashtra, 431001"],
    "state": "Maharashtra", "pincode": "431001",
    "mailing_name": "SAMRAT HOSIERY", "gst_registration_type": "Regular",
}
UP_PARTY = dict(MAHARASHTRA_PARTY, state="Uttar Pradesh")


def envelope(raw: dict, party: dict | None = MAHARASHTRA_PARTY,
             gstin: str = "27AWRPS7219L1ZL") -> str:
    return build_envelope(normalise_order(raw), load_order_settings(),
                          gstin, False, party)


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

print("party address")
addr_xml = envelope(queue_rows())
check("the master's address lines are written, in order",
      re.findall(r"<ADDRESS>([^<]*)</ADDRESS>", addr_xml),
      MAHARASHTRA_PARTY["address_lines"])
check("the buyer block carries the same lines",
      re.findall(r"<BASICBUYERADDRESS>([^<]*)</BASICBUYERADDRESS>", addr_xml),
      MAHARASHTRA_PARTY["address_lines"])
check("mailing name comes from the master, not the ledger name",
      re.findall(r"<PARTYMAILINGNAME>([^<]*)</PARTYMAILINGNAME>", addr_xml),
      ["SAMRAT HOSIERY"])
check("pincode is written for buyer and consignee",
      re.findall(r"<(?:PARTY|CONSIGNEE)PINCODE>([^<]*)</(?:PARTY|CONSIGNEE)PINCODE>",
                 addr_xml), ["431001", "431001"])
check("state comes from the master",
      re.findall(r"<STATENAME>([^<]*)</STATENAME>", addr_xml), ["Maharashtra"])
no_addr = envelope(queue_rows(), party=None)
check("a party with no details on file still builds (blank address)",
      "<ADDRESS>" in no_addr, False)
check("and falls back to the GSTIN prefix for its state",
      re.findall(r"<STATENAME>([^<]*)</STATENAME>", no_addr), ["Maharashtra"])

print("sales ledger follows the party's state")
ocfg = load_order_settings()
check("out-of-state party posts to the Central ledger",
      sales_ledger_for({}, ocfg, "Maharashtra"), "Sale Central 5%")
check("a party in the company's own state posts to Local",
      sales_ledger_for({}, ocfg, "Uttar Pradesh"), "Sale Local 5%")
check("state matching ignores case",
      sales_ledger_for({}, ocfg, "uttar pradesh"), "Sale Local 5%")
check("an unknown state falls back to Central",
      sales_ledger_for({}, ocfg, ""), "Sale Central 5%")
check("an explicit ledger on the order wins",
      sales_ledger_for({"sales_ledger": "Sale Local 5%"}, ocfg, "Maharashtra"),
      "Sale Local 5%")
check("the envelope writes the Local ledger for a UP party",
      set(re.findall(r"<LEDGERNAME>(Sale[^<]*)</LEDGERNAME>",
                     envelope(queue_rows(), party=UP_PARTY,
                              gstin="09ADWPK1913B1ZL"))),
      {"Sale Local 5%"})
check("and Central for an out-of-state one",
      set(re.findall(r"<LEDGERNAME>(Sale[^<]*)</LEDGERNAME>", addr_xml)),
      {"Sale Central 5%"})
check("an unregistered UP party is still Local (master state, no GSTIN)",
      set(re.findall(r"<LEDGERNAME>(Sale[^<]*)</LEDGERNAME>",
                     envelope(queue_rows(), party=UP_PARTY, gstin=""))),
      {"Sale Local 5%"})

print("unpriced orders")
check_raises(
    "a non-positive rate is refused at normalisation",
    lambda: normalise_order(queue_rows(rate=0)),
    exc=OrderDataError, contains="not positive")

print("writes are never retried")
# A replayed import is how PR-MAHATMA came to exist three times in the live
# book on 2026-08-21: _post retries 4x on any RequestException, and a POST
# that times out may already have committed inside Tally. Reads may retry;
# writes must go out exactly once and then be VERIFIED.
import tally_client
from tally_client import TallyConfig, TallyError, post_write

_sent = {"n": 0}


def _refuse(*_a, **_k):
    _sent["n"] += 1
    raise tally_client.requests.exceptions.ReadTimeout("read timed out")


_saved_post = tally_client.requests.post
tally_client.requests.post = _refuse
_dead = TallyConfig(host="127.0.0.1", port=9, company="X", timeout=1)
try:
    check_raises("a timed-out write raises", lambda: post_write(_dead, "<E/>"),
                 exc=TallyError, contains="NOT retried")
    check("a timed-out write is sent exactly ONCE", _sent["n"], 1)

    _sent["n"] = 0
    try:
        tally_client._post(_dead, "<E/>", attempts=2)
    except TallyError:
        pass
    check("a read still retries", _sent["n"], 2)


    class _LineError:
        status_code = 200
        content = b"<ENVELOPE><LINEERROR>totals do not match</LINEERROR></ENVELOPE>"

    tally_client.requests.post = lambda *_a, **_k: _LineError()
    # Tally answered and refused, so nothing was written. That must NOT be
    # dressed up as "may already have committed", and order_importer keys its
    # clean-failure branch off the words "line error".
    check_raises("a rejected write is reported as a clean line error",
                 lambda: post_write(_dead, "<E/>"),
                 exc=TallyError, contains="line error")
finally:
    tally_client.requests.post = _saved_post

print()
if FAILED:
    print(f"{FAILED} check(s) FAILED.")
    sys.exit(1)
print("All order-import tests passed.")
