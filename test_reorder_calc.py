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


# --- recency weighting ----------------------------------------------------
# Added because a 90-day flat average LAGS the trend: measured 2026-08-23,
# Camisol ran 44% above its own 90-day mean while its proposal came out flat,
# and Cycling Short ran 17% below while its proposal rose.

from datetime import date as _date, timedelta


def mvd(day, qty, vtype="Sales", item="A", size="32"):
    """A movement on a specific date."""
    return Movement(guid="g", date=day, voucher_type=vtype, voucher_number="1",
                    item_name=item, size_batch=size, godown="Pack",
                    bucket="in_stock", qty=qty, unit="Doz", qty_raw=str(abs(qty)))


FRM, TO = _date(2026, 5, 25), _date(2026, 8, 22)   # the live 90-day window


def test_half_life_zero_is_a_flat_average():
    w = C.day_weights(FRM, TO, 0)
    assert set(w.values()) == {1.0}
    assert len(w) == 90


def test_weight_halves_every_half_life():
    w = C.day_weights(FRM, TO, 30)
    assert w[TO.isoformat()] == 1.0                       # today: full weight
    assert abs(w[(TO - timedelta(days=30)).isoformat()] - 0.5) < 1e-9
    assert abs(w[(TO - timedelta(days=60)).isoformat()] - 0.25) < 1e-9


def test_weighting_off_reproduces_the_unweighted_level_EXACTLY():
    """The flag must not re-price 1,093 live levels as a side effect."""
    moves = [mvd("2026-06-01", -30), mvd("2026-08-01", -60)]
    flat = C.propose(C.demand_from_movements(moves), 90, 45, 0.5)
    same = C.propose(C.demand_from_movements(moves, C.day_weights(FRM, TO, 0)),
                     90, 45, 0.5, "trade", weight_total=90.0)
    assert flat[0]["proposed_level"] == same[0]["proposed_level"]
    assert flat[0]["daily_demand"] == same[0]["daily_demand"]


def _level(moves, half_life):
    w = C.day_weights(FRM, TO, half_life) if half_life else None
    total = sum(w.values()) if w else 0.0
    return C.propose(C.demand_from_movements(moves, w), 90, 45, 0.5,
                     "trade", total)[0]


def test_the_same_total_sold_recently_outranks_the_same_total_sold_early():
    recent = _level([mvd("2026-08-20", -90)], 30)
    early = _level([mvd("2026-05-26", -90)], 30)
    assert recent["proposed_level"] > early["proposed_level"]
    # and flat weighting cannot tell them apart at all
    assert (_level([mvd("2026-08-20", -90)], 0)["proposed_level"]
            == _level([mvd("2026-05-26", -90)], 0)["proposed_level"])


def test_an_accelerating_style_reads_trend_above_one():
    row = _level([mvd("2026-06-01", -10), mvd("2026-08-20", -50)], 30)
    assert row["trend"] > 1.0
    row = _level([mvd("2026-06-01", -50), mvd("2026-08-20", -10)], 30)
    assert row["trend"] < 1.0


def test_quiet_days_stay_in_the_denominator():
    """One burst is not a run rate. If the divisor were 'days that had a
    sale', every intermittent style would price as a fast mover."""
    row = _level([mvd("2026-08-22", -90)], 30)
    assert row["daily_demand"] < 5.0, row["daily_demand"]


def test_a_movement_dated_outside_the_window_carries_no_weight():
    """Chunk boundaries can hand back a stray neighbouring voucher. Defaulting
    an unknown date to weight 1.0 would count it as if it happened today."""
    row = _level([mvd("2025-01-01", -900)], 30)
    assert row["proposed_level"] == 0.0
    assert row["trade_qty"] == 900          # still reported, just not weighted


def test_tallys_placeholder_batch_is_not_a_size():
    """`Primary Batch` is what Tally writes when a batch-tracked item is
    billed without one. Measured 2026-08-23: it produced the single largest
    row in the sheet, a 2,029 level against a size that does not exist."""
    d = C.demand_from_movements([mv("Sales", "PANTY (PCS)", "Primary Batch", -900),
                                 mv("Sales", "A", "32", -10)])
    assert list(d) == [("A", "32")]


def test_the_placeholder_check_is_not_fooled_by_case_or_padding():
    d = C.demand_from_movements([mv("Sales", "X", "  PRIMARY BATCH ", -900)])
    assert d == {}
