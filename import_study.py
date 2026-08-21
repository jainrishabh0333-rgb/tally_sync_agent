#!/usr/bin/env python3
"""
import_study.py — find out, in ONE run, what this Tally's import will accept.

Five Sales Order import attempts have parked in Import Exceptions with the
same message. Each attempt tested one hypothesis and cost a full
push/download/run cycle. This script tests the whole hypothesis ladder in
one run, exactly the way study.py cracked the voucher-export problem.

It takes the smallest REAL operator-entered order from sample_orders.xml
(proof by construction that its shape is importable) and imports a series of
controlled mutations of it, each under an unmistakable test voucher number:

    P1  CLTEST-P1  the specimen as-is, renumbered      (control — must pass)
    P2  CLTEST-P2  P1 with every amount zeroed          (isolates: amounts)
    P3  CLTEST-P3  P2 with rates/MRP removed            (isolates: rate tags)
    P4  CLTEST-P4  P3 without batch (size) allocations  (isolates: batches)
    P5  CLTEST-P5  the pipeline's own envelope           (current generator)

The verdict table at the end names the exact boundary between importable
and not. Whatever imports is CLEARLY LABELLED: voucher numbers CLTEST-*,
narration "TEST — DELETE ME". Cancel them in Tally afterwards (X: Cancel
Vch), and delete whatever lands in Import Exceptions.

SAFETY: Sales Order vouchers only — the same byte-level whitelist as the
importer. P1/P2 carry the specimen's own party and (for P1) its amounts;
this is a diagnostic run, done deliberately, cleaned up afterwards.

    python import_study.py            # runs the ladder
    python import_study.py --print    # show the five envelopes, send nothing
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from sync import load_settings
from tally_client import TallyConfig, TallyError, post_write, assert_company_loaded

HERE = Path(__file__).resolve().parent
COMPANY = "SN JAIN INDUSTRIES PVT LTD - (26-27)"

# Tags whose values tie a voucher to an existing one — a clone must shed
# them or Tally will try to ALTER instead of Create (or reject the ids).
_STRIP_TAGS = ("GUID", "ALTERID", "MASTERID", "VOUCHERKEY", "VOUCHERRETAINKEY",
               "OLDAUDITENTRYIDS.LIST", "EXCHANGEACTIVITYID", "UPDATEDDATETIME",
               "VCHSTATUSDATE", "IRNACKUPDATEDATETIME")


def _strip_tag(xml: str, tag: str) -> str:
    if tag.endswith(".LIST"):
        return re.sub(rf"<{re.escape(tag)}[^>]*>.*?</{re.escape(tag)}>", "",
                      xml, flags=re.S)
    return re.sub(rf"<{re.escape(tag)}[^>]*>[^<]*</{re.escape(tag)}>", "", xml)


def _renumber(vch: str, number: str) -> str:
    vch = re.sub(r"<VOUCHERNUMBER>[^<]*</VOUCHERNUMBER>",
                 f"<VOUCHERNUMBER>{number}</VOUCHERNUMBER>", vch)
    vch = re.sub(r"<REFERENCE>[^<]*</REFERENCE>",
                 f"<REFERENCE>{number}</REFERENCE>", vch)
    vch = re.sub(r"<ORDERNO>[^<]*</ORDERNO>",
                 f"<ORDERNO>{number}</ORDERNO>", vch)
    vch = re.sub(r"REMOTEID=\"[^\"]*\"", "", vch, count=1)
    vch = re.sub(r"VCHKEY=\"[^\"]*\"", "", vch, count=1)
    for t in _STRIP_TAGS:
        vch = _strip_tag(vch, t)
    # An unmistakable label on everything this study creates.
    if "<NARRATION>" in vch:
        vch = re.sub(r"<NARRATION>[^<]*</NARRATION>",
                     "<NARRATION>TEST import study - DELETE ME</NARRATION>", vch)
    else:
        vch = vch.replace("</VOUCHERTYPENAME>",
                          "</VOUCHERTYPENAME>\n<NARRATION>TEST import study "
                          "- DELETE ME</NARRATION>", 1)
    return vch


def _zero_amounts(vch: str) -> str:
    return re.sub(r"<AMOUNT>[^<]*</AMOUNT>", "<AMOUNT>0</AMOUNT>", vch)


def _drop_rates(vch: str) -> str:
    vch = re.sub(r"<RATE>[^<]*</RATE>", "", vch)
    vch = re.sub(r"<UDF:VCHMRP\.LIST.*?</UDF:VCHMRP\.LIST>", "", vch, flags=re.S)
    return vch


def _drop_batches(vch: str) -> str:
    return re.sub(r"<BATCHALLOCATIONS\.LIST>.*?</BATCHALLOCATIONS\.LIST>", "",
                  vch, flags=re.S)


def _assert_sales_order_only(xml: str) -> None:
    kinds = set(re.findall(r'VCHTYPE="([^"]*)"', xml))
    if kinds != {"Sales Order"}:
        raise RuntimeError(f"SAFETY: refusing — voucher types {kinds!r}")


def _envelope(company: str, voucher: str) -> str:
    return (
        "<ENVELOPE>\n"
        " <HEADER><VERSION>1</VERSION><TALLYREQUEST>Import</TALLYREQUEST>"
        "<TYPE>Data</TYPE><ID>Vouchers</ID></HEADER>\n"
        " <BODY><DESC><STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>"
        "</STATICVARIABLES></DESC>\n"
        "  <DATA><TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
        + voucher +
        "\n  </TALLYMESSAGE></DATA></BODY></ENVELOPE>"
    )


def _verdict(resp: str) -> str:
    def n(tag):
        m = re.search(rf"<{tag}>\s*(-?\d+)\s*</{tag}>", resp)
        return int(m.group(1)) if m else 0
    if n("CREATED") > 0 or n("ALTERED") > 0:
        return "IMPORTED"
    if n("EXCEPTIONS") > 0:
        return "exceptions"
    if "<LINEERROR>" in resp:
        m = re.search(r"<LINEERROR>([^<]*)</LINEERROR>", resp)
        return f"error: {m.group(1)[:60]}"
    return "ignored (all zero)"


def main() -> int:
    print_only = "--print" in sys.argv

    sample = HERE / "sample_orders.xml"
    if not sample.exists():
        sys.exit("sample_orders.xml not found — run fetch_order_sample.py first.")
    raw = sample.read_text(encoding="utf-8")
    vouchers = re.findall(r"<VOUCHER[^>]*VCHTYPE=\"Sales Order\".*?</VOUCHER>",
                          raw, flags=re.S)
    with_batches = [v for v in vouchers if "BATCHALLOCATIONS.LIST" in v]
    if not with_batches:
        sys.exit("No batched Sales Order specimen found in sample_orders.xml.")
    base = min(with_batches, key=len)
    party = (re.search(r"<PARTYLEDGERNAME>([^<]*)</PARTYLEDGERNAME>", base)
             or [None, "?"])[1]
    print(f"Specimen: {party!r}, {len(base):,} chars — the control.")

    p1 = _renumber(base, "CLTEST-P1")
    p2 = _zero_amounts(_renumber(base, "CLTEST-P2"))
    p3 = _drop_rates(_zero_amounts(_renumber(base, "CLTEST-P3")))
    p4 = _drop_batches(_drop_rates(_zero_amounts(_renumber(base, "CLTEST-P4"))))

    probes = [("P1 specimen clone, renumbered (control)", p1),
              ("P2 P1 + amounts zeroed", p2),
              ("P3 P2 + rates/MRP removed", p3),
              ("P4 P3 + batch allocations removed", p4)]

    # P5: what the pipeline itself generates, via the real builder.
    try:
        from order_importer import build_envelope, normalise_order, OrderSettings
        row = {"order_key": "CLTEST-P5", "company": COMPANY,
               "party_ledger": party, "order_no": "CLTEST-P5",
               "order_date": "", "lines": [
                   {"item_name": (re.search(r"<STOCKITEMNAME>([^<]*)</STOCKITEMNAME>",
                                            base) or [None, "?"])[1],
                    "size_batch": (re.search(r"<BATCHNAME>([^<]*)</BATCHNAME>",
                                             base) or [None, "30"])[1],
                    "qty": 1, "unit": "Doz", "due_days": 0}]}
        p5_full = build_envelope(normalise_order(row), OrderSettings(), "")
        probes.append(("P5 pipeline's own envelope", None, p5_full))
    except Exception as exc:  # noqa: BLE001 — diagnostic tool, keep going
        print(f"(P5 skipped: {exc})")

    st = load_settings(HERE / "config.toml")
    cfg = TallyConfig(host=st.tally.host, port=st.tally.port,
                      company=COMPANY, timeout=st.tally.timeout)
    if not print_only:
        assert_company_loaded(cfg)

    results = []
    for probe in probes:
        label, vch = probe[0], probe[1]
        xml = probe[2] if len(probe) > 2 else _envelope(COMPANY, vch)
        _assert_sales_order_only(xml)
        if print_only:
            print(f"\n======== {label} ========\n{xml[:2000]}\n... "
                  f"({len(xml):,} chars)")
            continue
        print(f"\n--- {label} ---")
        try:
            resp = post_write(cfg, xml)
            v = _verdict(resp)
        except TallyError as exc:
            v = f"transport/line error: {str(exc)[:80]}"
        print(f"    -> {v}")
        results.append((label, v))

    if not print_only:
        print("\n================ VERDICTS ================")
        for label, v in results:
            print(f"  {v:22s}  {label}")
        print("\nClean-up: any IMPORTED row is a Sales Order numbered CLTEST-* "
              "with narration 'TEST - DELETE ME'. Cancel them in Tally "
              "(X: Cancel Vch). Delete CLTEST rows from Import Exceptions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
