# Tally XML requests — the distributor data layer

The exact request bodies the sync agent sends for the distributor mirror, so
they can be inspected, replayed with `curl`, and compared against what the
code builds (`distributor_fetch.py`). Everything here was verified against
the live server — TallyPrime Edit Log 7.0 (Gold), hosted TallyPrimeCloud —
on 2026-08-15. The older voucher/ledger/bill request shapes are documented in
code comments in `tally_client.py`; this file covers the requests added for
the distributor layer.

## The rules that keep these working

Re-derived the hard way once each; do not relax them:

1. **A Voucher Collection is scoped by `<FILTER>` + `<SYSTEM TYPE="Formulae">`,
   never by SVFROMDATE/SVTODATE.** The date variables bind to nothing on a
   Collection; the formula is the only scope.
2. **The FETCH field list decides whether the filter applies.** One
   unrecognised field and the build answers ZERO rows with STATUS=1 — an empty
   answer never proves the filter is broken, and a new field is only added
   after probing the exact request.
3. Operators inside the formula must be XML-escaped (`&gt;=`, `&lt;=`); date
   literals are `$$Date:"YYYYMMDD"`.
4. `<SVCURRENTCOMPANY>` is mandatory and must never be empty — an empty tag
   silently answers from whichever company is loaded.
5. Master collections (Ledger, StockItem) DO honour SVFROMDATE/SVTODATE for
   their closing figures; send them explicitly or the answer depends on
   whatever period the operator's session holds.
6. Tally exports whole sub-objects once any dotted field of theirs is in
   FETCH — the responses below are richer than their requests, and the extra
   (BILLEDQTY, BATCHRATE, UDFs) is parsed, not re-requested.

## 1. Sales Orders (and Sales invoices — same body, different type name)

One request per date chunk (`chunk_days`, default 7 — a fortnight of Sales
Orders is a 27 MB answer on this book).

```xml
<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>TB_DistVch</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVCURRENTCOMPANY>SN JAIN INDUSTRIES PVT LTD - (26-27)</SVCURRENTCOMPANY>
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="TB_DistVch" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <TYPE>Voucher</TYPE>
    <FETCH>Guid,Date,VoucherTypeName,VoucherNumber,PartyLedgerName,AllLedgerEntries.LedgerName,AllLedgerEntries.Amount,AllLedgerEntries.IsDeemedPositive,Reference,Narration,IsCancelled,IsOptional,AlterID,AllInventoryEntries.StockItemName,AllInventoryEntries.ActualQty,AllInventoryEntries.Rate,AllInventoryEntries.Amount,AllInventoryEntries.BatchAllocations.BatchName,AllInventoryEntries.BatchAllocations.ActualQty,AllInventoryEntries.BatchAllocations.OrderDueDate,AllInventoryEntries.BatchAllocations.OrderNo</FETCH>
    <FILTER>TBDistPeriod</FILTER>
   </COLLECTION>
   <SYSTEM TYPE="Formulae" NAME="TBDistPeriod">$Date &gt;= $$Date:"20260801" and $Date &lt;= $$Date:"20260807" and ($VoucherTypeName = "Sales Order")</SYSTEM>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>
```

For invoices the type clause is `$VoucherTypeName = "Sales"`; for delivery
notes (none exist in this book — the fetcher ships for the day that changes)
the configured `[orders].delivery_types` names are OR-ed into the clause.

**Measured** (2026-08-01..12): 172 Sales Orders / 27.7 MB; the batch
allocations carry `BATCHNAME` (the size), `ORDERNO`, `ORDERDUEDATE` (JD
attribute = Excel-style serial, epoch 1899-12-30; text form is either
`1-Aug-26` or `1 Days` relative to the voucher date), `BATCHRATE`,
`BATCHDISCOUNT` (the first 50), the `BatchDiscount2` UDF (the 20), and the
`BlncQty` UDF — the order-pad TDL's own per-size balance stock, which is the
ONLY per-size stock figure this build exports anywhere (see §4).

## 2. Receipts

Same envelope, receipt fields:

```
<FETCH>Guid,Date,VoucherTypeName,VoucherNumber,PartyLedgerName,AllLedgerEntries.LedgerName,AllLedgerEntries.Amount,AllLedgerEntries.IsDeemedPositive,Reference,Narration,IsCancelled,IsOptional,AlterID,AllLedgerEntries.BillAllocations.Name,AllLedgerEntries.BillAllocations.BillType,AllLedgerEntries.BillAllocations.Amount</FETCH>
```

with `$VoucherTypeName = "Receipt"`. **Measured**: 26 receipts / 67 KB for
two days; 17 of 52 bill allocations named a bill (`BILLTYPE` = `Agst Ref`),
the rest are on-account. Receipts in this book carry **no voucher number**
(25 of 25 sampled blank, `NUMBERINGSTYLE` = None) — they are keyed on GUID.

The agent first tries this fetch plus
`AllLedgerEntries.BankAllocations.InstrumentNumber/InstrumentDate/TransactionType`
(UTR for intimation matching). That trio is **unproven** on this build, so if
the rich request answers zero rows where the proven one answers rows, the
proven result is used and the downgrade is logged (`fetch_receipts`).

## 3. Ledger distributor fields

Master collection; all methods below were accepted in ONE request by this
build (no LINEERROR):

```xml
<COLLECTION NAME="TB_LedgerX" ...>
 <TYPE>Ledger</TYPE>
 <NATIVEMETHOD>Parent</NATIVEMETHOD> <NATIVEMETHOD>OpeningBalance</NATIVEMETHOD>
 <NATIVEMETHOD>ClosingBalance</NATIVEMETHOD> <NATIVEMETHOD>PartyGSTIN</NATIVEMETHOD>
 <NATIVEMETHOD>Email</NATIVEMETHOD> <NATIVEMETHOD>LedgerPhone</NATIVEMETHOD>
 <NATIVEMETHOD>LedgerMobile</NATIVEMETHOD> <NATIVEMETHOD>IsBillWiseOn</NATIVEMETHOD>
 <NATIVEMETHOD>GUID</NATIVEMETHOD> <NATIVEMETHOD>MasterId</NATIVEMETHOD>
 <NATIVEMETHOD>AlterId</NATIVEMETHOD> <NATIVEMETHOD>CreditLimit</NATIVEMETHOD>
 <NATIVEMETHOD>BillCreditPeriod</NATIVEMETHOD> <NATIVEMETHOD>PriceLevel</NATIVEMETHOD>
 <NATIVEMETHOD>LedgerStateName</NATIVEMETHOD> <NATIVEMETHOD>PinCode</NATIVEMETHOD>
 <NATIVEMETHOD>CountryName</NATIVEMETHOD> <NATIVEMETHOD>GSTRegistrationType</NATIVEMETHOD>
 <NATIVEMETHOD>MailingName</NATIVEMETHOD> <NATIVEMETHOD>Address</NATIVEMETHOD>
</COLLECTION>
```

**Measured** across the 123 ledgers under AGENT RK: `CREDITLIMIT` empty on
all, `BILLCREDITPERIOD` = 0 on all, `PRICELEVEL` empty on all,
`LEDGERMOBILE` set on 63. The fields are mirrored anyway — they light up the
day someone sets them in Tally, with no agent change. The **agent** is NOT in
any UDF (the ledger export carries no UDF tags at all); it is the immediate
group under Sundry Debtors, resolved in `sync.py`.

## 4. Per-size stock: what does NOT work, and what does

Three shapes that look right and are not:

* `<TYPE>Batch</TYPE>` — answers zero elements, always.
* `BatchAllocations.*` as dotted NATIVEMETHODs on a StockItem collection —
  blanks the whole collection (the same zero-rows failure mode as rule 2).
* `SOURCECOLLECTION` + `<WALK>BatchAllocations</WALK>` + `$$Owner` compute —
  **enumerates the (size, godown) pairs correctly** but `ClosingBalance` /
  `ClosingValue` on the walked object answer with the ITEM total repeated on
  every row (measured: 1395.75 Doz on all 12 sizes of `001 SPORT BRA`).

What works: the order-pad TDL computes balance stock per size into a
`BlncQty` UDF on every batch line of every Sales Order / Sales voucher —
`<UDF:BLNCQTY.LIST>` → `54.00 Doz`. The agent harvests the NEWEST value per
(item, size) from vouchers it is already fetching (zero extra Tally traffic)
and sends them to `upsert_stock_batches`, each row dated by its voucher
(`as_of`). Vouchers are punched daily here, so the figures track reality
closely — and the portal only ever shows in/low/out buckets, never numbers.

The size-enumeration walk is kept in `distributor_fetch.fetch_item_sizes`
(sizes an item exists in, without quantities):

```xml
<COLLECTION NAME="TB_SizeItems" ...><TYPE>StockItem</TYPE></COLLECTION>
<COLLECTION NAME="TB_Sizes" ...>
 <SOURCECOLLECTION>TB_SizeItems</SOURCECOLLECTION>
 <WALK>BatchAllocations</WALK>
 <COMPUTE>ItemName : $$Owner:$Name</COMPUTE>
 <NATIVEMETHOD>BatchName</NATIVEMETHOD>
 <NATIVEMETHOD>GodownName</NATIVEMETHOD>
</COLLECTION>
```

Answer elements are named `<ITEMBATCHALLOCATIONS>`.

## 5. Cadence

Per company, per `sync.py` run (Task Scheduler decides how often runs
happen; the existing install runs continuously):

| What | When | Why |
|---|---|---|
| Ledgers + distributor fields | every run (hourly cadence in practice) | masters move slowly; the AlterID short-circuit makes re-runs cheap |
| Outstanding bills snapshot | every run | destructive replace, guarded against empty answers |
| Vouchers | every run, incremental window (last synced date − `overlap_days`), chunked by `chunk_days` | |
| Sales orders / invoices / receipts | every run, same window and chunks | this is the distributor mirror |
| Size balances | every run, harvested from the same payloads | zero extra Tally traffic |
| Delivery notes | only if `[orders].delivery_types` is non-empty | this book has none |

Alter-id-incremental voucher windows (the brief's "~30s if feasible") are
NOT feasible against this Tally: it is one engine shared with ~8 live
operators, stops accepting connections while digesting large exports, and
has no server-side alter-id filter proven on voucher collections. The
last-N-days window with the AlterID short-circuit on the Frappe side is the
honest version of the same idea.
