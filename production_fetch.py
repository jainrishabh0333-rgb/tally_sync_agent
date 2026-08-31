"""
production_fetch.py — the factory's own vouchers, with their item lines.

Why this file has to exist
--------------------------
The mirror stores vouchers as LEDGER entries. Every voucher the factory posts
— `Stock Issue-Cutting`, `Stock Journal-Cutting`, `Job Work-Out`, `Pressing`,
`Stock Journal-Packed` — carries no ledger value at all: measured over
1 May..27 Aug 2026, all 929 cutting issues, 285 cutting journals, 433 job-work
vouchers and 916 packing journals total exactly 0.00. So the mirror has been
faithfully recording that the factory did 3,000 things worth nothing each,
and the entire production flow is invisible to it.

This fetches the same vouchers with their INVENTORY lines, which is where the
fabric, the garment, the size and the godown actually live.

What it unlocks
---------------
  * What is being cut, and what came back.
  * WIP by stage, from real movements rather than one column of a report
    that is only as fresh as the last time somebody exported it.
  * **Fabric consumed per garment** — a cutting voucher has the fabric going
    in and the garments coming out, on the same document. That is where
    `Style Fabric Norm` comes from, and it is the reason nobody has to type
    862 numbers.

The shape question this cannot answer from here
-----------------------------------------------
A Tally stock journal can express its two sides in three different ways, and
which one THIS build uses could not be checked when this was written — Tally
is open working hours only and it was 22:00. So all three are requested, all
three are parsed, and every line records the tag it came out of:

    INVENTORYENTRIESIN.LIST     -> Produced   (what the journal made)
    INVENTORYENTRIESOUT.LIST    -> Consumed   (what it ate)
    ALLINVENTORYENTRIES.LIST    -> sign of ACTUALQTY decides

The first real run settles it, and `--probe` prints which tag answered
without writing anything. This follows the same "try rich, measure, log the
downgrade" rule the invoice dispatch fields already use, for the same reason:
guessing a Tally shape and shipping it is how you get 475 rows of zeroes.

Run:
    python production_fetch.py --probe            # what shape does this build use
    python production_fetch.py --days 120         # fetch and push
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from distributor_fetch import _BASE, _header, _voucher_body        # noqa: E402
from tally_client import (                                          # noqa: E402
    TallyConfig, _parse_xml, _post, _text, assert_company_loaded,
    _parse_qty, _to_float,
)

log = logging.getLogger("production_fetch")

# Tally's own voucher-type names, mapped onto the sequence the floor works in.
# The map is the ONLY place Tally's naming is hard-coded; everything
# downstream reads `stage`.
STAGES = {
    "Stock Issue-Cutting": "Cutting Issue",
    "Stock Journal-Cutting": "Cutting",
    "Job Work-Out": "Job Work Out",
    "Job Work - (Dying Outward)": "Dyeing Out",
    "Job Work - (Dying Received)": "Dyeing In",
    "Pressing": "Pressing",
    "Stock Journal-Packed": "Packed",
    "Stock Journal-Unpacked": "Unpacked",
    "Stock Transfer": "Transfer",
    "Stock Journal": "Other",
}

# Each of the three shapes, and what a line found under it means.
_LISTS = (
    ("INVENTORYENTRIESIN.LIST", "Produced"),
    ("INVENTORYENTRIESOUT.LIST", "Consumed"),
    ("ALLINVENTORYENTRIES.LIST", ""),   # "" = decide from the sign
)

_ENTRY_FIELDS = ",".join(
    f"{p}.{f}"
    for p in ("AllInventoryEntries", "InventoryEntriesIn", "InventoryEntriesOut")
    for f in ("StockItemName", "ActualQty", "Rate", "Amount",
              "BatchAllocations.BatchName", "BatchAllocations.ActualQty",
              "BatchAllocations.GodownName")
)
_PRODUCTION = _BASE + ",Narration,IsCancelled,AlterID," + _ENTRY_FIELDS


def _lines(vel) -> list[dict]:
    """
    Every inventory line on one voucher, with its direction settled.

    Quantities come out POSITIVE with the meaning carried in `direction`, so
    nothing downstream has to remember whether this build signs a consumption
    negative. `source_tag` records which list answered — that is the evidence
    for which shape this build actually uses, and it is kept per line rather
    than per run because a build can mix them.
    """
    out: list[dict] = []
    for tag, fixed in _LISTS:
        for inv in vel.iter(tag):
            item = _text(inv.find("STOCKITEMNAME"))
            if not item:
                continue
            qty, unit, _raw = _parse_qty(_text(inv.find("ACTUALQTY")))
            direction = fixed or ("Consumed" if qty < 0 else "Produced")
            rate = _to_float(_text(inv.find("RATE")).split("/")[0]
                             .replace(",", "").strip() or 0)
            amount = abs(_to_float(_text(inv.find("AMOUNT"))))

            batches = inv.findall("BATCHALLOCATIONS.LIST")
            if not batches:
                out.append({"item_name": item, "size_batch": "", "godown": "",
                            "qty": abs(qty), "unit": unit,
                            "direction": direction, "rate": rate,
                            "amount": amount, "source_tag": tag})
                continue
            for b in batches:
                bq, bu, _ = _parse_qty(_text(b.find("ACTUALQTY")))
                out.append({
                    "item_name": item,
                    "size_batch": _text(b.find("BATCHNAME")),
                    "godown": _text(b.find("GODOWNNAME")),
                    "qty": abs(bq) or abs(qty),
                    "unit": bu or unit,
                    "direction": fixed or ("Consumed" if bq < 0 else direction),
                    "rate": rate, "amount": amount, "source_tag": tag,
                })
    return out


def fetch(cfg: TallyConfig, frm: date, to: date,
          vtypes: list[str] | None = None) -> list[dict]:
    """Production vouchers in a window, one payload per voucher."""
    assert_company_loaded(cfg)
    types = vtypes or list(STAGES)
    raw = _post(cfg, _voucher_body(cfg, frm, to, types, _PRODUCTION))

    out = []
    for vel in _parse_xml(raw).iter("VOUCHER"):
        h = _header(vel, cfg.company)
        if not h.get("guid"):
            continue
        # _header is shared with sale/receipt mirrors and does not carry the
        # voucher type; stage classification is the one consumer that needs it.
        h["voucher_type"] = _text(vel.find("VOUCHERTYPENAME"))
        h["stage"] = STAGES.get(h["voucher_type"], "Other")
        h["lines"] = _lines(vel)
        # A voucher with no inventory line is not a production event; keeping
        # it would inflate every count on the app's home screen with nothing.
        if h["lines"]:
            out.append(h)
    log.info("Fetched %d production vouchers for %s..%s", len(out), frm, to)
    return out


def probe(cfg: TallyConfig, days: int = 30) -> dict:
    """
    Which shape does THIS build use? Reads, prints, writes nothing.

    Run this once when Tally is open. The answer decides nothing in code —
    all three shapes are already handled — but it is the difference between
    knowing and assuming, and this repository has paid for that difference
    before.
    """
    to = date.today()
    frm = to - timedelta(days=days)
    vouchers = fetch(cfg, frm, to)
    tags = Counter(l["source_tag"] for v in vouchers for l in v["lines"])
    dirs = Counter(l["direction"] for v in vouchers for l in v["lines"])
    stages = Counter(v["stage"] for v in vouchers)
    units = Counter(l["unit"] for v in vouchers for l in v["lines"])
    sized = sum(1 for v in vouchers for l in v["lines"] if l["size_batch"])
    total = sum(len(v["lines"]) for v in vouchers)
    godowns = Counter(l["godown"] for v in vouchers for l in v["lines"] if l["godown"])

    print(f"\n{len(vouchers)} production vouchers, {total} lines, "
          f"{frm}..{to}\n")
    print("XML shape used by this build:")
    for tag, n in tags.most_common():
        print(f"  {tag:28} {n:6}")
    print("\nDirection:", dict(dirs))
    print("Sizes present on", sized, "of", total, "lines")
    print("\nBy stage:")
    for s, n in stages.most_common():
        print(f"  {s:16} {n:5}")
    print("\nUnits:", dict(units))
    print("Godowns:", dict(godowns.most_common(12)))
    return {"vouchers": len(vouchers), "lines": total, "tags": dict(tags)}


def push(client, vouchers: list[dict], chunk: int = 100) -> dict:
    """
    Chunked, because a whole quarter of cutting is a few thousand lines and
    Frappe Cloud's request limit is not generous. The endpoint is GUID-keyed
    and idempotent, so a chunk that fails can simply be sent again.
    """
    totals = {"created": 0, "updated": 0, "unchanged": 0}
    for i in range(0, len(vouchers), chunk):
        res = client.upsert_production_entries(vouchers[i:i + chunk]) or {}
        msg = res.get("message", res) if isinstance(res, dict) else {}
        for k in totals:
            totals[k] += int(msg.get(k) or 0)
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--probe", action="store_true",
                    help="report which XML shape this build uses; write nothing")
    ap.add_argument("--company", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Same config.toml, same Settings object, as every other job here.
    import dataclasses
    from sync import load_settings, resolve_companies
    from frappe_client import FrappeClient

    st = load_settings()
    companies = ([args.company] if args.company
                 else ([st.tally.company] if st.tally.company
                       else resolve_companies(st)))
    if not companies:
        raise SystemExit("No company configured or open in Tally.")

    to = date.today()
    frm = to - timedelta(days=args.days)
    fc = None if args.probe else FrappeClient(st.frappe)

    for company in companies:
        cfg = dataclasses.replace(st.tally, company=company)
        if args.probe:
            probe(cfg, args.days)
            continue
        vouchers = fetch(cfg, frm, to)
        log.info("%s: %s", company, push(fc, vouchers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# Zero-touch mode: ride the 15-minute sync instead of asking a person
# ---------------------------------------------------------------------------
#
# The owner's requirement, verbatim: "it will not be possible for me to run
# that command every time." So nothing here is a command. sync.py calls
# run_incremental() on every pass; each pass does two small things:
#
#   * the RECENT window — the last few days, re-read every pass so new and
#     edited production vouchers land within 15 minutes like everything else;
#   * one BACKFILL step — if history hasn't reached the anchor yet, one
#     modest chunk further back. The mirror itself is the progress marker
#     (its earliest production voucher), so there is no state file to lose,
#     and a box rebuild resumes exactly where the data says it should.
#
# The 150-day history therefore assembles itself over ~a dozen syncs —
# an afternoon of ordinary working hours — without one long pull that
# would sit on Tally's gateway, and without anyone touching the server.

RECENT_DAYS = 3
BACKFILL_CHUNK_DAYS = 12
BACKFILL_ANCHOR = date(2026, 4, 1)   # this book's opening day


def plan_windows(today: date, earliest: "date | None",
                 anchor: date = BACKFILL_ANCHOR) -> list[tuple[date, date]]:
    """
    The windows one pass should fetch. Pure, so the tests can hold it still.

    Always the recent window; plus one chunk stepping back from the earliest
    voucher already mirrored, until the anchor is reached. First-ever pass
    (nothing mirrored) starts stepping back from today.
    """
    windows = [(today - timedelta(days=RECENT_DAYS), today)]
    floor = earliest or today
    if floor > anchor:
        start = max(anchor, floor - timedelta(days=BACKFILL_CHUNK_DAYS))
        windows.append((start, floor - timedelta(days=1)))
    return windows


def run_incremental(cfg: TallyConfig, fc) -> dict:
    """
    One pass worth of production mirroring. Called from sync.py; must never
    raise past its own wall — production data is a passenger on the sync,
    and a passenger does not get to crash the bus.
    """
    today = date.today()
    earliest = fc.production_window(cfg.company)
    counts = {"production_vouchers": 0, "production_backfill_to": ""}
    for frm, to in plan_windows(today, earliest):
        if frm > to:
            continue
        vouchers = fetch(cfg, frm, to)
        if vouchers:
            push(fc, vouchers)
            counts["production_vouchers"] += len(vouchers)
        counts["production_backfill_to"] = str(frm)
    return counts
