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
    fetch_groups,
    fetch_ledgers,
    fetch_vouchers,
    list_companies,
    classify_group,
    resolve_group_chain,
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
    companies: list = dataclasses.field(default_factory=list)
    chunk_days: int = 31          # export vouchers in chunks to avoid timeouts
    overlap_days: int = 7         # re-pull recent days to catch back-dated edits
    fy_start_month: int = 4       # Indian financial year starts April


def _read_toml(cfg_path: Path) -> dict:
    """
    Parse config.toml, tolerating what Windows text editors do to files.

    Notepad on Windows Server writes UTF-8 *with* a byte-order mark by default,
    and can also be talked into UTF-16. Python's TOML parser rejects both with
    "Invalid statement (at line 1, column 1)", which gives no hint that the
    problem is three invisible bytes rather than anything the user typed. The
    encoding is detected and stripped here so an ordinary edit-and-save works.
    """
    raw = cfg_path.read_bytes()

    if raw.startswith(b"\xef\xbb\xbf"):          # UTF-8 BOM (Notepad default)
        text = raw[3:].decode("utf-8")
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):   # UTF-16, "Unicode" in Notepad
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")  # Notepad "ANSI"

    text = text.replace("\r\n", "\n").lstrip("﻿")

    try:
        return tomllib.loads(text)
    except Exception as exc:
        lines = text.splitlines()
        lineno = getattr(exc, "lineno", None)
        where = ""
        if isinstance(lineno, int) and 1 <= lineno <= len(lines):
            where = f"\n\n  Line {lineno}:  {lines[lineno - 1]!r}"
        elif lines:
            where = f"\n\n  First line:  {lines[0]!r}"
        sys.exit(
            f"Could not read {cfg_path.name}: {exc}{where}\n\n"
            "Every setting must look like  key = \"value\"  under a [section] header.\n"
            "Text values need double quotes. Compare against config.example.toml."
        )


def load_settings(path: Path | None = None) -> Settings:
    cfg_path = path or (HERE / "config.toml")
    data: dict = {}
    if cfg_path.exists():
        if tomllib is None:
            sys.exit(
                "config.toml found but no TOML parser available.\n"
                "Run: pip install tomli   (or use Python 3.11+)"
            )
        data = _read_toml(cfg_path)

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

    # Multi-company: `companies = [...]` is preferred. A single `company = "..."`
    # still works, and an empty list means "every company open in Tally".
    companies = t.get("companies") or []
    if isinstance(companies, str):
        companies = [companies]
    if not companies and tally.company:
        companies = [tally.company]

    missing = [
        n for n, v in (
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

    # The control panel is not the site. Pointing at it returns the Frappe
    # Cloud dashboard's HTML for every request, which is confusing to debug.
    host = frappe.url.lower()
    for wrong in ("frappecloud.com", "cloud.frappe.io", "frappe.io"):
        if wrong in host:
            sys.exit(
                f"frappe.url is set to {frappe.url!r}, which is the Frappe Cloud\n"
                "control panel, not your site. Use your own site address, the one\n"
                "you log in to, e.g.:\n"
                '    url = "https://yoursite.frappe.cloud"'
            )
    if not host.startswith(("http://", "https://")):
        sys.exit(f"frappe.url must start with https:// — got {frappe.url!r}")

    return Settings(
        tally=tally,
        frappe=frappe,
        companies=companies,
        chunk_days=int(s.get("chunk_days", 31)),
        overlap_days=int(s.get("overlap_days", 7)),
        fy_start_month=int(s.get("fy_start_month", 4)),
    )


def resolve_companies(st: Settings) -> list:
    """
    Decide which companies to sync.

    An empty `companies` list means "whatever is open in Tally right now",
    which is the friendliest default on a server where companies come and go.
    Configured names are checked against what Tally actually has open so a
    typo surfaces as a warning instead of silently syncing nothing.
    """
    try:
        open_now = [c["name"] for c in list_companies(st.tally)]
    except TallyError:
        open_now = []

    if not st.companies:
        if not open_now:
            sys.exit("No companies configured and none open in Tally. Open a company, or list them in config.toml.")
        log.info("No companies configured — syncing all %d open in Tally.", len(open_now))
        return open_now

    resolved, missing = [], []
    for want in st.companies:
        if want in open_now or not open_now:
            resolved.append(want)
        else:
            missing.append(want)
    for m in missing:
        log.warning("Company %r is configured but not open in Tally — skipping.", m)
    if not resolved:
        sys.exit(
            "None of the configured companies are open in Tally.\n"
            "Open in Tally: " + (", ".join(open_now) or "(none)")
        )
    return resolved


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

def _report_rejects(msg, kind: str) -> int:
    """
    Log any rows Frappe refused, and return how many.

    Frappe validates on the way in, and a real chart of accounts contains
    values it dislikes — an email field holding a bare domain, for instance.
    Those rows are skipped rather than failing the batch, but they must be
    visible: a silently short mirror is worse than a noisy one.
    """
    if not isinstance(msg, dict):
        return 0
    errs = msg.get("errors") or []
    if not errs:
        return 0
    log.warning("%d %s(s) rejected by Frappe:", len(errs), kind)
    for e in errs[:10]:
        label = e.get("ledger") or e.get("voucher") or "?"
        log.warning("    %-45s %s", label[:45], e.get("error", "")[:120])
    if len(errs) > 10:
        log.warning("    ... and %d more", len(errs) - 10)
    return int(msg.get("failed") or len(errs))


def sync_ledgers(st: Settings, fc: FrappeClient) -> int:
    log.info("Syncing ledger masters...")

    # Groups first: real charts of accounts nest customers under sub-groups
    # (e.g. "AGENT RK" under "Sundry Debtors"), and classifying by immediate
    # parent alone would miss them.
    groups = fetch_groups(st.tally)
    by_name = {g.name: g for g in groups}

    ledgers = fetch_ledgers(st.tally)
    if not ledgers:
        log.warning("No ledgers returned — check the company name in config.")
        return 0

    for l in ledgers:
        chain = resolve_group_chain(l.parent, by_name) if l.parent else []
        l.group_path = " > ".join(reversed(chain)) if chain else l.parent
        l.primary_group = classify_group(chain) if chain else l.parent

    resolved = sum(1 for l in ledgers if l.primary_group != l.parent)
    log.info("Resolved %d/%d ledgers to a reserved group above their own",
             resolved, len(ledgers))
    for grp in ("Sundry Debtors", "Sundry Creditors"):
        n = sum(1 for l in ledgers if l.primary_group == grp)
        direct = sum(1 for l in ledgers if l.parent == grp)
        if n > direct:
            log.info("  %s: %d ledgers (%d directly, %d via sub-groups)",
                     grp, n, direct, n - direct)

    payload = [dataclasses.asdict(l) for l in ledgers]
    # Push in batches so a huge chart of accounts doesn't blow the request size.
    pushed = 0
    rejected = 0
    for i in range(0, len(payload), 500):
        batch = payload[i:i + 500]
        res = fc.upsert_ledgers(batch) or {}
        msg = res.get("message", res) if isinstance(res, dict) else {}
        rejected += _report_rejects(msg, "ledger")
        pushed += len(batch)
        log.info("  ledgers %d/%d", pushed, len(payload))
    if rejected:
        log.warning("%d ledger(s) were rejected by Frappe and NOT mirrored "
                    "(details above). The rest synced normally.", rejected)
    return pushed - rejected


def sync_vouchers(st: Settings, fc: FrappeClient, frm: date, to: date) -> int:
    log.info("Syncing vouchers %s .. %s", frm, to)
    total = 0
    for c_from, c_to in date_chunks(frm, to, st.chunk_days):
        vouchers = fetch_vouchers(st.tally, c_from, c_to)
        if not vouchers:
            log.info("  %s..%s: none", c_from, c_to)
            continue
        payload = [dataclasses.asdict(v) for v in vouchers]
        rejected = 0
        for i in range(0, len(payload), 200):
            res = fc.upsert_vouchers(payload[i:i + 200]) or {}
            msg = res.get("message", res) if isinstance(res, dict) else {}
            rejected += _report_rejects(msg, "voucher")
        total += len(payload) - rejected
        log.info("  %s..%s: %d vouchers%s", c_from, c_to, len(payload) - rejected,
                 f" ({rejected} rejected)" if rejected else "")
    return total


def resolve_range(st: Settings, fc: FrappeClient, args, company: str = "") -> tuple[date, date]:
    today = date.today()
    if args.frm and args.to:
        return (
            datetime.strptime(args.frm, "%Y-%m-%d").date(),
            datetime.strptime(args.to, "%Y-%m-%d").date(),
        )
    if args.full:
        return fy_start(today, st.fy_start_month), today

    # Incremental: resume from the last synced voucher date, minus an overlap
    # window so back-dated or edited vouchers get picked up again. Each company
    # tracks its own high-water mark.
    state = {}
    try:
        state = fc.get_sync_state(company)
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
            names = [c["name"] for c in companies]
            # An empty `companies` list in config means "sync everything open",
            # so report what will actually be synced rather than checking a
            # single configured name that may deliberately be unset.
            wanted = st.companies or names
            print(f"  Open in Tally ({len(names)}):")
            for n in names:
                print(f"    - {n}{'  <-- will sync' if n in wanted else ''}")

            missing = [w for w in wanted if w not in names]
            if missing:
                print()
                print(f"  WARNING: {len(missing)} configured compan"
                      f"{'y is' if len(missing) == 1 else 'ies are'} NOT open, so "
                      f"{'it' if len(missing) == 1 else 'they'} will be skipped:")
                for m in missing:
                    print(f"    - {m}")
                print("  Open them in Tally (K: Company > Open) to include them.")
            if not st.companies:
                print()
                print(f"  config.toml lists no companies, so all {len(names)} open "
                      "will be synced.")
                if len(names) < 2:
                    print("  Open the other financial years in Tally first if you "
                          "want them included.")
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


def build_parser() -> argparse.ArgumentParser:
    """Separate from main() so tests can check it against what main() reads."""
    p = argparse.ArgumentParser(description="Sync TallyPrime data into Frappe.")
    p.add_argument("--check", action="store_true", help="test connectivity and exit")
    p.add_argument("--full", action="store_true", help="sync from start of financial year")
    p.add_argument("--from", dest="frm", metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="to", metavar="YYYY-MM-DD")
    p.add_argument("--ledgers-only", action="store_true")
    p.add_argument("--vouchers-only", action="store_true")
    p.add_argument("--company", metavar="NAME", default=None,
                   help="sync only this company, ignoring config.toml. "
                        "The name must match Tally exactly.")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()

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

    companies = [args.company] if args.company else resolve_companies(st)
    log.info("Syncing %d compan%s: %s",
             len(companies), "y" if len(companies) == 1 else "ies", ", ".join(companies))

    totals = {"ledgers": 0, "vouchers": 0}
    failed: list = []

    for company in companies:
        # Each company is its own Tally export scope.
        st.tally.company = company
        c_started = datetime.now()
        counts = {"company": company, "ledgers": 0, "vouchers": 0}
        log.info("--- %s ---", company)
        try:
            if not args.vouchers_only:
                counts["ledgers"] = sync_ledgers(st, fc)
            if not args.ledgers_only:
                frm, to = resolve_range(st, fc, args, company)
                counts["vouchers"] = sync_vouchers(st, fc, frm, to)
                counts["range"] = f"{frm}..{to}"
        except (TallyError, FrappeError) as exc:
            # One bad company must not abort the rest.
            log.error("Sync failed for %s: %s", company, exc)
            counts["error"] = str(exc)
            counts["seconds"] = round((datetime.now() - c_started).total_seconds(), 1)
            fc.log_sync("Failed", counts)
            failed.append(company)
            continue

        counts["seconds"] = round((datetime.now() - c_started).total_seconds(), 1)
        totals["ledgers"] += counts["ledgers"]
        totals["vouchers"] += counts["vouchers"]
        log.info("%s: %d ledgers, %d vouchers in %.1fs",
                 company, counts["ledgers"], counts["vouchers"], counts["seconds"])
        fc.log_sync("Success", counts)

    elapsed = round((datetime.now() - started).total_seconds(), 1)
    log.info("All done in %.1fs — %d ledgers, %d vouchers across %d compan%s%s",
             elapsed, totals["ledgers"], totals["vouchers"], len(companies),
             "y" if len(companies) == 1 else "ies",
             f" ({len(failed)} failed: {', '.join(failed)})" if failed else "")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
