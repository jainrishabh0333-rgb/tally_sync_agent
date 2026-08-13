#!/usr/bin/env python3
"""
fetch_order_sample.py — export TODAY's Sales Order vouchers from Tally, in full.

Read-only. One job: show exactly how THIS Tally serialises a Sales Order —
which godown name it stores for the "Any" placeholder, whether accounting
allocations are present, how batch (size) rows and due dates nest — so the
order importer can be corrected against ground truth instead of guesses.

The Day Book report on this build famously ignores date ranges and always
answers with the CURRENT day's vouchers. Here that defect is exactly what we
want: an operator entered a real Sales Order today, so today's Day Book
contains a perfect specimen.

    python fetch_order_sample.py

Writes sample_orders.xml next to itself. Send that file back. It contains
voucher structure for today's orders only — review before sending if that
concerns you; amounts on manual orders will be visible.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

from sync import load_settings
from tally_client import (TallyConfig, TallyError, _post, _voucher_request,
                          assert_company_loaded)

HERE = Path(__file__).resolve().parent


def main() -> int:
    st = load_settings(HERE / "config.toml")
    companies = st.companies or []
    if not companies:
        from tally_client import list_companies
        try:
            companies = [c["name"] for c in list_companies(st.tally)]
        except TallyError as exc:
            sys.exit(f"Could not list companies: {exc}")
    if not companies:
        sys.exit("No company open in Tally.")

    today = date.today()
    blocks: list[str] = []
    for company in companies:
        cfg = TallyConfig(host=st.tally.host, port=st.tally.port,
                          company=company, timeout=st.tally.timeout)
        try:
            assert_company_loaded(cfg)
        except TallyError as exc:
            print(f"  skipping {company!r}: {exc}")
            continue
        print(f"Fetching today's Day Book for {company!r} ...")
        raw = _post(cfg, _voucher_request(cfg, today, today, "daybook"))
        # Pull complete VOUCHER blocks for Sales Orders only. The report
        # export nests freely, so match lazily from each opening tag with
        # a Sales Order VCHTYPE to its closing tag.
        found = re.findall(
            r"<VOUCHER[^>]*VCHTYPE=\"Sales Order\".*?</VOUCHER>",
            raw, flags=re.S)
        print(f"  {len(found)} Sales Order voucher(s) in today's Day Book.")
        blocks.extend(found)

    if not blocks:
        print("No Sales Orders found for today. Enter one in Tally first "
              "(or check the right company is open), then re-run.")
        return 1

    out = HERE / "sample_orders.xml"
    out.write_text(
        "<!-- Today's Sales Orders as Tally itself serialises them -->\n"
        + "\n\n".join(blocks),
        encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size:,} bytes). Send this file back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
