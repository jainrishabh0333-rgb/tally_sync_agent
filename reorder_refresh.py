"""
reorder_refresh.py — keep the reorder report current without re-exporting.

The Tally Reorder Report is exported by hand, per stock group, and its rows
land in Frappe as `Tally Reorder Level`. Four of its columns go stale the
moment the next voucher is punched; only REORDER LEVEL is a standing decision.
This job refreshes the four and leaves the fifth alone.

    ITEM | Group | SIZE | IN STOCK | UNPACK QTY | STITCHING | PENDING ORDER |
    REORDER LEVEL | DEFICIT /SURPLUS

    deficit = in stock + unpack + stitching - pending order - reorder level

BASELINE + DELTA, not a full rebuild
------------------------------------
Deriving stock from scratch means replaying every inventory voucher of the
financial year — hundreds of megabytes, and slow. Unnecessary here: the export
already carries in-stock / unpack / stitching AS AT ITS EXPORT TIME. So the
stored row is the baseline and this job applies only the movements since, which
is a few days of data.

The consequence worth understanding: accuracy is anchored to the last export.
Re-exporting a group re-baselines it and clears any accumulated drift, and that
happens naturally whenever levels are revised. `--max-age-days` refuses to
refresh a baseline so old the delta can no longer be trusted, rather than
quietly reporting a stale figure as current.

WHY NOT the size-wise stock in Tally
------------------------------------
Measured 2026-08-21: per-size closing stock cannot be read from masters on this
build. A BatchAllocations walk answers ClosingBalance with the ITEM total
repeated on every size, and masters list only batches carrying an opening
balance. Movement summation is the only honest route, which is what
reorder_fetch.py does and what this job applies incrementally.

Run:
    python reorder_refresh.py --dry-run          # show what would change
    python reorder_refresh.py                    # write it back
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from tally_client import TallyConfig, _conversion_to, fetch_units
from reorder_fetch import (
    BUCKET_STITCHING,
    BUCKET_STOCK,
    BUCKET_UNPACK,
    BUCKET_WIP,
    fetch_godown_parents,
    fetch_movements,
)

log = logging.getLogger("sync")

LEVEL_DOCTYPE = "Tally Reorder Level"

# Columns this job owns. REORDER LEVEL is deliberately absent: it is a
# management decision and only an export may change it.
REFRESHED = ("in_stock", "unpack_qty", "stitching", "pending_order", "deficit")


# ---------------------------------------------------------------------------
# Reading the baseline
# ---------------------------------------------------------------------------

def fetch_baseline(fc, groups: list[str] | None = None) -> list[dict]:
    """Every stored reorder row, with the export timestamp that anchors it."""
    filters = None
    if groups:
        filters = f'[["stock_group","in",{groups!r}]]'.replace("'", '"')
    params = {
        "fields": '["name","item_name","size","stock_group","in_stock",'
                  '"unpack_qty","stitching","pending_order","reorder_level",'
                  '"deficit","as_of"]',
        "limit_page_length": 0,
    }
    if filters:
        params["filters"] = filters
    rows = fc._call("GET", f"/api/resource/{LEVEL_DOCTYPE.replace(' ', '%20')}",
                    params=params).get("data", [])
    log.info("Loaded %d reorder rows from Frappe", len(rows))
    return rows


def baseline_date(rows: list[dict]) -> date | None:
    """
    The OLDEST export among the rows being refreshed.

    Oldest, not newest: movements are applied from this date forward, and
    starting later than a row's own baseline would drop that row's movements
    silently. Applying a movement twice is visible and fixable; never applying
    it is not.
    """
    stamps = []
    for r in rows:
        raw = r.get("as_of")
        if not raw:
            continue
        try:
            stamps.append(datetime.fromisoformat(str(raw)).date())
        except ValueError:
            continue
    return min(stamps) if stamps else None


# ---------------------------------------------------------------------------
# Applying movements
# ---------------------------------------------------------------------------

def movement_deltas(cfg: TallyConfig, frm: date, to: date) -> dict:
    """
    {(item, size): {bucket: qty}} of movement since the baseline.

    Quantities are normalised to Doz, the unit the report is stated in.
    Anything that cannot be converted is dropped and counted rather than
    added raw — mixing Box and Doz into one figure would be silently wrong.
    """
    # An empty window is the normal state on the day the levels were exported:
    # the baseline is today, so the first day to apply is tomorrow. Asking
    # Tally for "tomorrow to today" returns nothing anyway, but it costs three
    # requests and writes a date range that reads backwards in the log —
    # which is exactly the sort of line that sends someone hunting a bug at
    # 7am. Say plainly that there is nothing to apply instead.
    if frm > to:
        log.info("Baseline is current as of %s — no movements to apply.", to)
        return {}

    units = {u.name: u for u in fetch_units(cfg)}
    parents = fetch_godown_parents(cfg)
    moves = fetch_movements(cfg, frm, to, units, parents)

    deltas: dict = defaultdict(lambda: defaultdict(float))
    unconvertible = 0
    for m in moves:
        if not m.size_batch:
            continue
        if m.unit == "Doz":
            factor = 1.0
        else:
            factor = _conversion_to(m.unit, "Doz", units)
            if factor is None:
                back = _conversion_to("Doz", m.unit, units)
                factor = (1.0 / back) if back else None
        if factor is None:
            unconvertible += 1
            continue
        deltas[(m.item_name, m.size_batch)][m.bucket] += m.qty * factor

    if unconvertible:
        log.warning("%d movements skipped: no conversion to Doz", unconvertible)
    log.info("Applied %d movements across %d item/size pairs",
             len(moves), len(deltas))
    return deltas


def refreshed_rows(baseline: list[dict], deltas: dict,
                   pending: dict | None = None) -> list[dict]:
    """
    Baseline plus delta, with the deficit recomputed.

    WIP (pressing / packing) is folded into stitching, matching how the report
    presents pipeline stock: goods part-made are not sellable but must not be
    re-cut either.
    """
    out = []
    for row in baseline:
        key = (row["item_name"], str(row["size"]))
        d = deltas.get(key) or {}
        in_stock = (row["in_stock"] or 0.0) + d.get(BUCKET_STOCK, 0.0)
        unpack = (row["unpack_qty"] or 0.0) + d.get(BUCKET_UNPACK, 0.0)
        stitching = ((row["stitching"] or 0.0)
                     + d.get(BUCKET_STITCHING, 0.0) + d.get(BUCKET_WIP, 0.0))
        pend = (pending or {}).get(key, row["pending_order"] or 0.0)
        level = row["reorder_level"] or 0.0
        new = {
            "name": row["name"],
            "item_name": row["item_name"],
            "size": row["size"],
            "stock_group": row.get("stock_group") or "",
            "in_stock": round(in_stock, 2),
            "unpack_qty": round(unpack, 2),
            "stitching": round(stitching, 2),
            "pending_order": round(pend, 2),
            "reorder_level": level,
            "deficit": round(in_stock + unpack + stitching - pend - level, 2),
        }
        new["changed"] = any(
            abs(new[f] - (row.get(f) or 0.0)) > 0.01 for f in REFRESHED)
        out.append(new)
    return out


# ---------------------------------------------------------------------------
# Writing back
# ---------------------------------------------------------------------------

def write_back(fc, rows: list[dict], dry_run: bool = False) -> int:
    """
    Update only rows whose numbers actually moved.

    Uses the plain REST resource API rather than a custom endpoint, so this
    needs no new code on the Frappe side — the sync user already has write
    access there.
    """
    changed = [r for r in rows if r["changed"]]
    if dry_run:
        log.info("[dry run] %d of %d rows would change", len(changed), len(rows))
        return len(changed)
    written = 0
    for r in changed:
        payload = {f: r[f] for f in REFRESHED}
        payload["as_of"] = datetime.now().isoformat(timespec="seconds")
        fc._call("PUT",
                 f"/api/resource/{LEVEL_DOCTYPE.replace(' ', '%20')}/"
                 f"{r['name'].replace(' ', '%20').replace('/', '%2F')}",
                 json=payload)
        written += 1
    log.info("Updated %d of %d reorder rows", written, len(rows))
    return written


# ---------------------------------------------------------------------------
# Report, in the source report's own column order
# ---------------------------------------------------------------------------

def format_report(rows: list[dict], limit: int = 40, shortfall_only: bool = True) -> str:
    """The rebuilt rows laid out as the Tally report lays them out."""
    body = [r for r in rows if r["deficit"] < 0] if shortfall_only else list(rows)
    body.sort(key=lambda r: r["deficit"])
    head = (f"{'ITEM':<32}{'GROUP':<14}{'SIZE':>5}{'IN STOCK':>10}"
            f"{'UNPACK':>9}{'STITCHING':>11}{'PENDING':>9}"
            f"{'LEVEL':>9}{'DEFICIT':>10}")
    lines = [head, "-" * len(head)]
    for r in body[:limit]:
        lines.append(
            f"{r['item_name'][:31]:<32}{r['stock_group'][:13]:<14}"
            f"{str(r['size']):>5}{r['in_stock']:>10.2f}{r['unpack_qty']:>9.2f}"
            f"{r['stitching']:>11.2f}{r['pending_order']:>9.2f}"
            f"{r['reorder_level']:>9.2f}{r['deficit']:>10.2f}")
    if len(body) > limit:
        lines.append(f"... {len(body) - limit} more")
    lines.append("")
    lines.append(f"{len(body)} of {len(rows)} sizes below reorder level")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def refresh(cfg: TallyConfig, fc, groups=None, max_age_days: int = 45,
            dry_run: bool = False, as_on: date | None = None) -> list[dict]:
    baseline = fetch_baseline(fc, groups)
    if not baseline:
        log.warning("No reorder rows stored — export the report from Tally first.")
        return []

    frm = baseline_date(baseline)
    to = as_on or date.today()
    if frm is None:
        log.warning("No baseline timestamps; cannot apply movements safely.")
        return []
    age = (to - frm).days
    if age > max_age_days:
        raise RuntimeError(
            f"Baseline is {age} days old (limit {max_age_days}). Re-export the "
            f"Reorder Report from Tally to re-baseline rather than trusting a "
            f"delta this long. Raise --max-age-days only if you accept the drift."
        )
    log.info("Baseline %s, refreshing to %s (%d days)", frm, to, age)

    deltas = movement_deltas(cfg, frm + timedelta(days=1), to)
    rows = refreshed_rows(baseline, deltas)
    write_back(fc, rows, dry_run=dry_run)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--groups", nargs="*", default=None)
    ap.add_argument("--max-age-days", type=int, default=45)
    ap.add_argument("--show", type=int, default=25, help="rows to print")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Same config.toml, same Settings object, as every other job here.
    from sync import load_settings, resolve_companies
    from frappe_client import FrappeClient

    st = load_settings()
    fc = FrappeClient(st.frappe)

    tally_cfg = st.tally
    if not tally_cfg.company:
        companies = resolve_companies(st)
        if not companies:
            raise SystemExit("No company configured or open in Tally.")
        # Reorder levels are per item+size and not company-scoped in the
        # report, so refreshing against more than one book would double-count.
        tally_cfg = dataclasses.replace(tally_cfg, company=companies[0])
        log.info("Using company %r", tally_cfg.company)

    rows = refresh(tally_cfg, fc, groups=args.groups,
                   max_age_days=args.max_age_days, dry_run=args.dry_run)
    if rows:
        print()
        print(format_report(rows, limit=args.show))


if __name__ == "__main__":
    main()
