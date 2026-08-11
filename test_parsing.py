"""
Offline tests for the Tally XML parsing layer.

These use captured-style TallyPrime XML so we can verify parsing without a live
Tally instance. Run:  python test_parsing.py
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from datetime import date

from tally_client import (
    Group, TallyConfig, TallyError, _clean_xml, _company_tag, _to_float,
    _to_debit_positive, _tally_date_to_iso, _xml_escape, resolve_group_chain,
)
import sync

failures: list[str] = []


def check(label: str, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


def check_true(label: str, cond, hint: str = ""):
    if not cond:
        failures.append(f"{label} {hint}".strip())
        print(f"  FAIL  {label} {hint}".rstrip())
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

print("\nhostile XML from real voucher narrations (live failure 2026-08-11)")
from tally_client import _parse_xml

# The live Day Book export died at line 2083 col 20 on an invalid token.
# Reproduce every class of garbage narrations are known to carry.
hostile = ("<ENVELOPE><BODY><DATA><TALLYMESSAGE>"
           "<VOUCHER><GUID>g1</GUID>"
           "<NARRATION>ctrl\x04byte\x1bescape\x00null</NARRATION>"
           "<PARTYLEDGERNAME>M&M Traders</PARTYLEDGERNAME>"
           "</VOUCHER>"
           "<VOUCHER><GUID>g2</GUID>"
           "<NARRATION>bad refs &#4; &#0; &#27; &#x1B; kept: &#65; &#x41;</NARRATION>"
           "<PARTYLEDGERNAME>rate < 500 per pc</PARTYLEDGERNAME>"
           "</VOUCHER>"
           "<VOUCHER><GUID>g3</GUID>"
           "<NARRATION>trailing amp & and <- arrow</NARRATION>"
           "</VOUCHER>"
           "</TALLYMESSAGE></DATA></BODY></ENVELOPE>")

root = _parse_xml(hostile)
vs = list(root.iter("VOUCHER"))
check("all vouchers survive hostile narrations", len(vs), 3)
check("control bytes stripped", vs[0].findtext("NARRATION"), "ctrlbyteescapenull")
check("ampersand in party name preserved", vs[0].findtext("PARTYLEDGERNAME"), "M&M Traders")
check("invalid numeric refs dropped, valid kept",
      vs[1].findtext("NARRATION"), "bad refs     kept: A A")
check("unescaped < in text survives", vs[1].findtext("PARTYLEDGERNAME"), "rate < 500 per pc")
check("trailing & and <- arrow survive", vs[2].findtext("NARRATION"), "trailing amp & and <- arrow")

# Backstop: garbage even _clean_xml cannot anticipate is repaired char-by-char.
weird = "<A><B>ok</B><C>x\ud800y</C></A>"   # lone surrogate
try:
    r2 = _parse_xml(weird)
    check("lone surrogate repaired by backstop", r2.findtext("B"), "ok")
except Exception as e:
    check("lone surrogate repaired by backstop", f"raised {type(e).__name__}", "no exception")

print("\nsign convention (verified against a live book, 2026-08-10)")
# TallyPrime exports Debit as NEGATIVE. Confirmed twice against Tally's own
# Group Summary for SN JAIN INDUSTRIES PVT LTD - (26-27):
#   V MART RETAIL LTD-HARYANA     Tally: Debit  1,20,08,830.20  XML: -1,20,08,830.20
#   SETH JI HOSIERY LLP-(Sale)    Tally: Credit   23,87,383.92  XML: +23,87,383.92
check("V Mart: debit balance becomes positive",
      _to_debit_positive("-12008830.20"), 12008830.20)
check("Seth Ji: credit balance becomes negative",
      _to_debit_positive("2387383.92"), -2387383.92)
check("raw parse is left untouched for auditing",
      _to_float("-12008830.20"), -12008830.20)
check("zero stays zero", _to_debit_positive("0"), 0.0)
check("debtors group net flips to positive (was -1,69,90,673.98)",
      round(_to_debit_positive("-16990673.98"), 2), 16990673.98)
check("creditors group net flips to negative (was +10,11,16,657.28)",
      round(_to_debit_positive("101116657.28"), 2), -101116657.28)

print("\ngroup hierarchy resolution (this book's real structure)")
# Sundry Debtors > AGENT RK > <customer ledgers>, and Sundry Debtors Online.
GROUPS = {g.name: g for g in [
    Group("Sundry Debtors", "Current Assets"),
    Group("Current Assets", "Primary"),
    Group("AGENT RK", "Sundry Debtors"),
    Group("AGENT JAISON", "Sundry Debtors"),
    Group("Sundry Debtors Online", "Sundry Debtors"),
    Group("Sundry Creditors", "Current Liabilities"),
    Group("Current Liabilities", "Primary"),
    Group("Stitchers", "Sundry Creditors"),
    Group("Transporter", "Sundry Creditors"),
]}
chain = resolve_group_chain("AGENT RK", GROUPS)
check("AGENT RK resolves up through Sundry Debtors",
      "Sundry Debtors" in chain, True)
check("Stitchers resolves up through Sundry Creditors",
      "Sundry Creditors" in resolve_group_chain("Stitchers", GROUPS), True)
check("a top-level group resolves to itself",
      resolve_group_chain("Sundry Debtors", GROUPS)[0], "Sundry Debtors")
check("unknown group degrades gracefully",
      resolve_group_chain("MYSTERY GROUP", GROUPS), ["MYSTERY GROUP"])
# A corrupt export must not hang the agent.
CYCLE = {g.name: g for g in [Group("A", "B"), Group("B", "C"), Group("C", "A")]}
cyc = resolve_group_chain("A", CYCLE)
check("a cyclic group tree terminates", len(cyc) <= 3, True)

print("\ncompany mis-binding guards")
try:
    _company_tag(TallyConfig(company=""))
    check("empty company is refused", "no error raised", "TallyError")
except TallyError:
    check("empty company is refused", True, True)
try:
    _company_tag(TallyConfig(company="   "))
    check("whitespace-only company is refused", "no error", "TallyError")
except TallyError:
    check("whitespace-only company is refused", True, True)
check("company name is xml-escaped in the request",
      _company_tag(TallyConfig(company="S N Jain & Sons")),
      "<SVCURRENTCOMPANY>S N Jain &amp; Sons</SVCURRENTCOMPANY>")
check("angle brackets escaped too", _xml_escape("<x>"), "&lt;x&gt;")

print("\ncommand-line arguments (guards against args.X with no add_argument)")
import ast, inspect, re
parser = sync.build_parser()
defined = {a.dest for a in parser._actions}

# Every `args.<name>` that main() reads must exist on the parser, or the tool
# dies with AttributeError only when that code path is first taken - which for
# --full meant it shipped broken.
src = inspect.getsource(sync.main)
used = set(re.findall(r"\bargs\.([a-zA-Z_][a-zA-Z0-9_]*)", src))
missing = sorted(used - defined)
check("every args.X used by main() is defined", missing, [])
expected = {"check", "full", "frm", "to", "company", "config", "verbose",
            "ledgers_only", "vouchers_only"}
check_true("parser defines every documented flag", expected <= defined,
           f"missing: {sorted(expected - defined)}")

# Each documented invocation must actually parse.
for argv in ([], ["--check"], ["--full"], ["--ledgers-only"], ["--vouchers-only"],
             ["--company", "SN JAIN INDUSTRIES PVT LTD - (26-27)"],
             ["--from", "2025-04-01", "--to", "2025-06-30"], ["-v"]):
    try:
        ns = parser.parse_args(argv)
        for name in used:
            getattr(ns, name)          # would raise if undefined
        check_true(f"parses {' '.join(argv) or '(no args)'}", True)
    except SystemExit:
        check_true(f"parses {' '.join(argv) or '(no args)'}", False, "argparse rejected it")
    except AttributeError as exc:
        check_true(f"parses {' '.join(argv) or '(no args)'}", False, str(exc))

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("All parsing tests passed.")
