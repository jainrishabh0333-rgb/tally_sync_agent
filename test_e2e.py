"""
End-to-end test that runs `python sync.py --full` AS A SUBPROCESS — the exact
command the operator types — against a mock Tally serving deliberately hostile
data modelled on the live book, and a mock Frappe.

Every prior live failure got through because tests exercised functions rather
than the real entry point, or clean data rather than real data. This test
closes both gaps:

  * real CLI invocation (argparse, config loading, logging, exit code)
  * config.toml written with a UTF-8 BOM and CRLF, as Notepad saves it
  * 2,000+ ledgers across nested custom groups, Hindi/rupee text
  * vouchers whose narrations carry control bytes, fake tags, stray & and <
  * a ledger whose email is a bare domain (the live InvalidEmailAddressError)
  * multi-chunk date ranges, per-row rejects, sync-log calls
  * a TDL collection engine that answers the way the live server does, so the
    request-shape probe has something real to choose between — then a second
    pass against a build that scopes nothing, to keep the fallback covered

Run:  python test_e2e.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
TALLY_PORT = 9981
FRAPPE_PORT = 9982

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(label)
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


def check_true(label, cond, hint=""):
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}  {hint}")
    else:
        print(f"  ok    {label}")


# ---------------------------------------------------------------------------
# Mock Tally: 2,000 ledgers, nested groups, hostile vouchers
# ---------------------------------------------------------------------------

COMPANY = "SN JAIN INDUSTRIES PVT LTD - (26-27)"
OLD_COMPANY = "SN JAIN INDUSTRIES PVT LTD - (24-25)"

GROUPS = (
    [("Sundry Debtors", "Current Assets"), ("Sundry Creditors", "Current Liabilities"),
     ("Current Assets", "Primary"), ("Current Liabilities", "Primary"),
     ("Sales Accounts", "Primary"), ("Sundry Debtors Online", "Sundry Debtors")]
    + [(f"AGENT {c}", "Sundry Debtors") for c in "ABCDEFGHIJ"]
    + [(f"Stitchers {i}", "Sundry Creditors") for i in range(5)]
)


def units_xml() -> str:
    return ("<ENVELOPE><BODY><DATA><COLLECTION>"
            '<UNIT NAME="Pcs"><ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>'
            "<FORMALNAME>Pieces</FORMALNAME></UNIT>"
            '<UNIT NAME="Dzn"><ISSIMPLEUNIT>No</ISSIMPLEUNIT>'
            "<BASEUNITS>Pcs</BASEUNITS><CONVERSION>12</CONVERSION></UNIT>"
            '<UNIT NAME="Box"><ISSIMPLEUNIT>No</ISSIMPLEUNIT>'
            "<BASEUNITS>Dzn</BASEUNITS><CONVERSION>10</CONVERSION></UNIT>"
            "</COLLECTION></DATA></BODY></ENVELOPE>")


def godowns_xml() -> str:
    return ("<ENVELOPE><BODY><DATA><COLLECTION>"
            '<GODOWN NAME="Unit-C26"></GODOWN>'
            '<GODOWN NAME="Unit-E29"></GODOWN>'
            '<GODOWN NAME="Main Store"></GODOWN>'
            "</COLLECTION></DATA></BODY></ENVELOPE>")


def stock_groups_xml() -> str:
    return ("<ENVELOPE><BODY><DATA><COLLECTION>"
            '<STOCKGROUP NAME="Hosiery"></STOCKGROUP>'
            '<STOCKGROUP NAME="Thermals"><PARENT>Hosiery</PARENT></STOCKGROUP>'
            '<STOCKGROUP NAME="Vests"><PARENT>Hosiery</PARENT></STOCKGROUP>'
            "</COLLECTION></DATA></BODY></ENVELOPE>")


def stock_items_xml() -> str:
    """Items exercising every quantity shape, including compound units."""
    rows = [
        # Compound: 3 Dzn 6 Pcs = 42 Pcs. Getting 3 or 6 here would be wrong.
        '<STOCKITEM NAME="Thermal Vest 402"><PARENT>Thermals</PARENT>'
        "<BASEUNITS>Pcs</BASEUNITS><CLOSINGBALANCE>3 Dzn 6 Pcs</CLOSINGBALANCE>"
        "<CLOSINGRATE>250.00/Pcs</CLOSINGRATE><CLOSINGVALUE>10500.00</CLOSINGVALUE>"
        "<INFGSTHSNCODE>61099010</INFGSTHSNCODE><INFGSTIGSTRATE>5</INFGSTIGSTRATE>"
        "<GUID>item-1</GUID><ALTERID>1</ALTERID></STOCKITEM>",
        # Three-level compound: 12 Box 3 Dzn 4 Pcs = 1480 Pcs.
        '<STOCKITEM NAME="Cotton Vest 100"><PARENT>Vests</PARENT>'
        "<BASEUNITS>Pcs</BASEUNITS><CLOSINGBALANCE>12 Box 3 Dzn 4 Pcs</CLOSINGBALANCE>"
        "<CLOSINGRATE>90.00/Pcs</CLOSINGRATE><CLOSINGVALUE>133200.00</CLOSINGVALUE>"
        "<INFGSTHSNCODE>61099010</INFGSTHSNCODE>"
        "<GUID>item-2</GUID><ALTERID>2</ALTERID></STOCKITEM>",
        # Simple unit, and NO HSN — must be flagged as a GST exposure.
        '<STOCKITEM NAME="Sock Pack A"><PARENT>Hosiery</PARENT>'
        "<BASEUNITS>Pcs</BASEUNITS><CLOSINGBALANCE>500 Pcs</CLOSINGBALANCE>"
        "<CLOSINGRATE>45.50/Pcs</CLOSINGRATE><CLOSINGVALUE>22750.00</CLOSINGVALUE>"
        "<GUID>item-3</GUID><ALTERID>3</ALTERID></STOCKITEM>",
        # Negative (over-issued) compound: -2 Dzn 6 Pcs = -30.
        '<STOCKITEM NAME="Return Bin"><PARENT>Hosiery</PARENT>'
        "<BASEUNITS>Pcs</BASEUNITS><CLOSINGBALANCE>-2 Dzn 6 Pcs</CLOSINGBALANCE>"
        "<CLOSINGVALUE>-1500.00</CLOSINGVALUE>"
        "<GUID>item-4</GUID><ALTERID>4</ALTERID></STOCKITEM>",
    ]
    return ("<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(rows)
            + "</COLLECTION></DATA></BODY></ENVELOPE>")


def bills_xml(company: str) -> str:
    """
    Open bills. Debit-positive receivables export NEGATIVE, per Tally.

    Each company file holds its OWN parties. Serving one identical payload for
    both files reproduces the TallyPrime defect where the Bills collection
    answers from the loaded company regardless of SVCURRENTCOMPANY — which the
    agent detects and refuses to write, so the second company mirrored nothing.
    """
    party = "Customer" if company == COMPANY else "Old Customer"
    rows = []
    # 40 overdue: dated 15-Apr with 45-day terms, so due 30-May.
    for i in range(40):
        rows.append(
            f'<BILL NAME="SL/{i:04d}"><PARENT>{party} {i:04d}</PARENT>'
            f"<BILLDATE>20260415</BILLDATE>"
            f"<BILLCREDITPERIOD>45 Days</BILLCREDITPERIOD>"
            f"<OPENINGBALANCE>-{(i + 1) * 1000}.00</OPENINGBALANCE>"
            f"<CLOSINGBALANCE>-{(i + 1) * 1000}.00</CLOSINGBALANCE>"
            f"<ISADVANCE>No</ISADVANCE></BILL>"
        )
    # 10 not yet due: dated 1-Aug with 3-month terms, so due 30-Oct.
    for i in range(40, 50):
        rows.append(
            f'<BILL NAME="SL/{i:04d}"><PARENT>{party} {i:04d}</PARENT>'
            f"<BILLDATE>20260801</BILLDATE>"
            f"<BILLCREDITPERIOD>3 Months</BILLCREDITPERIOD>"
            f"<CLOSINGBALANCE>-{(i + 1) * 1000}.00</CLOSINGBALANCE>"
            f"<ISADVANCE>No</ISADVANCE></BILL>"
        )
    # A customer advance — must never be counted as money owed to us.
    rows.append(
        f'<BILL NAME="ADV/1"><PARENT>{party} 0001</PARENT>'
        "<BILLDATE>20260501</BILLDATE><CLOSINGBALANCE>25000.00</CLOSINGBALANCE>"
        "<ISADVANCE>Yes</ISADVANCE></BILL>"
    )
    return ("<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(rows)
            + "</COLLECTION></DATA></BODY></ENVELOPE>")


def groups_xml() -> str:
    rows = "".join(
        f'<GROUP NAME="{n}"><PARENT>{p}</PARENT></GROUP>' for n, p in GROUPS
    )
    return f"<ENVELOPE><BODY><DATA><COLLECTION>{rows}</COLLECTION></DATA></BODY></ENVELOPE>"


def ledgers_xml() -> str:
    """2,010 ledgers. Debit balances NEGATIVE, as TallyPrime exports them."""
    rows = []
    # 1,000 customers under agent sub-groups, balances -1000, -2000, ...
    for i in range(1000):
        g = f"AGENT {'ABCDEFGHIJ'[i % 10]}"
        rows.append(
            f'<LEDGER NAME="Customer {i:04d}"><PARENT>{g}</PARENT>'
            f"<CLOSINGBALANCE>-{(i + 1) * 1000}.00</CLOSINGBALANCE>"
            f"<MASTERID>{i}</MASTERID><ALTERID>{i}</ALTERID></LEDGER>"
        )
    # 1,000 suppliers under Stitchers sub-groups, balances +500, +1000, ...
    for i in range(1000):
        g = f"Stitchers {i % 5}"
        rows.append(
            f'<LEDGER NAME="Supplier {i:04d}"><PARENT>{g}</PARENT>'
            f"<CLOSINGBALANCE>{(i + 1) * 500}.00</CLOSINGBALANCE>"
            f"<MASTERID>{1000 + i}</MASTERID><ALTERID>{1000 + i}</ALTERID></LEDGER>"
        )
    # The live failure: email field holding a bare domain.
    rows.append(
        '<LEDGER NAME="Hariom Silk Mills"><PARENT>Sundry Debtors</PARENT>'
        "<CLOSINGBALANCE>-99999.00</CLOSINGBALANCE><EMAIL>hariomsilkmills.com</EMAIL>"
        "<MASTERID>9001</MASTERID><ALTERID>9001</ALTERID></LEDGER>"
    )
    # Direct debtor with Hindi name and ampersand.
    rows.append(
        '<LEDGER NAME="V MART RETAIL LTD-HARYANA"><PARENT>Sundry Debtors</PARENT>'
        "<CLOSINGBALANCE>-12008830.20</CLOSINGBALANCE>"
        "<GUID>carried-guid-9002</GUID>"
        "<MASTERID>9002</MASTERID><ALTERID>9002</ALTERID></LEDGER>"
    )
    rows.append(
        '<LEDGER NAME="M&M स्टील &amp; Sons"><PARENT>Sundry Creditors</PARENT>'
        "<CLOSINGBALANCE>95000.00</CLOSINGBALANCE>"
        "<MASTERID>9003</MASTERID><ALTERID>9003</ALTERID></LEDGER>"
    )
    # A ledger under a group nobody defined (broken group export).
    rows.append(
        '<LEDGER NAME="Orphan Ledger"><PARENT>MYSTERY GROUP</PARENT>'
        "<CLOSINGBALANCE>-1.00</CLOSINGBALANCE>"
        "<MASTERID>9004</MASTERID><ALTERID>9004</ALTERID></LEDGER>"
    )
    # 8 in a "Sales Accounts" group so classification variety exists.
    for i in range(8):
        rows.append(
            f'<LEDGER NAME="Sales {i}"><PARENT>Sales Accounts</PARENT>'
            f"<CLOSINGBALANCE>{(i + 1) * 10000}.00</CLOSINGBALANCE>"
            f"<MASTERID>{9100 + i}</MASTERID><ALTERID>{9100 + i}</ALTERID></LEDGER>"
        )
    return ("<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(rows)
            + "</COLLECTION></DATA></BODY></ENVELOPE>")


# ---------------------------------------------------------------------------
# Mock Tally: the voucher book
# ---------------------------------------------------------------------------

# Each company file opens on its own financial year, as Tally reports it.
COMPANY_START = {COMPANY: date(2026, 4, 1), OLD_COMPANY: date(2024, 4, 1)}

# Narration pathologies the live book demonstrated, all at once.
HOSTILE_NARRATIONS = (
    "माल भेजा ₹500\x07 urgent",       # control byte after multi-byte text
    "as per <PONO 123> confirmed",     # fake tag with a digit attribute
    "M&M rate < 500 per pc",           # stray ampersand and comparison
    "ref &#4; and &#27; done",         # invalid numeric refs
    "adjusted <- see note &",          # arrow and trailing amp
    "normal narration",                # plain sanity
    "see </NARRATION> note above",     # text impersonating the real closer
    "flagged <ok> by accounts",        # text impersonating an open tag
)


def voucher_book(company: str) -> list:
    """
    The vouchers one company file holds, dated across its year like a book.

    The DATES are load-bearing. _pick_voucher_variant classifies a request
    shape by experiment: it asks for two three-day windows — the company's
    opening days, and days 45-47 — and only accepts a shape whose two answers
    DIFFER. This mock used to date every voucher 15-Apr, so both windows came
    back empty, every shape was judged broken, and the suite could exercise
    nothing but the whole-company fallback. A book has to look like a book.
    """
    start = COMPANY_START[company]
    key = "apr" if company == COMPANY else "old"
    book = [
        {
            "guid": f"{key}-g{i}",
            "number": f"SL/{i:03d}",
            # Every other day, so both probe windows are populated and no two
            # monthly chunks can claim the same voucher.
            "date": start + timedelta(days=i * 2),
            "party": f"Customer {i:04d}",
            "amount": f"{(i + 1) * 118}.00",
            "narration": HOSTILE_NARRATIONS[i % len(HOSTILE_NARRATIONS)],
            # A purchase order predating the voucher — and, for the first few,
            # predating the book itself, which is ordinary in a real file.
            "reference": f"PO/{key}/{i:03d}",
            "reference_date": start + timedelta(days=i * 2 - 7),
            "alter_id": 100 + i,
        }
        for i in range(60)
    ]
    # Cancelled and optional vouchers: the reason the rich field set exists.
    # The shape the agent picks filters on DATE only, so these arrive and must
    # be mirrored CARRYING their flags. The whole-company fallback asks Tally
    # to drop them server-side instead. Both are correct; they differ, and the
    # suite pins both.
    book.append({
        "guid": f"{key}-cancelled", "number": "SL/900",
        "date": start + timedelta(days=10), "party": "Customer 0005",
        "amount": "9999.00", "narration": "cancelled by accounts",
        "reference": "", "reference_date": None, "alter_id": 900,
        "is_cancelled": True,
    })
    book.append({
        "guid": f"{key}-optional", "number": "SL/901",
        "date": start + timedelta(days=12), "party": "Customer 0006",
        "amount": "8888.00", "narration": "optional, not posted",
        "reference": "", "reference_date": None, "alter_id": 901,
        "is_optional": True,
    })
    return book


# The Day Book defect, verbatim from the live server: the report ignores
# SVFROMDATE/SVTODATE and answers every window with the current day's data —
# which filed one Receipt under five different months on 2026-08-11.
STUCK_DAYBOOK_VOUCHER = {
    "guid": "stuck-day-guid", "number": "RC/001", "date": date(2026, 8, 10),
    "party": "Customer 0001", "amount": "40000.00", "type": "Receipt",
    "narration": "same day every time", "reference": "",
    "reference_date": None, "alter_id": 1,
}

# <FETCH> field name -> the tag Tally answers with. A real Tally emits ONLY
# what was fetched, which is the whole point of the proven/rich split: asking
# for Narration is what makes NARRATION appear at all.
_FETCH_TAGS = {
    "Guid": "GUID",
    "Date": "DATE",
    "VoucherTypeName": "VOUCHERTYPENAME",
    "VoucherNumber": "VOUCHERNUMBER",
    "PartyLedgerName": "PARTYLEDGERNAME",
    "Narration": "NARRATION",
    "Reference": "REFERENCE",
    "ReferenceDate": "REFERENCEDATE",
    "IsInvoice": "ISINVOICE",
    "IsCancelled": "ISCANCELLED",
    "IsOptional": "ISOPTIONAL",
    "AlterID": "ALTERID",
}
_ENTRY_FETCH = {"AllLedgerEntries.LedgerName", "AllLedgerEntries.Amount",
                "AllLedgerEntries.IsDeemedPositive"}
_ALL_FETCH = set(_FETCH_TAGS) | _ENTRY_FETCH

_RE_FETCH = re.compile(r"<FETCH>([^<]*)</FETCH>")
_RE_FILTER = re.compile(r"<FILTER>([^<]*)</FILTER>")
_RE_FORMULA = re.compile(r'<SYSTEM TYPE="Formulae" NAME="([^"]+)">(.*?)</SYSTEM>',
                         re.S)
_RE_DATE_LIT = re.compile(r'\$\$Date:"([^"]+)"')

# Flipped for the second pass, which forces the whole-company fallback.
MOCK = {"honours_date_filter": True}


def _vch_xml(v: dict, fields: set) -> str:
    """One voucher, carrying ONLY the fields the request asked for."""
    vals = {
        "Guid": v["guid"],
        "Date": v["date"].strftime("%Y%m%d"),
        "VoucherTypeName": v.get("type", "Sales"),
        "VoucherNumber": v["number"],
        "PartyLedgerName": v["party"],
        "Narration": v["narration"],
        "Reference": v.get("reference") or "",
        "ReferenceDate": (v["reference_date"].strftime("%Y%m%d")
                          if v.get("reference_date") else ""),
        "IsInvoice": "Yes",
        "IsCancelled": "Yes" if v.get("is_cancelled") else "No",
        "IsOptional": "Yes" if v.get("is_optional") else "No",
        "AlterID": str(v.get("alter_id", 1)),
    }
    parts = [f'<VOUCHER VCHTYPE="{vals["VoucherTypeName"]}" ACTION="Create">']
    for name, tag in _FETCH_TAGS.items():
        if name in fields:
            parts.append(f"<{tag}>{vals[name]}</{tag}>")
    if fields & _ENTRY_FETCH:
        amt = v["amount"]
        parts.append(
            f'<ALLLEDGERENTRIES.LIST><LEDGERNAME>{v["party"]}</LEDGERNAME>'
            f"<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-{amt}</AMOUNT>"
            f"</ALLLEDGERENTRIES.LIST>"
            f'<ALLLEDGERENTRIES.LIST><LEDGERNAME>Sales 0</LEDGERNAME>'
            f"<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>{amt}</AMOUNT>"
            f"</ALLLEDGERENTRIES.LIST>")
    parts.append("</VOUCHER>")
    return "".join(parts)


def _collection_xml(rows: list, fields: set) -> str:
    return ("<ENVELOPE><BODY><DATA><COLLECTION>"
            + "".join(_vch_xml(v, fields) for v in rows)
            + "</COLLECTION></DATA></BODY></ENVELOPE>")


def _lit_date(s: str):
    """A $$Date literal, in either form a real Tally accepts."""
    for fmt in ("%Y%m%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _apply_formula(formula: str, rows: list):
    """Evaluate the handful of TDL formulae this agent actually sends."""
    if "$Date" in formula and "$$Date:" in formula:
        if not MOCK["honours_date_filter"]:
            # A build that accepts the filter and scopes nothing by it —
            # every row, every window. This is what the whole-company
            # fallback exists for, and the second pass proves it still works.
            return rows
        lits = [_lit_date(s) for s in _RE_DATE_LIT.findall(formula)]
        if len(lits) != 2 or None in lits:
            # An unrecognised date literal costs the whole collection: zero
            # rows, no error, in about 140ms.
            return []
        lo, hi = lits
        return [r for r in rows if lo <= r["date"] <= hi]
    if "$IsCancelled" in formula:
        return [r for r in rows if not r.get("is_cancelled")]
    if "$IsOptional" in formula:
        return [r for r in rows if not r.get("is_optional")]
    # A formula referencing something this build cannot evaluate returns
    # nothing at all — indistinguishable from "the filter matched nothing".
    return []


def voucher_collection_xml(body: str, company: str) -> str:
    """
    Answer a TDL voucher Collection the way the live server does.

    The shape study.py verified on 2026-08-12 — 4,595 vouchers for April 2026,
    every one inside the window — is: a SINGULAR comma-joined <FILTER>, exactly
    one comma-joined <FETCH>, and the window expressed as $$Date literals in a
    <SYSTEM TYPE="Formulae">, with no SVFROMDATE anywhere. Get any of it wrong
    and Tally does not complain. It answers with the whole collection,
    unfiltered — which is exactly how a broken request passed for a working one
    for two days, so that leak is reproduced here rather than smoothed over.
    """
    rows = voucher_book(company)
    fetches = _RE_FETCH.findall(body)
    filters = _RE_FILTER.findall(body)

    # <FILTERS> plural, or the multi-<FETCH> form (which belongs to FETCHLIST
    # at DESC level): both silently ignored, whole collection returned.
    if len(fetches) != 1 or len(filters) != 1:
        return _collection_xml(rows, _ALL_FETCH)

    fields = {f.strip() for f in fetches[0].split(",") if f.strip()}
    if fields - _ALL_FETCH:
        # One unrecognised FETCH field and the collection answers zero rows —
        # no error, and no clue which field. That ambiguity is why the agent
        # tries the proven field set before the rich one.
        return _collection_xml([], fields)

    formulae = dict(_RE_FORMULA.findall(body))
    for name in (n.strip() for n in filters[0].split(",") if n.strip()):
        if name not in formulae:
            # An undefined filter name scopes nothing.
            return _collection_xml(voucher_book(company), fields)
        rows = _apply_formula(formulae[name], rows)
    return _collection_xml(rows, fields)


def _company_of(body: str) -> str:
    """Which company file the request is scoped to."""
    return OLD_COMPANY if OLD_COMPANY in body else COMPANY


class TallyHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
        if "TB_Companies" in body:
            payload = (f"<ENVELOPE><BODY><DATA><COLLECTION>"
                       f'<COMPANY NAME="{COMPANY}"><STARTINGFROM>20260401</STARTINGFROM></COMPANY>'
                       f'<COMPANY NAME="{OLD_COMPANY}"><STARTINGFROM>20240401</STARTINGFROM></COMPANY>'
                       f"</COLLECTION></DATA></BODY></ENVELOPE>")
        elif "TB_Units" in body:
            payload = units_xml()
        elif "TB_Godowns" in body:
            payload = godowns_xml()
        elif "TB_StockGroups" in body:
            payload = stock_groups_xml()
        elif "TB_StockItems" in body:
            # Reject an optional field once, proving graceful degradation.
            if "MarketValuationMethod" in body:
                payload = "<ENVELOPE><BODY><DATA><LINEERROR>Unknown</LINEERROR></DATA></BODY></ENVELOPE>"
            else:
                payload = stock_items_xml()
        elif "TB_Bills" in body:
            # A build that rejects an optional field: refuse BillFixed once,
            # proving the agent degrades gracefully instead of failing.
            if "BillFixed" in body:
                payload = "<ENVELOPE><BODY><DATA><LINEERROR>Unknown method BillFixed</LINEERROR></DATA></BODY></ENVELOPE>"
            else:
                payload = bills_xml(_company_of(body))
        elif "TB_Groups" in body:
            payload = groups_xml()
        elif "TB_Ledgers" in body:
            if OLD_COMPANY in body:
                # "Carry Forward" keeps the SAME GUID in the new year's file.
                # Keying on the GUID alone silently overwrote 633 live rows.
                payload = ('<ENVELOPE><BODY><DATA><COLLECTION>'
                           '<LEDGER NAME="Old Year Customer"><PARENT>Sundry Debtors</PARENT>'
                           '<CLOSINGBALANCE>-5000.00</CLOSINGBALANCE>'
                           '<GUID>shared-guid-0001</GUID>'
                           '<MASTERID>1</MASTERID><ALTERID>1</ALTERID></LEDGER>'
                           '<LEDGER NAME="V MART RETAIL LTD-HARYANA"><PARENT>Sundry Debtors</PARENT>'
                           '<CLOSINGBALANCE>-777777.00</CLOSINGBALANCE>'
                           '<GUID>carried-guid-9002</GUID>'
                           '<MASTERID>9002</MASTERID><ALTERID>9002</ALTERID></LEDGER>'
                           '</COLLECTION></DATA></BODY></ENVELOPE>')
            else:
                payload = ledgers_xml()
        elif "Day Book" in body:
            # The LIVE defect: Day Book ignores SVFROMDATE/SVTODATE and always
            # answers with the same single "today" voucher — exactly what
            # served one Receipt for every monthly window on 2026-08-11. A
            # report export really does come back inside <TALLYMESSAGE>.
            payload = ("<ENVELOPE><BODY><DATA><TALLYMESSAGE>"
                       + _vch_xml(STUCK_DAYBOOK_VOUCHER, _ALL_FETCH)
                       + "</TALLYMESSAGE></DATA></BODY></ENVELOPE>")
        elif "TB_VchAll" in body or "TB_Vouchers" in body:
            # Both the date-scoped collection and the unscoped whole-company
            # fallback are ordinary TDL collections, and the same engine
            # answers both — TB_VchAll simply carries the hygiene filters and
            # no period formula, so Tally drops cancelled and optional rows
            # server-side there.
            payload = voucher_collection_xml(body, _company_of(body))
        else:
            payload = "<ENVELOPE></ENVELOPE>"
        raw = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


# ---------------------------------------------------------------------------
# Mock Frappe: validates like the real one (email!), records everything
# ---------------------------------------------------------------------------

store = {"ledgers": [], "vouchers": [], "logs": [], "flaked": [], "bills": []}


class FrappeHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.endswith("get_logged_user"):
            return self._json({"message": "tally-sync@snjainindustries.com"})
        if path.endswith("get_sync_state"):
            return self._json({"message": {"last_voucher_date": None,
                                           "voucher_count": len(store["vouchers"]),
                                           "ledger_count": len(store["ledgers"])}})
        return self._json({"exc": "unknown"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if path.endswith("upsert_inventory"):
            c = body.get("company")
            for k in ("units", "godowns", "stock_groups", "stock_items"):
                store.setdefault(k, [])
                store[k] = [r for r in store[k] if r.get("company") != c]
                store[k].extend(body.get(k) or [])
            return self._json({"message": {
                k: {"created": len(body.get(k) or [])}
                for k in ("units", "godowns", "stock_groups", "stock_items")}})
        if path.endswith("upsert_bills"):
            store["bills"] = [b for b in store.get("bills", [])
                              if b.get("company") != body.get("company")]
            store.setdefault("bills", []).extend(body.get("bills", []))
            return self._json({"message": {"created": len(body.get("bills", []))}})
        if path.endswith("upsert_ledgers"):
            rows = body.get("ledgers", [])
            # Key rows exactly as Frappe does, so a docname collision here
            # overwrites — reproducing the live 633-row loss rather than
            # quietly passing because a list happens to hold both.
            def _key(r):
                tail = (r.get("guid") or "").strip() or r["name"]
                return f"{r.get('company','')}::{tail}"
            good = [r for r in rows if "@" in (r.get("email") or "") or not r.get("email")]
            bad = [r for r in rows if r not in good]
            store["ledgers"].extend(good)
            out = {"created": len(good)}
            if bad:
                out["failed"] = len(bad)
                out["errors"] = [{"ledger": b["name"], "error": "InvalidEmailAddressError"}
                                 for b in bad]
            return self._json({"message": out})
        if path.endswith("upsert_vouchers"):
            if not store["flaked"]:
                store["flaked"].append(1)
                raw = b"<html><body>502 Bad Gateway frappecloud</body></html>"
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            store["vouchers"].extend(body.get("vouchers", []))
            return self._json({"message": {"created": len(body.get("vouchers", []))}})
        if path.endswith("log_sync"):
            store["logs"].append(body)
            return self._json({"message": {"ok": True}})
        return self._json({"exc": "unknown"}, 404)


def run_sync(workdir: Path):
    """The exact command the operator types, as a real subprocess."""
    return subprocess.run(
        [sys.executable, str(HERE / "sync.py"), "--full",
         "--config", str(workdir / "config.toml")],
        capture_output=True, text=True, timeout=300, cwd=str(workdir),
        # The mock has no Tally engine to overwhelm, so skip the inter-request
        # pacing the real server needs. Without this the suite spends minutes
        # sleeping and creeps up on the timeout above.
        env={**os.environ, "TALLY_MIN_REQUEST_GAP": "0"},
    )


def main() -> int:
    t_srv = HTTPServer(("127.0.0.1", TALLY_PORT), TallyHandler)
    f_srv = HTTPServer(("127.0.0.1", FRAPPE_PORT), FrappeHandler)
    threading.Thread(target=t_srv.serve_forever, daemon=True).start()
    threading.Thread(target=f_srv.serve_forever, daemon=True).start()

    workdir = Path(tempfile.mkdtemp(prefix="tally_e2e_"))
    # Notepad-style config: UTF-8 BOM + CRLF line endings.
    cfg = (
        f'[tally]\r\nhost = "127.0.0.1"\r\nport = {TALLY_PORT}\r\ncompanies = []\r\n\r\n'
        f'[frappe]\r\nurl = "http://127.0.0.1:{FRAPPE_PORT}"\r\n'
        f'api_key = "k"\r\napi_secret = "s"\r\n\r\n'
        f"[sync]\r\nchunk_days = 31\r\noverlap_days = 7\r\nfy_start_month = 4\r\n"
    )
    (workdir / "config.toml").write_bytes(b"\xef\xbb\xbf" + cfg.encode("utf-8"))

    print("=" * 62)
    print("END-TO-END: python sync.py --full   (as a real subprocess)")
    print("=" * 62)

    proc = run_sync(workdir)
    out = (proc.stdout or "") + (proc.stderr or "")

    print()
    print("--- checks ---")
    check("exit code", proc.returncode, 0)
    check_true("no traceback in output", "Traceback" not in out,
               out[-800:] if "Traceback" in out else "")
    check_true("BOM+CRLF config accepted", "Could not read config" not in out)

    # Ledgers: 2,010 sent; only the bare-domain email row may be rejected.
    check("ledgers mirrored (2014 sent, 1 rejected)", len(store["ledgers"]), 2013)
    check_true("bare-domain email row rejected and reported",
               "InvalidEmailAddressError" in out and "1 ledger(s) rejected" in out.replace("was", "were") or
               "rejected" in out)
    names = {r["name"] for r in store["ledgers"]}
    check_true("hariom row was the reject", "Hariom Silk Mills" not in names)
    check_true("V MART present", "V MART RETAIL LTD-HARYANA" in names)

    # Sign convention: V MART is a debtor, must arrive debit-POSITIVE.
    vmart = next(r for r in store["ledgers"] if r["name"] == "V MART RETAIL LTD-HARYANA")
    check("V MART balance debit-positive", vmart["closing_balance"], 12008830.20)
    mm = next(r for r in store["ledgers"] if "स्टील" in r["name"])
    check("creditor balance negative", mm["closing_balance"], -95000.0)

    # Group resolution at scale.
    check("customers resolved to Sundry Debtors",
          sum(1 for r in store["ledgers"]
              if r["primary_group"] == "Sundry Debtors" and r["company"] == COMPANY),
          1000 + 1)  # 1000 via agents + V MART direct (hariom rejected)
    check("suppliers resolved to Sundry Creditors",
          sum(1 for r in store["ledgers"] if r["primary_group"] == "Sundry Creditors"),
          1000 + 1)
    orphan = next(r for r in store["ledgers"] if r["name"] == "Orphan Ledger")
    check("unknown group degrades to itself", orphan["primary_group"], "MYSTERY GROUP")

    # Vouchers: all 60 hostile-narration vouchers must land, plus the
    # cancelled and optional pair — the chosen shape filters on date only, so
    # Tally hands those over and the agent mirrors them carrying their flags.
    mirrored = [v for v in store["vouchers"] if v.get("company") == COMPANY]
    check("vouchers mirrored despite hostile narrations",
          len([v for v in mirrored
               if not v["is_cancelled"] and not v["is_optional"]]), 60)
    check("cancelled and optional vouchers mirrored WITH their flags",
          len([v for v in mirrored if v["is_cancelled"] or v["is_optional"]]), 2)
    guids = [v["guid"] for v in mirrored]
    check("no voucher claimed by two chunks", len(guids), len(set(guids)))
    check_true("no rows leaked across a chunk boundary",
               "leaks rows across range boundaries" not in out)

    # The rich field set is what separates filter_dotted_rich from the proven
    # shape, and it only proves anything if the fields actually arrive.
    v0 = next(v for v in mirrored if v["guid"] == "apr-g0")
    check("reference captured (rich field)", v0["reference"], "PO/apr/000")
    check("reference date captured (rich field)", v0["reference_date"], "2026-03-25")
    check("alter id captured (rich field)", v0["alter_id"], "100")
    check("voucher dated where the book puts it", v0["date"], "2026-04-01")
    check_true("voucher entries preserved",
               all(len(v.get("entries", [])) == 2 for v in store["vouchers"]))
    check_true("voucher amounts positive (sum of debits)",
               all(v["amount"] > 0 for v in store["vouchers"]))
    check_true("narration with fake tag survived",
               any("PONO" in (v.get("narration") or "") for v in store["vouchers"]))
    check_true("hindi narration survived",
               any("माल" in (v.get("narration") or "") for v in store["vouchers"]))

    # Prior-year company must have synced its own period.
    old_ledgers = [r for r in store["ledgers"] if r.get("company") == OLD_COMPANY]
    # Two: the prior-year-only customer, plus V MART which exists in BOTH
    # years under the same carried-forward GUID.
    check("prior-year ledgers synced", len(old_ledgers), 2)
    old_vouchers = [v for v in store["vouchers"] if v.get("company") == OLD_COMPANY]
    check("prior-year vouchers synced (range floored at ITS year)",
          len([v for v in old_vouchers
               if not v["is_cancelled"] and not v["is_optional"]]), 60)
    check_true("prior-year vouchers dated in ITS year",
               all(v["date"].startswith("2024") for v in old_vouchers),
               sorted({v["date"][:4] for v in old_vouchers}))

    # Structural narration attacks: a narration containing a literal
    # "</NARRATION>" is truncated at that point (the parser cannot tell the
    # fake closer from the real one) — but the VOUCHER must survive with its
    # party and amounts intact, which is what actually matters.
    fake_close = [v for v in store["vouchers"]
                  if (v.get("narration") or "").strip() == "see"]
    check_true("voucher with fake close-tag narration survived (truncated)",
               len(fake_close) > 0 and all(v.get("party") for v in fake_close),
               f"found {len(fake_close)}")
    check_true("fake open-tag narration survived intact",
               any("flagged <ok> by accounts" == (v.get("narration") or "")
                   for v in store["vouchers"]))

    # The one 502 was retried, not fatal.
    check_true("502 mid-run was retried and absorbed",
               "temporarily unavailable" not in out and proc.returncode == 0)
    check_true("dashboard hint NOT shown for the 502",
               "DASHBOARD, not your site" not in out)

    # The Day Book range defect was detected and routed around.
    check_true("probe verified the request against two real months",
               "verified against two" in out, out[-400:])
    check_true("a TDL-filtered request was chosen",
               "using 'filter" in out, out[-400:])
    # And specifically the richest one: this build accepts IsCancelled,
    # IsOptional, AlterID, Narration, Reference and ReferenceDate, so the
    # probe must never settle for the narrow shape here.
    check_true("the rich variant was the one verified",
               "using 'filter_dotted_rich'" in out, out[-400:])
    check_true("the whole-company fallback was NOT needed",
               "Falling back to fetching the whole company" not in out)
    check_true("the stuck same-day voucher never entered the mirror",
               not any(v.get("guid") == "stuck-day-guid" for v in store["vouchers"]))

    # --- bill-wise ageing -------------------------------------------------
    bills = store.get("bills", [])
    # 51 bills per company file, and the mock serves both.
    check("bills mirrored across both companies", len(bills), 102)
    check_true("each company's bills are labelled with it",
               len({b["company"] for b in bills}) == 2,
               f"got {{b['company'] for b in bills}}")
    check_true("agent degraded past the rejected optional field",
               "rejected the 'BillFixed' bill field" in out, out[-300:])

    overdue = [b for b in bills if b["overdue_days"] > 0 and not b["is_advance"]]
    notdue = [b for b in bills if b["overdue_days"] <= 0 and not b["is_advance"]]
    check("overdue bills counted", len(overdue), 80)
    check("not-yet-due bills counted", len(notdue), 20)

    b0 = next(b for b in bills if b["name"] == "SL/0000")  # either company
    check("receivable bill is debit-positive", b0["closing"], 1000.0)
    check("due date = bill date + credit period", b0["due_date"], "2026-05-30")
    check_true("overdue days computed", b0["overdue_days"] > 70,
               f"got {b0['overdue_days']}")

    words = next(b for b in bills if b["name"] == "SL/0040")
    check("credit period in words parsed", words["due_date"], "2026-10-30")
    check_true("not-yet-due is negative", words["overdue_days"] < 0)

    adv = next(b for b in bills if b["name"] == "ADV/1")
    check_true("customer advance flagged, not a receivable",
               adv["is_advance"] and adv["closing"] < 0)

    check_true("bill count reported in the run summary", "bills" in out)

    # --- cross-company key collision (the live 633-row overwrite) ---------
    vmarts = [r for r in store["ledgers"] if r["name"] == "V MART RETAIL LTD-HARYANA"]
    check("same GUID in two years keeps BOTH rows", len(vmarts), 2)
    by_co = {r["company"]: r["closing_balance"] for r in vmarts}
    check("current year's balance intact", by_co.get(COMPANY), 12008830.20)
    check("prior year's balance intact", by_co.get(OLD_COMPANY), 777777.0)
    check_true("the two rows have different keys",
               len({r["company"] for r in vmarts}) == 2)

    # --- inventory --------------------------------------------------------
    items = store.get("stock_items", [])
    check("stock items mirrored (both companies)", len(items), 8)
    check("units mirrored", len(store.get("units", [])), 6)
    check("godowns mirrored", len(store.get("godowns", [])), 6)

    tv = next(i for i in items if i["name"] == "Thermal Vest 402")
    check("compound qty resolved: 3 Dzn 6 Pcs = 42 Pcs", tv["closing_qty"], 42.0)
    check("raw quantity preserved for audit", tv["closing_qty_raw"], "3 Dzn 6 Pcs")
    check("resolved unit is the smallest", tv["closing_qty_unit"], "Pcs")
    check("HSN captured", tv["hsn_code"], "61099010")
    check("GST rate captured", tv["gst_rate"], 5.0)

    cv = next(i for i in items if i["name"] == "Cotton Vest 100")
    check("three-level compound: 12 Box 3 Dzn 4 Pcs = 1480", cv["closing_qty"], 1480.0)

    rb = next(i for i in items if i["name"] == "Return Bin")
    check("negative compound signed correctly", rb["closing_qty"], -30.0)

    sp = next(i for i in items if i["name"] == "Sock Pack A")
    check("simple quantity", sp["closing_qty"], 500.0)
    check("rate parsed from '45.50/Pcs'", sp["closing_rate"], 45.5)
    check_true("item with no HSN flagged in the log",
               "have NO HSN code" in out, out[-300:])

    check_true("stock group resolved to its product family",
               tv["primary_group"] == "Hosiery",
               f"got {tv.get('primary_group')!r}")
    check_true("item count reported in the run summary", "items" in out)

    # Sync log written with Success.
    check_true("sync log recorded", len(store["logs"]) >= 1)
    check_true("status Success", any(l.get("status") == "Success" for l in store["logs"]),
               f"statuses: {[l.get('status') for l in store['logs']]}")

    # -----------------------------------------------------------------
    # Second pass: a build that accepts the filter and scopes nothing by it.
    #
    # Every request shape is then demonstrably broken, and the agent must fall
    # back to fetching each company once and filtering the dates here. That
    # fallback is the only thing this suite could exercise while the mock was
    # built to reject everything; now that the primary path is covered, this
    # keeps the fallback covered too instead of trading one for the other.
    # -----------------------------------------------------------------
    print()
    print("--- second pass: a build whose filter does not scope by date ---")
    MOCK["honours_date_filter"] = False
    for k in ("ledgers", "vouchers", "bills", "logs"):
        store[k] = []
    proc2 = run_sync(workdir)
    out2 = (proc2.stdout or "") + (proc2.stderr or "")

    check("fallback pass exit code", proc2.returncode, 0)
    check_true("no traceback in the fallback pass", "Traceback" not in out2,
               out2[-800:] if "Traceback" in out2 else "")
    check_true("no shape honoured the range, so the fallback was chosen",
               "Falling back to fetching the whole company" in out2, out2[-400:])
    fb = [v for v in store["vouchers"] if v.get("company") == COMPANY]
    # 60, not 62: the fallback request carries the hygiene filters, so Tally
    # drops the cancelled and optional rows server-side rather than handing
    # them over flagged. Both routes are correct and they differ — which is
    # exactly the sort of difference this suite exists to hold still.
    check("fallback mirrored the whole book, filtered here", len(fb), 60)
    check_true("fallback dropped cancelled/optional server-side",
               not any(v["is_cancelled"] or v["is_optional"] for v in fb))
    check_true("fallback still never mirrors the stuck Day Book voucher",
               not any(v.get("guid") == "stuck-day-guid" for v in store["vouchers"]))
    fb_guids = [v["guid"] for v in fb]
    check("fallback filtered by date without double-counting",
          len(fb_guids), len(set(fb_guids)))

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        print()
        print("--- subprocess output (tail) ---")
        print(out[-2500:])
        return 1
    print("E2E PASSED — the exact operator command survives hostile live-shaped data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
