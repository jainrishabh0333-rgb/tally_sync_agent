#!/usr/bin/env python3
"""
order_importer.py — approved Sales Orders from Frappe into TallyPrime.

The ONE write path into Tally, and deliberately a narrow one:

    chat -> Frappe queue DocType -> this script -> Tally XML Import (port 9000)

Scope, agreed with the MD and ENFORCED IN CODE — not convention:
  * Voucher type is hard-whitelisted to "Sales Order" (ALLOWED_VCHTYPE).
    The envelope is re-checked immediately before every send; anything else
    raises and nothing is sent.
  * Priced from the queue's own rates. This build REFUSES zero-value
    vouchers (proven 2026-08-13: the same clone imported with real amounts
    and went to Import Exceptions with them zeroed), so an order carries a
    rate per line, amounts netted through the 50+20 discount chain, and the
    party debited with the total. No MRP, ever. Every amount is re-derived
    from rate x qty on the outgoing bytes before the send.
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
    # The company GST registration every voucher must bind to — this company
    # runs MULTIPLE registrations and every specimen names one. Constants
    # from the live specimens; override in [orders] if the company ever
    # changes registration.
    gst_registration: str = "Uttar Pradesh Registration"
    cmp_gstin: str = "09ABHCS0526J1Z7"
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
        gst_registration=(str(o.get("gst_registration",
                                    "Uttar Pradesh Registration")).strip()
                          or "Uttar Pradesh Registration"),
        cmp_gstin=(str(o.get("cmp_gstin", "09ABHCS0526J1Z7")).strip()
                   or "09ABHCS0526J1Z7"),
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
        # A rate rides through untouched when present — the envelope decides
        # priced vs not by its presence, so a dropped rate here would silently
        # produce the zero-value shape Tally refuses.
        rate = _to_qty(ln.get("rate"))
        if rate is not None and rate <= 0:
            raise OrderDataError(
                f"line {i} ({item}): rate {ln.get('rate')!r} is not positive"
            )
        lines.append({"item": item, "unit": unit, "qty": total,
                      "sizes": sizes, "rate": rate})

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
    if "<MRP" in xml:
        raise RuntimeError(
            "SAFETY: refusing to send — envelope contains an MRP tag; "
            "this flow never writes MRP."
        )
    # Ledger lines ARE present — a real order carries the party line and a
    # sales-ledger allocation, and Tally parks the voucher in Import
    # Exceptions without them (learned from 22 exported specimens).
    #
    # Two money shapes are legal, and which one applies is decided by the
    # presence of RATE, never by intent:
    #
    #   quantity-only — every AMOUNT exactly zero. Kept for the record; this
    #     build REFUSES such vouchers (CREATED=0, proven live 2026-08-13), so
    #     it is not a shape any caller should choose.
    #   priced — every line carries RATE and a non-zero AMOUNT, and the
    #     party's LEDGERENTRIES AMOUNT is the negative of the inventory total
    #     (debit exports negative). The arithmetic is re-checked below on the
    #     outgoing bytes, so a pricing bug cannot reach the books as a
    #     plausible-looking number.
    amounts = [a.strip() for a in re.findall(r"<AMOUNT>([^<]*)</AMOUNT>", xml)]
    priced = "<RATE>" in xml
    if not priced:
        nonzero = [a for a in amounts if a not in ("0", "0.00")]
        if nonzero:
            raise RuntimeError(
                f"SAFETY: refusing to send — non-zero AMOUNT(s) {nonzero!r} "
                "in an unpriced envelope."
            )
        return
    _assert_priced_arithmetic(xml)


# The discount chain this book sells on. Measured, not assumed: of 4,364
# inventory lines across 204 Sales Orders exported 2026-08-01..13, 4,337
# carry DISCOUNT 50 and an AMOUNT equal to rate x qty x 0.4 — a 50% then 20%
# chain — and the remaining 27 carry DISCOUNT 0 at full value. Tally itself
# writes only the FIRST step in the DISCOUNT tag and the fully-netted figure
# in AMOUNT, so that is what is written back.
DISCOUNT_FIRST = 50.0
NET_FACTOR = 0.4
_MONEY_TOL = 0.02


def _net_amount(rate: float, qty: float) -> float:
    return round(rate * qty * NET_FACTOR, 2)


def _assert_priced_arithmetic(xml: str) -> None:
    """
    Re-derive every amount from rate x qty on the outgoing bytes.

    A wrong rate is a business decision made badly; a wrong AMOUNT against a
    right rate is a silent corruption that nobody reading the voucher would
    catch. This catches the second kind before it is sent.
    """
    total = 0.0
    for block in re.findall(
            r"<ALLINVENTORYENTRIES\.LIST>(.*?)</ALLINVENTORYENTRIES\.LIST>",
            xml, re.S):
        def one(tag: str, text: str = block) -> str:
            m = re.search(rf"<{tag}>([^<]*)</{tag}>", text)
            return m.group(1).strip() if m else ""
        rate = float(one("RATE").split("/")[0] or 0)
        qty = float((one("ACTUALQTY") or "0").split()[0])
        amount = float(one("AMOUNT") or 0)
        if rate <= 0 or qty <= 0:
            raise RuntimeError(
                f"SAFETY: refusing to send — line with rate {rate!r} and "
                f"qty {qty!r}; a priced order needs both."
            )
        if abs(amount - _net_amount(rate, qty)) > _MONEY_TOL:
            raise RuntimeError(
                f"SAFETY: refusing to send — line AMOUNT {amount} does not "
                f"equal rate {rate} x qty {qty} x {NET_FACTOR}."
            )
        total += amount
        # The batches under a line must add up to the line, or Tally would
        # book a different quantity than the sizes say.
        bsum = sum(float(m) for m in re.findall(
            r"<BATCHALLOCATIONS\.LIST>(?:(?!</BATCHALLOCATIONS).)*?"
            r"<AMOUNT>([^<]*)</AMOUNT>", block, re.S))
        if abs(bsum - amount) > _MONEY_TOL:
            raise RuntimeError(
                f"SAFETY: refusing to send — batch amounts {bsum} do not sum "
                f"to line amount {amount}."
            )
    party = re.search(
        r"<LEDGERENTRIES\.LIST>.*?<AMOUNT>([^<]*)</AMOUNT>", xml, re.S)
    if not party:
        raise RuntimeError("SAFETY: refusing to send — no party ledger entry.")
    if abs(float(party.group(1)) + round(total, 2)) > _MONEY_TOL:
        raise RuntimeError(
            f"SAFETY: refusing to send — party ledger {party.group(1)} is not "
            f"minus the inventory total {total:.2f} (debit exports negative)."
        )


# First two digits of a GSTIN are the state code. Needed because every real
# voucher in this book carries PLACEOFSUPPLY and party state, and this company
# runs MULTIPLE GST registrations — vouchers must be locatable.
_GST_STATES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman & Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}


def _state_from_gstin(gstin: str) -> str:
    return _GST_STATES.get((gstin or "")[:2], "")


def _due_literal(d: "date") -> str:
    """Due dates as Tally itself writes them in orders: 1-Sep-26, 12-Aug-26."""
    return f"{d.day}-{d.strftime('%b-%y')}"


def build_envelope(o: dict, ocfg: "OrderSettings", party_gstin: str,
                   optional: bool = False) -> str:
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
      both present in every specimen.

    A line carrying `rate` is priced: RATE/DISCOUNT on the line, BATCHRATE/
    BATCHDISCOUNT per size, amounts netted through the discount chain, and
    the party ledger debited with the total. A line without one keeps the
    original zero-amount shape — which this build refuses, so it is only
    still here for the record.
    """
    esc = _xml_escape
    d = _fmt_date(o["order_date"])

    inv, grand = [], 0.0
    for ln in o["lines"]:
        rate = float(ln.get("rate") or 0)
        batches = []
        for sz in ln["sizes"]:
            due = _due_literal(o["order_date"] + timedelta(days=sz["due_days"]))
            q = _fmt_qty(sz["qty"], ln["unit"])
            b_amt = (f"{_net_amount(rate, sz['qty']):.2f}" if rate else "0")
            batches.append(
                "    <BATCHALLOCATIONS.LIST>\n"
                f"     <BATCHNAME>{esc(sz['size'])}</BATCHNAME>\n"
                f"     <ORDERNO>{esc(o['order_no'])}</ORDERNO>\n"
                + (f"     <BATCHRATE>{rate:.2f}/{esc(ln['unit'])}</BATCHRATE>\n"
                   f"     <BATCHDISCOUNT>{DISCOUNT_FIRST:g}</BATCHDISCOUNT>\n"
                   if rate else "")
                + f"     <AMOUNT>{b_amt}</AMOUNT>\n"
                f"     <ACTUALQTY>{q}</ACTUALQTY>\n"
                f"     <BILLEDQTY>{q}</BILLEDQTY>\n"
                f"     <ORDERDUEDATE>{due}</ORDERDUEDATE>\n"
                "    </BATCHALLOCATIONS.LIST>"
            )
        lq = _fmt_qty(ln["qty"], ln["unit"])
        # Batches are rounded individually, so the line takes their sum rather
        # than its own rounding of the total — otherwise a half-paisa gap
        # between the two would trip the arithmetic check.
        line_amt = (round(sum(_net_amount(rate, sz["qty"]) for sz in ln["sizes"]), 2)
                    if rate else 0)
        inv.append(
            "   <ALLINVENTORYENTRIES.LIST>\n"
            f"    <STOCKITEMNAME>{esc(ln['item'])}</STOCKITEMNAME>\n"
            "    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n"
            + (f"    <RATE>{rate:.2f}/{esc(ln['unit'])}</RATE>\n"
               f"    <DISCOUNT>{DISCOUNT_FIRST:g}</DISCOUNT>\n" if rate else "")
            + f"    <AMOUNT>{line_amt:.2f}</AMOUNT>\n"
            f"    <ACTUALQTY>{lq}</ACTUALQTY>\n"
            f"    <BILLEDQTY>{lq}</BILLEDQTY>\n"
            + "\n".join(batches) + "\n"
            "    <ACCOUNTINGALLOCATIONS.LIST>\n"
            f"     <LEDGERNAME>{esc(ocfg.sales_ledger)}</LEDGERNAME>\n"
            "     <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n"
            "     <LEDGERFROMITEM>No</LEDGERFROMITEM>\n"
            # The line every specimen carries and the first two imports left
            # out: without it Tally REMOVES zero-amount allocations on
            # import, then drops the item lines that depended on them, and
            # the voucher arrives empty — "No accounting or inventory
            # entries are available". For a quantity-only order this flag is
            # the difference between importing and not existing.
            "     <REMOVEZEROENTRIES>No</REMOVEZEROENTRIES>\n"
            "     <ISPARTYLEDGER>No</ISPARTYLEDGER>\n"
            f"     <AMOUNT>{line_amt:.2f}</AMOUNT>\n"
            "    </ACCOUNTINGALLOCATIONS.LIST>\n"
            "   </ALLINVENTORYENTRIES.LIST>"
        )
        grand += line_amt

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
        # Present in EVERY operator-entered specimen and absent from the
        # first three attempts. This company runs multiple GST registrations
        # (tax units), and TallyPrime 3+ requires each voucher to bind to
        # one; the voucher-number series and manual numbering style likewise.
        "   <ISINVOICE>No</ISINVOICE>\n"
        # Optional = Tally's own draft state: the voucher exists, holds its
        # lines, affects nothing anywhere — not even the order book — until
        # a human regularises it. Import validation is laxer for drafts,
        # which is exactly the property a zero-value order needs.
        + ("   <ISOPTIONAL>Yes</ISOPTIONAL>\n" if optional else "")
        + "   <NUMBERINGSTYLE>Manual</NUMBERINGSTYLE>\n"
        "   <VOUCHERNUMBERSERIES>Default</VOUCHERNUMBERSERIES>\n"
        f"   <GSTREGISTRATION TAXTYPE=\"GST\" "
        f"TAXREGISTRATION=\"{esc(ocfg.cmp_gstin)}\">"
        f"{esc(ocfg.gst_registration)}</GSTREGISTRATION>\n"
        f"   <CMPGSTIN>{esc(ocfg.cmp_gstin)}</CMPGSTIN>\n"
        f"   <EFFECTIVEDATE>{d}</EFFECTIVEDATE>\n"
        + (f"   <PARTYGSTIN>{esc(party_gstin)}</PARTYGSTIN>\n" if party_gstin else "")
        + (f"   <PLACEOFSUPPLY>{esc(_state_from_gstin(party_gstin))}</PLACEOFSUPPLY>\n"
           f"   <STATENAME>{esc(_state_from_gstin(party_gstin))}</STATENAME>\n"
           f"   <CONSIGNEESTATENAME>{esc(_state_from_gstin(party_gstin))}</CONSIGNEESTATENAME>\n"
           if _state_from_gstin(party_gstin) else "")
        + f"   <NARRATION>Queued from photographed order via Claude "
        f"({esc(o['order_key'])}).</NARRATION>\n"
        + "\n".join(inv) + "\n"
        "   <LEDGERENTRIES.LIST>\n"
        f"    <LEDGERNAME>{esc(o['party'])}</LEDGERNAME>\n"
        "    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n"
        "    <LEDGERFROMITEM>No</LEDGERFROMITEM>\n"
        "    <REMOVEZEROENTRIES>No</REMOVEZEROENTRIES>\n"
        "    <ISPARTYLEDGER>Yes</ISPARTYLEDGER>\n"
        # Debit exports NEGATIVE in this book: the party owes the total, so
        # the party line carries minus the inventory sum.
        f"    <AMOUNT>{-grand:.2f}</AMOUNT>\n"
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
    """(party names, item names, party->gstin) — fetched ONCE per run."""
    if company not in cache:
        cfg.company = company
        ledgers = fetch_ledgers(cfg)
        parties = {l.name for l in ledgers}
        gstins = {l.name: (l.gstin or "").strip() for l in ledgers}
        items = {i.name for i in fetch_stock_items(cfg, date.today())}
        log.info("Cached masters for %r: %d ledgers, %d stock items.",
                 company, len(parties), len(items))
        cache[company] = (parties, items, gstins)
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
    # Keep the WHOLE response on disk: the first two failures were diagnosed
    # through a 500-char window that cut off exactly at the interesting tag,
    # and each blind retry costs the operator a full push/download/run cycle.
    dump = HERE / "last_import_response.xml"
    try:
        dump.write_text(resp, encoding="utf-8")
        where = f" (full response saved to {dump.name})"
    except OSError:
        where = ""
    head = " ".join(resp.split())[:300]
    _mark(fc, key, "Failed",
          error=f"Tally did not confirm the import (CREATED=0): {head}")
    log.error("Order %s: Tally did not confirm the import%s.", key, where)
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
            party_gstin = ""
            if _company_open(cfg, o["company"], open_cache):
                try:
                    parties, items, gstins = _masters(cfg, o["company"], masters_cache)
                    party_gstin = gstins.get(o["party"], "")
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
            xml = build_envelope(o, ocfg, party_gstin, args.optional)
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
            parties, items, gstins = _masters(cfg, o["company"], masters_cache)
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

        xml = build_envelope(o, ocfg, gstins.get(o["party"], ""), args.optional)
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
    p.add_argument("--optional", action="store_true",
                   help="import as an OPTIONAL (draft) voucher — posts even "
                        "less than a Sales Order (nothing at all, not even "
                        "into order books) until staff regularise it in "
                        "Tally. The escape hatch if Tally refuses zero-value "
                        "regular orders.")
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
