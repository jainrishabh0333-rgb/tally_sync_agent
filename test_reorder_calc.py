#!/usr/bin/env python3
"""Tests for the days-of-cover reorder level proposal.

The arithmetic is trivial; the traps are not. Every test below stands for
something that would silently produce a plausible wrong level: a dispatch read
with the wrong sign, a return counted as a sale, a marketplace sale folded
into trade demand, a level rounded DOWN, and a level standing against a style
with no dispatch at all.

Run:  python3 -m pytest test_reorder_calc.py -q
"""
from __future__ import annotations

import reorder_level_calc as C
from reorder_fetch import Movement


def mv(vtype, item, size, qty, unit="Doz"):
    """A movement signed the way Tally's godown lists sign it: - is outward."""
    return Movement(guid="g", date="2026-08-01", voucher_type=vtype,
                    voucher_number="1", item_name=item, size_batch=size,
                    godown="Pack", bucket="in_stock", qty=qty, unit=unit,
                    qty_raw=str(abs(qty)))


def test_dispatch_is_flipped_to_positive_demand():
    """A sale leaves the godown negative and must read as positive demand."""
    d = C.demand_from_movements([mv("Sales", "A", "32", -10)])
    assert d[("A", "32")].trade_qty == 10


def test_a_return_nets_off_rather_than_adding():
    """Credit Note comes back IN. Counting its magnitude would double-count
    a return as extra demand and raise the level on a style being sent back."""
    d = C.demand_from_movements([mv("Sales", "A", "32", -10),
                                 mv("Credit Note", "A", "32", +4)])
    assert d[("A", "32")].trade_qty == 6


def test_marketplace_is_measured_but_kept_out_of_trade():
    d = C.demand_from_movements([mv("Sales", "A", "32", -10),
                                 mv("Meesho Sale", "A", "32", -6)])
    got = d[("A", "32")]
    assert (got.trade_qty, got.other_qty, got.all_qty) == (10, 6, 16)


def test_online_return_never_credits_trade():
    """`Credit Note-Online Sale` reverses a marketplace sale. Netting it
    against trade would credit a return to a channel that never sold."""
    d = C.demand_from_movements([mv("Sales", "A", "32", -10),
                                 mv("Credit Note-Online Sale", "A", "32", +4)])
    assert d[("A", "32")].trade_qty == 10
    assert d[("A", "32")].other_qty == -4


def test_forty_five_days_of_cover_is_half_a_ninety_day_window():
    rows = C.propose(C.demand_from_movements([mv("Sales", "A", "32", -90)]),
                     window_days=90, days_cover=45, step=0.5)
    assert rows[0]["daily_demand"] == 1.0
    assert rows[0]["proposed_level"] == 45.0


def test_levels_round_up_never_down():
    """A level is a floor. Rounding 44.2 down to 44 quietly removes cover."""
    assert C.round_up(44.2, 0.5) == 44.5
    assert C.round_up(44.5, 0.5) == 44.5
    assert C.round_up(0.01, 0.5) == 0.5


def test_a_net_returned_style_asks_for_zero_not_a_negative_level():
    rows = C.propose(C.demand_from_movements([mv("Sales", "A", "32", -2),
                                              mv("Credit Note", "A", "32", +9)]),
                     window_days=90, days_cover=45)
    assert rows[0]["proposed_level"] == 0.0


def test_mixed_units_are_flagged_not_summed_away():
    """No Box-to-Doz factor exists in this book, so a pair seen in both units
    cannot be totalled. It must arrive on the sheet marked, not silently."""
    rows = C.propose(C.demand_from_movements([mv("Sales", "A", "32", -10, "Doz"),
                                              mv("Sales", "A", "32", -5, "Box")]),
                     window_days=90, days_cover=45)
    assert rows[0]["mixed_units"] == "Box,Doz"


def test_a_level_with_no_dispatch_survives_into_the_sheet():
    """The whole point of the exercise: stock the floor holds for nothing.
    Dropping these rows would leave every dead level in place untouched."""
    rows = C.merge_current([], {("DEAD", "32"): 120.0}, {"DEAD": "Panty"},
                           {"DEAD": "Box"})
    assert len(rows) == 1
    assert rows[0]["proposed_level"] == 0.0
    assert rows[0]["current_level"] == 120.0
    assert rows[0]["change"] == -120.0
    assert "NO DISPATCH" in rows[0]["status"]


def test_a_style_with_demand_and_no_level_reads_as_new():
    rows = C.propose(C.demand_from_movements([mv("NEWSTYLE", "32", "32", -90)]),
                     window_days=90, days_cover=45)
    merged = C.merge_current(rows, {}, {}, {})
    assert merged[0]["current_level"] is None
    assert merged[0]["change"] is None
    assert merged[0]["status"].startswith("NEW")


def test_unsized_lines_are_dropped_rather_than_pooled_under_one_blank_size():
    """A line with no batch cannot be attributed to a size, and pooling them
    would invent a phantom size carrying every unsized sale."""
    d = C.demand_from_movements([mv("Sales", "A", "", -10)])
    assert d == {}


def test_config_toml_survives_what_notepad_writes():
    """The Tally box's config.toml is edited in Notepad, which saves a UTF-8
    BOM by default. Handing those bytes straight to tomllib fails with
    'Invalid statement (at line 1, column 1)' — an error that names a line
    with nothing visibly wrong on it. Measured on the box 2026-08-23.

    The decoder is asserted directly rather than through tomllib, which this
    workspace's Python 3.9 does not carry; the Tally box runs 3.14 and does.
    """
    body = '[frappe]\nurl = "https://x.test"\n'
    for raw in (body.encode("utf-8"),                        # plain
                b"\xef\xbb\xbf" + body.encode("utf-8"),      # Notepad UTF-8+BOM
                body.encode("utf-16"),                       # Notepad "Unicode"
                body.replace("\n", "\r\n").encode("utf-8")):  # CRLF
        assert C._decode_toml(raw) == body, raw[:4]
