#!/usr/bin/env python3
"""Upload the dispatch-readiness page to the Frappe desk page.

    python3 push_dispatch_page.py out/dispatch.html out/report.json

Stores one row per report date in `Dispatch Readiness Snapshot`, which the
`Dispatch Readiness` desk page reads. Re-running for the same date replaces
that date rather than adding a second copy.

This is the desk PAGE. `push_report_frappe.py` is the desk NOTE — the two are
independent: the Note is a static summary that survives in email and print,
the page is the browsable report with search and party grouping. Pushing one
does not update the other.

Authenticates with the same API key the sync agent already uses, so there is
no shared secret to manage and no guest endpoint to leave open.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import requests

ENV = Path(__file__).resolve().parent.parent / "mcp_server" / ".env"


def creds():
    vals = {}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.strip().split("=", 1)
            vals[k] = v
    return (vals["FRAPPE_URL"].rstrip("/"),
            {"Authorization": f"token {vals['FRAPPE_API_KEY']}:"
                              f"{vals['FRAPPE_API_SECRET']}"})


def _iso(s, fmt="%d-%b-%Y"):
    """The report prints dates for people; the doctype stores them for sorting."""
    return datetime.strptime(s, fmt).date().isoformat()


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    page = Path(sys.argv[1]).read_text()
    report = json.loads(Path(sys.argv[2]).read_text())

    orders = report["orders"]
    got = sum(o["got"] for o in orders)
    need = sum(o["need"] for o in orders)

    # window_from prints without a year ("08-Jul"); it is always inside the
    # window ending at as_of, so borrow that year and step back if it lands
    # in the future
    as_of = _iso(report["as_of"])
    wf = report.get("window_from") or ""
    window_from = None
    if wf:
        year = int(as_of[:4])
        window_from = _iso(f"{wf}-{year}")
        if window_from > as_of:
            window_from = _iso(f"{wf}-{year - 1}")

    url, H = creds()
    r = requests.post(
        f"{url}/api/method/tally_bridge.dispatch_readiness.store",
        headers=H, timeout=180,
        json={
            "as_of": as_of,
            "window_from": window_from,
            "generated_on": datetime.now().replace(microsecond=0).isoformat(sep=" "),
            "order_count": len(orders),
            "coverage_pct": round(100.0 * got / need, 1) if need else 0.0,
            "blocking_items": len(report.get("blocking") or []),
            "unsighted_items": len(report.get("unknown") or []),
            "page_html": page,
            "payload_json": json.dumps(report, separators=(",", ":")),
        })
    if not r.ok:
        # The two failures this actually hits, both fixable in a minute, and
        # both easy to misread from a wall of traceback JSON.
        blob = r.text[:1500]
        if "No module named" in blob:
            sys.exit("The site does not have tally_bridge.dispatch_readiness "
                     "yet.\nOn Frappe Cloud the SITE update only applies an "
                     "already-built bench: update the APP on the bench and "
                     "Deploy first, then update the site.")
        if "PermissionError" in blob or r.status_code == 403:
            sys.exit("Permission refused. The uploading user needs the "
                     "'Dispatch Readiness Publisher' role — assign it on the "
                     "User record in Desk. Everything else it does is "
                     "read-only, which is why it is not a System Manager.")
        sys.exit(f"{r.status_code}: {blob}")
    msg = r.json().get("message", {})
    print(f"{msg.get('action', 'stored')} {msg.get('name')} — "
          f"{url}/app/dispatch-readiness")


if __name__ == "__main__":
    main()
