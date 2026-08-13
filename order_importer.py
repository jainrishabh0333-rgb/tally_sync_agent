#!/usr/bin/env python3
"""
order_importer.py — approved Sales Orders from Frappe into TallyPrime.

The ONE write path into Tally, and deliberately a narrow one:

    chat -> Frappe queue DocType -> this script -> Tally XML Import (port 9000)

Scope, agreed with the MD and ENFORCED IN CODE — not convention:
  * Voucher type is hard-whitelisted to "Sales Order" (ALLOWED_VCHTYPE).
    The envelope is re-checked immediately before every send; anything else
    raises and nothing is sent.
  * Quantity-only. No rates, no MRP, no amounts, no ledger entries — staff
    price the order later in Tally.
  * Party and stock item names must EXACT-MATCH existing masters. Tally's
    import silently AUTO-CREATES a master on any name mismatch, so names are
    validated against the live masters BEFORE any XML is built. A mismatch
    marks the order Failed, naming the missing master.
  * Entry convention (mirrors the operator's real screen): ONE inventory line
    per style item; the SIZES are BATCH allocations under that line — the
    batch name IS the size ("28"/"30"/"32", "S".."3XL") — each with its own
    due date as a days offset from the order date. Quantities in Doz,
    fractions allowed.

Idempotency / crash behaviour:
  * The queue docname IS the order_key. Only status=Pending rows are touched.
  * Each order is marked Importing BEFORE the send; the envelope is sent
    exactly ONCE (attempts=1). A transport failure after a send may mean
    Tally DID import, so it is never retried — the order is marked Failed
    with "response lost — CHECK TALLY before retrying, the order may exist".
  * A crash mid-import leaves the row at Importing, never a double-post.
  * Orders whose company is not open in Tally are skipped (left Pending).

The Tally engine is shared with ~8 live operators: orders are processed
strictly SEQUENTIALLY, and tally_client._post paces requests 1.5s apart.

Usage
-----
    python order_importer.py --dry-run             # print XML, send nothing
    python order_importer.py                       # one pass and exit
    python order_importer.py --company "NAME"      # only this company's orders
Config: config.toml (same file as sync.py), optional [orders] section:
    sales_ledger = "Sale Central 5%"   # accounting allocation on each line
    poll         = false   # true = keep running, drain the queue every 60s

Expected order shape from Frappe (tally_bridge.api.pending_sales_orders):
    {"order_key": "...", "order_no": "...", "company": "...", "party": "...",
     "order_date": "2026-08-13", "items": [
        {"item": "STYLE 1234", "unit": "Doz", "sizes": [
            {"size": "28", "qty": 2.5, "due_days": 7}, ...]}, ...]}
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import sync
from frappe_client import FrappeClient, FrappeError
from tally_client import (
    TallyError,
    _fmt_date,
    _post,
    _xml_escape,
    assert_company_loaded,
    fetch_ledgers,
    fetch_stock_items,
)

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / "orders.log"

log = logging.getLogger("orders")

# THE whitelist. This importer writes Sales Orders — non-accounting, posts to
# no ledger, cancellable — and nothing else, ever. Checked at build time AND
# re-checked immediately before every send (_assert_sales_order).
ALLOWED_VCHTYPE = "Sales Order"

POLL_SECONDS = 60          # pause between passes when [orders].poll = true


class OrderDataError(ValueError):
    """The order data itself is unusable — marks the order Failed."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class OrderSettings:
    # The zero-amount accounting allocation each order line carries — every
    # operator-entered specimen names one. Staff correct it while pricing if
    # a party needs the Local ledger instead.
    sales_ledger: str = "Sale Central 5%"
    poll: bool = False


def load_order_settings(path: Path | None = None) -> OrderSettings:
    """Read the optional [orders] section; discovery identical to sync.py."""
    cfg_path = path or (sync.HERE / "config.toml")
    data: dict = {}
    if cfg_path.exists() and sync.tomllib is not None:
        data = sync._read_toml(cfg_path)
    o = data.get("orders", {}) or {}
    return OrderSettings(
        sales_ledger=(str(o.get("sales_ledger", "Sale Central 5%")).strip()
                      or "Sale Central 5%"),
        poll=bool(o.get("poll", False)),
    )


# ---------------------------------------------------------------------------
# Order normalisation and validation
# ---------------------------------------------------------------------------

def _to_qty(value) -> "float | None":
    """Parse a quantity that may arrive as 2.5, "2.5" or "2.50 Doz"."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = re.match(r"^(-?\d+(?:\.\d+)?)", str(value).strip().replace(",", ""))
    return float(m.group(1)) if m else None


def _parse_order_date(s: str) -> date:
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        return datetime.strptime(s, "%Y%m%d").date()
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def normalise_order(raw: dict) -> dict:
    """
    Reduce a queue row to exactly what the envelope needs, or raise
    OrderDataError with a message an operator can act on.
    """
    if not isinstance(raw, dict):
        raise OrderDataError("order row is not a mapping")

    key = str(raw.get("order_key") or raw.get("name") or "").strip()
    if not key:
        raise OrderDataError("order has no order_key")

    # Defence in depth: even if the queue somehow carries another voucher
    # type, this importer refuses it. Accounting vouchers stay read-only.
    vt = str(raw.get("voucher_type") or ALLOWED_VCHTYPE).strip()
    if vt != ALLOWED_VCHTYPE:
        raise OrderDataError(
            f"voucher type {vt!r} is not allowed — this importer writes "
            f"{ALLOWED_VCHTYPE!r} only"
        )

    company = str(raw.get("company") or "").strip()
    if not company:
        raise OrderDataError("order has no company")
    party = str(raw.get("party") or raw.get("party_ledger") or "").strip()
    if not party:
        raise OrderDataError("order has no party")

    raw_date = str(raw.get("order_date") or "").strip()
    if raw_date:
        try:
            order_date = _parse_order_date(raw_date)
        except ValueError:
            raise OrderDataError(f"unreadable order_date {raw_date!r} "
                                 "(want YYYY-MM-DD)")
    else:
        order_date = date.today()
        log.info("Order %s carries no order_date — using today (%s).",
                 key, order_date)

    default_due = raw.get("due_days", 0)
    lines_in = raw.get("items") or raw.get("lines") or []
    if not isinstance(lines_in, list) or not lines_in:
        raise OrderDataError("order has no item lines")

    # The Frappe queue stores lines FLAT — one child row per (item, size):
    # {item_name, size_batch, qty, unit, due_days}. The nested items->sizes
    # shape below is kept for hand-written payloads and tests. Detect the
    # flat form by the size_batch key and regroup it by item, preserving
    # first-appearance order, so both shapes flow through one code path.
    if any(isinstance(ln, dict) and "size_batch" in ln for ln in lines_in):
        grouped: "dict[str, dict]" = {}
        for i, ln in enumerate(lines_in, 1):
            if not isinstance(ln, dict):
                raise OrderDataError(f"line {i} is not a mapping")
            item = str(ln.get("item_name") or ln.get("item") or "").strip()
            if not item:
                raise OrderDataError(f"line {i} has no stock item name")
            g = grouped.setdefault(item, {"item": item,
                                          "unit": str(ln.get("unit") or "Doz"),
                                          "sizes": []})
            g["sizes"].append({
                "size": str(ln.get("size_batch") or "").strip(),
                "qty": ln.get("qty"),
                "due_days": ln.get("due_days", default_due),
            })
        lines_in = list(grouped.values())

    lines = []
    for i, ln in enumerate(lines_in, 1):
        if not isinstance(ln, dict):
            raise OrderDataError(f"line {i} is not a mapping")
        item = str(ln.get("item") or ln.get("stock_item")
                   or ln.get("item_name") or "").strip()
        if not item:
            raise OrderDataError(f"line {i} has no stock item name")
        unit = str(ln.get("unit") or "Doz").strip() or "Doz"

        sizes_in = ln.get("sizes") or ln.get("batches") or []
        if not isinstance(sizes_in, list) or not sizes_in:
            raise OrderDataError(f"line {i} ({item}) has no size allocations")
        sizes = []
        for j, sz in enumerate(sizes_in, 1):
            if not isinstance(sz, dict):
                raise OrderDataError(f"line {i} ({item}) size {j} is not a mapping")
            size_name = str(sz.get("size") or sz.get("batch") or "").strip()
            if not size_name:
                raise OrderDataError(f"line {i} ({item}) size {j} has no size name")
            qty = _to_qty(sz.get("qty"))
            if qty is None or qty <= 0:
                raise OrderDataError(
                    f"line {i} ({item}) size {size_name!r}: quantity "
                    f"{sz.get('qty')!r} is not a positive number"
                )
            try:
                due_days = int(sz.get("due_days",
                                      ln.get("due_days", default_due)) or 0)
            except (TypeError, ValueError):
                raise OrderDataError(
                    f"line {i} ({item}) size {size_name!r}: due_days "
                    f"{sz.get('due_days')!r} is not a whole number of days"
                )
            sizes.append({"size": size_name, "qty": qty, "due_days": due_days})

        total = sum(s["qty"] for s in sizes)
        stated = _to_qty(ln.get("qty")) if ln.get("qty") not in (None, "") else None
        if stated is not None and abs(stated - total) > 0.005:
            raise OrderDataError(
                f"line {i} ({item}): stated quantity {stated:g} does not match "
                f"the sum of its sizes {total:g} — fix the order in the queue"
            )
        lines.append({"item": item, "unit": unit, "qty": total, "sizes": sizes})

    return {
        "order_key": key,
        "order_no": str(raw.get("order_no") or "").strip() or key,
        "company": company,
        "party": party,
        "order_date": order_date,
        "lines": lines,
    }


def validate_masters(o: dict, parties: set, items: set) -> str:
    """
    Names must exact-match live masters — Tally auto-creates on mismatch.
    Returns "" when clean, else the Failed message naming every miss.
    """
    probs = []
    if o["party"] not in parties:
        probs.append(f"party ledger {o['party']!r} does not exist in Tally")
    seen = set()
    for ln in o["lines"]:
        if ln["item"] not in items and ln["item"] not in seen:
            probs.append(f"stock item {ln['item']!r} does not exist in Tally")
            seen.add(ln["item"])
    if not probs:
        return ""
    return ("refused — importing would silently AUTO-CREATE a master: "
            + "; ".join(probs)
            + ". Fix the name to match Tally exactly, or create the master "
              "in Tally first, then re-queue.")


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

def _fmt_qty(qty: float, unit: str) -> str:
    """Quantities as the operator types them: '2.50 Doz'."""
    return f"{qty:.2f} {unit}"


def _assert_sales_order(xml: str) -> None:
    """
    The whitelist, verified on the OUTGOING BYTES, not on intent.

    All user text in the envelope is XML-escaped (quotes become &quot;), so
    the only VCHTYPE attribute and VOUCHERTYPENAME element the regexes can
    match are the ones this module wrote. Exactly one of each, both reading
    "Sales Order", or nothing is sent. RuntimeError on purpose: a violation
    is a code bug, and the run must stop rather than mark-and-continue.
    """
    assert ALLOWED_VCHTYPE == "Sales Order"
    kinds = re.findall(r'VCHTYPE="([^"]*)"', xml)
    names = re.findall(r"<VOUCHERTYPENAME>([^<]*)</VOUCHERTYPENAME>", xml)
    if kinds != [ALLOWED_VCHTYPE] or names != [ALLOWED_VCHTYPE]:
        raise RuntimeError(
            "SAFETY: refusing to send — envelope is not exactly one "
            f"{ALLOWED_VCHTYPE!r} voucher (VCHTYPE={kinds!r}, "
            f"VOUCHERTYPENAME={names!r})."
        )
    for banned in ("<RATE>", "<MRP", "<DISCOUNT>"):
        if banned in xml:
            raise RuntimeError(
                f"SAFETY: refusing to send — envelope contains {banned!r}; "
                "this flow is quantity-only."
            )
    # Ledger lines ARE present — a real order carries the party line and a
    # sales-ledger allocation, and Tally parks the voucher in Import
    # Exceptions without them (learned from 22 exported specimens). The
    # money guarantee moves to the amounts themselves: every AMOUNT in the
    # envelope must be exactly zero.
    nonzero = [a for a in re.findall(r"<AMOUNT>([^<]*)</AMOUNT>", xml)
               if a.strip() not in ("0", "0.00")]
    if nonzero:
        raise RuntimeError(
            f"SAFETY: refusing to send — non-zero AMOUNT(s) {nonzero!r}; "
            "this flow is quantity-only."
        )


def _due_literal(d: "date") -> str:
    """Due dates as Tally itself writes them in orders: 1-Sep-26, 12-Aug-26."""
    return f"{d.day}-{d.strftime('%b-%y')}"


def build_envelope(o: dict, sales_ledger: str) -> str:
    """
    Import envelope for ONE sales order — quantity-only, shaped to match how
    THIS Tally serialises operator-entered orders (22 live specimens,
    exported 2026-08-13, sample_orders.xml):

    - NO GODOWNNAME anywhere. The order screen's "Any" is the ABSENCE of a
      godown, not a godown named "Any" — writing one parked the first import
      in Import Exceptions.
    - Each BATCHALLOCATIONS.LIST (BATCHNAME = size) carries ORDERNO and its
      own ORDERDUEDATE, written as d-MMM-yy exactly as the specimens show.
    - Each inventory line carries an ACCOUNTINGALLOCATIONS.LIST naming the
      sales ledger, and the voucher carries the party LEDGERENTRIES.LIST —
      both present in every specimen. Quantity-only means their AMOUNTs are
      zero (staff price the order in Tally), not that they are absent.
    """
    esc = _xml_escape
    d = _fmt_date(o["order_date"])

    inv = []
    for ln in o["lines"]:
        batches = []
        for sz in ln["sizes"]:
            due = _due_literal(o["order_date"] + timedelta(days=sz["due_days"]))
            q = _fmt_qty(sz["qty"], ln["unit"])
            batches.append(
                "    <BATCHALLOCATIONS.LIST>\n"
                f"     <BATCHNAME>{esc(sz['size'])}</BATCHNAME>\n"
                f"     <ORDERNO>{esc(o['order_no'])}</ORDERNO>\n"
                "     <AMOUNT>0</AMOUNT>\n"
                f"     <ACTUALQTY>{q}</ACTUALQTY>\n"
                f"     <BILLEDQTY>{q}</BILLEDQTY>\n"
                f"     <ORDERDUEDATE>{due}</ORDERDUEDATE>\n"
                "    </BATCHALLOCATIONS.LIST>"
            )
        lq = _fmt_qty(ln["qty"], ln["unit"])
        inv.append(
            "   <ALLINVENTORYENTRIES.LIST>\n"
            f"    <STOCKITEMNAME>{esc(ln['item'])}</STOCKITEMNAME>\n"
            "    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n"
            "    <AMOUNT>0</AMOUNT>\n"
            f"    <ACTUALQTY>{lq}</ACTUALQTY>\n"
            f"    <BILLEDQTY>{lq}</BILLEDQTY>\n"
            + "\n".join(batches) + "\n"
            "    <ACCOUNTINGALLOCATIONS.LIST>\n"
            f"     <LEDGERNAME>{esc(sales_ledger)}</LEDGERNAME>\n"
            "     <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n"
            "     <ISPARTYLEDGER>No</ISPARTYLEDGER>\n"
            "     <AMOUNT>0</AMOUNT>\n"
            "    </ACCOUNTINGALLOCATIONS.LIST>\n"
            "   </ALLINVENTORYENTRIES.LIST>"
        )

    xml = (
        "<ENVELOPE>\n"
        " <HEADER><VERSION>1</VERSION><TALLYREQUEST>Import</TALLYREQUEST>"
        "<TYPE>Data</TYPE><ID>Vouchers</ID></HEADER>\n"
        " <BODY><DESC><STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{esc(o['company'])}</SVCURRENTCOMPANY>"
        "</STATICVARIABLES></DESC>\n"
        "  <DATA><TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
        f"  <VOUCHER VCHTYPE=\"{ALLOWED_VCHTYPE}\" ACTION=\"Create\" "
        "OBJVIEW=\"Invoice Voucher View\">\n"
        f"   <DATE>{d}</DATE>\n"
        f"   <VOUCHERTYPENAME>{ALLOWED_VCHTYPE}</VOUCHERTYPENAME>\n"
        f"   <VOUCHERNUMBER>{esc(o['order_no'])}</VOUCHERNUMBER>\n"
        f"   <REFERENCE>{esc(o['order_key'])}</REFERENCE>\n"
        f"   <PARTYLEDGERNAME>{esc(o['party'])}</PARTYLEDGERNAME>\n"
        f"   <PARTYNAME>{esc(o['party'])}</PARTYNAME>\n"
        f"   <BASICBASEPARTYNAME>{esc(o['party'])}</BASICBASEPARTYNAME>\n"
        f"   <BASICBUYERNAME>{esc(o['party'])}</BASICBUYERNAME>\n"
        "   <PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>\n"
        f"   <NARRATION>Queued from photographed order via Claude "
        f"({esc(o['order_key'])}); quantities only, to be priced.</NARRATION>\n"
        + "\n".join(inv) + "\n"
        "   <LEDGERENTRIES.LIST>\n"
        f"    <LEDGERNAME>{esc(o['party'])}</LEDGERNAME>\n"
        "    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n"
        "    <ISPARTYLEDGER>Yes</ISPARTYLEDGER>\n"
        "    <AMOUNT>0</AMOUNT>\n"
        "   </LEDGERENTRIES.LIST>\n"
        "  </VOUCHER>\n"
        "  </TALLYMESSAGE></DATA></BODY></ENVELOPE>"
    )
    _assert_sales_order(xml)
    return xml


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _tag_int(text: str, tag: str) -> int:
    m = re.search(rf"<{tag}>\s*(-?\d+)\s*</{tag}>", text)
    return int(m.group(1)) if m else 0


def _vch_from_response(text: str) -> str:
    for tag in ("LASTVCHID", "VCHNUMBER", "VOUCHERNUMBER"):
        m = re.search(rf"<{tag}>\s*([^<]+?)\s*</{tag}>", text)
        if m:
            return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# Per-company caches (one Tally round-trip each, per run)
# ---------------------------------------------------------------------------

def _company_open(cfg, company: str, cache: dict) -> bool:
    """assert_company_loaded once per company per pass; False = leave Pending."""
    if company not in cache:
        cfg.company = company
        try:
            assert_company_loaded(cfg)
            cache[company] = True
        except TallyError as exc:
            cache[company] = False
            log.warning("Company %r is not available (%s) — its orders stay "
                        "Pending for the next run.", company, exc)
    return cache[company]


def _masters(cfg, company: str, cache: dict) -> tuple:
    """(party names, item names) for a company — fetched ONCE per run."""
    if company not in cache:
        cfg.company = company
        parties = {l.name for l in fetch_ledgers(cfg)}
        items = {i.name for i in fetch_stock_items(cfg, date.today())}
        log.info("Cached masters for %r: %d ledgers, %d stock items.",
                 company, len(parties), len(items))
        cache[company] = (parties, items)
    return cache[company]


# ---------------------------------------------------------------------------
# State machine around one send
# ---------------------------------------------------------------------------

def _mark(fc: FrappeClient, order_key: str, status: str,
          error: str = "", tally_vch_no: str = "") -> bool:
    """
    Record a state change in Frappe; every transition is logged.

    A failure to RECORD is loud but never fatal to the loop: if Imported/
    Failed cannot be written, the row stays at Importing — which the importer
    never touches again, so the worst case is a manual look, not a double-post.
    """
    try:
        fc.mark_order_result(order_key, status, error=error,
                             tally_vch_no=tally_vch_no)
        log.info("Order %s -> %s%s", order_key, status,
                 f" ({error})" if error else "")
        return True
    except FrappeError as exc:
        log.error("Order %s: could NOT record status %r in Frappe (%s). "
                  "The row keeps its previous status — check it by hand.",
                  order_key, status, exc)
        return False


def import_order(fc: FrappeClient, cfg, o: dict, xml: str) -> str:
    """
    Send ONE order to Tally. Returns 'imported' | 'failed' | 'skipped'.

    Mark Importing first; send exactly ONCE (attempts=1); never blind-retry —
    a lost response after a send may mean Tally DID import the voucher.
    """
    key = o["order_key"]

    if not _mark(fc, key, "Importing"):
        # Could not even record the attempt — do not send. Still Pending,
        # so the next run picks it up.
        return "skipped"

    cfg.company = o["company"]
    _assert_sales_order(xml)                      # the whitelist, every send
    try:
        resp = _post(cfg, xml, attempts=1)
    except TallyError as exc:
        msg = str(exc)
        if "line error" in msg.lower():
            # Tally answered and rejected it — a real, safe-to-report failure.
            _mark(fc, key, "Failed", error=msg)
        else:
            # Transport failure. The envelope may or may not have arrived —
            # a timeout AFTER the import commits looks identical from here.
            _mark(fc, key, "Failed",
                  error="response lost — CHECK TALLY before retrying, "
                        f"the order may exist. ({msg})")
        return "failed"

    created, altered = _tag_int(resp, "CREATED"), _tag_int(resp, "ALTERED")
    if created > 0 or altered > 0:
        vch = _vch_from_response(resp) or o["order_no"]
        _mark(fc, key, "Imported", tally_vch_no=vch)
        log.info("Order %s imported into %r (voucher %s, created=%d altered=%d).",
                 key, o["company"], vch, created, altered)
        return "imported"

    # CREATED=0 with no LINEERROR is still a failure — Tally quietly ignored it.
    head = " ".join(resp.split())[:300]
    _mark(fc, key, "Failed",
          error=f"Tally did not confirm the import (CREATED=0): {head}")
    return "failed"


# ---------------------------------------------------------------------------
# One pass over the queue
# ---------------------------------------------------------------------------

def run_pass(st: sync.Settings, ocfg: OrderSettings, fc: FrappeClient,
             args) -> tuple:
    """Drain the queue once, sequentially. Returns (imported, failed, skipped)."""
    imported = failed = skipped = 0
    cfg = st.tally

    try:
        orders = fc.get_pending_sales_orders(args.company or "")
    except FrappeError as exc:
        log.error("Could not fetch pending orders from Frappe: %s", exc)
        return imported, failed, skipped
    if args.company:  # belt and braces over the server-side filter
        orders = [o for o in orders
                  if isinstance(o, dict)
                  and str(o.get("company", "")).strip() == args.company]
    if not orders:
        log.info("No pending sales orders.")
        return imported, failed, skipped
    log.info("%d pending order(s)%s.", len(orders),
             f" for {args.company!r}" if args.company else "")

    open_cache: dict = {}
    masters_cache: dict = {}

    for raw in orders:
        key = (str(raw.get("order_key") or raw.get("name") or "?").strip()
               if isinstance(raw, dict) else "?")
        try:
            o = normalise_order(raw)
        except OrderDataError as exc:
            if args.dry_run:
                print(f"--- {key}: NOT BUILDABLE — {exc} ---")
            else:
                log.error("Order %s: %s — marking Failed.", key, exc)
                _mark(fc, key, "Failed", error=str(exc))
            failed += 1
            continue

        if args.dry_run:
            # Build and PRINT, send nothing, change no status. Validation
            # still runs when Tally is reachable so the first real order can
            # be eyeballed together with what would happen to it.
            xml = build_envelope(o, ocfg.sales_ledger)
            if _company_open(cfg, o["company"], open_cache):
                try:
                    parties, items = _masters(cfg, o["company"], masters_cache)
                    err = validate_masters(o, parties, items)
                    if err:
                        log.warning("Order %s WOULD FAIL: %s", key, err)
                        failed += 1
                    else:
                        imported += 1     # i.e. "would import"
                except TallyError as exc:
                    log.warning("Order %s: could not validate masters (%s).",
                                key, exc)
                    skipped += 1
            else:
                skipped += 1
            print(f"--- DRY RUN — {key} ({o['company']}) ---")
            print(xml)
            print()
            continue

        if not _company_open(cfg, o["company"], open_cache):
            log.info("Order %s skipped — company %r is not open in Tally.",
                     key, o["company"])
            skipped += 1
            continue

        try:
            parties, items = _masters(cfg, o["company"], masters_cache)
        except TallyError as exc:
            log.warning("Order %s: cannot read masters for %r (%s) — leaving "
                        "it Pending.", key, o["company"], exc)
            skipped += 1
            continue

        # The configured sales ledger is a name going into the import too —
        # subject to the same auto-create hazard as party and items. A miss
        # is a CONFIG error, not an order error: leave the order Pending and
        # tell the operator to fix config.toml.
        if ocfg.sales_ledger not in parties:
            log.error("Order %s left Pending — [orders].sales_ledger %r does "
                      "not exist as a ledger in %r. Fix config.toml.",
                      key, ocfg.sales_ledger, o["company"])
            skipped += 1
            continue

        err = validate_masters(o, parties, items)
        if err:
            log.error("Order %s: %s", key, err)
            _mark(fc, key, "Failed", error=err)
            failed += 1
            continue

        xml = build_envelope(o, ocfg.sales_ledger)
        outcome = import_order(fc, cfg, o, xml)
        imported += outcome == "imported"
        failed += outcome == "failed"
        skipped += outcome == "skipped"

    return imported, failed, skipped


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Import approved Sales Orders from the Frappe queue into "
                    "TallyPrime. Quantity-only; Sales Order vouchers only.")
    p.add_argument("--dry-run", action="store_true",
                   help="build and PRINT the XML for every pending order; "
                        "send nothing, change no status")
    p.add_argument("--once", action="store_true",
                   help="one pass and exit (the default unless [orders].poll "
                        "is true in config.toml)")
    p.add_argument("--company", metavar="NAME", default=None,
                   help="only process orders for this company "
                        "(exact Tally name)")
    p.add_argument("--retry", metavar="ORDER_KEY", default=None,
                   help="move ONE Failed order back to Pending first, then "
                        "run the normal pass. Only do this after checking in "
                        "Tally that the order did not actually import.")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main() -> int:
    # Windows consoles default to cp1252; a Hindi party name in any print()
    # would otherwise kill the run with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)-7s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
                  logging.StreamHandler()],
    )

    st = sync.load_settings(args.config)
    ocfg = load_order_settings(args.config)
    fc = FrappeClient(st.frappe)

    if args.retry:
        # Failed -> Pending is a transition the queue reserves for an explicit
        # human decision, because a Failed order whose response was lost may
        # ALREADY be inside Tally. The flag is that decision, made typed-out.
        try:
            fc.mark_order_result(args.retry.strip(), "Pending")
            log.info("Order %s moved back to Pending for this run.",
                     args.retry.strip())
        except FrappeError as exc:
            log.error("Could not re-queue %s: %s", args.retry, exc)
            return 1

    if args.dry_run:
        log.info("DRY RUN — envelopes are printed, nothing is sent, no "
                 "status changes.")

    last_failed = 0
    try:
        while True:
            imported, failed, skipped = run_pass(st, ocfg, fc, args)
            last_failed = failed
            if args.dry_run:
                log.info("Dry run: %d would import, %d would fail, "
                         "%d not checkable (company closed).",
                         imported, failed, skipped)
            else:
                log.info("Done: %d imported, %d failed, %d skipped "
                         "(company closed).", imported, failed, skipped)
            if args.once or args.dry_run or not ocfg.poll:
                break
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        log.info("Stopped by operator.")
        return 0
    return 1 if last_failed else 0


if __name__ == "__main__":
    sys.exit(main())
