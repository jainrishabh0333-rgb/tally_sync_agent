"""
Offline tests for the production voucher parser.

A Tally stock journal can express its two sides in three different ways and
this build's shape could not be measured when the parser was written (Tally
is open working hours only). So the parser handles all three, and these tests
pin all three — including the one that matters most: a consumption must come
out as direction "Consumed" with a POSITIVE quantity, whichever shape it
arrived in. Get that wrong and every fabric norm is negative or inverted.

Run:  python test_production.py
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

from production_fetch import STAGES, _lines

failures: list[str] = []


def check(label: str, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


def check_true(label: str, cond, hint: str = ""):
    if not cond:
        failures.append(f"{label} {hint}".strip())
        print(f"  FAIL  {label} {hint}".rstrip())
    else:
        print(f"  ok    {label}")


def vch(xml: str):
    return ET.fromstring("<VOUCHER>" + xml + "</VOUCHER>")


# --------------------------------------------------------------- shape one
# IN / OUT lists: the direction is the tag, and the sign is irrelevant.

print("shape: INVENTORYENTRIESIN / OUT")
v = vch("""
 <INVENTORYENTRIESOUT.LIST>
  <STOCKITEMNAME>LYCRA 40 DEN</STOCKITEMNAME>
  <ACTUALQTY>-120.500 Kgs</ACTUALQTY>
  <RATE>340.00/Kgs</RATE><AMOUNT>-40970.00</AMOUNT>
 </INVENTORYENTRIESOUT.LIST>
 <INVENTORYENTRIESIN.LIST>
  <STOCKITEMNAME>2 WAY BRA (28X40)-(Doz)</STOCKITEMNAME>
  <ACTUALQTY>50.000 Doz</ACTUALQTY>
  <BATCHALLOCATIONS.LIST>
   <BATCHNAME>32</BATCHNAME><GODOWNNAME>CUTTING</GODOWNNAME>
   <ACTUALQTY>50.000 Doz</ACTUALQTY>
  </BATCHALLOCATIONS.LIST>
 </INVENTORYENTRIESIN.LIST>
""")
lines = _lines(v)
check("two lines parsed", len(lines), 2)
out = [l for l in lines if l["item_name"] == "LYCRA 40 DEN"][0]
made = [l for l in lines if l["item_name"].startswith("2 WAY")][0]
check("fabric is Consumed", out["direction"], "Consumed")
check("fabric qty is positive", out["qty"], 120.5)
check("fabric unit kept", out["unit"], "Kgs")
check("garment is Produced", made["direction"], "Produced")
check("garment size from batch", made["size_batch"], "32")
check("godown captured", made["godown"], "CUTTING")
check("source tag recorded", out["source_tag"], "INVENTORYENTRIESOUT.LIST")

# --------------------------------------------------------------- shape two
# ALLINVENTORYENTRIES: nothing but the sign says which way it went.

print("\nshape: ALLINVENTORYENTRIES, sign decides")
v = vch("""
 <ALLINVENTORYENTRIES.LIST>
  <STOCKITEMNAME>COTTON LYCRA</STOCKITEMNAME>
  <ACTUALQTY>-80.000 Kgs</ACTUALQTY>
 </ALLINVENTORYENTRIES.LIST>
 <ALLINVENTORYENTRIES.LIST>
  <STOCKITEMNAME>ANUSHKA-(Doz)</STOCKITEMNAME>
  <ACTUALQTY>36.000 Doz</ACTUALQTY>
 </ALLINVENTORYENTRIES.LIST>
""")
lines = _lines(v)
by = {l["item_name"]: l for l in lines}
check("negative qty means Consumed", by["COTTON LYCRA"]["direction"], "Consumed")
check("and is stored positive", by["COTTON LYCRA"]["qty"], 80.0)
check("positive qty means Produced", by["ANUSHKA-(Doz)"]["direction"], "Produced")

# ------------------------------------------------------------- batch signs
# A batch allocation under an OUT list is still a consumption, even though
# its own quantity is written positive. The tag wins over the sign.

print("\nbatch lines under a typed list keep the list's direction")
v = vch("""
 <INVENTORYENTRIESOUT.LIST>
  <STOCKITEMNAME>RIB 2X2</STOCKITEMNAME>
  <ACTUALQTY>-15.000 Kgs</ACTUALQTY>
  <BATCHALLOCATIONS.LIST>
   <BATCHNAME>Primary Batch</BATCHNAME><ACTUALQTY>15.000 Kgs</ACTUALQTY>
  </BATCHALLOCATIONS.LIST>
 </INVENTORYENTRIESOUT.LIST>
""")
l = _lines(v)[0]
check("tag wins over a positive batch qty", l["direction"], "Consumed")
check("batch qty used", l["qty"], 15.0)

# ------------------------------------------------------------ double export
# A build can answer with BOTH shapes at once — the same lines under the
# typed lists and again under ALLINVENTORYENTRIES. Keeping both doubles
# every mirrored quantity; the typed copy wins, the untyped copy goes.

print("\na double export is deduplicated, typed lists win")
v = vch("""
 <INVENTORYENTRIESOUT.LIST>
  <STOCKITEMNAME>COTTON LYCRA</STOCKITEMNAME>
  <ACTUALQTY>-80.000 Kgs</ACTUALQTY>
 </INVENTORYENTRIESOUT.LIST>
 <INVENTORYENTRIESIN.LIST>
  <STOCKITEMNAME>ANUSHKA-(Doz)</STOCKITEMNAME>
  <ACTUALQTY>36.000 Doz</ACTUALQTY>
 </INVENTORYENTRIESIN.LIST>
 <ALLINVENTORYENTRIES.LIST>
  <STOCKITEMNAME>COTTON LYCRA</STOCKITEMNAME>
  <ACTUALQTY>80.000 Kgs</ACTUALQTY>
 </ALLINVENTORYENTRIES.LIST>
 <ALLINVENTORYENTRIES.LIST>
  <STOCKITEMNAME>ANUSHKA-(Doz)</STOCKITEMNAME>
  <ACTUALQTY>36.000 Doz</ACTUALQTY>
 </ALLINVENTORYENTRIES.LIST>
""")
lines = _lines(v)
check("only the typed lines survive", len(lines), 2)
check_true("no untyped line rides along",
           all(l["source_tag"] != "ALLINVENTORYENTRIES.LIST" for l in lines),
           "- ALLINVENTORYENTRIES must be dropped when IN/OUT answered")
check("the consumption keeps its typed direction",
      [l["direction"] for l in lines if l["item_name"] == "COTTON LYCRA"],
      ["Consumed"])

# ------------------------------------------------------------------ misc
print("\nrobustness")
check("a line with no item name is dropped",
      len(_lines(vch("<ALLINVENTORYENTRIES.LIST><ACTUALQTY>5</ACTUALQTY>"
                     "</ALLINVENTORYENTRIES.LIST>"))), 0)
check("empty voucher yields nothing", len(_lines(vch(""))), 0)

print("\nstage map")
check("cutting issue maps", STAGES["Stock Issue-Cutting"], "Cutting Issue")
check("cutting journal maps", STAGES["Stock Journal-Cutting"], "Cutting")
check("packing maps", STAGES["Stock Journal-Packed"], "Packed")
check_true("every mapped stage is a Select option on the doctype",
           set(STAGES.values()) <= {
               "Cutting Issue", "Cutting", "Job Work Out", "Job Work In",
               "Dyeing Out", "Dyeing In", "Pressing", "Packed", "Unpacked",
               "Transfer", "Other"},
           "- a stage the doctype does not offer would fail on save")

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("All production parsing tests passed.")


def test_plan_windows_first_ever_pass():
    """Nothing mirrored yet: recent window plus a first chunk back from today."""
    from datetime import date, timedelta
    from production_fetch import plan_windows, RECENT_DAYS, BACKFILL_CHUNK_DAYS
    today = date(2026, 8, 31)
    w = plan_windows(today, None)
    assert w[0] == (today - timedelta(days=RECENT_DAYS), today)
    assert w[1] == (today - timedelta(days=BACKFILL_CHUNK_DAYS),
                    today - timedelta(days=1))


def test_plan_windows_steps_back_until_anchor_then_stops():
    from datetime import date, timedelta
    from production_fetch import plan_windows, BACKFILL_ANCHOR, BACKFILL_CHUNK_DAYS
    today = date(2026, 8, 31)
    # mid-backfill: one chunk further back from the mirrored floor
    floor = date(2026, 6, 15)
    w = plan_windows(today, floor)
    assert w[1] == (floor - timedelta(days=BACKFILL_CHUNK_DAYS),
                    floor - timedelta(days=1))
    # nearly there: the chunk clamps to the anchor, never before it
    floor = BACKFILL_ANCHOR + timedelta(days=3)
    w = plan_windows(today, floor)
    assert w[1][0] == BACKFILL_ANCHOR
    # done: anchor reached, only the recent window remains, forever
    assert len(plan_windows(today, BACKFILL_ANCHOR)) == 1


def test_gap_window_finds_the_oldest_hole():
    from datetime import date
    from production_fetch import gap_window, BACKFILL_CHUNK_DAYS
    today = date(2026, 9, 15)
    book = {date(2026, 9, 5): 17, date(2026, 9, 7): 5, date(2026, 9, 8): 19}
    mirror = {date(2026, 9, 8): 19}
    frm, to = gap_window(book, mirror, today)
    assert frm == date(2026, 9, 5)
    # a chunk from 5 Sep would run past today — the window clamps to today
    assert to == today
    # with room to spare, it is a full chunk
    frm2, to2 = gap_window(book, mirror, date(2026, 10, 30))
    assert (frm2, to2) == (date(2026, 9, 5),
                           date(2026, 9, 5) + __import__("datetime").timedelta(days=BACKFILL_CHUNK_DAYS - 1))


def test_gap_window_complete_coverage_means_none():
    from datetime import date
    from production_fetch import gap_window
    book = {date(2026, 9, 5): 17}
    assert gap_window(book, {date(2026, 9, 5): 17}, date(2026, 9, 15)) is None
    # more mirrored than booked = a deletion, not a gap
    assert gap_window(book, {date(2026, 9, 5): 18}, date(2026, 9, 15)) is None
    assert gap_window({}, {}, date(2026, 9, 15)) is None


def test_gap_window_leaves_the_recent_window_alone():
    from datetime import date
    from production_fetch import gap_window, RECENT_DAYS
    today = date(2026, 9, 15)
    inside = today - __import__("datetime").timedelta(days=RECENT_DAYS - 1)
    assert gap_window({inside: 4}, {}, today) is None


def test_gap_window_never_reaches_before_the_anchor():
    from datetime import date
    from production_fetch import gap_window, BACKFILL_ANCHOR
    before = BACKFILL_ANCHOR - __import__("datetime").timedelta(days=30)
    assert gap_window({before: 9}, {}, date(2026, 9, 15)) is None


def test_plan_windows_anchor_is_the_books_opening_day():
    from datetime import date
    from production_fetch import BACKFILL_ANCHOR
    assert BACKFILL_ANCHOR == date(2026, 4, 1)
