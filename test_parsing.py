"""
Offline tests for the Tally XML parsing layer.

These use captured-style TallyPrime XML so we can verify parsing without a live
Tally instance. Run:  python test_parsing.py
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from datetime import date

from tally_client import _clean_xml, _to_float, _tally_date_to_iso
import sync

failures: list[str] = []


def check(label: str, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


print("amount parsing")
check("plain", _to_float("1234.50"), 1234.50)
check("commas", _to_float("1,23,456.78"), 123456.78)
check("negative", _to_float("-1,234.50"), -1234.50)
check("Dr suffix", _to_float("5000.00 Dr"), 5000.00)
check("Cr suffix", _to_float("5000.00 Cr"), -5000.00)
check("empty", _to_float(""), 0.0)
check("garbage", _to_float("N/A"), 0.0)

print("\ndate conversion")
check("yyyymmdd", _tally_date_to_iso("20250415"), "2025-04-15")
check("passthrough", _tally_date_to_iso("2025-04-15"), "2025-04-15")

print("\nxml sanitising (Tally emits raw ampersands)")
dirty = "<A><NAME>Tata &amp; Sons</NAME><B>M&M Motors</B><C>&#4;junk</C></A>"
cleaned = _clean_xml(dirty)
try:
    root = ET.fromstring(cleaned)
    check("valid entity preserved", root.findtext("NAME"), "Tata & Sons")
    check("raw ampersand escaped", root.findtext("B"), "M&M Motors")
    check("control byte stripped", root.findtext("C"), "junk")
except ET.ParseError as exc:
    failures.append(f"clean_xml produced invalid XML: {exc}")
    print(f"  FAIL  clean_xml produced invalid XML: {exc}")

print("\nfinancial year start")
check("mid-FY (Aug 2025)", sync.fy_start(date(2025, 8, 8), 4), date(2025, 4, 1))
check("pre-FY (Feb 2025)", sync.fy_start(date(2025, 2, 8), 4), date(2024, 4, 1))
check("boundary (Apr 1)", sync.fy_start(date(2025, 4, 1), 4), date(2025, 4, 1))

print("\ndate chunking")
chunks = list(sync.date_chunks(date(2025, 1, 1), date(2025, 3, 15), 31))
check("chunk count", len(chunks), 3)
check("first chunk start", chunks[0][0], date(2025, 1, 1))
check("last chunk end", chunks[-1][1], date(2025, 3, 15))
check("no gaps", all(
    chunks[i][1].toordinal() + 1 == chunks[i + 1][0].toordinal()
    for i in range(len(chunks) - 1)
), True)
single = list(sync.date_chunks(date(2025, 1, 1), date(2025, 1, 1), 31))
check("single day", single, [(date(2025, 1, 1), date(2025, 1, 1))])

print("\nvoucher extraction from Day Book XML")
DAYBOOK = """<ENVELOPE><BODY><DATA><TALLYMESSAGE>
 <VOUCHER VCHTYPE="Sales" ACTION="Create">
  <GUID>abc-123-guid</GUID>
  <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
  <VOUCHERNUMBER>SL/0042</VOUCHERNUMBER>
  <DATE>20250415</DATE>
  <PARTYLEDGERNAME>Acme Traders</PARTYLEDGERNAME>
  <NARRATION>Invoice for steel rods</NARRATION>
  <ISCANCELLED>No</ISCANCELLED>
  <ALTERID>901</ALTERID>
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>Acme Traders</LEDGERNAME>
   <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
   <AMOUNT>-118000.00</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>Sales Account</LEDGERNAME>
   <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
   <AMOUNT>100000.00</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>Output CGST</LEDGERNAME>
   <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
   <AMOUNT>9000.00</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>Output SGST</LEDGERNAME>
   <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
   <AMOUNT>9000.00</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
 </VOUCHER>
</TALLYMESSAGE></DATA></BODY></ENVELOPE>"""

root = ET.fromstring(_clean_xml(DAYBOOK))
vel = next(root.iter("VOUCHER"))
check("guid", vel.findtext("GUID"), "abc-123-guid")
check("voucher number", vel.findtext("VOUCHERNUMBER"), "SL/0042")
check("date iso", _tally_date_to_iso(vel.findtext("DATE")), "2025-04-15")
entries = list(vel.iter("ALLLEDGERENTRIES.LIST"))
check("entry count", len(entries), 4)
total_debit = sum(
    _to_float(e.findtext("AMOUNT")) for e in entries if _to_float(e.findtext("AMOUNT")) > 0
)
check("voucher total (sum of debits)", round(total_debit, 2), 118000.00)
net = sum(_to_float(e.findtext("AMOUNT")) for e in entries)
check("entries balance to zero", round(net, 2), 0.0)

print("\nledger extraction")
LEDGERS = """<ENVELOPE><BODY><DATA><COLLECTION>
 <LEDGER NAME="Acme Traders">
  <PARENT>Sundry Debtors</PARENT>
  <OPENINGBALANCE>-50000.00</OPENINGBALANCE>
  <CLOSINGBALANCE>-168000.00</CLOSINGBALANCE>
  <PARTYGSTIN>27AABCU9603R1ZM</PARTYGSTIN>
  <ISBILLWISEON>Yes</ISBILLWISEON>
  <MASTERID>77</MASTERID>
  <ALTERID>1204</ALTERID>
 </LEDGER>
</COLLECTION></DATA></BODY></ENVELOPE>"""
lroot = ET.fromstring(_clean_xml(LEDGERS))
lel = next(lroot.iter("LEDGER"))
check("ledger name from attr", lel.get("NAME"), "Acme Traders")
check("group", lel.findtext("PARENT"), "Sundry Debtors")
check("gstin", lel.findtext("PARTYGSTIN"), "27AABCU9603R1ZM")
check("bill-wise flag", lel.findtext("ISBILLWISEON").lower() == "yes", True)
check("closing balance", _to_float(lel.findtext("CLOSINGBALANCE")), -168000.00)

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("All parsing tests passed.")
