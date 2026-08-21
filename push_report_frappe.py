#!/usr/bin/env python3
"""Upsert the dispatch-readiness report as a desk Note on the Frappe site.

Desk Notes are visible to desk (staff) logins only — Website/portal users
cannot open them, which keeps the MD's internal-only rule. Title is stable
so the same Note updates in place every morning.

    python3 push_report_frappe.py "/path/to/latest.txt"
"""
import html, sys
from pathlib import Path
import requests

ENV = Path("/Users/rishabhsmac/FRAPPE/mcp_server/.env")
TITLE = "DISPATCH READINESS (auto)"

def main():
    text = Path(sys.argv[1]).read_text()
    vals = {}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.strip().split("=", 1); vals[k] = v
    url = vals["FRAPPE_URL"].rstrip("/")
    H = {"Authorization":
         f"token {vals['FRAPPE_API_KEY']}:{vals['FRAPPE_API_SECRET']}"}
    body = ("<div style='font-family:monospace;white-space:pre;"
            "font-size:12px'>" + html.escape(text) + "</div>")
    q = requests.get(f"{url}/api/resource/Note",
                     params={"filters": f'[["title","=","{TITLE}"]]',
                             "fields": '["name"]'},
                     headers=H, timeout=30).json()["data"]
    if q:
        r = requests.put(f"{url}/api/resource/Note/{q[0]['name']}",
                         headers=H, json={"content": body}, timeout=30)
    else:
        r = requests.post(f"{url}/api/resource/Note", headers=H, timeout=30,
                          json={"title": TITLE, "content": body, "public": 1})
    r.raise_for_status()
    print(f"{url}/app/note/{r.json()['data']['name']}")

if __name__ == "__main__":
    main()
