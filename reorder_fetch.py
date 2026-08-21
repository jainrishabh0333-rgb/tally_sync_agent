"""
reorder_fetch.py — godown-wise, size-wise stock movements for the reorder platform.

The Reorder Report in Tally (custom TDL, Gateway → Reorder Reports) shows, per
item and per size: IN STOCK | UNPACK QTY | STITCHING | PENDING ORDER |
REORDER LEVEL | DEFICIT/SURPLUS. Its arithmetic was reverse-engineered from a
live screenshot and matches on every row tested (2026-08-21):

    Deficit = IN STOCK + UNPACK QTY + STITCHING - PENDING ORDER - REORDER LEVEL

Note the PENDING ORDER term: the `Pooja Ragenee Reorder Platform` prototype
omits it and therefore under-states need on any article with an open order
book. Tally is the source of truth.

This module rebuilds the four measurable columns from primitives, so the
platform does not depend on anyone running that report by hand. What it
cannot supply is REORDER LEVEL, which is a management decision held in custom
TDL storage — see the notes at the bottom.

Hard-won rules, each PROVEN against the live build (TallyPrime Edit Log GOLD,
company "SN JAIN INDUSTRIES PVT LTD - (26-27)", probed 2026-08-21):

  * `...BatchAllocations.GodownName` IS a valid FETCH field, though it is
    absent from distributor_fetch's `_INVENTORY` list. Adding it left the
    voucher count unchanged (155 on 2026-08-20) and yielded 1,625
    godown-tagged batch rows over 19 godowns. Anything read WITHOUT it gets
    an empty GODOWNNAME and silently buckets to nothing — which is what
    `_inventory_lines()` in distributor_fetch does today.

  * Quantities are USUALLY unsigned — direction normally comes from which
    list a line sits in, not from its sign. But not always: reversals post as
    NEGATIVE lines inside the IN list (measured: Stock Transfer #STP carries
    -39.00 Box in INVENTORYENTRIESIN). So the line's own sign and the list's
    direction MULTIPLY. An abs() here silently doubles the error on every
    such line, and a short sample will not contain one.

  * `ALLINVENTORYENTRIES.LIST` is the UNION of the in and out lists:
    measured ALL == IN + OUT on every stock journal / transfer inspected.
    Summing it double-counts AND loses direction. For any voucher carrying
    IN/OUT lists, read those; fall back to ALLINVENTORYENTRIES only when it
    has neither.

  * Per-size CLOSING stock cannot be read from masters on this build. A
    BatchAllocations walk answers ClosingBalance with the ITEM total repeated
    on every size (measured: 36.00 on both sizes of 1326 CL S-XL-(Doz)), and
    masters list only batches that carry an OPENING balance — 2 rows where
    the report shows 4 sizes. Hence movement summation here.

  * A Voucher Collection is scoped by <FILTER> + <SYSTEM Formulae>, per the
    house rule in distributor_fetch.

Parse functions are pure (XML string in, payloads out) so they are testable
without a live Tally.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from tally_client import (
    TallyConfig,
    _company_tag,
    _parse_qty,
    _parse_xml,
    _post,
    _tally_date_to_iso,
    _text,
    assert_company_loaded,
)

log = logging.getLogger("sync")


# ---------------------------------------------------------------------------
# Field list — PROVEN 2026-08-21, see module docstring
# ---------------------------------------------------------------------------

_MOVEMENT = (
    "Guid,Date,VoucherTypeName,VoucherNumber,PartyLedgerName,"
    "Reference,Narration,IsCancelled,IsOptional,AlterID"
    ",AllInventoryEntries.StockItemName,AllInventoryEntries.ActualQty"
    ",AllInventoryEntries.IsDeemedPositive"
    ",AllInventoryEntries.BatchAllocations.BatchName"
    ",AllInventoryEntries.BatchAllocations.ActualQty"
    ",AllInventoryEntries.BatchAllocations.GodownName"
    ",InventoryEntriesIn.StockItemName,InventoryEntriesIn.ActualQty"
    ",InventoryEntriesIn.BatchAllocations.BatchName"
    ",InventoryEntriesIn.BatchAllocations.ActualQty"
    ",InventoryEntriesIn.BatchAllocations.GodownName"
    ",InventoryEntriesOut.StockItemName,InventoryEntriesOut.ActualQty"
    ",InventoryEntriesOut.BatchAllocations.BatchName"
    ",InventoryEntriesOut.BatchAllocations.ActualQty"
    ",InventoryEntriesOut.BatchAllocations.GodownName"
)

# Voucher types that MOVE stock.
#
# Built from a FULL financial-year census (2026-04-01..2026-08-20, 20k+
# vouchers), NOT from a sample. That distinction is the whole point: an
# earlier list drawn from a 3-day window reconciled 335/335 items perfectly
# and was still wrong — "Stock Journal-Cutting" (359 vouchers over the year)
# simply had not occurred in those three days, and every marketplace channel
# is its own voucher type. Omitting a sales channel OVERSTATES stock, which
# understates reorder need, which under-cuts. Re-run the census before
# trusting this list on another company file or a new financial year.
#
# Sales Order is deliberately absent: it carries item lines but commits stock
# rather than moving it, and feeds PENDING ORDER instead (pending_by_size()).
STOCK_VOUCHER_TYPES = [
    # Direct sales and returns
    "Sales",
    "Purchase",
    "Credit Note",                        # sales return: stock back in
    "Credit Note-Online Sale",
    "Debit Note",                         # purchase return: stock out
    "Debit Note-Purchase",
    # Marketplace channels — each is a distinct voucher type in this book
    "Flipkart Sale",
    "Myntra Sale",
    "Meesho Sale",
    "Amazon Sale",
    "Limeroad Sale",
    "Shopify Sale",
    "V-Mart Sale",
    "Ajio Sale",
    # Production / internal movement
    "Stock Journal",
    "Stock Journal-Unpacked",
    "Stock Journal-Packed",
    "Stock Journal-Cutting",
    "Stock Issue-Cutting",
    "Stock Transfer",
    "Pressing",
    # Job work
    "Job Work-Out",
    "Job Work - (Dying Outward)",
    "Job Work - (Dying Received)",
    # Branch transfers
    "Receipt Challan(Branch Transfer)",
    "Delivery Challan(Branch Transfer)",
]


# ---------------------------------------------------------------------------
# Godown -> report column
# ---------------------------------------------------------------------------
#
# The report's three stock columns are godown buckets, and this book already
# models the shop floor as a godown TREE, so classification follows Tally's
# own hierarchy rather than reading names. Measured live 2026-08-21:
#
#     Job Worker         45 children   Aakash Trading, Guddu Dyeing, ...
#     Stitching          42 children   A.K Enterprises, DHOOM CREATIONS, ...
#     Job Worker-Cutter   6 children   the six Cutter-* godowns
#     In Stock            2 children   Pack, Unpack
#     PRODUCTION          2 children   Inhouse, Job Worker
#
# Rule confirmed by the owner: goods sitting with a job worker are STITCHING
# WIP. That covers both trees — `Stitching` and `Job Worker` — while
# `Job Worker-Cutter` stays with cutting. A leaf such as "DHOOM CREATIONS"
# says nothing in its own name; only its parent does, which is why
# classify_godown() must be given fetch_godown_parents() to work properly.
#
# Still unverified: whether these buckets reproduce the report's own column
# totals. The item-level reconciliation CANNOT check this — it sums across
# godowns, so a misplaced bucket cancels out inside the total and still
# passes. Only a Reorder Report export settles it; use
# validate_against_report() when one is available.

BUCKET_STOCK = "in_stock"
BUCKET_UNPACK = "unpack"
BUCKET_STITCHING = "stitching"
BUCKET_CUTTING = "cutting"
BUCKET_OTHER = "other"

_EXACT_BUCKETS = {
    "main location": BUCKET_STOCK,
    "in stock": BUCKET_STOCK,
    "pack": BUCKET_STOCK,
    "unpack": BUCKET_UNPACK,
    "v mart unpack": BUCKET_UNPACK,
    # Parent godowns. Reached by walking up from a leaf, which is what makes
    # the 87 job-worker godowns classify themselves.
    "stitching": BUCKET_STITCHING,
    "job worker": BUCKET_STITCHING,     # goods with a job worker = stitching WIP
    "job worker-cutter": BUCKET_CUTTING,
    "cutting": BUCKET_CUTTING,
}


def fetch_godown_parents(cfg: TallyConfig) -> dict:
    """{godown: parent godown} — the hierarchy classify_godown() walks."""
    body = f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>TB_GdnTree</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="TB_GdnTree" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <TYPE>Godown</TYPE><FETCH>Name,Parent</FETCH>
   </COLLECTION>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""
    tree = {}
    for g in _parse_xml(_post(cfg, body)).iter("GODOWN"):
        name = g.get("NAME") or _text(g.find("NAME"))
        if name:
            tree[name] = _text(g.find("PARENT"))
    log.info("Fetched parents for %d godowns", len(tree))
    return tree


def classify_godown(name: str, parents: dict | None = None) -> str:
    """
    Which reorder-report column a godown's stock belongs to.

    Classification follows Tally's OWN godown hierarchy rather than guessing
    from names, because this book already models the shop floor that way:
    45 godowns sit under `Job Worker`, 42 under `Stitching`, the six cutters
    under `Job Worker-Cutter`, and Pack/Unpack under `In Stock`. A leaf like
    "DHOOM CREATIONS" carries no hint in its name — its parent does.

    Pass `parents` from fetch_godown_parents(). Without it only leaf names can
    be matched, and every job-worker godown falls to BUCKET_OTHER — which
    understates stitching WIP and overstates reorder need.
    """
    seen: set[str] = set()
    cur = (name or "").strip()
    if not cur:
        return BUCKET_OTHER
    for _ in range(8):
        key = cur.lower()
        if key in _EXACT_BUCKETS:
            return _EXACT_BUCKETS[key]
        if key.startswith("cutter-") or key.startswith("cutting"):
            return BUCKET_CUTTING
        if key.startswith("unpack"):
            return BUCKET_UNPACK
        if key.startswith("pack"):
            return BUCKET_STOCK
        # "Inhouse-*" are in-house stitching units: 40-odd of them already sit
        # under the Stitching / Job Worker parents, and the two that hang off
        # PRODUCTION and the E-29 branch are the same kind of place.
        if key.startswith("inhouse"):
            return BUCKET_STITCHING
        # Owner's rule: goods with a job worker are stitching WIP. Catches the
        # job-work godowns parented at the root rather than under Job Worker.
        if key.startswith("jobwork") or key.startswith("job work"):
            return BUCKET_STITCHING
        if not parents:
            break
        nxt = parents.get(cur, "")
        # "Primary" is Tally's synthetic root and classifies nothing.
        if not nxt or nxt == "Primary" or nxt in seen:
            break
        seen.add(nxt)
        cur = nxt
    return BUCKET_OTHER


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

@dataclass
class Movement:
    """One signed stock movement of one size of one item in one godown."""
    guid: str
    date: str
    voucher_type: str
    voucher_number: str
    item_name: str
    size_batch: str
    godown: str
    bucket: str
    qty: float            # signed: + into the godown, - out of it
    unit: str
    qty_raw: str


def _movement_body(cfg: TallyConfig, frm: date, to: date, fields: str) -> str:
    """Date- and type-scoped Voucher Collection, filter-dotted shape."""
    fd, td = frm.strftime("%Y%m%d"), to.strftime("%Y%m%d")
    types = " or ".join(f'$VoucherTypeName = "{t}"' for t in STOCK_VOUCHER_TYPES)
    cond = (f'$Date &gt;= $$Date:"{fd}" and $Date &lt;= $$Date:"{td}" '
            f'and ({types})')
    return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>TB_ReorderMv</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="TB_ReorderMv" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <TYPE>Voucher</TYPE>
    <FETCH>{fields}</FETCH>
    <FILTER>TBReorderPeriod</FILTER>
   </COLLECTION>
   <SYSTEM TYPE="Formulae" NAME="TBReorderPeriod">{cond}</SYSTEM>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


def _batch_rows(entry, sign: int, head: dict, units: dict | None,
                parents: dict | None = None) -> list[Movement]:
    """Every batch allocation under one inventory entry, signed."""
    item = _text(entry.find("STOCKITEMNAME"))
    out: list[Movement] = []
    for b in entry.findall("BATCHALLOCATIONS.LIST"):
        raw = _text(b.find("ACTUALQTY"))
        qty, unit, qty_raw = _parse_qty(raw, units)
        size = _text(b.find("BATCHNAME"))
        godown = _text(b.find("GODOWNNAME"))
        if not item or qty in (None, 0):
            continue
        # NEVER abs() this. Tally does write genuinely negative quantities —
        # a reversal posts as a negative line inside the IN list rather than
        # as a line in OUT (measured: Stock Transfer #STP, -39.00 Box in
        # INVENTORYENTRIESIN). Taking the magnitude flips such a line's
        # direction and doubles its error. The line's own sign and the list's
        # direction multiply; they do not override one another.
        out.append(Movement(
            guid=head["guid"], date=head["date"],
            voucher_type=head["voucher_type"],
            voucher_number=head["voucher_number"],
            item_name=item, size_batch=size, godown=godown,
            bucket=classify_godown(godown, parents),
            qty=qty * sign, unit=unit, qty_raw=qty_raw or raw,
        ))
    return out


def parse_movements(raw: str, units: dict | None = None,
                    parents: dict | None = None) -> list[Movement]:
    """
    Signed per-(item, size, godown) movements out of a voucher export.

    Direction rule, measured rather than assumed: quantities are unsigned, and
    ALLINVENTORYENTRIES duplicates the IN/OUT lists. So a voucher carrying
    IN/OUT lists is read from those alone; only a voucher with neither falls
    back to ALLINVENTORYENTRIES, where ISDEEMEDPOSITIVE carries the direction.
    """
    moves: list[Movement] = []
    for vel in _parse_xml(raw).iter("VOUCHER"):
        if _text(vel.find("ISCANCELLED")).lower() == "yes":
            continue
        if _text(vel.find("ISOPTIONAL")).lower() == "yes":
            continue
        head = {
            "guid": _text(vel.find("GUID")),
            "date": _tally_date_to_iso(_text(vel.find("DATE"))),
            "voucher_type": _text(vel.find("VOUCHERTYPENAME")),
            "voucher_number": _text(vel.find("VOUCHERNUMBER")),
        }
        if not head["guid"]:
            continue

        ins = vel.findall("INVENTORYENTRIESIN.LIST")
        outs = vel.findall("INVENTORYENTRIESOUT.LIST")
        if ins or outs:
            for e in ins:
                moves.extend(_batch_rows(e, +1, head, units, parents))
            for e in outs:
                moves.extend(_batch_rows(e, -1, head, units, parents))
            continue

        for e in vel.findall("ALLINVENTORYENTRIES.LIST"):
            inward = _text(e.find("ISDEEMEDPOSITIVE")).lower() == "yes"
            moves.extend(_batch_rows(e, +1 if inward else -1, head, units,
                                     parents))
    return moves


def fetch_movements(cfg: TallyConfig, frm: date, to: date,
                    units: dict | None = None,
                    parents: dict | None = None) -> list[Movement]:
    """
    Stock movements for a date window. One request.

    Keep the window SMALL. Measured 2026-08-20: a single day of this book is
    ~17 MB of XML. Chunk exactly as sync.py does; a whole-year pull in one
    request will not return.
    """
    assert_company_loaded(cfg)
    raw = _post(cfg, _movement_body(cfg, frm, to, _MOVEMENT))
    moves = parse_movements(raw, units, parents)
    log.info("Fetched %d stock movements for %s..%s", len(moves), frm, to)
    return moves


# ---------------------------------------------------------------------------
# Deriving stock from opening + movements
# ---------------------------------------------------------------------------

def opening_from_masters(raw: str, units: dict | None = None) -> dict:
    """
    {(item, size, godown): qty} of OPENING balances, from a stock-item export.

    Masters carry opening batches only; sizes that opened at zero are simply
    absent, which is correct here — they start at zero and the movements
    build them up.
    """
    opening: dict[tuple[str, str, str], float] = defaultdict(float)
    for it in _parse_xml(raw).iter("STOCKITEM"):
        item = it.get("NAME") or _text(it.find("NAME"))
        for b in it.findall("BATCHALLOCATIONS.LIST"):
            qty, _unit, _raw = _parse_qty(_text(b.find("OPENINGBALANCE")), units)
            if not item or not qty:
                continue
            key = (item, _text(b.find("BATCHNAME")), _text(b.find("GODOWNNAME")))
            opening[key] += qty
    return dict(opening)


def derive_stock(opening: dict, movements: list[Movement],
                 parents: dict | None = None) -> dict:
    """
    {(item, size, bucket): qty} — closing stock per reorder-report column.

    closing = opening + sum(signed movements), which is how Tally itself
    derives a godown balance. Buckets collapse the many real godowns onto the
    report's IN STOCK / UNPACK QTY / STITCHING columns.
    """
    totals: dict[tuple[str, str, str], float] = defaultdict(float)
    for (item, size, godown), qty in opening.items():
        totals[(item, size, classify_godown(godown, parents))] += qty
    for m in movements:
        totals[(m.item_name, m.size_batch, m.bucket)] += m.qty
    return dict(totals)


def pending_by_size(order_lines: list[dict]) -> dict:
    """
    {(item, size): pending qty} for the PENDING ORDER column.

    Feed it Tally Sales Order Line rows from the existing mirror — that
    doctype already carries item_name, size_batch and pending_qty, so this
    column needs no new Tally traffic at all.
    """
    pending: dict[tuple[str, str], float] = defaultdict(float)
    for ln in order_lines:
        qty = ln.get("pending_qty") or 0.0
        if qty:
            pending[(ln.get("item_name") or "", ln.get("size_batch") or "")] += qty
    return dict(pending)


def reorder_rows(stock: dict, pending: dict, levels: dict) -> list[dict]:
    """
    The reorder report, rebuilt.

        deficit = in_stock + unpack + stitching - pending - level

    `levels` is {(item, size): qty}. Rows with no level are still returned,
    with level 0.0 and `has_level` False, so the platform can show what is
    unconfigured rather than silently treating it as "no need".
    """
    keys = {(i, s) for (i, s, _b) in stock} | set(pending) | set(levels)
    rows = []
    for item, size in sorted(keys):
        in_stock = stock.get((item, size, BUCKET_STOCK), 0.0)
        unpack = stock.get((item, size, BUCKET_UNPACK), 0.0)
        stitching = stock.get((item, size, BUCKET_STITCHING), 0.0)
        pend = pending.get((item, size), 0.0)
        level = levels.get((item, size))
        rows.append({
            "item_name": item,
            "size": size,
            "in_stock": in_stock,
            "unpack": unpack,
            "stitching": stitching,
            "cutting": stock.get((item, size, BUCKET_CUTTING), 0.0),
            "pending_order": pend,
            "reorder_level": level or 0.0,
            "has_level": level is not None,
            "deficit": in_stock + unpack + stitching - pend - (level or 0.0),
        })
    return rows


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_against_report(rows: list[dict], expected: list[dict]) -> list[dict]:
    """
    Compare rebuilt rows against rows read off the real Reorder Report.

    `expected` items need item_name, size and any of the numeric columns.
    Returns one dict per mismatch. Nothing here is trusted until this runs
    green on a decent sample: the godown->column mapping above is inferred
    from godown names, and only the report can confirm it.
    """
    got = {(r["item_name"], str(r["size"])): r for r in rows}
    cols = ("in_stock", "unpack", "stitching", "pending_order",
            "reorder_level", "deficit")
    problems = []
    for exp in expected:
        key = (exp["item_name"], str(exp["size"]))
        actual = got.get(key)
        if actual is None:
            problems.append({"key": key, "issue": "missing from rebuilt rows"})
            continue
        for c in cols:
            if c not in exp:
                continue
            if abs(float(exp[c]) - float(actual[c])) > 0.01:
                problems.append({
                    "key": key, "column": c,
                    "expected": float(exp[c]), "got": float(actual[c]),
                })
    return problems


# ---------------------------------------------------------------------------
# What this module does NOT do
# ---------------------------------------------------------------------------
#
# REORDER LEVEL is not fetchable. Probed exhaustively 2026-08-21:
#   * native REORDERLEVEL on the stock item reads back empty;
#   * the item master carries no UDFs at all (single-object export, FETCH *);
#   * the custom report `ReorderReport` resolves by name but returns only its
#     variable block (ROITEMGRPNAME, ROARTICLENAMEN, ROSIZECOLORSIZENAME,
#     ROFINALSTATUS, ROMAINCOLL); injected variables are ignored because the
#     report prompts for the group interactively;
#   * ASCII/HTML/SDF/Excel export formats and the TDL meta-collections all
#     hang the shared server.
# So levels are supplied out-of-band: a Reorder Report export parsed into
# {(item, size): level} and cached. They are a management decision and change
# rarely, whereas everything above is recomputed live from this module.
#
# The durable fix is on the TDL side — an XML export part on ReorderReport, or
# the name of the object holding the level — after which levels join the live
# pull and no manual step remains.
