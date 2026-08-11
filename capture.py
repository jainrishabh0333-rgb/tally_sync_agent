#!/usr/bin/env python3
"""
capture.py — collect a small diagnostic bundle from your Tally, to share.

Read-only. Sends each candidate request, saves what Tally actually replies,
and writes everything into a folder you can zip and send. Nothing is written
to Tally and nothing is sent anywhere by this script.

Run on the Tally server:

    python capture.py

Then zip the `tally_capture` folder it creates and upload that.

PRIVACY: amounts and party names are replaced with placeholders by default,
because the point is the SHAPE of the response, not the money. Pass --raw only
if you are happy to share actual figures.

    python capture.py --raw
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from tally_client import (
    TallyConfig, TallyError, _post, _parse_xml, _text, _company_tag,
    _voucher_request, _tally_date_to_iso, list_companies, _company_start,
)

HERE = Path(__file__).resolve().parent
OUT = HERE / "tally_capture"

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore


def load_cfg() -> TallyConfig:
    host, port, company = "localhost", 9000, ""
    p = HERE / "config.toml"
    if p.exists() and tomllib is not None:
        raw = p.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        t = tomllib.loads(raw.decode("utf-8", "replace")).get("tally", {})
        host = t.get("host", host)
        port = int(t.get("port", port))
        comps = t.get("companies") or []
        company = (comps[0] if comps else t.get("company", "")) or ""
    return TallyConfig(host=host, port=int(port), company=company, timeout=90)


_AMOUNT = re.compile(r"(<(?:AMOUNT|CLOSINGBALANCE|OPENINGBALANCE|CLOSINGVALUE)[^>]*>)"
                     r"([^<]*)(</)")
_NAMES = re.compile(r"(<(?:LEDGERNAME|PARTYLEDGERNAME|NARRATION)[^>]*>)([^<]*)(</)")


def redact(xml: str) -> str:
    """Keep structure, drop figures and party names."""
    xml = _AMOUNT.sub(lambda m: f"{m.group(1)}-999.00{m.group(3)}", xml)
    xml = _NAMES.sub(lambda m: f"{m.group(1)}REDACTED{m.group(3)}", xml)
    return xml


def save(name: str, text: str, raw: bool, limit: int = 120_000) -> None:
    body = text if raw else redact(text)
    truncated = len(body) > limit
    (OUT / name).write_text(
        body[:limit] + ("\n\n<!-- truncated for size -->" if truncated else ""),
        encoding="utf-8",
    )
    print(f"    saved {name}  ({len(text):,} chars"
          + (", truncated" if truncated else "") + ")")


def probe(cfg: TallyConfig, label: str, body: str, raw: bool) -> str:
    print(f"  {label} ...", end=" ", flush=True)
    try:
        resp = _post(cfg, body)
    except TallyError as exc:
        print(f"FAILED: {str(exc)[:90]}")
        (OUT / f"{label}.error.txt").write_text(str(exc), encoding="utf-8")
        return "error"
    try:
        root = _parse_xml(resp)
        vouchers = sum(1 for _ in root.iter("VOUCHER"))
        dates = sorted({_tally_date_to_iso(_text(v.find("DATE")))
                        for v in root.iter("VOUCHER")} - {""})
        span = f"{dates[0]}..{dates[-1]}" if dates else "no dates"
        print(f"{vouchers} vouchers, {span}")
    except TallyError as exc:
        print(f"unparseable: {str(exc)[:70]}")
    save(f"{label}.xml", resp, raw)
    return "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect a Tally diagnostic bundle.")
    ap.add_argument("--raw", action="store_true",
                    help="include real amounts and names (default: redacted)")
    ap.add_argument("--company", help="which company (default: first configured/open)")
    args = ap.parse_args()

    cfg = load_cfg()
    OUT.mkdir(exist_ok=True)

    print("=" * 66)
    print("Tally diagnostic capture   (read-only)")
    print("=" * 66)
    print(f"Amounts and names: {'INCLUDED (--raw)' if args.raw else 'redacted'}")
    print()

    # --- 1. what is open ---------------------------------------------------
    print("Companies open in Tally:")
    try:
        comps = list_companies(cfg)
    except TallyError as exc:
        print(f"  cannot reach Tally: {exc}")
        return 1
    for c in comps:
        print(f"  - {c['name']}   (books from {_tally_date_to_iso(c.get('starting_from') or '')})")
    (OUT / "companies.txt").write_text(
        "\n".join(f"{c['name']}\t{c.get('starting_from','')}" for c in comps),
        encoding="utf-8")

    cfg.company = args.company or cfg.company or (comps[0]["name"] if comps else "")
    if not cfg.company:
        print("No company available.")
        return 1
    print(f"\nCapturing for: {cfg.company}")
    start = _company_start(cfg) or date.today().replace(month=4, day=1)
    frm, to = start, start + timedelta(days=29)
    print(f"Sample window : {frm} .. {to}\n")

    # --- 2. every voucher request shape ------------------------------------
    print("Voucher request shapes:")
    for variant in ("filter_plain", "filter_plain_dmy", "filter",
                    "daybook", "daybook_typed", "register"):
        probe(cfg, variant, _voucher_request(cfg, frm, to, variant), args.raw)

    # --- 3. an unfiltered sample, to prove vouchers exist at all -----------
    print("\nUnfiltered (proves whether the company holds vouchers):")
    probe(cfg, "all_unfiltered", _voucher_request(cfg, frm, to, "all"), args.raw)

    # --- 4. one raw master sample, for field-name checking -----------------
    print("\nMaster samples:")
    for label, coll_id, tally_type, methods in (
        ("bills", "TB_Bills", "Bills",
         ["Name", "Parent", "BillDate", "BillCreditPeriod", "ClosingBalance"]),
        ("stock_items", "TB_StockItems", "StockItem",
         ["Name", "Parent", "BaseUnits", "ClosingBalance", "ClosingValue"]),
        ("units", "TB_Units", "Unit",
         ["Name", "BaseUnits", "Conversion"]),
    ):
        lines = "\n     ".join(f"<NATIVEMETHOD>{m}</NATIVEMETHOD>" for m in methods)
        body = f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>{coll_id}</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVFROMDATE TYPE="Date">{frm:%Y%m%d}</SVFROMDATE>
   <SVTODATE TYPE="Date">{to:%Y%m%d}</SVTODATE>
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="{coll_id}" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <TYPE>{tally_type}</TYPE>
     {lines}
   </COLLECTION>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""
        probe(cfg, label, body, args.raw)

    print()
    print("=" * 66)
    print(f"Done. Everything is in:  {OUT}")
    print()
    print("Right-click the 'tally_capture' folder > Send to > Compressed folder,")
    print("then upload the zip.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
