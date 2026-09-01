"""
reorder_seed.py — re-baseline the reorder table from Tally primitives.

The stored `Tally Reorder Level` rows are anchored to a hand-exported
Reorder Report. This job removes that dependency: it derives the three
stock columns from Tally's own primitives — opening batch balances plus a
full replay of every stock-moving voucher since 1 April — and rewrites the
baseline. After one successful run, the daily `reorder_refresh` carries the
derived baseline forward and NOBODY ever needs to export the report again
for stock to be right. (REORDER LEVEL itself stays a management decision:
this job never touches `reorder_level` or the proposed_* columns.)

The method is the one validated 2026-08-21/25 in `reorder_fetch`:

  * closing(item, size, godown) = opening + sum of signed movements, which
    reconciled 2,906/2,906 items against Tally's own closing balances and
    matched the report's STITCHING column to the decimal on spot checks;
  * godowns collapse onto IN STOCK / UNPACK / STITCHING via Tally's own
    godown HIERARCHY (classify_godown + fetch_godown_parents) — never by
    leaf name alone;
  * PENDING ORDER comes from the mirrored sales-order lines and is deduped
    with the audited restatement rule (identical item/size/qty/rate tuple
    within a (party, base order) family counts once — measured 2026-09-01:
    ~6,070 phantom units otherwise).

RUN IT ON THE TALLY BOX (localhost:9000), Tally open, ideally off-hours:
the full-year movement pull is a few hundred MB in chunks and takes
~15 minutes. `--dry-run` derives and reports but writes nothing.

    python reorder_seed.py --dry-run
    python reorder_seed.py

Rows are UPDATED where a (item, size) row exists and CREATED (level 0,
which the planner shows as "no level set") where sizes have stock, WIP or
orders but no row — the report never exported those, so the planner was
blind to them.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from collections import defaultdict
from datetime import date, datetime

from reorder_fetch import (
    BUCKET_STITCHING,
    BUCKET_STOCK,
    BUCKET_UNPACK,
    BUCKET_WIP,
    classify_godown,
    fetch_godown_parents,
)
from reorder_refresh import LEVEL_DOCTYPE, collect_movements, fetch_baseline
from tally_client import (
    TallyConfig,
    _fetch_master,
    _parse_qty,
    _text,
    fetch_units,
)

log = logging.getLogger("sync")

SNAP = ("in_stock", "unpack_qty", "stitching", "pending_order", "deficit")


# ---------------------------------------------------------------------------
# Opening balances, per (item, size, godown)
# ---------------------------------------------------------------------------

def fetch_openings(cfg: TallyConfig, units: dict | None,
                   fy_start: date) -> dict:
    """
    {(item, size, godown): qty} at the financial-year start.

    Masters carry opening batches only — a size that opened at zero is
    absent, which is correct: movements build it up from zero. The period is
    pinned to the FY start so OPENINGBALANCE answers the year's opening, not
    whatever period the session last held.
    """
    els = _fetch_master(
        cfg, "TB_SeedOpenings", "StockItem", "STOCKITEM",
        ["Name"],
        ["BatchAllocations.BatchName", "BatchAllocations.GodownName",
         "BatchAllocations.OpeningBalance"],
        frm=fy_start, to=fy_start,
    )
    opening: dict = defaultdict(float)
    batches = 0
    for el in els:
        item = el.get("NAME") or _text(el.find("NAME"))
        if not item:
            continue
        for b in el.findall("BATCHALLOCATIONS.LIST"):
            qty, _u, _raw = _parse_qty(_text(b.find("OPENINGBALANCE")), units)
            if not qty:
                continue
            opening[(item, _text(b.find("BATCHNAME")),
                     _text(b.find("GODOWNNAME")))] += qty
            batches += 1
    log.info("Openings: %d items, %d opening batches", len(els), batches)
    return dict(opening)


# ---------------------------------------------------------------------------
# Pending orders from the mirror, deduped (the 1-Sep audited rule)
# ---------------------------------------------------------------------------

def pending_deduped(fc) -> dict:
    """
    {(item, size): pending} from mirrored open orders.

    Keeps Open/Partial, non-cancelled, non-optional, non-test orders (the
    order_status STRING is the operative signal — is_cancelled is 0 even on
    Cancelled rows). Within a family keyed (party, base_order_no), one copy
    of each identical (item, size, qty, rate) line: restatement chains and
    duplicated blocks count once; genuine R-remainders differ in qty and
    survive untouched.
    """
    orders = fc._call("GET", "/api/resource/Tally%20Sales%20Order", params={
        "fields": '["name","voucher_number","base_order_no","party",'
                  '"order_status","is_cancelled","is_optional"]',
        "limit_page_length": 0}).get("data", [])
    keep = {}
    for o in orders:
        if (o.get("order_status") in ("Open", "Partial")
                and not o.get("is_cancelled") and not o.get("is_optional")
                and "test" not in (o.get("party") or "").lower()
                and "test" not in (o.get("voucher_number") or "").lower()):
            keep[o["name"]] = ((o.get("party") or "").strip(),
                              (o.get("base_order_no")
                               or o.get("voucher_number") or "").strip())
    lines = fc._call("GET", "/api/resource/Tally%20Sales%20Order%20Line",
                     params={
                         "fields": '["parent","item_name","size_batch",'
                                   '"pending_qty","qty","rate"]',
                         "parent": "Tally Sales Order",
                         "filters": '[["pending_qty",">",0]]',
                         "limit_page_length": 0}).get("data", [])
    seen: set = set()
    pending: dict = defaultdict(float)
    dropped = 0.0
    for ln in sorted(lines, key=lambda x: x.get("parent") or ""):
        fam = keep.get(ln.get("parent"))
        qty = float(ln.get("pending_qty") or 0)
        if not fam or qty <= 0:
            continue
        key = ((ln.get("item_name") or "").strip(),
               (ln.get("size_batch") or "").strip())
        tup = (fam, key, float(ln.get("qty") or 0), float(ln.get("rate") or 0))
        if tup in seen:
            dropped += qty
            continue
        seen.add(tup)
        pending[key] += qty
    log.info("Pending: %d keys from %d kept orders; %.1f units dropped as "
             "duplicate/restated", len(pending), len(keep), dropped)
    return dict(pending)


# ---------------------------------------------------------------------------
# The seed itself
# ---------------------------------------------------------------------------

def seed(cfg: TallyConfig, fc, dry_run: bool = False,
         as_on: date | None = None) -> dict:
    as_on = as_on or date.today()
    fy_start = date(as_on.year if as_on.month >= 4 else as_on.year - 1, 4, 1)

    units = {u.name: u for u in fetch_units(cfg)}
    parents = fetch_godown_parents(cfg)
    opening = fetch_openings(cfg, units, fy_start)
    moves = collect_movements(cfg, fy_start, as_on)

    # Stock group for rows this run CREATES — the mirror already knows it.
    group_of = {r["item_name"]: r.get("stock_group") or ""
                for r in fc._call(
                    "GET", "/api/resource/Tally%20Stock%20Item", params={
                        "fields": '["item_name","stock_group"]',
                        "filters": f'[["company","=","{cfg.company}"]]',
                        "limit_page_length": 0}).get("data", [])}

    totals: dict = defaultdict(float)
    for (item, size, godown), qty in opening.items():
        totals[(item, size, classify_godown(godown, parents))] += qty
    for m in moves:
        totals[(m.item_name, m.size_batch, m.bucket)] += m.qty

    pending = pending_deduped(fc)

    baseline = fetch_baseline(fc)
    existing = {(r["item_name"], str(r["size"])): r for r in baseline}

    keys = ({(i, s) for (i, s, _b) in totals} | set(pending)) - {("", "")}
    now = datetime.now().isoformat(timespec="seconds")
    updated = created = skipped = 0
    for item, size in sorted(keys):
        in_stock = round(totals.get((item, size, BUCKET_STOCK), 0.0), 2)
        unpack = round(totals.get((item, size, BUCKET_UNPACK), 0.0), 2)
        # Owner's rule: pressing/packing-in-progress is WIP alongside
        # stitching — the report's STITCHING column carries both.
        stitching = round(totals.get((item, size, BUCKET_STITCHING), 0.0)
                          + totals.get((item, size, BUCKET_WIP), 0.0), 2)
        pend = round(pending.get((item, size), 0.0), 2)
        row = existing.get((item, str(size)))
        level = float(row["reorder_level"] or 0) if row else 0.0
        payload = {
            "in_stock": in_stock, "unpack_qty": unpack, "stitching": stitching,
            "pending_order": pend,
            "deficit": round(in_stock + unpack + stitching - pend - level, 2),
            "as_of": now,
        }
        if row:
            unchanged = all(abs(payload[f] - float(row.get(f) or 0)) <= 0.01
                            for f in SNAP)
            if unchanged:
                skipped += 1
                continue
            if not dry_run:
                fc._call("PUT",
                         f"/api/resource/{LEVEL_DOCTYPE.replace(' ', '%20')}/"
                         f"{row['name'].replace(' ', '%20').replace('/', '%2F')}",
                         json=payload)
            updated += 1
        else:
            if not (in_stock or unpack or stitching or pend):
                skipped += 1
                continue
            payload.update({"item_name": item, "size": size,
                            "company": cfg.company, "reorder_level": 0.0,
                            "stock_group": group_of.get(item, "")})
            if not dry_run:
                fc._call("POST",
                         f"/api/resource/{LEVEL_DOCTYPE.replace(' ', '%20')}",
                         json=payload)
            created += 1

    log.info("%sSeed: %d updated, %d created, %d unchanged/empty (of %d keys)",
             "[dry run] " if dry_run else "", updated, created, skipped,
             len(keys))
    return {"updated": updated, "created": created, "skipped": skipped}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from sync import load_settings, resolve_companies
    from frappe_client import FrappeClient

    st = load_settings()
    fc = FrappeClient(st.frappe)
    cfg = st.tally
    if not cfg.company:
        companies = resolve_companies(st)
        if not companies:
            raise SystemExit("No company configured or open in Tally.")
        cfg = dataclasses.replace(cfg, company=companies[0])
        log.info("Using company %r", cfg.company)
    seed(cfg, fc, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
