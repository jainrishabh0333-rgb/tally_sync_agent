#!/usr/bin/env python3
"""
probe_vouchers.py — find which voucher request YOUR TallyPrime honours.

Read-only. Sends several candidate export requests to Tally and reports, for
each, how many vouchers came back and whether the DATES actually fall inside
the window asked for. Nothing is written to Tally or to Frappe.

Why this exists: TallyPrime builds differ in which export respects
SVFROMDATE/SVTODATE. On this server the Day Book report returned the same
single voucher for every month of the year — silently. Rather than guess at
another request shape, this asks your Tally directly.

Run on the Tally server:

    python probe_vouchers.py

Then send the output back. It prints no financial detail beyond voucher
counts, dates and types.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from tally_client import (
    TallyConfig, TallyError, _parse_xml, _post, _text, _company_tag,
    _fmt_date, _tally_date_to_iso, list_companies,
)

HERE = Path(__file__).resolve().parent

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore


def _cfg() -> TallyConfig:
    host, port, company = "localhost", 9000, ""
    p = HERE / "config.toml"
    if p.exists() and tomllib is not None:
        raw = p.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        t = tomllib.loads(raw.decode("utf-8")).get("tally", {})
        host = t.get("host", host)
        port = int(t.get("port", port))
        comps = t.get("companies") or []
        company = (comps[0] if comps else t.get("company", "")) or ""
    return TallyConfig(host=host, port=int(port), company=company, timeout=180)


# --------------------------------------------------------------------------
# Candidate requests. Each returns a complete envelope.
# --------------------------------------------------------------------------

def v_report(cfg, frm, to, report, date_attr):
    dates = (f'<SVFROMDATE{date_attr}>{_fmt_date(frm)}</SVFROMDATE>'
             f'<SVTODATE{date_attr}>{_fmt_date(to)}</SVTODATE>')
    return f"""<ENVELOPE><HEADER><VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST><TYPE>Data</TYPE><ID>{report}</ID></HEADER>
<BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
{dates}{_company_tag(cfg)}</STATICVARIABLES>
<TDL><TDLMESSAGE><REPORT NAME="{report}" ISMODIFY="No"><FORMS>{report}</FORMS></REPORT>
</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


def v_collection_filter(cfg, frm, to, dfmt):
    """Voucher collection with an explicit TDL date filter."""
    def lit(d):
        return d.strftime("%d-%b-%Y") if dfmt == "dmy" else _fmt_date(d)
    flt = (f'$Date &gt;= $$Date:"{lit(frm)}" AND $Date &lt;= $$Date:"{lit(to)}"')
    return f"""<ENVELOPE><HEADER><VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>PV_Vch</ID></HEADER>
<BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>{_fmt_date(frm)}</SVFROMDATE><SVTODATE>{_fmt_date(to)}</SVTODATE>
{_company_tag(cfg)}</STATICVARIABLES>
<TDL><TDLMESSAGE>
<COLLECTION NAME="PV_Vch" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
 <TYPE>Voucher</TYPE>
 <FILTERS>PV_DateFilter</FILTERS>
 <FETCH>Date, Guid, VoucherTypeName, VoucherNumber, PartyLedgerName, Narration, IsCancelled, AlterId</FETCH>
 <FETCH>AllLedgerEntries.*</FETCH>
</COLLECTION>
<SYSTEM TYPE="Formulae" NAME="PV_DateFilter">{flt}</SYSTEM>
</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


def v_collection_svdates(cfg, frm, to):
    """Voucher collection relying on SVFROMDATE/SVTODATE alone."""
    return f"""<ENVELOPE><HEADER><VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>PV_Vch2</ID></HEADER>
<BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>{_fmt_date(frm)}</SVFROMDATE><SVTODATE>{_fmt_date(to)}</SVTODATE>
{_company_tag(cfg)}</STATICVARIABLES>
<TDL><TDLMESSAGE>
<COLLECTION NAME="PV_Vch2" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
 <TYPE>Voucher</TYPE>
 <BELONGSTO>Yes</BELONGSTO>
 <FETCH>Date, Guid, VoucherTypeName, VoucherNumber, PartyLedgerName, AlterId</FETCH>
</COLLECTION>
</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


CANDIDATES = [
    ("Day Book (plain dates)",        lambda c, f, t: v_report(c, f, t, "Day Book", "")),
    ("Day Book (TYPE=Date attr)",     lambda c, f, t: v_report(c, f, t, "Day Book", ' TYPE="Date"')),
    ("Voucher Register",              lambda c, f, t: v_report(c, f, t, "Voucher Register", "")),
    ("Daybook (one word)",            lambda c, f, t: v_report(c, f, t, "Daybook", "")),
    ("Collection + filter (d-Mon-Y)", lambda c, f, t: v_collection_filter(c, f, t, "dmy")),
    ("Collection + filter (YYYYMMDD)", lambda c, f, t: v_collection_filter(c, f, t, "ymd")),
    ("Collection + SV dates only",    lambda c, f, t: v_collection_svdates(c, f, t)),
]


def sample(cfg, build, frm, to):
    """Send one candidate; return (count, in_range, out_of_range, dates, note)."""
    try:
        raw = _post(cfg, build(cfg, frm, to))
    except TallyError as exc:
        return None, None, None, [], f"request failed: {str(exc)[:110]}"
    try:
        root = _parse_xml(raw)
    except TallyError as exc:
        return None, None, None, [], f"unparseable: {str(exc)[:110]}"

    dates, has_entries = [], 0
    total = 0
    for vel in root.iter("VOUCHER"):
        total += 1
        d = _tally_date_to_iso(_text(vel.find("DATE")))
        if d:
            dates.append(d)
        if next(vel.iter("ALLLEDGERENTRIES.LIST"), None) is not None:
            has_entries += 1
    inr = sum(1 for d in dates if str(frm) <= d <= str(to))
    out = len(dates) - inr
    note = f"{has_entries}/{total} carry ledger entries" if total else "empty"
    return total, inr, out, dates, note


def main() -> int:
    cfg = _cfg()
    print("=" * 70)
    print("Which voucher request does YOUR TallyPrime honour?  (read-only)")
    print("=" * 70)

    try:
        comps = list_companies(cfg)
    except TallyError as exc:
        print(f"Cannot reach Tally: {exc}")
        return 1
    names = [c["name"] for c in comps]
    if not cfg.company or cfg.company not in names:
        if not names:
            print("No companies open in Tally.")
            return 1
        cfg.company = names[0]
    print(f"Company: {cfg.company}")
    print(f"Also open: {', '.join(n for n in names if n != cfg.company) or '(none)'}")

    # Two windows: one absurd (must return nothing) and one real month.
    absurd = (date(1901, 1, 1), date(1901, 1, 2))
    real = (date(2026, 4, 1), date(2026, 4, 30))

    print()
    print(f"{'candidate':32} {'1901':>10}   {'Apr-2026':>10}   verdict")
    print("-" * 70)

    winners = []
    for label, build in CANDIDATES:
        a_total, _, _, _, a_note = sample(cfg, build, *absurd)
        r_total, r_in, r_out, r_dates, r_note = sample(cfg, build, *real)

        if a_total is None or r_total is None:
            verdict = "ERROR"
            detail = a_note if a_total is None else r_note
        elif a_total > 0:
            verdict = "IGNORES DATES"
            detail = f"returned {a_total} for 1901"
        elif r_total == 0:
            verdict = "empty"
            detail = "nothing for Apr-2026 either"
        elif r_out:
            verdict = "LEAKS"
            detail = f"{r_out} outside the window"
        else:
            verdict = "WORKS"
            detail = r_note
            winners.append(label)
        print(f"{label:32} {str(a_total):>10}   {str(r_total):>10}   {verdict}")
        print(f"{'':32} {'':10}   {'':10}   {detail}")

    print()
    print("=" * 70)
    if winners:
        print("USABLE:", ", ".join(winners))
        print("Send this output back — the agent will be set to use the first one.")
    else:
        print("None honoured the date range. Send this output back; a different")
        print("approach (fetching everything once and filtering locally) is next.")
        print()
        print("Also useful: TallyPrime version from Help > About.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
