"""
Parsing tests for distributor_fetch.py — no live Tally needed.

The fixture XML is synthetic but shaped exactly like the measured exports
(2026-08-15 probes): TYPE attributes on tags, UDF lists renamed by
tally_client's cleaner, party ledger amounts on the credit convention, the
three forms of ORDERDUEDATE seen in one book.
"""

from datetime import date

from distributor_fetch import (
    _classify_taxes,
    _header,
    _inventory_lines,
    _party_amount,
    _receipt_payload,
    harvest_size_balances,
    parse_due_date,
    parse_item_sizes,
    parse_ledger_extras,
)
from tally_client import _parse_xml


SO_XML = """<ENVELOPE><BODY><DATA><COLLECTION>
<VOUCHER>
 <DATE TYPE="Date">20260801</DATE>
 <GUID>guid-so-1</GUID>
 <VOUCHERTYPENAME>Sales Order</VOUCHERTYPENAME>
 <PARTYLEDGERNAME TYPE="String">KAVITA HOSIERY- AJMER</PARTYLEDGERNAME>
 <VOUCHERNUMBER>SO/K3/187 R</VOUCHERNUMBER>
 <REFERENCE TYPE="String">SO/K3/187 R</REFERENCE>
 <ALLINVENTORYENTRIES.LIST>
  <STOCKITEMNAME TYPE="String">001 SPORT BRA (28X40)-(Doz)</STOCKITEMNAME>
  <RATE TYPE="Rate">2400.00/Doz</RATE>
  <DISCOUNT TYPE="Number">50</DISCOUNT>
  <AMOUNT TYPE="Amount">7200.00</AMOUNT>
  <ACTUALQTY TYPE="Quantity">7.50 Doz</ACTUALQTY>
  <BILLEDQTY TYPE="Quantity">7.50 Doz</BILLEDQTY>
  <BATCHALLOCATIONS.LIST>
   <BATCHNAME TYPE="String">28</BATCHNAME>
   <ORDERNO TYPE="String">SO/K3/187 R</ORDERNO>
   <BATCHDISCOUNT TYPE="Number">50</BATCHDISCOUNT>
   <AMOUNT TYPE="Amount">480.00</AMOUNT>
   <ACTUALQTY TYPE="Quantity">0.50 Doz</ACTUALQTY>
   <BILLEDQTY TYPE="Quantity">0.50 Doz</BILLEDQTY>
   <BATCHRATE TYPE="Rate">2400.00/Doz</BATCHRATE>
   <ORDERDUEDATE TYPE="Due Date" JD="46234" P="1-Aug-26">1-Aug-26</ORDERDUEDATE>
   <UDF_BATCHDISCOUNT2.LIST DESC="`BatchDiscount2`" ISLIST="YES" TYPE="Number" INDEX="543">
    <UDF_BATCHDISCOUNT2 DESC="`BatchDiscount2`">20</UDF_BATCHDISCOUNT2>
   </UDF_BATCHDISCOUNT2.LIST>
   <UDF_BLNCQTY.LIST DESC="`BlncQty`" ISLIST="YES" TYPE="Quantity" INDEX="111">
    <UDF_BLNCQTY DESC="`BlncQty`">54.00 Doz</UDF_BLNCQTY>
   </UDF_BLNCQTY.LIST>
  </BATCHALLOCATIONS.LIST>
  <BATCHALLOCATIONS.LIST>
   <BATCHNAME TYPE="String">30</BATCHNAME>
   <ORDERNO TYPE="String">SO/K3/187 R</ORDERNO>
   <BATCHDISCOUNT TYPE="Number">50</BATCHDISCOUNT>
   <AMOUNT TYPE="Amount">960.00</AMOUNT>
   <ACTUALQTY TYPE="Quantity">1.00 Doz</ACTUALQTY>
   <BATCHRATE TYPE="Rate">2400.00/Doz</BATCHRATE>
   <ORDERDUEDATE TYPE="Due Date" P="1 Days">1 Days</ORDERDUEDATE>
   <UDF_BLNCQTY.LIST><UDF_BLNCQTY>12.00 Doz</UDF_BLNCQTY></UDF_BLNCQTY.LIST>
  </BATCHALLOCATIONS.LIST>
 </ALLINVENTORYENTRIES.LIST>
 <LEDGERENTRIES.LIST>
  <LEDGERNAME TYPE="String">KAVITA HOSIERY- AJMER</LEDGERNAME>
  <ISDEEMEDPOSITIVE TYPE="Logical">Yes</ISDEEMEDPOSITIVE>
  <AMOUNT TYPE="Amount">-7200.00</AMOUNT>
 </LEDGERENTRIES.LIST>
 <ALLLEDGERENTRIES.LIST>
  <LEDGERNAME TYPE="String">KAVITA HOSIERY- AJMER</LEDGERNAME>
  <ISDEEMEDPOSITIVE TYPE="Logical">Yes</ISDEEMEDPOSITIVE>
  <AMOUNT TYPE="Amount">-7200.00</AMOUNT>
 </ALLLEDGERENTRIES.LIST>
</VOUCHER>
</COLLECTION></DATA></BODY></ENVELOPE>"""


INVOICE_XML = """<ENVELOPE><BODY><DATA><COLLECTION>
<VOUCHER>
 <DATE TYPE="Date">20260810</DATE>
 <GUID>guid-inv-1</GUID>
 <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
 <PARTYLEDGERNAME TYPE="String">KAVITA HOSIERY- AJMER</PARTYLEDGERNAME>
 <VOUCHERNUMBER>SNJ/26-27/2132</VOUCHERNUMBER>
 <REFERENCE TYPE="String">SO/K3/182 R</REFERENCE>
 <ALLLEDGERENTRIES.LIST>
  <LEDGERNAME TYPE="String">KAVITA HOSIERY- AJMER</LEDGERNAME>
  <ISDEEMEDPOSITIVE TYPE="Logical">Yes</ISDEEMEDPOSITIVE>
  <AMOUNT TYPE="Amount">-162061.00</AMOUNT>
  <BILLALLOCATIONS.LIST>
   <NAME>SNJ/26-27/2132</NAME>
   <BILLTYPE>New Ref</BILLTYPE>
   <AMOUNT>-162061.00</AMOUNT>
  </BILLALLOCATIONS.LIST>
 </ALLLEDGERENTRIES.LIST>
 <ALLLEDGERENTRIES.LIST>
  <LEDGERNAME TYPE="String">Sale Central 5%</LEDGERNAME>
  <ISDEEMEDPOSITIVE TYPE="Logical">No</ISDEEMEDPOSITIVE>
  <AMOUNT TYPE="Amount">154344.00</AMOUNT>
 </ALLLEDGERENTRIES.LIST>
 <ALLLEDGERENTRIES.LIST>
  <LEDGERNAME TYPE="String">IGST Output</LEDGERNAME>
  <ISDEEMEDPOSITIVE TYPE="Logical">No</ISDEEMEDPOSITIVE>
  <AMOUNT TYPE="Amount">7717.20</AMOUNT>
 </ALLLEDGERENTRIES.LIST>
 <ALLLEDGERENTRIES.LIST>
  <LEDGERNAME TYPE="String">Rounded Off</LEDGERNAME>
  <ISDEEMEDPOSITIVE TYPE="Logical">No</ISDEEMEDPOSITIVE>
  <AMOUNT TYPE="Amount">-0.20</AMOUNT>
 </ALLLEDGERENTRIES.LIST>
</VOUCHER>
</COLLECTION></DATA></BODY></ENVELOPE>"""


RECEIPT_XML = """<ENVELOPE><BODY><DATA><COLLECTION>
<VOUCHER>
 <DATE TYPE="Date">20260810</DATE>
 <GUID>guid-rcpt-1</GUID>
 <VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>
 <PARTYLEDGERNAME>3R WHOLE SALE- VIJAYAPURA</PARTYLEDGERNAME>
 <VOUCHERNUMBER></VOUCHERNUMBER>
 <ALLLEDGERENTRIES.LIST>
  <LEDGERNAME>3R WHOLE SALE- VIJAYAPURA</LEDGERNAME>
  <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
  <AMOUNT>43204.00</AMOUNT>
  <BILLALLOCATIONS.LIST>
   <NAME>SNJ/26-27/949</NAME>
   <BILLTYPE>Agst Ref</BILLTYPE>
   <AMOUNT>43204.00</AMOUNT>
  </BILLALLOCATIONS.LIST>
 </ALLLEDGERENTRIES.LIST>
 <ALLLEDGERENTRIES.LIST>
  <LEDGERNAME>HDFC Bank(50200060672969)</LEDGERNAME>
  <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
  <AMOUNT>-43204.00</AMOUNT>
 </ALLLEDGERENTRIES.LIST>
</VOUCHER>
</COLLECTION></DATA></BODY></ENVELOPE>"""


def _first_voucher(xml):
    return next(iter(_parse_xml(xml).iter("VOUCHER")))


# ---------------------------------------------------------------------------
# Due dates: the three forms this book mixes in adjacent lines
# ---------------------------------------------------------------------------

class DueEl:
    """Stand-in element: parse_due_date reads .get() and .text only."""
    def __init__(self, text="", jd=""):
        self.text = text
        self._jd = jd

    def get(self, key):
        return self._jd if key == "JD" else ""


def test_due_date_jd_serial_is_excel_style():
    # Live pair: JD='46234' printed beside P='1-Aug-26'.
    assert parse_due_date(DueEl(jd="46234"), "2026-08-01") == "2026-08-01"


def test_due_date_absolute_text():
    assert parse_due_date(DueEl(text="1-Aug-26"), "") == "2026-08-01"


def test_due_date_relative_days():
    assert parse_due_date(DueEl(text="5 Days"), "2026-08-01") == "2026-08-06"


def test_due_date_garbage_is_empty():
    assert parse_due_date(DueEl(text="Not Applicable"), "2026-08-01") == ""
    assert parse_due_date(None, "2026-08-01") == ""


# ---------------------------------------------------------------------------
# Sales order parse
# ---------------------------------------------------------------------------

def test_so_header_and_amount():
    v = _first_voucher(SO_XML)
    h = _header(v, "TESTCO")
    assert h["guid"] == "guid-so-1"
    assert h["party"] == "KAVITA HOSIERY- AJMER"
    # Party line exports debit-NEGATIVE; the headline value is its abs.
    assert _party_amount(v, h["party"]) == 7200.00


def test_so_lines_one_per_size_with_both_discounts():
    v = _first_voucher(SO_XML)
    lines = _inventory_lines(v, "2026-08-01")
    assert len(lines) == 2
    l28 = next(l for l in lines if l["size_batch"] == "28")
    assert l28["qty"] == 0.5
    assert l28["unit"] == "Doz"
    assert l28["rate"] == 2400.00
    assert l28["discount"] == 50
    assert l28["discount2"] == 20          # the UDF second step
    assert l28["amount"] == 480.00         # net of the FULL chain
    assert l28["order_no"] == "SO/K3/187 R"
    assert l28["due_date"] == "2026-08-01"
    assert l28["balance_qty"] == 54.0      # per-size stock from BlncQty
    # relative '1 Days' resolves against the voucher date
    l30 = next(l for l in lines if l["size_batch"] == "30")
    assert l30["due_date"] == "2026-08-02"


# ---------------------------------------------------------------------------
# Invoice parse: GST breakup and bill refs
# ---------------------------------------------------------------------------

def test_invoice_tax_classification():
    v = _first_voucher(INVOICE_XML)
    taxes = _classify_taxes(v, "KAVITA HOSIERY- AJMER")
    assert taxes["igst"] == 7717.20
    assert taxes["cgst"] == 0.0
    assert taxes["round_off"] == -0.20
    assert taxes["bill_refs"] == "SNJ/26-27/2132"
    amount = _party_amount(v, "KAVITA HOSIERY- AJMER")
    assert amount == 162061.00
    taxable = amount - taxes["igst"] - taxes["round_off"]
    # 162061 - 7717.20 + 0.20 = 154344 == the sales ledger line exactly.
    assert round(taxable, 2) == 154344.00


# ---------------------------------------------------------------------------
# Receipt parse
# ---------------------------------------------------------------------------

def test_receipt_payload():
    v = _first_voucher(RECEIPT_XML)
    p = _receipt_payload(v, "TESTCO")
    assert p["party"] == "3R WHOLE SALE- VIJAYAPURA"
    assert p["amount"] == 43204.00
    assert p["mode"] == "HDFC Bank(50200060672969)"
    assert p["voucher_number"] == ""       # this book numbers no receipts
    assert p["allocations"] == [{"bill_ref": "SNJ/26-27/949",
                                 "bill_type": "Agst Ref",
                                 "amount": 43204.00}]


# ---------------------------------------------------------------------------
# Size-balance harvest
# ---------------------------------------------------------------------------

def test_harvest_keeps_newest_per_size():
    older = {"date": "2026-08-01", "voucher_number": "SO/1", "lines": [
        {"item_name": "X", "size_batch": "28", "balance_qty": 99.0,
         "balance_unit": "Doz"}]}
    newer = {"date": "2026-08-09", "voucher_number": "SO/2", "lines": [
        {"item_name": "X", "size_batch": "28", "balance_qty": 40.0,
         "balance_unit": "Doz"}]}
    cancelled = {"date": "2026-08-11", "voucher_number": "SO/3",
                 "is_cancelled": True, "lines": [
        {"item_name": "X", "size_batch": "28", "balance_qty": 1.0,
         "balance_unit": "Doz"}]}
    out = harvest_size_balances([older, newer, cancelled])
    assert out == [{"item_name": "X", "batch_name": "28",
                    "closing_qty": 40.0, "closing_qty_unit": "Doz",
                    "as_of": "2026-08-09", "source_voucher": "SO/2"}]


def test_harvest_skips_lines_without_the_udf():
    p = {"date": "2026-08-09", "voucher_number": "SO/2", "lines": [
        {"item_name": "X", "size_batch": "28", "balance_qty": 0.0,
         "balance_unit": ""}]}
    assert harvest_size_balances([p]) == []


# ---------------------------------------------------------------------------
# Ledger extras and size walk
# ---------------------------------------------------------------------------

LEDGER_XML = """<ENVELOPE><BODY><DATA><COLLECTION>
<LEDGER NAME="AMIT CORPORATION-SOLAPUR">
 <ADDRESS.LIST><ADDRESS>Panjwani Market,</ADDRESS><ADDRESS>Solapur - 413006</ADDRESS></ADDRESS.LIST>
 <PARENT>AGENT RK</PARENT>
 <BILLCREDITPERIOD>0</BILLCREDITPERIOD>
 <LEDGERMOBILE>7875283888</LEDGERMOBILE>
 <CREDITLIMIT></CREDITLIMIT>
 <LEDGERSTATENAME>Maharashtra</LEDGERSTATENAME>
 <PINCODE>413006</PINCODE>
 <COUNTRYNAME>India</COUNTRYNAME>
 <GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>
 <MAILINGNAME>AMIT CORPORATION-SOLAPUR</MAILINGNAME>
</LEDGER>
</COLLECTION></DATA></BODY></ENVELOPE>"""


def test_ledger_extras():
    out = parse_ledger_extras(LEDGER_XML)
    row = out["AMIT CORPORATION-SOLAPUR"]
    assert row["mobile"] == "7875283888"
    assert row["credit_limit"] == 0.0
    assert row["credit_period"] == ""      # '0' means none, not 'zero days'
    assert row["address"] == "Panjwani Market, Solapur - 413006"
    assert row["state"] == "Maharashtra"


SIZES_XML = """<ENVELOPE><BODY><DATA><COLLECTION>
<ITEMBATCHALLOCATIONS><ITEMNAME>X</ITEMNAME><BATCHNAME>28</BATCHNAME><GODOWNNAME>Pack</GODOWNNAME></ITEMBATCHALLOCATIONS>
<ITEMBATCHALLOCATIONS><ITEMNAME>X</ITEMNAME><BATCHNAME>30</BATCHNAME><GODOWNNAME>Pack</GODOWNNAME></ITEMBATCHALLOCATIONS>
<ITEMBATCHALLOCATIONS><ITEMNAME>X</ITEMNAME><BATCHNAME>28</BATCHNAME><GODOWNNAME>Other</GODOWNNAME></ITEMBATCHALLOCATIONS>
</COLLECTION></DATA></BODY></ENVELOPE>"""


def test_parse_item_sizes_dedupes_across_godowns():
    assert parse_item_sizes(SIZES_XML) == {"X": ["28", "30"]}
