#!/usr/bin/env python3
"""Tests for the dispatch-readiness page's parser and its derived figures.

The parser reads a report written by pending_readiness.py's own text writer,
which pads columns to fixed widths — so the tests below feed it the exact
shapes that writer emits, including the ones that break naive parsing: a
voucher number longer than its 16-char pad, a party name containing double
spaces, an order whose lines are all unmeasured, and an order the writer
rounds up to "100%" that is not actually complete.

Run:  python3 -m pytest test_readiness.py -q
"""
from __future__ import annotations

import readiness_html as R

REPORT = """DISPATCH READINESS — 21-Aug-2026 (orders 07-Jul onward, oldest order takes stock first)
4 pending orders

 READY  PO007897-0726BLR 2026-07-07  V MART RETAIL LTD-KARNATAKA            1344/1344
  100%  PO028308-0726PWL 2026-07-25  V MART RETAIL LTD-HARYANA              2374/2380
        short 6: JIO LYCRA D CUP -SOLID (32X40)-(1 PCS)
   50%  SO/A-VERY-LONG-VOUCHER/9 2026-08-01  SOME  DOUBLE  SPACED - PARTY      5/10 (+2.5 unknown)
        short 3: ITEM ONE S-XL-(Doz)
        short 2: ITEM TWO XXL-(Doz)
    0%  1205             2026-08-13  SAMRAT HOSIERY-AURANGABAD              0/0 (+194 unknown)

========================================================================
ITEMS BLOCKING DISPATCH — pending demand the stock cannot cover
(short qty across all pending orders after oldest-first allocation)

  blocks 135 orders  short     1746   1100 SPORT BRA (28X40)-(Doz)
        32:481.5 34:434 30:300.5 28:81
  blocks   2 orders  short        5   ITEM ONE S-XL-(Doz)
        36:5

NO RECENT STOCK SIGHTING (excluded from percentages — count them by hand or move the item once, and BlncQty will report it):
   42 orders  qty    119.5   WATER SPORTS BRA (28X40)-(Doz)
"""


def parsed():
    return R.parse_text(REPORT)


def test_counts():
    r = parsed()
    assert r["order_count"] == 4
    assert len(r["blocking"]) == 2
    assert len(r["unknown"]) == 1


def test_long_voucher_and_double_spaced_party():
    # the writer pads to 16 chars but does not truncate, so a longer voucher
    # shifts every column after it; the party then holds internal double spaces
    o = [x for x in parsed()["orders"] if x["voucher"].startswith("SO/A-VERY")][0]
    assert o["voucher"] == "SO/A-VERY-LONG-VOUCHER/9"
    assert o["party"] == "SOME  DOUBLE  SPACED - PARTY"
    assert (o["got"], o["need"], o["unknown"]) == (5.0, 10.0, 2.5)
    assert [s["item"] for s in o["shorts"]] == ["ITEM ONE S-XL-(Doz)",
                                                "ITEM TWO XXL-(Doz)"]


def test_rounded_100_is_not_ready():
    # 2374/2380 prints as "100%" but is short by 6 — reading the printed
    # integer would file it as complete and disagree with the READY flag
    o = [x for x in parsed()["orders"] if x["voucher"] == "PO028308-0726PWL"][0]
    assert o["ready"] is False
    assert o["pct"] < 100.0
    bands = R._summarise(parsed())["bands"]
    assert dict((b["label"], b["count"]) for b in bands)["Ready in full"] == 1


def test_all_lines_unmeasured_is_not_nothing_to_send():
    # need == 0 with unknown > 0 means uncounted, not empty-handed
    s = R._summarise(parsed())
    assert s["unmeasured"] == 1
    assert s["nothing"] == 0
    labels = dict((b["label"], b["count"]) for b in s["bands"])
    assert labels["Not measured at all"] == 1


def test_bands_partition_the_orders():
    s = R._summarise(parsed())
    assert sum(b["count"] for b in s["bands"]) == s["orders"]
    assert s["ready"] + s["partial"] + s["nothing"] + s["unmeasured"] == s["orders"]


def test_headline_percentage_uses_measurable_qty_only():
    s = R._summarise(parsed())
    assert s["need"] == 1344 + 2380 + 10          # the unmeasured order adds 0
    assert s["got"] == 1344 + 2374 + 5
    assert s["unknown"] == 2.5 + 194
    assert round(s["pct"], 1) == round(100 * s["got"] / s["need"], 1)


def test_sizes_and_ages():
    r = parsed()
    assert r["blocking"][0]["sizes"] == {"32": 481.5, "34": 434.0,
                                         "30": 300.5, "28": 81.0}
    R._summarise(r)
    assert [o["age"] for o in r["orders"] if o["voucher"] == "1205"] == [8]


def test_render_is_self_contained():
    html = R.render(parsed())
    assert "__DATA__" not in html and "__ASOF__" not in html
    assert "21-Aug-2026" in html
    for bad in ("http://", "https://", "//cdn"):
        assert bad not in html, f"page reaches out to {bad}"
