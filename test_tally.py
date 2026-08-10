#!/usr/bin/env python3
"""
test_tally.py — read-only smoke test against a live TallyPrime.

Run this ON the machine where TallyPrime is running (or one that can reach it).
It needs NO Frappe account and writes nothing anywhere: it only asks Tally for
data and prints a summary, so you can confirm the connection and see that the
numbers look right before wiring up the rest of the system.

Usage
-----
    python test_tally.py                          # uses config.toml if present
    python test_tally.py --company "SN JAIN INDUSTRIES"
    python test_tally.py --host 192.168.1.7 --port 9000
    python test_tally.py --days 90                # widen the voucher sample

Output is deliberately plain ASCII so it renders correctly in the Windows
console, which does not handle rupee signs or box-drawing characters well.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from tally_client import (
    TallyConfig,
    TallyError,
    fetch_ledgers,
    fetch_vouchers,
    list_companies,
)

HERE = Path(__file__).resolve().parent

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore


def money(n: float) -> str:
    """Indian-style grouping without any non-ASCII currency symbol."""
    neg = n < 0
    s = f"{abs(n):.2f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    out = whole + "." + frac
    return ("-" + out) if neg else out


def load_tally_config(args) -> TallyConfig:
    host, port, company = args.host, args.port, args.company
    cfg_path = HERE / "config.toml"
    if cfg_path.exists() and tomllib is not None and not (host and company):
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh).get("tally", {})
        host = host or data.get("host")
        port = port or data.get("port")
        company = company or data.get("company")
    return TallyConfig(
        host=host or "localhost",
        port=int(port or 9000),
        company=company or "",
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only smoke test against TallyPrime.")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--company")
    p.add_argument("--days", type=int, default=30,
                   help="how many days of vouchers to sample (default 30)")
    args = p.parse_args()

    cfg = load_tally_config(args)

    print("=" * 62)
    print("TallyPrime connection test  (read-only, nothing is modified)")
    print("=" * 62)
    print(f"Connecting to {cfg.url} ...")

    # ---- 1. reachability + company discovery ----------------------------
    try:
        companies = list_companies(cfg)
    except TallyError as exc:
        print()
        print("FAILED to reach Tally.")
        print(f"  {exc}")
        print()
        print("Checklist:")
        print("  1. Is TallyPrime open, with a company loaded?")
        print("  2. F1 > Settings > Connectivity > Client/Server configuration")
        print("       'TallyPrime acts as' must be Server, port 9000.")
        print("  3. Restart TallyPrime after changing that setting.")
        print("  4. Run this on the same machine as Tally, or set --host.")
        return 1

    print("  OK - Tally is responding.")
    if not companies:
        print()
        print("  WARNING: Tally answered, but reports no open companies.")
        print("  Open your company in Tally, then run this again.")
        return 1

    print()
    print(f"Companies open in Tally ({len(companies)}):")
    for c in companies:
        print(f"  - {c['name']}")

    if not cfg.company:
        cfg.company = companies[0]["name"]
        print()
        print(f"No company configured; using: {cfg.company}")
    elif cfg.company not in [c["name"] for c in companies]:
        print()
        print(f"  WARNING: configured company '{cfg.company}' is not in that list.")
        print("  Copy the exact name printed above into config.toml.")
        print("  Continuing with the configured name anyway...")

    # ---- 2. ledgers ------------------------------------------------------
    print()
    print("-" * 62)
    print("Reading ledger masters ...")
    try:
        ledgers = fetch_ledgers(cfg)
    except TallyError as exc:
        print(f"  FAILED: {exc}")
        return 1

    if not ledgers:
        print("  No ledgers returned. Usually means the company name does not match.")
        return 1

    print(f"  OK - {len(ledgers)} ledgers.")

    groups: dict[str, int] = {}
    for l in ledgers:
        groups[l.parent or "(no group)"] = groups.get(l.parent or "(no group)", 0) + 1
    print()
    print("  Largest groups:")
    for g, n in sorted(groups.items(), key=lambda kv: -kv[1])[:8]:
        print(f"    {n:5d}  {g}")

    debtors = [l for l in ledgers if l.parent == "Sundry Debtors"]
    creditors = [l for l in ledgers if l.parent == "Sundry Creditors"]
    print()
    print(f"  Sundry Debtors   : {len(debtors):5d} ledgers, "
          f"net {money(sum(l.closing_balance for l in debtors))}")
    print(f"  Sundry Creditors : {len(creditors):5d} ledgers, "
          f"net {money(sum(l.closing_balance for l in creditors))}")

    if debtors:
        print()
        print("  Top 5 debtor balances (check these against Tally):")
        for l in sorted(debtors, key=lambda x: -abs(x.closing_balance))[:5]:
            print(f"    {money(l.closing_balance):>18}  {l.name[:40]}")

    with_gstin = sum(1 for l in ledgers if l.gstin)
    print()
    print(f"  Ledgers carrying a GSTIN: {with_gstin}")

    # ---- 3. vouchers -----------------------------------------------------
    to = date.today()
    frm = to - timedelta(days=args.days)
    print()
    print("-" * 62)
    print(f"Reading vouchers for the last {args.days} days ({frm} to {to}) ...")
    try:
        vouchers = fetch_vouchers(cfg, frm, to)
    except TallyError as exc:
        print(f"  FAILED: {exc}")
        return 1

    print(f"  OK - {len(vouchers)} vouchers.")
    if not vouchers:
        print()
        print("  None found in that window. Not necessarily a problem -")
        print(f"  try a wider range:  python test_tally.py --days 365")
        print()
        print("Tally connection works. Ledgers read fine.")
        return 0

    kinds: dict[str, list] = {}
    for v in vouchers:
        kinds.setdefault(v.voucher_type, []).append(v)
    print()
    print("  By voucher type:")
    for k, vs in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
        total = sum(v.amount for v in vs)
        print(f"    {len(vs):5d}  {k[:28]:28} {money(total):>18}")

    print()
    print("  5 most recent vouchers:")
    for v in sorted(vouchers, key=lambda x: x.date, reverse=True)[:5]:
        party = (v.party or "-")[:26]
        print(f"    {v.date}  {v.voucher_type[:10]:10} {v.voucher_number[:12]:12} "
              f"{party:26} {money(v.amount):>16}")

    # ---- 4. integrity ----------------------------------------------------
    print()
    print("-" * 62)
    print("Integrity check (entries of each voucher should net to zero) ...")
    bad = []
    no_entries = 0
    for v in vouchers:
        if not v.entries:
            no_entries += 1
            continue
        net = sum(e.amount for e in v.entries)
        if abs(net) > 0.01:
            bad.append((v, net))

    if no_entries:
        print(f"  NOTE: {no_entries} voucher(s) came back with no ledger entries.")
    if bad:
        print(f"  WARNING: {len(bad)} voucher(s) do not balance:")
        for v, net in bad[:5]:
            print(f"    {v.date} {v.voucher_type} {v.voucher_number} -> off by {money(net)}")
        print("  This usually means the XML export dropped entries.")
    if not bad and not no_entries:
        print("  OK - every voucher balances.")

    print()
    print("=" * 62)
    print("Tally connection works. The sync agent will be able to read this data.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
