#!/usr/bin/env python3
"""
study.py — try many voucher-request forms against YOUR Tally in one run.

Read-only. Instead of one hypothesis per push/download cycle, this sends ~30
systematic variations of the voucher request and reports which actually
return dated vouchers. One run answers what has taken days of guessing.

    python study.py

It prints a compact table and writes study_result.txt next to itself. Send
that file back — it contains counts and element names, not your figures.

Optional:
    python study.py --company "SN JAIN INDUSTRIES PVT LTD - (25-26)"
    python study.py --keep-samples     # also save the raw XML of winners
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from tally_client import (
    TallyConfig, TallyError, _post, _parse_xml, _text, _company_tag,
    _fmt_date, _tally_date_to_iso, list_companies, _company_start,
)

HERE = Path(__file__).resolve().parent

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
        host, port = t.get("host", host), int(t.get("port", port))
        comps = t.get("companies") or []
        company = (comps[0] if comps else t.get("company", "")) or ""
    # Short timeout: this is a survey, and a form that hangs is not a winner.
    return TallyConfig(host=host, port=int(port), company=company, timeout=40)


# --------------------------------------------------------------------------
# The experiment matrix
# --------------------------------------------------------------------------

def envelope(cfg, coll_id, inner, statics="") -> str:
    return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>{coll_id}</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   {statics}
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
{inner}
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


def report_envelope(cfg, report, frm, to, typed=False) -> str:
    a = ' TYPE="Date"' if typed else ""
    return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Data</TYPE><ID>{report}</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVFROMDATE{a}>{_fmt_date(frm)}</SVFROMDATE><SVTODATE{a}>{_fmt_date(to)}</SVTODATE>
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <REPORT NAME="{report}" ISMODIFY="No"><FORMS>{report}</FORMS></REPORT>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


FIELD_SETS = {
    "fetch_dotted": "<FETCH>Guid,Date,VoucherTypeName,VoucherNumber,PartyLedgerName,"
                    "AllLedgerEntries.LedgerName,AllLedgerEntries.Amount,"
                    "AllLedgerEntries.IsDeemedPositive</FETCH>",
    "fetch_star": "<FETCH>*</FETCH>",
    "fetch_plain": "<FETCH>Guid,Date,VoucherTypeName,VoucherNumber,PartyLedgerName</FETCH>",
    "native": ("<NATIVEMETHOD>Date</NATIVEMETHOD>"
               "<NATIVEMETHOD>Guid</NATIVEMETHOD>"
               "<NATIVEMETHOD>VoucherTypeName</NATIVEMETHOD>"
               "<NATIVEMETHOD>VoucherNumber</NATIVEMETHOD>"
               "<NATIVEMETHOD>PartyLedgerName</NATIVEMETHOD>"
               "<NATIVEMETHOD>AllLedgerEntries</NATIVEMETHOD>"),
    "none": "",
}


def build_cases(cfg, frm, to):
    """Every combination worth testing, as (label, envelope)."""
    fd, td = _fmt_date(frm), _fmt_date(to)
    dmy_f, dmy_t = frm.strftime("%d-%b-%Y"), to.strftime("%d-%b-%Y")
    cases = []

    # A. Collection + TDL date filter, across field sets and date literals.
    for fs_name, fs in FIELD_SETS.items():
        for lit_name, lo, hi in (("ymd", fd, td), ("dmy", dmy_f, dmy_t)):
            cond = f'$Date &gt;= $$Date:"{lo}" and $Date &lt;= $$Date:"{hi}"'
            inner = (f'   <COLLECTION NAME="ST_V" ISMODIFY="No" ISFIXED="No" '
                     f'ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">\n'
                     f"    <TYPE>Voucher</TYPE>\n    {fs}\n"
                     f"    <FILTER>ST_P</FILTER>\n   </COLLECTION>\n"
                     f'   <SYSTEM TYPE="Formulae" NAME="ST_P">{cond}</SYSTEM>')
            cases.append((f"coll+filter[{fs_name},{lit_name}]",
                          envelope(cfg, "ST_V", inner)))

    # B. Same, but ALSO sending SVFROMDATE/SVTODATE.
    cond = f'$Date &gt;= $$Date:"{fd}" and $Date &lt;= $$Date:"{td}"'
    inner = ('   <COLLECTION NAME="ST_V" ISMODIFY="No"><TYPE>Voucher</TYPE>\n'
             f'    {FIELD_SETS["fetch_dotted"]}\n    <FILTER>ST_P</FILTER>\n   </COLLECTION>\n'
             f'   <SYSTEM TYPE="Formulae" NAME="ST_P">{cond}</SYSTEM>')
    cases.append(("coll+filter+SVdates",
                  envelope(cfg, "ST_V", inner,
                           f'<SVFROMDATE TYPE="Date">{fd}</SVFROMDATE>'
                           f'<SVTODATE TYPE="Date">{td}</SVTODATE>')))

    # C. Collection, NO filter — does it return anything at all?
    for fs_name in ("fetch_dotted", "native", "none"):
        inner = ('   <COLLECTION NAME="ST_V" ISMODIFY="No"><TYPE>Voucher</TYPE>\n'
                 f'    {FIELD_SETS[fs_name]}\n   </COLLECTION>')
        cases.append((f"coll_nofilter[{fs_name}]", envelope(cfg, "ST_V", inner)))

    # D. Collection scoped by voucher TYPE rather than date.
    inner = ('   <COLLECTION NAME="ST_V" ISMODIFY="No"><TYPE>Sales Voucher</TYPE>\n'
             f'    {FIELD_SETS["fetch_dotted"]}\n   </COLLECTION>')
    cases.append(("coll[Sales Voucher]", envelope(cfg, "ST_V", inner)))

    # E. Built-in reports, both date-attribute forms.
    for rep in ("Day Book", "Daybook", "Voucher Register", "Ledger Vouchers"):
        cases.append((f"report[{rep}]", report_envelope(cfg, rep, frm, to)))
    cases.append(('report[Day Book,typed]', report_envelope(cfg, "Day Book", frm, to, True)))

    return cases


def analyse(xml: str, frm: date, to: date) -> dict:
    """What did Tally actually send back?"""
    try:
        root = _parse_xml(xml)
    except TallyError as exc:
        return {"status": f"unparseable: {str(exc)[:40]}"}

    tags = Counter(el.tag for el in root.iter())
    vouchers = list(root.iter("VOUCHER"))
    dates = [_tally_date_to_iso(_text(v.find("DATE"))) for v in vouchers]
    dated = [d for d in dates if d]
    in_range = [d for d in dated if str(frm) <= d <= str(to)]
    entries = sum(1 for v in vouchers for _ in v.iter("ALLLEDGERENTRIES.LIST"))

    if not vouchers:
        # Maybe the rows come back under a different element name entirely.
        interesting = [t for t, n in tags.most_common(8)
                       if t not in ("ENVELOPE", "BODY", "DATA", "COLLECTION",
                                    "TALLYMESSAGE", "DESC", "HEADER")]
        return {"status": "no VOUCHER elements", "vouchers": 0,
                "top_tags": interesting[:5], "bytes": len(xml)}

    return {
        "status": "ok",
        "vouchers": len(vouchers),
        "dated": len(dated),
        "in_range": len(in_range),
        "span": f"{min(dated)}..{max(dated)}" if dated else "-",
        "entries": entries,
        "bytes": len(xml),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Survey Tally voucher request forms.")
    ap.add_argument("--company")
    ap.add_argument("--keep-samples", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg()
    try:
        comps = list_companies(cfg)
    except TallyError as exc:
        print(f"Cannot reach Tally: {exc}")
        return 1
    names = [c["name"] for c in comps]
    cfg.company = args.company or (cfg.company if cfg.company in names else "") or (names[0] if names else "")
    if not cfg.company:
        print("No company open in Tally.")
        return 1

    start = _company_start(cfg) or date.today().replace(month=4, day=1)
    frm, to = start, start + timedelta(days=29)

    lines = []

    def out(s=""):
        print(s)
        lines.append(s)

    out("=" * 78)
    out("Tally voucher request survey   (read-only)")
    out("=" * 78)
    out(f"Company : {cfg.company}")
    out(f"Window  : {frm} .. {to}")
    out(f"Also open: {', '.join(n for n in names if n != cfg.company) or '(none)'}")
    out()

    cases = build_cases(cfg, frm, to)
    out(f"Trying {len(cases)} request forms...")
    out()
    out(f"{'form':34} {'vouchers':>8} {'dated':>6} {'in-range':>9} {'entries':>8}  result")
    out("-" * 78)

    winners = []
    for label, body in cases:
        try:
            resp = _post(cfg, body)
        except TallyError as exc:
            out(f"{label:34} {'':>8} {'':>6} {'':>9} {'':>8}  FAILED {str(exc)[:24]}")
            continue
        a = analyse(resp, frm, to)
        if a["status"] == "ok":
            out(f"{label:34} {a['vouchers']:>8} {a['dated']:>6} {a['in_range']:>9} "
                f"{a['entries']:>8}  {a['span']}")
            if a["in_range"] and a["in_range"] == a["dated"]:
                winners.append((label, a, resp))
        elif a["status"] == "no VOUCHER elements":
            out(f"{label:34} {0:>8} {'':>6} {'':>9} {'':>8}  no VOUCHER tags; "
                f"saw {a.get('top_tags')}")
        else:
            out(f"{label:34} {'':>8} {'':>6} {'':>9} {'':>8}  {a['status']}")

    out()
    out("=" * 78)
    if winners:
        best = max(winners, key=lambda w: (w[1]["entries"], w[1]["vouchers"]))
        out(f"WORKS: {len(winners)} form(s) returned only in-range dated vouchers.")
        for lbl, a, _ in winners:
            out(f"   {lbl}  ({a['vouchers']} vouchers, {a['entries']} ledger entries)")
        out()
        out(f"BEST : {best[0]}")
        if args.keep_samples:
            (HERE / "study_winner.xml").write_text(best[2][:200_000], encoding="utf-8")
            out("   raw sample written to study_winner.xml")
    else:
        out("NONE of these forms returned in-range dated vouchers.")
        out("Send study_result.txt back — the table above narrows it down a lot.")
    out("=" * 78)

    (HERE / "study_result.txt").write_text("\n".join(lines), encoding="utf-8")
    print()
    print(f"Written to: {HERE / 'study_result.txt'}   <- send me this file")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
