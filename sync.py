#!/usr/bin/env python3
"""
sync.py — Tally -> Frappe sync agent.

Runs on any Windows/Linux machine on the same LAN as TallyPrime. Makes only
outbound connections: reads Tally over local HTTP, pushes to Frappe Cloud over
HTTPS. Tally is never written to.

Usage
-----
    python sync.py --check                 # verify Tally + Frappe connectivity
    python sync.py                         # incremental sync (default)
    python sync.py --full                  # re-sync from financial year start
    python sync.py --from 2025-04-01 --to 2025-06-30
    python sync.py --ledgers-only

Configure via config.toml (preferred) or environment variables. See
config.example.toml.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from frappe_client import FrappeClient, FrappeConfig, FrappeError
from tally_client import (
    TallyConfig,
    TallyError,
    fetch_ledgers,
    fetch_vouchers,
    list_companies,
)

try:  # Python 3.11+ has tomllib; fall back to tomli if present.
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / "sync.log"

log = logging.getLogger("sync")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Settings:
    tally: TallyConfig
    frappe: FrappeConfig
    chunk_days: int = 31          # export vouchers in chunks to avoid timeouts
    overlap_days: int = 7         # re-pull recent days to catch back-dated edits
    fy_start_month: int = 4       # Indian financial year starts April


def load_settings(path: Path | None = None) -> Settings:
    cfg_path = path or (HERE / "config.toml")
    data: dict = {}
    if cfg_path.exists():
        if tomllib is None:
            sys.exit(
                "config.toml found but no TOML parser available.\n"
                "Run: pip install tomli   (or use Python 3.11+)"
            )
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)

    t = data.get("tally", {})
    f = data.get("frappe", {})
    s = data.get("sync", {})

    tally = TallyConfig(
        host=os.getenv("TALLY_HOST", t.get("host", "localhost")),
        port=int(os.getenv("TALLY_PORT", t.get("port", 9000))),
        company=os.getenv("TALLY_COMPANY", t.get("company", "")),
        timeout=int(t.get("timeout", 120)),
    )
    frappe = FrappeConfig(
        url=os.getenv("FRAPPE_URL", f.get("url", "")),
        api_key=os.getenv("FRAPPE_API_KEY", f.get("api_key", "")),
        api_secret=os.getenv("FRAPPE_API_SECRET", f.get("api_secret", "")),
    )

    missing = [
        n for n, v in (
            ("tally.company", tally.company),
            ("frappe.url", frappe.url),
            ("frappe.api_key", frappe.api_key),
            ("frappe.api_secret", frappe.api_secret),
        ) if not v
    ]
    if missing:
        sys.exit(
            "Missing configuration: " + ", ".join(missing) +
            f"\nCreate {cfg_path} from config.example.toml, or set env vars."
        )

    return Settings(
        tally=tally,
        frappe=frappe,
        chunk_days=int(s.get("chunk_days", 31)),
        overlap_days=int(s.get("overlap_days", 7)),
        fy_start_month=int(s.get("fy_start_month", 4)),
    )


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def fy_start(today: date, fy_start_month: int) -> date:
    """Start of the current financial year."""
    year = today.year if today.month >= fy_start_month else today.year - 1
    return date(year, fy_start_month, 1)


def date_chunks(frm: date, to: date, days: int):
    """Yield (start, end) windows of at most `days` length."""
    cur = frm
    while cur <= to:
        end = min(cur + timedelta(days=days - 1), to)
        yield cur, end
        cur = end + timedelta(days=1)


# ---------------------------------------------------------------------------
# Sync steps
# ---------------------------------------------------------------------------

def sync_ledgers(st: Settings, fc: FrappeClient) -> int:
    log.info("Syncing ledger masters...")
    ledgers = fetch_ledgers(st.tally)
    if not ledgers:
        log.warning("No ledgers returned — check the company name in config.")
        return 0
    payload = [dataclasses.asdict(l) for l in ledgers]
    # Push in batches so a huge chart of accounts doesn't blow the request size.
    pushed = 0
    for i in range(0, len(payload), 500):
        batch = payload[i:i + 500]
        fc.upsert_ledgers(batch)
        pushed += len(batch)
        log.info("  ledgers %d/%d", pushed, len(payload))
    return pushed


def sync_vouchers(st: Settings, fc: FrappeClient, frm: date, to: date) -> int:
    log.info("Syncing vouchers %s .. %s", frm, to)
    total = 0
    for c_from, c_to in date_chunks(frm, to, st.chunk_days):
        vouchers = fetch_vouchers(st.tally, c_from, c_to)
        if not vouchers:
            log.info("  %s..%s: none", c_from, c_to)
            continue
        payload = [dataclasses.asdict(v) for v in vouchers]
        for i in range(0, len(payload), 200):
            fc.upsert_vouchers(payload[i:i + 200])
        total += len(payload)
        log.info("  %s..%s: %d vouchers", c_from, c_to, len(payload))
    return total


def resolve_range(st: Settings, fc: FrappeClient, args) -> tuple[date, date]:
    today = date.today()
    if args.frm and args.to:
        return (
            datetime.strptime(args.frm, "%Y-%m-%d").date(),
            datetime.strptime(args.to, "%Y-%m-%d").date(),
        )
    if args.full:
        return fy_start(today, st.fy_start_month), today

    # Incremental: resume from the last synced voucher date, minus an overlap
    # window so back-dated or edited vouchers get picked up again.
    state = {}
    try:
        state = fc.get_sync_state()
    except FrappeError as exc:
        log.warning("Could not read sync state (%s) — falling back to full sync.", exc)

    last = state.get("last_voucher_date")
    if last:
        start = datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=st.overlap_days)
        start = max(start, fy_start(today, st.fy_start_month))
        return start, today
    return fy_start(today, st.fy_start_month), today


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def check(st: Settings) -> int:
    ok = True
    print("Checking Tally...")
    try:
        companies = list_companies(st.tally)
        print(f"  OK — Tally reachable at {st.tally.url}")
        if companies:
            print("  Open companies:")
            for c in companies:
                mark = " <-- configured" if c["name"] == st.tally.company else ""
                print(f"    - {c['name']}{mark}")
            names = [c["name"] for c in companies]
            if st.tally.company not in names:
                print(f"  WARNING: configured company '{st.tally.company}' is not open in Tally.")
                ok = False
        else:
            print("  WARNING: no companies reported. Is a company loaded in Tally?")
            ok = False
    except TallyError as exc:
        print(f"  FAILED — {exc}")
        ok = False

    print("Checking Frappe...")
    try:
        user = FrappeClient(st.frappe).ping()
        print(f"  OK — authenticated to {st.frappe.url} as {user}")
    except FrappeError as exc:
        print(f"  FAILED — {exc}")
        ok = False

    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Sync TallyPrime data into Frappe.")
    p.add_argument("--check", action="store_true", help="test connectivity and exit")
    p.add_argument("--full", action="store_true", help="sync from start of financial year")
    p.add_argument("--from", dest="frm", metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="to", metavar="YYYY-MM-DD")
    p.add_argument("--ledgers-only", action="store_true")
    p.add_argument("--vouchers-only", action="store_true")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)-7s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )

    st = load_settings(args.config)

    if args.check:
        return check(st)

    fc = FrappeClient(st.frappe)
    started = datetime.now()
    counts = {"ledgers": 0, "vouchers": 0}

    try:
        if not args.vouchers_only:
            counts["ledgers"] = sync_ledgers(st, fc)
        if not args.ledgers_only:
            frm, to = resolve_range(st, fc, args)
            counts["vouchers"] = sync_vouchers(st, fc, frm, to)
            counts["range"] = f"{frm}..{to}"
    except (TallyError, FrappeError) as exc:
        log.error("Sync failed: %s", exc)
        fc.log_sync("Failed", {"error": str(exc), **counts})
        return 1

    elapsed = (datetime.now() - started).total_seconds()
    counts["seconds"] = round(elapsed, 1)
    log.info("Sync complete in %.1fs — %s", elapsed, counts)
    fc.log_sync("Success", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
