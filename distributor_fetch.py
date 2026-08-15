"""
distributor_fetch.py — Tally exports for the distributor-facing mirror.

Everything a distributor sees in the portal is pulled here: sales orders,
sales invoices, receipts, delivery notes (when the book ever issues any) and
batch-wise stock. Built on tally_client's primitives and its hard-won rules:

  * A Voucher Collection is scoped by <FILTER> + <SYSTEM Formulae>, never by
    SVFROMDATE — and THE FETCH FIELD LIST DECIDES WHETHER THE FILTER APPLIES.
    Every field list in this file was probed against the live build
    (TallyPrime Edit Log 7.0, 2026-08-15) before being trusted; an unproven
    field would make Tally answer with zero rows and no error.
  * Amounts arrive credit-positive; the mirror stores debit-positive.
  * Quantities are display strings ("7.50 Doz"); resolve via the unit table
    and always keep the raw string.

The parse functions are pure (XML string in, payload dicts out) so they are
testable without a live Tally.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from tally_client import (
    TallyConfig,
    TallyError,
    _company_tag,
    _parse_qty,
    _parse_rate,
    _parse_xml,
    _post,
    _tally_date_to_iso,
    _text,
    _to_float,
    assert_company_loaded,
)

log = logging.getLogger("sync")

# ---------------------------------------------------------------------------
# Field lists — each PROVEN against the live build before use
# ---------------------------------------------------------------------------

_BASE = ("Guid,Date,VoucherTypeName,VoucherNumber,PartyLedgerName,"
         "AllLedgerEntries.LedgerName,AllLedgerEntries.Amount,"
         "AllLedgerEntries.IsDeemedPositive")

# Sales Orders / Sales invoices with item lines and size (batch) allocations.
# Probed 2026-08-15: 172 Sales Orders and 32 Sales came back complete, with
# BATCHNAME / ORDERNO / ORDERDUEDATE / BATCHRATE / BATCHDISCOUNT / the
# BatchDiscount2 UDF all present. Tally exports whole sub-objects once any
# dotted field of theirs is fetched, which is why the response is richer than
# the request.
_INVENTORY = _BASE + (
    ",Reference,Narration,IsCancelled,IsOptional,AlterID"
    ",AllInventoryEntries.StockItemName,AllInventoryEntries.ActualQty"
    ",AllInventoryEntries.Rate,AllInventoryEntries.Amount"
    ",AllInventoryEntries.BatchAllocations.BatchName"
    ",AllInventoryEntries.BatchAllocations.ActualQty"
    ",AllInventoryEntries.BatchAllocations.OrderDueDate"
    ",AllInventoryEntries.BatchAllocations.OrderNo"
)

# Receipts with bill-wise allocations. Probed the same day: 26 receipts,
# BILLALLOCATIONS carrying NAME / BILLTYPE / AMOUNT.
_RECEIPT = _BASE + (
    ",Reference,Narration,IsCancelled,IsOptional,AlterID"
    ",AllLedgerEntries.BillAllocations.Name"
    ",AllLedgerEntries.BillAllocations.BillType"
    ",AllLedgerEntries.BillAllocations.Amount"
)

# Optional upgrade for receipts: bank allocations (UTR / instrument number).
# NOT yet proven on this build — used only via the fallback in fetch_receipts,
# never assumed.
_RECEIPT_BANK = _RECEIPT + (
    ",AllLedgerEntries.BankAllocations.InstrumentNumber"
    ",AllLedgerEntries.BankAllocations.InstrumentDate"
    ",AllLedgerEntries.BankAllocations.TransactionType"
)


def _voucher_body(cfg: TallyConfig, frm: date, to: date, vtypes: list[str],
                  fields: str) -> str:
    """One voucher-type-scoped Collection request, filter-dotted shape."""
    fd, td = frm.strftime("%Y%m%d"), to.strftime("%Y%m%d")
    types = " or ".join(f'$VoucherTypeName = "{t}"' for t in vtypes)
    cond = (f'$Date &gt;= $$Date:"{fd}" and $Date &lt;= $$Date:"{td}" '
            f'and ({types})')
    return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>TB_DistVch</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="TB_DistVch" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <TYPE>Voucher</TYPE>
    <FETCH>{fields}</FETCH>
    <FILTER>TBDistPeriod</FILTER>
   </COLLECTION>
   <SYSTEM TYPE="Formulae" NAME="TBDistPeriod">{cond}</SYSTEM>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


# ---------------------------------------------------------------------------
# Date helpers for Tally's "Due Date" type
# ---------------------------------------------------------------------------

_DMY_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{2,4})$")
_DAYS_RE = re.compile(r"^(\d+)\s*Days?$", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

# Tally's JD attribute is an Excel-style serial. Anchored on a live pair
# (JD='46234', P='1-Aug-26'): 1899-12-30 + 46234 days = 2026-08-01. Tested.
_JD_EPOCH = date(1899, 12, 30)


def parse_due_date(el, voucher_date: str) -> str:
    """
    Resolve an ORDERDUEDATE element to ISO, or "".

    Three forms appear in the same book, sometimes in adjacent lines:
    an absolute "1-Aug-26", a relative "1 Days" (from the voucher date), and
    the JD serial attribute. The serial is preferred — it is unambiguous —
    with the text forms as fallback.
    """
    if el is None:
        return ""
    jd = (el.get("JD") or "").strip()
    if jd.isdigit():
        try:
            return (_JD_EPOCH + timedelta(days=int(jd))).isoformat()
        except OverflowError:
            pass
    txt = (el.text or "").strip()
    m = _DMY_RE.match(txt)
    if m:
        d, mon, y = int(m.group(1)), _MONTHS.get(m.group(2).lower()), int(m.group(3))
        if mon:
            y += 2000 if y < 100 else 0
            try:
                return date(y, mon, d).isoformat()
            except ValueError:
                return ""
    m = _DAYS_RE.match(txt)
    if m and len(voucher_date) == 10:
        try:
            return (date.fromisoformat(voucher_date)
                    + timedelta(days=int(m.group(1)))).isoformat()
        except ValueError:
            return ""
    return ""


def _udf_number(parent, name: str) -> float:
    """Read a numeric UDF that tally_client's cleaner renamed UDF:x -> UDF_x."""
    el = parent.find(f"UDF_{name}.LIST/UDF_{name}")
    if el is None:
        el = parent.find(f"UDF_{name}")
    return _to_float(_text(el))


# ---------------------------------------------------------------------------
# Shared voucher-header parse
# ---------------------------------------------------------------------------

def _header(vel, company: str) -> dict:
    return {
        "guid": _text(vel.find("GUID")),
        "company": company,
        "voucher_number": _text(vel.find("VOUCHERNUMBER")),
        "date": _tally_date_to_iso(_text(vel.find("DATE"))),
        "party": _text(vel.find("PARTYLEDGERNAME")),
        "reference": _text(vel.find("REFERENCE")),
        "narration": _text(vel.find("NARRATION")),
        "is_cancelled": _text(vel.find("ISCANCELLED")).lower() == "yes",
        "is_optional": _text(vel.find("ISOPTIONAL")).lower() == "yes",
        "alter_id": _text(vel.find("ALTERID")),
    }


def _party_amount(vel, party: str) -> float:
    """
    The voucher's headline value: the party's own ledger entry, made positive.

    On a Sales invoice the party line is the debit (exported negative), on a
    Receipt it is the credit (exported positive) — abs() of the party line is
    the printed figure either way. Falls back to summing inventory amounts
    when the party line is missing (some order exports omit ledger entries).
    """
    for le in vel.iter("ALLLEDGERENTRIES.LIST"):
        if _text(le.find("LEDGERNAME")) == party:
            amt = _to_float(_text(le.find("AMOUNT")))
            if amt:
                return abs(amt)
    total = 0.0
    for inv in vel.iter("ALLINVENTORYENTRIES.LIST"):
        total += abs(_to_float(_text(inv.find("AMOUNT"))))
    return round(total, 2)


def _inventory_lines(vel, voucher_date: str) -> list[dict]:
    """
    One payload line per (item, batch allocation) — the size grain.

    An inventory entry with no batch allocations still yields one line with an
    empty size, so nothing silently disappears.
    """
    lines = []
    for inv in vel.iter("ALLINVENTORYENTRIES.LIST"):
        item = _text(inv.find("STOCKITEMNAME"))
        if not item:
            continue
        rate, rate_unit = _parse_rate(_text(inv.find("RATE")))
        discount = _to_float(_text(inv.find("DISCOUNT")))
        batches = inv.findall("BATCHALLOCATIONS.LIST")
        if not batches:
            qty, unit, _raw = _parse_qty(_text(inv.find("ACTUALQTY")))
            lines.append({
                "item_name": item, "size_batch": "", "godown": "",
                "qty": abs(qty), "unit": unit,
                "billed_qty": abs(_parse_qty(_text(inv.find("BILLEDQTY")))[0]),
                "rate": rate, "rate_unit": rate_unit,
                "discount": discount,
                "discount2": _udf_number(inv, "BATCHDISCOUNT2"),
                "amount": abs(_to_float(_text(inv.find("AMOUNT")))),
                "due_date": "", "order_no": "",
            })
            continue
        for b in batches:
            qty, unit, _raw = _parse_qty(_text(b.find("ACTUALQTY")))
            brate, brate_unit = _parse_rate(_text(b.find("BATCHRATE")))
            order_no = _text(b.find("ORDERNO"))
            # The order-pad TDL computes balance stock PER SIZE into a BlncQty
            # UDF on every batch line. This is the only per-size stock figure
            # this build exports anywhere: the StockItem BatchAllocations walk
            # answers with the ITEM total repeated on every batch row
            # (measured — 1395.75 Doz on all 12 sizes), so the voucher UDF,
            # dated by its voucher, is the honest source.
            bal_qty, bal_unit, _ = _parse_qty(_text(
                b.find("UDF_BLNCQTY.LIST/UDF_BLNCQTY")))
            lines.append({
                "item_name": item,
                "size_batch": _text(b.find("BATCHNAME")),
                "godown": _text(b.find("GODOWNNAME")),
                "qty": abs(qty), "unit": unit,
                "billed_qty": abs(_parse_qty(_text(b.find("BILLEDQTY")))[0]),
                "rate": brate or rate,
                "rate_unit": brate_unit or rate_unit,
                "discount": _to_float(_text(b.find("BATCHDISCOUNT"))) or discount,
                "discount2": _udf_number(b, "BATCHDISCOUNT2"),
                "amount": abs(_to_float(_text(b.find("AMOUNT")))),
                "due_date": parse_due_date(b.find("ORDERDUEDATE"), voucher_date),
                "order_no": "" if order_no.lower() == "not applicable" else order_no,
                "balance_qty": bal_qty,
                "balance_unit": bal_unit,
            })
    return lines


def harvest_size_balances(payloads: list[dict]) -> list[dict]:
    """
    Latest per-size stock balance out of a batch of voucher payloads.

    One row per (item, size), carrying the NEWEST BlncQty seen and the voucher
    it came from, so every figure is dated and auditable. Vouchers are punched
    daily in this book, which keeps the figures fresh in practice — and the
    portal buckets them as in/low/out anyway, never showing the number.
    """
    best: dict[tuple, dict] = {}
    for p in payloads:
        if p.get("is_cancelled") or p.get("is_optional"):
            continue
        as_of = p.get("date") or ""
        voucher = p.get("voucher_number") or p.get("invoice_no") or ""
        for line in p.get("lines") or []:
            size = line.get("size_batch") or ""
            unit = line.get("balance_unit") or ""
            if not size or not unit:
                continue          # a blank unit means the UDF was absent
            key = (line["item_name"], size)
            cur = best.get(key)
            if cur is None or as_of >= cur["as_of"]:
                best[key] = {
                    "item_name": line["item_name"],
                    "batch_name": size,
                    "closing_qty": line.get("balance_qty") or 0.0,
                    "closing_qty_unit": unit,
                    "as_of": as_of,
                    "source_voucher": voucher,
                }
    return list(best.values())


# ---------------------------------------------------------------------------
# GST classification (invoices)
# ---------------------------------------------------------------------------

_TAX_PATTERNS = (
    ("igst", re.compile(r"\bigst\b", re.I)),
    ("cgst", re.compile(r"\bcgst\b", re.I)),
    ("sgst", re.compile(r"\bsgst\b|\butgst\b", re.I)),
    ("cess", re.compile(r"\bcess\b", re.I)),
)
_ROUND_RE = re.compile(r"round", re.I)


def _classify_taxes(vel, party: str) -> dict:
    """
    Split an invoice's non-party ledger entries into the GST breakup.

    Name-based, because that is all the export offers — and this book's names
    are regular ("IGST Output", "Rounded Off", "Sale Central 5%"). Taxable
    value is DERIVED (total - taxes - round-off) rather than summed from sales
    ledgers, so an unrecognised ledger name cannot corrupt it.
    """
    out = {"cgst": 0.0, "sgst": 0.0, "igst": 0.0, "cess": 0.0, "round_off": 0.0}
    bill_refs = []
    for le in vel.iter("ALLLEDGERENTRIES.LIST"):
        name = _text(le.find("LEDGERNAME"))
        if not name:
            continue
        amt = _to_float(_text(le.find("AMOUNT")))
        if name == party:
            for ba in le.findall("BILLALLOCATIONS.LIST"):
                ref = _text(ba.find("NAME"))
                if ref:
                    bill_refs.append(ref)
            continue
        if _ROUND_RE.search(name):
            out["round_off"] += amt
            continue
        for key, pat in _TAX_PATTERNS:
            if pat.search(name):
                out[key] += abs(amt)
                break
    out = {k: round(v, 2) for k, v in out.items()}
    out["bill_refs"] = ", ".join(bill_refs)
    return out


# ---------------------------------------------------------------------------
# Public fetchers
# ---------------------------------------------------------------------------

def fetch_sales_orders(cfg: TallyConfig, frm: date, to: date) -> list[dict]:
    """Sales Orders with one line per (item, size)."""
    assert_company_loaded(cfg)
    raw = _post(cfg, _voucher_body(cfg, frm, to, ["Sales Order"], _INVENTORY))
    out = []
    for vel in _parse_xml(raw).iter("VOUCHER"):
        h = _header(vel, cfg.company)
        if not h["guid"]:
            continue
        h["amount"] = _party_amount(vel, h["party"])
        h["lines"] = _inventory_lines(vel, h["date"])
        out.append(h)
    log.info("Fetched %d sales orders for %s..%s", len(out), frm, to)
    return out


def fetch_invoices(cfg: TallyConfig, frm: date, to: date,
                   vtypes: list[str] | None = None) -> list[dict]:
    """Sales invoices with lines, GST breakup and bill refs."""
    assert_company_loaded(cfg)
    raw = _post(cfg, _voucher_body(cfg, frm, to, vtypes or ["Sales"], _INVENTORY))
    out = []
    for vel in _parse_xml(raw).iter("VOUCHER"):
        h = _header(vel, cfg.company)
        if not h["guid"]:
            continue
        h["invoice_no"] = h.pop("voucher_number")
        h["amount"] = _party_amount(vel, h["party"])
        taxes = _classify_taxes(vel, h["party"])
        h.update(taxes)
        h["taxable_value"] = round(
            h["amount"] - taxes["cgst"] - taxes["sgst"] - taxes["igst"]
            - taxes["cess"] - taxes["round_off"], 2)
        h["lines"] = _inventory_lines(vel, h["date"])
        out.append(h)
    log.info("Fetched %d invoices for %s..%s", len(out), frm, to)
    return out


def fetch_delivery_notes(cfg: TallyConfig, frm: date, to: date,
                         vtypes: list[str]) -> list[dict]:
    """
    Delivery notes, for books that issue them.

    MEASURED 2026-08-15: this book has NO Delivery Note voucher type — goods
    reach a distributor as a Sales invoice — so with the default config this
    returns [] quickly and cheaply. The fetcher exists for the day that
    changes.
    """
    if not vtypes:
        return []
    assert_company_loaded(cfg)
    raw = _post(cfg, _voucher_body(cfg, frm, to, vtypes, _INVENTORY))
    out = []
    for vel in _parse_xml(raw).iter("VOUCHER"):
        h = _header(vel, cfg.company)
        if not h["guid"]:
            continue
        h["order_ref"] = h["reference"]
        h["amount"] = _party_amount(vel, h["party"])
        h["lines"] = _inventory_lines(vel, h["date"])
        out.append(h)
    log.info("Fetched %d delivery notes for %s..%s", len(out), frm, to)
    return out


def _receipt_payload(vel, company: str) -> dict | None:
    h = _header(vel, company)
    if not h["guid"]:
        return None
    party = h["party"]
    amount = 0.0
    mode = ""
    allocations = []
    instrument_no = instrument_date = txn_type = ""
    for le in vel.iter("ALLLEDGERENTRIES.LIST"):
        name = _text(le.find("LEDGERNAME"))
        if not name:
            continue
        amt = _to_float(_text(le.find("AMOUNT")))
        is_debit = _text(le.find("ISDEEMEDPOSITIVE")).lower() == "yes"
        if name == party:
            amount = abs(amt)
            for ba in le.findall("BILLALLOCATIONS.LIST"):
                ref = _text(ba.find("NAME"))
                ba_amt = abs(_to_float(_text(ba.find("AMOUNT"))))
                if ref or ba_amt:
                    allocations.append({
                        "bill_ref": ref,
                        "bill_type": _text(ba.find("BILLTYPE")),
                        "amount": ba_amt,
                    })
        elif is_debit:
            # The debit side of a receipt is where the money landed.
            mode = mode or name
            for bk in le.findall("BANKALLOCATIONS.LIST"):
                instrument_no = instrument_no or _text(bk.find("INSTRUMENTNUMBER"))
                instrument_date = instrument_date or _tally_date_to_iso(
                    _text(bk.find("INSTRUMENTDATE")))
                txn_type = txn_type or _text(bk.find("TRANSACTIONTYPE"))
    h.update({
        "amount": amount, "mode": mode,
        "instrument_no": instrument_no, "instrument_date": instrument_date,
        "transaction_type": txn_type, "allocations": allocations,
    })
    return h


def fetch_receipts(cfg: TallyConfig, frm: date, to: date) -> list[dict]:
    """
    Receipts with bill-wise allocations, and bank details where offered.

    The bank-allocation fields are an UNPROVEN upgrade on this build, and an
    unknown FETCH field makes Tally return zero rows with no error. So the
    rich request is tried first and judged against the proven one: if it
    returns nothing where the proven shape returns rows, the proven result is
    used and the downgrade is logged.
    """
    assert_company_loaded(cfg)
    rich_rows = None
    try:
        raw = _post(cfg, _voucher_body(cfg, frm, to, ["Receipt"], _RECEIPT_BANK))
        rich_rows = [r for r in (_receipt_payload(v, cfg.company)
                                 for v in _parse_xml(raw).iter("VOUCHER")) if r]
    except TallyError as exc:
        log.info("Receipt fetch with bank fields failed (%s) — using the "
                 "proven field set.", exc)

    if rich_rows:
        log.info("Fetched %d receipts (with bank details) for %s..%s",
                 len(rich_rows), frm, to)
        return rich_rows

    raw = _post(cfg, _voucher_body(cfg, frm, to, ["Receipt"], _RECEIPT))
    rows = [r for r in (_receipt_payload(v, cfg.company)
                        for v in _parse_xml(raw).iter("VOUCHER")) if r]
    if rich_rows is not None and rows:
        log.info("Bank-allocation fields blank this build's receipt export "
                 "(0 rows vs %d proven) — UTR matching will rely on amounts.",
                 len(rows))
    log.info("Fetched %d receipts for %s..%s", len(rows), frm, to)
    return rows


# ---------------------------------------------------------------------------
# Size enumeration (which sizes exist per item)
# ---------------------------------------------------------------------------
#
# Per-size CLOSING STOCK cannot be read from masters on this build: a
# SOURCECOLLECTION + WALK over BatchAllocations enumerates every (size,
# godown) pair correctly, but ClosingBalance/ClosingValue on the walked
# object answer with the ITEM total repeated on every row (measured live,
# 2026-08-15: 1395.75 Doz on all 12 sizes of 001 SPORT BRA). Per-size
# figures therefore come from harvest_size_balances() above. This walk is
# still the authority on WHICH sizes an item comes in.

def _size_walk_request(cfg: TallyConfig, as_on: date, start: date) -> str:
    return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>TB_Sizes</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVFROMDATE TYPE="Date">{start.strftime('%Y%m%d')}</SVFROMDATE>
   <SVTODATE TYPE="Date">{as_on.strftime('%Y%m%d')}</SVTODATE>
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="TB_SizeItems" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <TYPE>StockItem</TYPE>
   </COLLECTION>
   <COLLECTION NAME="TB_Sizes" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <SOURCECOLLECTION>TB_SizeItems</SOURCECOLLECTION>
    <WALK>BatchAllocations</WALK>
    <COMPUTE>ItemName : $$Owner:$Name</COMPUTE>
    <NATIVEMETHOD>BatchName</NATIVEMETHOD>
    <NATIVEMETHOD>GodownName</NATIVEMETHOD>
   </COLLECTION>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


def parse_item_sizes(raw: str) -> dict[str, list[str]]:
    """{item name: [size, ...]} from the size walk, order preserved."""
    sizes: dict[str, list[str]] = {}
    for el in _parse_xml(raw).iter("ITEMBATCHALLOCATIONS"):
        item = _text(el.find("ITEMNAME"))
        batch = _text(el.find("BATCHNAME"))
        if not item or not batch:
            continue
        bucket = sizes.setdefault(item, [])
        if batch not in bucket:
            bucket.append(batch)
    return sizes


def fetch_item_sizes(cfg: TallyConfig, as_on: date, start: date) -> dict[str, list[str]]:
    """Which sizes each item exists in. One modest request per company."""
    assert_company_loaded(cfg)
    raw = _post(cfg, _size_walk_request(cfg, as_on, start))
    sizes = parse_item_sizes(raw)
    log.info("Fetched size lists for %d items", len(sizes))
    return sizes


# ---------------------------------------------------------------------------
# Ledger extras — the distributor-facing master fields
# ---------------------------------------------------------------------------

# Every method here was accepted by the live build in one request
# (2026-08-15). CreditLimit and PriceLevel came back EMPTY on every ledger
# sampled — mirrored anyway so they light up the day someone sets them.
_LEDGER_EXTRA_METHODS = [
    "Parent", "OpeningBalance", "ClosingBalance", "PartyGSTIN", "Email",
    "LedgerPhone", "LedgerMobile", "IsBillWiseOn", "GUID", "MasterId",
    "AlterId", "CreditLimit", "BillCreditPeriod", "PriceLevel",
    "LedgerStateName", "PinCode", "CountryName", "GSTRegistrationType",
    "MailingName", "Address",
]


def ledger_extras_request(cfg: TallyConfig, as_on: date, start: date) -> str:
    lines = "\n     ".join(f"<NATIVEMETHOD>{m}</NATIVEMETHOD>"
                           for m in _LEDGER_EXTRA_METHODS)
    return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>TB_LedgerX</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVFROMDATE TYPE="Date">{start.strftime('%Y%m%d')}</SVFROMDATE>
   <SVTODATE TYPE="Date">{as_on.strftime('%Y%m%d')}</SVTODATE>
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="TB_LedgerX" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <TYPE>Ledger</TYPE>
     {lines}
   </COLLECTION>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


def parse_ledger_extras(raw: str) -> dict[str, dict]:
    """
    Per-ledger distributor fields, keyed by ledger name.

    Merged into the ordinary ledger payload by sync.py — one extra request per
    company, not a second ledger pipeline.
    """
    out: dict[str, dict] = {}
    for el in _parse_xml(raw).iter("LEDGER"):
        name = el.get("NAME") or _text(el.find("NAME"))
        if not name:
            continue
        address = " ".join(
            t for t in (_text(a) for a in el.iter("ADDRESS")) if t)
        credit_period = _text(el.find("BILLCREDITPERIOD"))
        out[name] = {
            "credit_limit": abs(_to_float(_text(el.find("CREDITLIMIT")))),
            "credit_period": "" if credit_period == "0" else credit_period,
            "price_level": _text(el.find("PRICELEVEL")),
            "mobile": _text(el.find("LEDGERMOBILE")),
            "state": _text(el.find("LEDGERSTATENAME")),
            "pincode": _text(el.find("PINCODE")),
            "country": _text(el.find("COUNTRYNAME")),
            "gst_registration_type": _text(el.find("GSTREGISTRATIONTYPE")),
            "mailing_name": _text(el.find("MAILINGNAME")),
            "address": address,
        }
    return out


def fetch_ledger_extras(cfg: TallyConfig, as_on: date, start: date) -> dict[str, dict]:
    assert_company_loaded(cfg)
    raw = _post(cfg, ledger_extras_request(cfg, as_on, start))
    extras = parse_ledger_extras(raw)
    log.info("Fetched distributor fields for %d ledgers", len(extras))
    return extras
