"""
reorder_level_calc.py — propose reorder levels as N days of cover.

The REORDER LEVEL column of the Tally reorder report is the one figure the
platform cannot measure: it is a standing management decision held in custom
TDL storage. This script proposes one, from demand.

    average daily dispatch = dispatch over the window / days in the window
    proposed level         = average daily dispatch x days of cover

With the defaults (90-day window, 45 days of cover) that reduces to exactly
HALF the last 90 days' dispatch — a useful sanity check when reading the sheet.

Why the level is a COVER figure and not a full stock target: the report's own
arithmetic already nets the order book out separately.

    deficit = in stock + unpack + stitching - PENDING ORDER - reorder level

So the level is the free stock to hold once every open order is filled, which
is precisely "N days of stock", and the two terms must not be conflated.

What counts as dispatch
-----------------------
`--demand trade` (default) counts the `Sales` voucher type alone, net of
`Credit Note` returns. `--demand all` adds the marketplace channels, V-Mart and
branch delivery challans. Either way the other channels are MEASURED and
reported in their own column, so choosing `trade` shows what it leaves out
instead of silently understating a style that also sells online.

UNITS: DO NOT CONVERT
---------------------
All 15 units in this book are SIMPLE — Conversion 0.0, no BaseUnits — so no
Box-to-Doz factor exists to convert with. Finished goods split 3,650 Box-based
against 1,053 Doz-based, and the item NAME lies: most Box-unit items carry
"-(Doz)" in the name. Quantities are therefore summed in each item's own unit
and the unit is carried onto every output row. Mixed units within one
(item, size) are reported loudly, never summed away. Same trap as
reorder_refresh.py, which learned it the expensive way.

Sizes come from the batch name, as everywhere else in this book. Measured
2026-08-23: 793 of 793 sales lines in a day carried one.

WHAT THIS CANNOT DO
-------------------
Write the level back into Tally. Native REORDERLEVEL reads back empty on this
build and the real value lives in custom TDL storage with no known field name,
so the last step is a person typing the approved numbers into the reorder
report. The sheet is ordered by size of change to make that as short as
possible, and `--push` parks the proposal beside the current level in Frappe
(`Tally Reorder Level.proposed_level`) so the dashboard can show both without
touching the exported figure.

Run:
    python reorder_level_calc.py --host 100.74.103.22 \
        --company "SN JAIN INDUSTRIES PVT LTD - (26-27)" \
        --window 90 --days-cover 45 --csv out/reorder_proposal.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import reorder_fetch as rf
from frappe_client import FrappeClient, FrappeConfig
from tally_client import TallyConfig, _post

log = logging.getLogger("sync")

LEVEL_DOCTYPE = "Tally Reorder Level"
ITEM_DOCTYPE = "Tally Stock Item"

# The stock groups the reorder report actually covers. A filter is needed
# because plenty of things move on a `Sales` voucher without ever wanting a
# reorder level: measured 2026-08-23, an unfiltered 90-day window also
# proposed levels for Raw Material (27 sizes), V Mart consignment stock (78),
# fabric, packing material and the marketplace-only item masters. Left in,
# they read as 240,000 units of "missing" level and bury the finished goods.
FINISHED_GROUPS = ["Panty", "Bra", "Camisol", "Cycling Short", "Bloomer"]

# Trade counter sales and their returns. `Credit Note-Online Sale` is
# deliberately NOT here — it reverses a marketplace sale, and netting it
# against trade dispatch would credit a return to a channel that never sold.
TRADE_TYPES = ["Sales", "Credit Note"]

# Everything else that takes finished goods out of the factory. Measured in
# its own column whichever mode is chosen, so the cost of excluding it is
# visible on the sheet rather than left to be discovered later.
OTHER_OUTWARD_TYPES = [
    "Flipkart Sale", "Myntra Sale", "Meesho Sale", "Amazon Sale",
    "Limeroad Sale", "Shopify Sale", "V-Mart Sale", "Ajio Sale",
    "Credit Note-Online Sale",
    "Delivery Challan(Branch Transfer)",
]


# ---------------------------------------------------------------------------
# Demand
# ---------------------------------------------------------------------------

@dataclass
class Demand:
    """Dispatch of one size of one item over the window, split by channel."""
    item_name: str
    size: str
    unit: str
    trade_qty: float = 0.0      # Sales less Credit Note
    other_qty: float = 0.0      # marketplace / V-Mart / branch challans
    units_seen: frozenset = frozenset()

    @property
    def all_qty(self) -> float:
        return self.trade_qty + self.other_qty


def demand_from_movements(moves: list) -> dict:
    """
    {(item, size): Demand} — dispatch, positive, per channel group.

    Movements are signed for the GODOWN (+ in, - out), so a dispatch arrives
    here negative and is flipped once. Returns keep their sign and therefore
    net themselves off, which is what makes a heavily-returned style ask for a
    smaller level rather than a larger one.
    """
    by_key: dict = {}
    units_seen: dict = defaultdict(set)
    trade = set(TRADE_TYPES)
    for m in moves:
        if not m.size_batch:
            continue
        key = (m.item_name, m.size_batch)
        d = by_key.get(key)
        if d is None:
            d = by_key[key] = Demand(item_name=m.item_name, size=m.size_batch,
                                     unit=m.unit)
        if m.unit:
            units_seen[key].add(m.unit)
        if m.voucher_type in trade:
            d.trade_qty += -m.qty
        else:
            d.other_qty += -m.qty
    for key, seen in units_seen.items():
        by_key[key].units_seen = frozenset(seen)
    return by_key


def round_up(value: float, step: float) -> float:
    """Round a level UP to the next step. A level is a floor, so never down."""
    if step <= 0:
        return value
    return math.ceil(round(value / step, 6)) * step


def propose(demand: dict, window_days: int, days_cover: int,
            step: float = 0.5, mode: str = "trade") -> list[dict]:
    """
    One proposal row per (item, size). Pure — no Tally, no Frappe.

    `mode` picks which measured demand drives the level; the other channel is
    still carried onto the row so the sheet can show what the choice costs.
    """
    rows = []
    for (item, size), d in demand.items():
        basis = d.trade_qty if mode == "trade" else d.all_qty
        basis = max(0.0, basis)
        daily = basis / window_days if window_days else 0.0
        rows.append({
            "item_name": item,
            "size": size,
            "unit": d.unit,
            "trade_qty": round(d.trade_qty, 2),
            "other_qty": round(d.other_qty, 2),
            "basis_qty": round(basis, 2),
            "daily_demand": round(daily, 4),
            "proposed_level": round_up(daily * days_cover, step),
            "mixed_units": ",".join(sorted(d.units_seen))
                           if len(d.units_seen) > 1 else "",
        })
    return rows


def merge_current(rows: list[dict], levels: dict, groups: dict,
                  base_units: dict) -> list[dict]:
    """
    Attach the level Tally holds today, the stock group, and the item's own
    unit; then add back every levelled row that saw NO dispatch at all.

    Those zero-dispatch rows are the point of the exercise as much as the busy
    ones — a level standing against a style nobody has bought in the window is
    stock the floor is being told to hold for nothing. They are listed with a
    proposal of 0 and left for a human to confirm, because "no trade dispatch"
    and "dead" are not the same claim: a seasonal or online-only style reads
    identically here.
    """
    out = []
    seen = set()
    for r in rows:
        key = (r["item_name"], r["size"])
        seen.add(key)
        r = dict(r)
        r["current_level"] = levels.get(key)
        r["stock_group"] = groups.get(r["item_name"], "")
        r["base_unit"] = base_units.get(r["item_name"], "")
        out.append(r)
    for key, lvl in levels.items():
        if key in seen:
            continue
        out.append({
            "item_name": key[0], "size": key[1],
            "unit": base_units.get(key[0], ""),
            "base_unit": base_units.get(key[0], ""),
            "stock_group": groups.get(key[0], ""),
            "trade_qty": 0.0, "other_qty": 0.0, "basis_qty": 0.0,
            "daily_demand": 0.0, "proposed_level": 0.0,
            "mixed_units": "", "current_level": lvl,
        })
    for r in out:
        cur = r.get("current_level")
        r["change"] = (round(r["proposed_level"] - cur, 2)
                       if cur is not None else None)
        r["status"] = _status(r)
    return out


def _status(r: dict) -> str:
    cur = r.get("current_level")
    if cur is None:
        return "NEW — no level set today"
    if r["basis_qty"] <= 0:
        return "NO DISPATCH in window — level held against nothing"
    if cur <= 0:
        return "NEW — level is zero today"
    ratio = r["proposed_level"] / cur
    if ratio >= 1.5:
        return "RAISE sharply"
    if ratio >= 1.1:
        return "raise"
    if ratio <= 0.5:
        return "CUT sharply"
    if ratio <= 0.9:
        return "cut"
    return "about right"


# ---------------------------------------------------------------------------
# Pulling
# ---------------------------------------------------------------------------

def date_chunks(frm: date, to: date, days: int):
    cur = frm
    while cur <= to:
        end = min(cur + timedelta(days=days - 1), to)
        yield cur, end
        cur = end + timedelta(days=1)


def collect(cfg: TallyConfig, frm: date, to: date, types: list,
            chunk_days: int = 7, cache_dir: str = "") -> list:
    """
    Movements for the window, chunked, with every chunk cached on disk.

    Tally here is SHARED PRODUCTION and per-chunk times vary about ninefold
    with other users' load, so a ninety-day pull is long enough that losing it
    to one timeout matters. Each chunk's raw XML is written as it arrives and
    re-read on a later run, which makes the job resumable and makes a reparse
    free.
    """
    rf.STOCK_VOUCHER_TYPES = types
    moves = []
    chunks = list(date_chunks(frm, to, chunk_days))
    for i, (a, b) in enumerate(chunks, 1):
        path = os.path.join(cache_dir, f"mv_{a}_{b}.xml") if cache_dir else ""
        raw = ""
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
            log.info("[%d/%d] %s..%s from cache (%.1f MB)",
                     i, len(chunks), a, b, len(raw) / 1e6)
        else:
            raw = _post(cfg, rf._movement_body(cfg, a, b, rf._MOVEMENT))
            log.info("[%d/%d] %s..%s fetched (%.1f MB)",
                     i, len(chunks), a, b, len(raw) / 1e6)
            if path:
                os.makedirs(cache_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(raw)
        moves.extend(rf.parse_movements(raw, None, None))
    log.info("Parsed %d movements over %s..%s", len(moves), frm, to)
    return moves


# ---------------------------------------------------------------------------
# Frappe side
# ---------------------------------------------------------------------------

def frappe_from_env() -> FrappeConfig | None:
    url = os.environ.get("FRAPPE_URL", "")
    key = os.environ.get("FRAPPE_API_KEY", "")
    sec = os.environ.get("FRAPPE_API_SECRET", "")
    if not (url and key and sec):
        return None
    return FrappeConfig(url=url.rstrip("/"), api_key=key, api_secret=sec)


def _get_list(fc: FrappeClient, doctype: str, fields: list,
              filters: list | None = None, page: int = 2000) -> list:
    out, start = [], 0
    while True:
        params = {
            "doctype": doctype,
            "fields": json.dumps(fields),
            "limit_start": start,
            "limit_page_length": page,
        }
        if filters:
            params["filters"] = json.dumps(filters)
        got = fc._call("GET", "/api/method/frappe.client.get_list",
                       params=params).get("message", [])
        out.extend(got)
        if len(got) < page:
            return out
        start += page


def load_reference(fc: FrappeClient) -> tuple:
    """(levels, groups, base_units) out of the mirror."""
    lv = _get_list(fc, LEVEL_DOCTYPE,
                   ["name", "item_name", "size", "reorder_level"])
    levels = {(r["item_name"], str(r["size"] or "")): r["reorder_level"]
              for r in lv}
    names = {(r["item_name"], str(r["size"] or "")): r["name"] for r in lv}
    it = _get_list(fc, ITEM_DOCTYPE, ["item_name", "stock_group", "base_units"])
    groups = {r["item_name"]: r["stock_group"] or "" for r in it}
    base = {r["item_name"]: r["base_units"] or "" for r in it}
    log.info("Reference: %d levels, %d stock items", len(levels), len(groups))
    return levels, groups, base, names


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

COLUMNS = ["stock_group", "item_name", "size", "unit", "base_unit",
           "trade_qty", "other_qty", "basis_qty", "daily_demand",
           "proposed_level", "current_level", "change", "status",
           "mixed_units"]


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ordered = sorted(
        rows,
        key=lambda r: (-abs(r["change"]) if r["change"] is not None else -1e9,
                       r["item_name"], r["size"]),
    )
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ordered)
    log.info("Wrote %d rows to %s", len(ordered), path)


def summarise(rows: list[dict], window_days: int, days_cover: int) -> str:
    by_group: dict = defaultdict(lambda: [0, 0.0, 0.0])
    for r in rows:
        g = by_group[r["stock_group"] or "(ungrouped)"]
        g[0] += 1
        g[1] += r["proposed_level"]
        g[2] += r["current_level"] or 0.0
    out = [f"{days_cover} days of cover from a {window_days}-day window",
           "",
           f"{'group':<16}{'rows':>7}{'proposed':>12}{'current':>12}{'change':>12}"]
    for g in sorted(by_group):
        n, prop, cur = by_group[g]
        out.append(f"{g:<16}{n:>7}{prop:>12,.1f}{cur:>12,.1f}{prop-cur:>+12,.1f}")
    counts: dict = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    out.append("")
    for s in sorted(counts, key=lambda k: -counts[k]):
        out.append(f"  {counts[s]:>6}  {s}")
    mixed = [r for r in rows if r["mixed_units"]]
    if mixed:
        out.append("")
        out.append(f"  !! {len(mixed)} item/size pairs carry MIXED units — "
                   "their totals are NOT trustworthy")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("TALLY_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--company", default=os.environ.get("TALLY_COMPANY", ""))
    ap.add_argument("--window", type=int, default=90,
                    help="days of dispatch history to measure (default 90)")
    ap.add_argument("--days-cover", type=int, default=45,
                    help="days of stock the level should hold (default 45)")
    ap.add_argument("--to", default="", help="window end, YYYY-MM-DD (default today)")
    ap.add_argument("--demand", choices=["trade", "all"], default="trade")
    ap.add_argument("--round", type=float, default=0.5,
                    help="round levels UP to this step (default 0.5)")
    ap.add_argument("--chunk-days", type=int, default=7)
    ap.add_argument("--cache", default="out/reorder_cache")
    ap.add_argument("--csv", default="out/reorder_proposal.csv")
    ap.add_argument("--groups", default=",".join(FINISHED_GROUPS),
                    help="stock groups to propose levels for; 'all' to filter "
                         "nothing (default: the five finished-goods groups)")
    ap.add_argument("--push", action="store_true",
                    help="write proposals into Frappe beside the current level")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    to = date.fromisoformat(a.to) if a.to else date.today()
    frm = to - timedelta(days=a.window - 1)

    cfg = TallyConfig(host=a.host, port=a.port, company=a.company, timeout=280)
    types = TRADE_TYPES + OTHER_OUTWARD_TYPES
    moves = collect(cfg, frm, to, types, a.chunk_days, a.cache)
    demand = demand_from_movements(moves)

    rows = propose(demand, a.window, a.days_cover, a.round, a.demand)

    fcfg = frappe_from_env()
    if fcfg is None:
        log.error("FRAPPE_URL / FRAPPE_API_KEY / FRAPPE_API_SECRET not set — "
                  "cannot read current levels; writing demand only.")
        rows = merge_current(rows, {}, {}, {})
    else:
        fc = FrappeClient(fcfg)
        levels, groups, base, names = load_reference(fc)
        rows = merge_current(rows, levels, groups, base)
        if a.push:
            push_proposals(fc, rows, names, a.window, a.days_cover, to)

    if a.groups.lower() != "all":
        # Compared on a leading-dot-stripped name: the book carries a stray
        # `.Cycling Short` group whose rows are ordinary cycling shorts.
        want = {g.strip().lstrip(".").lower() for g in a.groups.split(",")}
        before = len(rows)
        rows = [r for r in rows
                if (r["stock_group"] or "").lstrip(".").lower() in want]
        log.info("Scoped to %s: %d rows of %d", a.groups, len(rows), before)

    write_csv(rows, a.csv)
    print()
    print(summarise(rows, a.window, a.days_cover))
    return 0


def push_proposals(fc: FrappeClient, rows: list[dict], names: dict,
                   window_days: int, days_cover: int, as_of: date) -> None:
    """
    Park the proposal beside the exported level, never on top of it.

    `reorder_level` holds what Tally says and is the baseline reorder_refresh
    brings forward; overwriting it with a proposal would make the dashboard
    disagree with the shop floor's own report and silently re-baseline the
    daily refresh. The proposal lands in its own fields and stays advisory
    until someone types it into Tally and re-exports.
    """
    sent = 0
    for r in rows:
        name = names.get((r["item_name"], r["size"]))
        if not name:
            continue
        fc._call("PUT", f"/api/resource/{LEVEL_DOCTYPE}/{name}", json={
            "proposed_level": r["proposed_level"],
            "daily_demand": r["daily_demand"],
            "demand_window_days": window_days,
            "days_cover": days_cover,
            "proposed_as_of": as_of.isoformat(),
        })
        sent += 1
    log.info("Pushed %d proposals to Frappe", sent)


if __name__ == "__main__":
    sys.exit(main())
