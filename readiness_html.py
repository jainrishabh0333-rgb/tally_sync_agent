#!/usr/bin/env python3
"""Render the dispatch-readiness report as a self-contained HTML dashboard.

    python3 readiness_html.py "~/Desktop/DISPATCH REPORT/latest.txt" -o latest.html
    python3 pending_readiness.py --host <h> --html latest.html      # direct

The report answers two different questions and the text version buries both:

  * which pending orders can go out today, and what single item is holding
    back the ones that nearly can;
  * what the factory should cut next — the items blocking the most orders.

So the page leads with one figure (share of pending demand dispatchable now),
then splits into three tabs: orders, blocking items, and the items with no
recent BlncQty sighting that are excluded from every percentage.

INTERNAL ONLY, same as the text report — it names parties and orders, and the
party-facing rule says none of that leaves the building. The output is a plain
file with no network calls, so it can be opened from the desktop or pushed to
the desk Note by push_report_frappe.py.

`parse_text()` reads the text report so any archived run can be re-rendered;
`render()` takes the structured dict that pending_readiness.py builds in
memory, which is the path that avoids a reparse.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Parsing the text report
# ---------------------------------------------------------------------------

_HEAD = re.compile(
    r"^DISPATCH READINESS\s+.\s+(?P<asof>\d{2}-\w{3}-\d{4})\s*"
    r"\(orders (?P<from>[\w-]+) onward")
_ORDER = re.compile(
    r"^\s*(?P<flag>READY|\d+%)\s\s"
    r"(?P<vno>\S.*?)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s\s"
    r"(?P<party>.*?)\s+"
    r"(?P<got>[\d.]+)/(?P<need>[\d.]+)"
    r"(?:\s+\(\+(?P<unknown>[\d.]+)\sunknown\))?\s*$")
_SHORT = re.compile(r"^\s+short (?P<qty>[\d.]+): (?P<item>.+?)\s*$")
_BLOCK = re.compile(
    r"^\s+blocks\s+(?P<orders>\d+) orders\s+short\s+(?P<qty>[\d.]+)\s+"
    r"(?P<item>.+?)\s*$")
_SIZES = re.compile(r"^\s+(?P<pairs>(?:[\w.-]+:[\d.]+\s*)+)$")
_UNKNOWN = re.compile(
    r"^\s+(?P<orders>\d+) orders\s+qty\s+(?P<qty>[\d.]+)\s+(?P<item>.+?)\s*$")


def parse_text(text: str) -> dict:
    """Turn a text dispatch report back into the structured dict.

    Written against the exact writer in pending_readiness.py. Voucher numbers
    longer than the 16-char pad shift every later column, so the fields are
    matched by shape rather than by fixed offsets.
    """
    report = {"as_of": "", "window_from": "", "orders": [],
              "blocking": [], "unknown": []}
    section = "orders"
    current = None

    for line in text.splitlines():
        if not line.strip():
            continue
        m = _HEAD.match(line)
        if m:
            report["as_of"] = m.group("asof")
            report["window_from"] = m.group("from")
            continue
        if line.startswith("ITEMS BLOCKING"):
            section, current = "blocking", None
            continue
        if line.lstrip().startswith("NO RECENT STOCK SIGHTING"):
            section, current = "unknown", None
            continue
        if line.startswith("=") or line.startswith("(short qty"):
            continue

        if section == "orders":
            m = _ORDER.match(line)
            if m:
                flag = m.group("flag")
                current = {
                    "voucher": m.group("vno"),
                    "date": m.group("date"),
                    "party": m.group("party"),
                    "got": float(m.group("got")),
                    "need": float(m.group("need")),
                    "unknown": float(m.group("unknown") or 0.0),
                    # recomputed, not the printed integer: the writer rounds,
                    # so a 99.7% order prints as "100%" and would otherwise
                    # land in a band the READY flag disagrees with
                    "pct": (100.0 * float(m.group("got")) / float(m.group("need"))
                            if float(m.group("need")) else 0.0),
                    "ready": flag == "READY",
                    "shorts": [],
                }
                report["orders"].append(current)
                continue
            m = _SHORT.match(line)
            if m and current is not None:
                current["shorts"].append(
                    {"item": m.group("item"), "qty": float(m.group("qty"))})
            continue

        if section == "blocking":
            m = _BLOCK.match(line)
            if m:
                current = {"item": m.group("item"),
                           "orders": int(m.group("orders")),
                           "qty": float(m.group("qty")), "sizes": {}}
                report["blocking"].append(current)
                continue
            m = _SIZES.match(line)
            if m and current is not None:
                for pair in m.group("pairs").split():
                    size, _, qty = pair.rpartition(":")
                    current["sizes"][size] = float(qty)
            continue

        m = _UNKNOWN.match(line)
        if m:
            report["unknown"].append({"item": m.group("item"),
                                      "orders": int(m.group("orders")),
                                      "qty": float(m.group("qty"))})

    report["order_count"] = len(report["orders"])
    return report


# ---------------------------------------------------------------------------
# Derived figures
# ---------------------------------------------------------------------------

# Three tones, not four. The four-tone version (good / warning / serious /
# critical) put warning-yellow and serious-orange on adjacent bars, which
# measures OKLab dE 13.6 under normal vision — below the 15 floor, so two
# neighbouring bands were hard to tell apart even with full colour vision.
# good / warning / critical clears both the CVD and normal-vision gates in
# light and dark. The tones map to an action: ship it, ship most of it, do not
# plan on it.
#
# Best-to-worst, the order the list reads in. Tests take the whole order, not
# just the percentage: an order whose lines are ALL unmeasured has need == 0 and
# would otherwise be filed as "nothing to send", which is a different — and
# much more alarming — thing than "we have not counted it".
BANDS = [
    ("Ready in full", lambda o: o["ready"], "good"),
    ("90 - 99%", lambda o: o["need"] and 90 <= o["pct"] < 99.95 and not o["ready"],
     "warning"),
    ("75 - 89%", lambda o: o["need"] and 75 <= o["pct"] < 90, "warning"),
    ("50 - 74%", lambda o: o["need"] and 50 <= o["pct"] < 75, "critical"),
    ("25 - 49%", lambda o: o["need"] and 25 <= o["pct"] < 50, "critical"),
    ("Under 25%", lambda o: o["need"] and 0.005 < o["pct"] < 25, "critical"),
    ("Nothing to send", lambda o: o["need"] and o["pct"] <= 0.005, "critical"),
    ("Not measured at all", lambda o: not o["need"], "unknown"),
]


def _summarise(report: dict) -> dict:
    orders = report["orders"]
    got = sum(o["got"] for o in orders)
    need = sum(o["need"] for o in orders)
    unknown = sum(o["unknown"] for o in orders)
    ready = [o for o in orders if o["ready"]]
    nothing = [o for o in orders if o["need"] and o["pct"] <= 0.005]
    unmeasured = [o for o in orders if not o["need"]]

    try:
        asof = datetime.strptime(report["as_of"], "%d-%b-%Y").date()
    except ValueError:
        asof = date.today()
    for o in orders:
        try:
            o["age"] = (asof - date.fromisoformat(o["date"])).days
        except ValueError:
            o["age"] = 0

    bands = []
    for label, test, tone in BANDS:
        rows = [o for o in orders if test(o)]
        bands.append({"label": label, "tone": tone, "count": len(rows),
                      "qty": round(sum(o["need"] + o["unknown"] for o in rows), 2)})

    return {
        "orders": len(orders),
        "ready": len(ready),
        "ready_qty": round(sum(o["need"] for o in ready), 2),
        "partial": len(orders) - len(ready) - len(nothing) - len(unmeasured),
        "nothing": len(nothing),
        "unmeasured": len(unmeasured),
        "got": round(got, 2),
        "need": round(need, 2),
        "short": round(need - got, 2),
        "unknown": round(unknown, 2),
        "pct": round(100.0 * got / need, 1) if need else 0.0,
        "bands": bands,
        "blocking_items": len(report["blocking"]),
        "blocking_qty": round(sum(b["qty"] for b in report["blocking"]), 2),
        "stale": max((o["age"] for o in orders), default=0),
        "over_30": len([o for o in orders if o["age"] > 30 and not o["ready"]]),
    }


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

_TEMPLATE = Path(__file__).with_name("readiness_template.html")

# The text report carries no company, so the masthead falls back to this.
# Tally's company names end in a financial-year suffix — "SN JAIN INDUSTRIES
# PVT LTD - (26-27)" — which is bookkeeping, not letterhead.
COMPANY = "SN Jain Industries Pvt Ltd"
_FY_SUFFIX = re.compile(r"\s*-\s*\(\d{2}-\d{2}\)\s*$")


def _company(name: str) -> str:
    return _FY_SUFFIX.sub("", name).strip() or COMPANY


def render(report: dict) -> str:
    """Structured report dict -> one self-contained HTML page."""
    summary = _summarise(report)
    payload = {
        "company": _company(report.get("company") or COMPANY),
        "as_of": report.get("as_of", ""),
        "window_from": report.get("window_from", ""),
        "orders": report["orders"],
        "blocking": report["blocking"],
        "unknown": report["unknown"],
        "summary": summary,
    }
    doc = _TEMPLATE.read_text()
    return doc.replace(
        "/*__DATA__*/null",
        json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    ).replace("__ASOF__", html.escape(report.get("as_of", "")))


# ---------------------------------------------------------------------------
# The Frappe desk Note
# ---------------------------------------------------------------------------

def note_html(report: dict, text: str) -> str:
    """Summary tables plus the full text report, for the desk Note.

    A Note is a rich-text field, not a page: its editor strips <style> and
    <script>, so everything here is plain tags with INLINE styles only — the
    same shape the existing Note already survives with. No interactivity, so
    the summary has to carry the answer on its own; the browsable page is a
    separate file.

    The Note this writes to is `public: 0` and desk-only. Never flip that —
    the report names parties, orders and shortfalls.
    """
    S = _summarise(report)
    e = html.escape

    def table(rows, head=None, right=(1,)):
        out = ['<table style="border-collapse:collapse;font-size:13px;'
               'margin:6px 0 16px">']
        if head:
            out.append("<tr>" + "".join(
                '<th style="text-align:%s;padding:3px 14px 4px 0;'
                'border-bottom:1px solid #c3c2b7;font-size:11px;'
                'letter-spacing:.06em;text-transform:uppercase;color:#898781">'
                "%s</th>" % ("right" if i in right else "left", e(h))
                for i, h in enumerate(head)) + "</tr>")
        for r in rows:
            out.append("<tr>" + "".join(
                '<td style="text-align:%s;padding:3px 14px 3px 0;'
                'border-bottom:1px solid #e1e0d9">%s</td>'
                % ("right" if i in right else "left", c)
                for i, c in enumerate(r)) + "</tr>")
        out.append("</table>")
        return "".join(out)

    def num(v, d=1):
        return f"{v:,.{d}f}".rstrip("0").rstrip(".") if d else f"{v:,.0f}"

    parties = {}
    for o in report["orders"]:
        g = parties.setdefault(o["party"], {"n": 0, "got": 0.0, "need": 0.0})
        g["n"] += 1; g["got"] += o["got"]; g["need"] += o["need"]
    top_parties = sorted(parties.items(), key=lambda x: -x[1]["n"])[:10]

    h = ['<div style="font-family:system-ui,-apple-system,sans-serif;'
         'color:#0b0b0b">']
    h.append('<div style="font-size:11px;letter-spacing:.1em;'
             'text-transform:uppercase;color:#d03b3b;font-weight:600">'
             "Internal — not for circulation</div>")
    h.append('<div style="font-size:13px;color:#52514e;margin:6px 0 14px">'
             f'As at <b>{e(report.get("as_of", ""))}</b> · orders from '
             f'{e(report.get("window_from", ""))} onward · '
             f'{S["orders"]:,} pending · oldest order allocated first</div>')
    h.append('<div style="font-size:32px;font-weight:600;line-height:1.1">'
             f'{S["pct"]}%</div>')
    h.append('<div style="font-size:13px;color:#52514e;margin:2px 0 18px">'
             f'of measurable pending demand is dispatchable today — '
             f'{num(S["got"])} of {num(S["need"])} coverable, '
             f'{num(S["short"])} short. A further {num(S["unknown"])} sits on '
             "lines with no stock sighting and is excluded.</div>")

    h.append("<b>Orders</b>")
    h.append(table([
        ["Ready in full", f'<b>{S["ready"]:,}</b>', f'{num(S["ready_qty"])} qty'],
        ["Part short", f'{S["partial"]:,}', ""],
        ["Nothing to send", f'{S["nothing"]:,}', ""],
        ["Not measured at all", f'{S["unmeasured"]:,}', ""],
        ["Short and over 30 days old", f'{S["over_30"]:,}',
         f'oldest {S["stale"]} days'],
    ], right=(1,)))

    h.append("<b>By readiness</b>")
    h.append(table([[b["label"], f'{b["count"]:,}', num(b["qty"]) + " qty"]
                    for b in S["bands"]],
                   head=["Band", "Orders", "Quantity"], right=(1, 2)))

    h.append("<b>Items blocking dispatch — cutting priority</b>")
    h.append(table([[e(b["item"]), f'{b["orders"]:,}', num(b["qty"])]
                    for b in report["blocking"][:10]],
                   head=["Item", "Blocks", "Short qty"], right=(1, 2)))

    h.append("<b>Parties with the most pending orders</b>")
    h.append(table([[e(p), f'{g["n"]:,}',
                     f'{100*g["got"]/g["need"]:.0f}%' if g["need"] else "—",
                     num(g["need"])]
                    for p, g in top_parties],
                   head=["Party / ledger", "Orders", "Coverage", "Ordered"],
                   right=(1, 2, 3)))

    h.append('<div style="font-size:12px;color:#898781;margin:18px 0 6px">'
             "Quantities are in each item's own billing unit (Doz, Box, Pcs, "
             "10pcs) and are not converted, so a total mixes units. "
             "Percentages are computed within each order and are unaffected. "
             "The browsable version of this report — searchable by ledger, "
             "order or item, and groupable by party — is produced by "
             "<code>pending_readiness.py --html</code>.</div>")
    h.append('<hr style="border:0;border-top:1px solid #c3c2b7;margin:18px 0">')
    h.append('<div style="font-size:12px;color:#52514e;margin-bottom:6px">'
             "Full report</div>")
    h.append('<div style="font-family:monospace;white-space:pre;font-size:12px">'
             + e(text) + "</div>")
    h.append("</div>")
    return "".join(h)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path,
                    help="a text dispatch report, or a .json dump of one")
    ap.add_argument("-o", "--out", type=Path, default=None)
    a = ap.parse_args()

    raw = a.source.expanduser().read_text()
    report = json.loads(raw) if a.source.suffix == ".json" else parse_text(raw)
    out = a.out or a.source.expanduser().with_suffix(".html")
    out.write_text(render(report))
    print(f"{out}  ({report['order_count']} orders, "
          f"{len(report['blocking'])} blocking items)")


if __name__ == "__main__":
    main()
