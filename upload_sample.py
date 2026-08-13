#!/usr/bin/env python3
"""
upload_sample.py — send sample_orders.xml to Frappe so the Mac side can fetch it.

The Tally server accepts no inbound connections, so a file created here can
only leave by being PUSHED somewhere both sides already reach: the Frappe
site. This uploads sample_orders.xml as a private attachment on the failed
order-queue row it exists to diagnose — private, and readable by the
read-only key because that key can read the queue DocType.

    python upload_sample.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

from sync import load_settings

HERE = Path(__file__).resolve().parent
ATTACH_TO = "SO-KUMARHOSIERY-20260813-CS15"


def main() -> int:
    sample = HERE / "sample_orders.xml"
    if not sample.exists():
        sys.exit("sample_orders.xml not found — run fetch_order_sample.py first.")

    st = load_settings(HERE / "config.toml")
    fr = st.frappe
    resp = requests.post(
        f"{fr.url.rstrip('/')}/api/method/upload_file",
        headers={"Authorization": f"token {fr.api_key}:{fr.api_secret}"},
        files={"file": ("sample_orders.xml", sample.open("rb"), "text/xml")},
        data={"is_private": "1",
              "doctype": "Tally Order Queue",
              "docname": ATTACH_TO},
        timeout=300,
    )
    if resp.status_code != 200:
        sys.exit(f"Upload failed: HTTP {resp.status_code}: {resp.text[:300]}")
    info = resp.json().get("message", {})
    print(f"Uploaded ({sample.stat().st_size:,} bytes) -> {info.get('file_url')}")
    print("Done — tell Claude it is up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
