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

try:  # Python 3.11+ has tomllib; fall back to tomli if present.
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore

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

# Tally's placeholder batch name, used when an item is billed without a batch.
# It is NOT a size, and a level proposed against it is a level against
# nothing: measured 2026-08-23, `PANTY (PCS)` carried one of 2,029 — the
# single largest row in the sheet — while three sibling rows netted NEGATIVE
# dispatch. Dropped from proposals, but counted and logged, because for most
# items it is entirely correct: fabric, raw material and packing stock are not
# size-tracked and have no batch to give.
UNSIZED_BATCHES = {"primary batch"}

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
    trade_wqty: float = 0.0     # the same, each line scaled by its day's weight
    other_wqty: float = 0.0
    units_seen: frozenset = frozenset()

    @property
    def all_qty(self) -> float:
        return self.trade_qty + self.other_qty

    @property
    def all_wqty(self) -> float:
        return self.trade_wqty + self.other_wqty


def day_weights(frm: date, to: date, half_life: int) -> dict:
    """
    {iso date: weight} across the window, halving every `half_life` days.

    Exponential decay rather than a shorter window, because those fail
    differently. A short window swings a level on one slow fortnight; decay
    keeps every day's evidence and only discounts it, so a style with a long
    steady history and a soft month drifts down instead of collapsing.

    `half_life <= 0` returns weight 1.0 for every day — the flat average,
    which is the default and reduces propose() to plain qty / window_days
    EXACTLY, not approximately. That equality is asserted in the tests: a
    weighting scheme that quietly moves the unweighted answer would re-price
    1,093 live levels as a side effect of adding a flag nobody switched on.
    """
    span = (to - frm).days
    out = {}
    for i in range(span + 1):
        d = frm + timedelta(days=i)
        age = (to - d).days
        out[d.isoformat()] = 1.0 if half_life <= 0 else 0.5 ** (age / half_life)
    return out


def demand_from_movements(moves: list, weights: dict | None = None) -> dict:
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
    unsized = 0
    unsized_qty = 0.0
    for m in moves:
        if not m.size_batch:
            continue
        if m.size_batch.strip().lower() in UNSIZED_BATCHES:
            unsized += 1
            unsized_qty += -m.qty
            continue
        key = (m.item_name, m.size_batch)
        d = by_key.get(key)
        if d is None:
            d = by_key[key] = Demand(item_name=m.item_name, size=m.size_batch,
                                     unit=m.unit)
        if m.unit:
            units_seen[key].add(m.unit)
        # A movement dated outside the window cannot be weighted, so it is
        # not silently given weight 1.0 — that would let a stray voucher from
        # a neighbouring chunk count as if it happened today.
        w = 1.0 if weights is None else weights.get(m.date, 0.0)
        if m.voucher_type in trade:
            d.trade_qty += -m.qty
            d.trade_wqty += -m.qty * w
        else:
            d.other_qty += -m.qty
            d.other_wqty += -m.qty * w
    for key, seen in units_seen.items():
        by_key[key].units_seen = frozenset(seen)
    if unsized:
        # Deliberately log.info, not warning. Most of this total is fabric,
        # raw material and packing stock, which are not size-tracked at all
        # and SHOULD carry a placeholder — measured 2026-08-23, 341,825 units
        # of it against only 4 finished-goods rows. Crying wolf at every run
        # would train the reader to skip the line that matters.
        log.info("%d line(s) totalling %.0f carry a placeholder batch rather "
                 "than a size and get no level. Normal for goods that are not "
                 "size-tracked; only worth chasing for finished goods.",
                 unsized, unsized_qty)
    return by_key


def round_up(value: float, step: float) -> float:
    """Round a level UP to the next step. A level is a floor, so never down."""
    if step <= 0:
        return value
    return math.ceil(round(value / step, 6)) * step


def propose(demand: dict, window_days: int, days_cover: int,
            step: float = 0.5, mode: str = "trade",
            weight_total: float = 0.0) -> list[dict]:
    """
    One proposal row per (item, size). Pure — no Tally, no Frappe.

    `mode` picks which measured demand drives the level; the other channel is
    still carried onto the row so the sheet can show what the choice costs.

    `weight_total` is sum(day_weights) across the window and is the divisor
    for the weighted total — the weighted MEAN of daily demand, so days with
    no dispatch still sit in the denominator. Divide by the number of days
    that happened to have a sale instead and every intermittent style reads
    as a fast mover. Pass 0 for the flat average over window_days.

    Both rates land on the row. `daily_flat` is the plain average and
    `daily_demand` is whichever drives the level, so `trend` (their ratio)
    reads off the sheet directly: above 1 means the style is accelerating and
    a flat average would have under-levelled it.
    """
    rows = []
    for (item, size), d in demand.items():
        basis = max(0.0, d.trade_qty if mode == "trade" else d.all_qty)
        flat = basis / window_days if window_days else 0.0
        if weight_total > 0:
            wbasis = max(0.0, d.trade_wqty if mode == "trade" else d.all_wqty)
            daily = wbasis / weight_total
        else:
            daily = flat
        rows.append({
            "item_name": item,
            "size": size,
            "unit": d.unit,
            "trade_qty": round(d.trade_qty, 2),
            "other_qty": round(d.other_qty, 2),
            "basis_qty": round(basis, 2),
            "daily_demand": round(daily, 4),
            "daily_flat": round(flat, 4),
            "trend": round(daily / flat, 3) if flat else None,
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
            "daily_demand": 0.0, "daily_flat": 0.0, "trend": None,
            "proposed_level": 0.0,
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

    NEVER read this cache by globbing it. A file is keyed by its exact date
    range, so changing --chunk-days leaves the OLD chunking in place beside
    the new one and both cover the same days. This function is safe because it
    reads only the filenames its own date_chunks() produces; anything that
    globs `mv_*.xml` double-counts every overlapping day. Measured 2026-08-23:
    43 files for a 90-day window (a 3-day and a 7-day chunking), and a script
    that globbed them reported exactly twice the real dispatch -- a number
    wrong by a clean factor, which is the kind that reads as plausible.
    """
    rf.STOCK_VOUCHER_TYPES = types
    moves = []
    chunks = list(date_chunks(frm, to, chunk_days))
    if cache_dir and os.path.isdir(cache_dir):
        wanted = {f"mv_{a}_{b}.xml" for a, b in chunks}
        stale = [f for f in os.listdir(cache_dir)
                 if f.startswith("mv_") and f not in wanted]
        if stale:
            log.warning("%d cached chunk(s) in %s are from a DIFFERENT "
                        "--chunk-days and are ignored here. They overlap these "
                        "dates: do not glob this directory.",
                        len(stale), cache_dir)
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

def _decode_toml(raw: bytes) -> str:
    """
    Decode config.toml the way sync.py does, because Notepad wrote it.

    `tomllib.load()` on the raw handle is WRONG here and fails with
    "Invalid statement (at line 1, column 1)" — measured on the Tally box
    2026-08-23. That file is edited in Notepad on Windows, which saves a
    UTF-8 BOM by default and offers "Unicode" (UTF-16) and "ANSI" (cp1252)
    besides. The BOM lands as an invisible character before the first
    section header, so the error names line 1 and points at nothing visible.

    sync.py has always handled this; this function was written later and did
    not, so the agent read its own config on the Mac (no file at all) and
    failed on the one machine that has it.
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):   # UTF-16, "Unicode" in Notepad
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")   # Notepad "ANSI"
    return text.replace("\r\n", "\n").lstrip("\ufeff")


def _config_toml() -> dict:
    """
    config.toml beside this file, or {} — never an exit.

    Read directly rather than through sync.load_settings(), which validates
    the whole sync configuration and sys.exit()s on anything missing. This
    script has to run in two places with different halves of that config
    present: the Mac reaches Tally over Tailscale and carries Frappe keys in
    the environment with no config.toml at all, while the Tally box has the
    file and nothing in the environment. Borrowing the strict loader would
    make each of them fail on the other's missing half.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "config.toml")
    if not os.path.exists(path) or tomllib is None:
        return {}
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        return tomllib.loads(_decode_toml(raw))
    except (OSError, ValueError) as exc:
        # A permission error here is expected on the Tally box, where
        # config.toml is owned by an account whose ACL excludes it. Say so
        # instead of falling through to "keys not set", which sends the
        # reader looking for a config problem that does not exist.
        log.warning("Could not read config.toml (%s) — falling back to the "
                    "environment.", exc)
        return {}


def frappe_config(cfg_toml: dict | None = None) -> FrappeConfig | None:
    """Frappe credentials from the environment, else from config.toml."""
    f = (cfg_toml or {}).get("frappe", {})
    url = os.environ.get("FRAPPE_URL", "") or f.get("url", "")
    key = os.environ.get("FRAPPE_API_KEY", "") or f.get("api_key", "")
    sec = os.environ.get("FRAPPE_API_SECRET", "") or f.get("api_secret", "")
    if not (url and key and sec):
        return None
    return FrappeConfig(url=str(url).rstrip("/"), api_key=str(key),
                        api_secret=str(sec))


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
           "trade_qty", "other_qty", "basis_qty", "daily_flat",
           "daily_demand", "trend", "proposed_level", "current_level",
           "change", "status", "mixed_units"]


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
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--company", default="")
    ap.add_argument("--window", type=int, default=90,
                    help="days of dispatch history to measure (default 90)")
    ap.add_argument("--days-cover", type=int, default=45,
                    help="days of stock the level should hold (default 45)")
    ap.add_argument("--to", default="", help="window end, YYYY-MM-DD (default today)")
    ap.add_argument("--demand", choices=["trade", "all"], default="trade")
    ap.add_argument("--half-life", type=int, default=0, metavar="DAYS",
                    help="weight recent dispatch more heavily, halving every "
                         "DAYS. 0 (default) is a flat average over the whole "
                         "window. 30 on a 90-day window gives the last month "
                         "about twice the pull of the month before it.")
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

    conf = _config_toml()
    t = conf.get("tally", {})
    cfg = TallyConfig(
        host=a.host or os.environ.get("TALLY_HOST") or t.get("host", "localhost"),
        port=a.port or int(os.environ.get("TALLY_PORT") or t.get("port", 9000)),
        company=(a.company or os.environ.get("TALLY_COMPANY")
                 or t.get("company") or (t.get("companies") or [""])[0]),
        timeout=280,
    )
    log.info("Tally %s:%s company=%r", cfg.host, cfg.port, cfg.company)
    types = TRADE_TYPES + OTHER_OUTWARD_TYPES
    moves = collect(cfg, frm, to, types, a.chunk_days, a.cache)
    weights = day_weights(frm, to, a.half_life) if a.half_life > 0 else None
    weight_total = sum(weights.values()) if weights else 0.0
    if weights:
        log.info("Recency weighting: half-life %d days, effective window "
                 "%.1f days of %d", a.half_life, weight_total, a.window)
    demand = demand_from_movements(moves, weights)

    rows = propose(demand, a.window, a.days_cover, a.round, a.demand,
                   weight_total)

    fcfg = frappe_config(conf)
    if fcfg is None:
        log.error("No Frappe credentials in the environment or config.toml — "
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
