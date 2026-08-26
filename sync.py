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

import distributor_fetch
from frappe_client import FrappeClient, FrappeConfig, FrappeError
from tally_client import (
    TallyConfig,
    TallyError,
    TallyUnreachable,
    _parse_credit_days,
    _tally_date_to_iso,
    fetch_bills,
    fetch_godowns,
    fetch_groups,
    fetch_stock_groups,
    fetch_stock_items,
    fetch_units,
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
    chunk_days: int = 7           # export vouchers in chunks to avoid timeouts
    overlap_days: int = 7         # re-pull recent days to catch back-dated edits
    fy_start_month: int = 4       # Indian financial year starts April
    company_starts: dict = dataclasses.field(default_factory=dict)
    # Voucher type names treated as delivery notes. Empty by default is
    # CORRECT for this book: it has no such voucher type (measured
    # 2026-08-15) — goods reach a distributor as a Sales invoice. Set
    # [orders].delivery_types in config.toml if that ever changes.
    delivery_types: list = dataclasses.field(default_factory=list)


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
        timeout=int(f.get("timeout", 180)),
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

    o = data.get("orders", {})
    delivery_types = o.get("delivery_types") or []
    if isinstance(delivery_types, str):
        delivery_types = [delivery_types]

    return Settings(
        tally=tally,
        frappe=frappe,
        companies=companies,
        chunk_days=int(s.get("chunk_days", 7)),
        overlap_days=int(s.get("overlap_days", 7)),
        fy_start_month=int(s.get("fy_start_month", 4)),
        delivery_types=[str(t) for t in delivery_types],
    )


def learn_company_starts(st: Settings) -> list:
    """
    Record where each open company file's books begin; return what is open.

    Split out of resolve_companies because it is a side effect that EVERY code
    path needs, including `--company`. Without it st.company_starts is empty
    and resolve_range floors the window at the CURRENT financial year — so the
    documented `--company "...(22-23)" --full` would ask a 2022-23 file for
    2026 dates, mirror nothing, and log the run as Success.
    """
    try:
        infos = list_companies(st.tally)
    except TallyError as exc:
        # Never silent. Tally refusing connections while it digests a big
        # export is routine here, and swallowing it left company_starts empty
        # — which silently restores the very wrong-window bug this function
        # exists to prevent.
        log.warning("Could not list companies (%s) — each file's own start "
                    "date is unknown for this run.", exc)
        infos = []
    # Each company file covers its own period (books here are one file per
    # financial year), so remember where each begins.
    starts = {}
    for c in infos:
        raw_start = (c.get("starting_from") or "").strip()
        if not raw_start:
            log.warning("Tally reported no start date for %r.", c["name"])
            continue
        try:
            starts[c["name"]] = datetime.strptime(
                _tally_date_to_iso(raw_start), "%Y-%m-%d").date()
        except ValueError:
            log.warning("Could not read the start date %r for %r.",
                        raw_start, c["name"])
    st.company_starts = starts
    return [c["name"] for c in infos]


def resolve_companies(st: Settings) -> list:
    """
    Decide which companies to sync.

    An empty `companies` list means "whatever is open in Tally right now",
    which is the friendliest default on a server where companies come and go.
    Configured names are checked against what Tally actually has open so a
    typo surfaces as a warning instead of silently syncing nothing.
    """
    open_now = learn_company_starts(st)

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

def sync_inventory(st: Settings, fc: FrappeClient) -> int:
    """
    Mirror inventory masters: units, godowns, stock groups, then items.

    Units are fetched FIRST and passed into the item parse, because compound
    quantities ("3 Dzn 6 Pcs") cannot be resolved without the conversion
    table — and an unresolved quantity in a hosiery book is not a rounding
    error, it is off by a factor of twelve.
    """
    log.info("Syncing inventory masters...")
    try:
        units = fetch_units(st.tally)
        godowns = fetch_godowns(st.tally)
        groups = fetch_stock_groups(st.tally)
    except TallyError as exc:
        log.warning("Skipping inventory for %s: %s", st.tally.company, exc)
        return 0

    unit_map = {u.name: u for u in units}
    try:
        items = fetch_stock_items(st.tally, date.today(), unit_map)
    except TallyError as exc:
        log.warning("Could not read stock items for %s: %s", st.tally.company, exc)
        items = []

    if not items and not units:
        log.info("  no inventory in this company — likely an accounts-only book.")
        return 0

    # Resolve each item's stock group to its root, the same way ledgers are
    # resolved, so reporting groups by product family rather than sub-group.
    by_name = {g.name: g for g in groups}
    for g in groups:
        chain = resolve_group_chain(g.parent, by_name) if g.parent else []
        g.primary_group = chain[-1] if chain else (g.parent or g.name)
    root_of = {g.name: (g.primary_group or g.name) for g in groups}
    for it in items:
        it.primary_group = root_of.get(it.parent, it.parent)

    res = fc.upsert_inventory(
        st.tally.company,
        units=[dataclasses.asdict(u) for u in units],
        godowns=[dataclasses.asdict(g) for g in godowns],
        stock_groups=[dataclasses.asdict(g) for g in groups],
        stock_items=[dataclasses.asdict(i) for i in items],
    ) or {}
    msg = res.get("message", res) if isinstance(res, dict) else {}
    for kind in ("units", "godowns", "stock_groups", "stock_items"):
        _report_rejects(msg.get(kind) or {}, kind[:-1])

    compound = sum(1 for i in items if i.closing_qty_raw.count(" ") > 1)
    no_hsn = sum(1 for i in items if not i.hsn_code and i.closing_value)
    log.info("  %d units, %d godowns, %d groups, %d items",
             len(units), len(godowns), len(groups), len(items))
    if compound:
        log.info("  %d items use compound units — resolved via the unit table.", compound)
    if no_hsn:
        log.warning("  %d items hold stock but have NO HSN code (GST exposure).", no_hsn)
    return len(items)


# Signature of the last company's bill fetch, to catch a request that ignores
# the company scope. TallyPrime's Bills collection has been seen answering
# from the loaded company regardless of SVCURRENTCOMPANY: two different
# company files returned an identical 51 bills / 41 overdue, seconds apart.
_last_bill_fetch: tuple = ()

# Bills go to Frappe in batches, like vouchers. The live book holds ~4,600 open
# bills; one POST of that size is several megabytes inside a single database
# transaction. 500 keeps each request small without making the run chatty.
_BILL_BATCH = 500


def sync_bills(st: Settings, fc: FrappeClient) -> int:
    """
    Mirror outstanding bills for the current company.

    Tally returns only bills that are still unpaid, so this is a snapshot: the
    Frappe side clears the company's previous rows before inserting, or paid
    invoices would linger and overstate what is owed.
    """
    global _last_bill_fetch
    log.info("Syncing outstanding bills...")
    try:
        bills = fetch_bills(st.tally, date.today())
    except TallyError as exc:
        # Bill-wise details may simply be switched off for this company. That
        # is a configuration choice, not a sync failure — carry on.
        log.warning("Skipping bills for %s: %s", st.tally.company, exc)
        return 0
    if not bills:
        log.info("  no outstanding bills reported.")
        return 0

    # Two company files cannot legitimately hold the same bills for the same
    # parties. If they do, the export is not honouring the company scope and
    # the rows would be filed under the wrong company — worse than no rows.
    signature = (len(bills), round(sum(b.closing for b in bills), 2),
                 frozenset(b.party for b in bills))
    if signature == _last_bill_fetch:
        log.error(
            "  REFUSING to write: %s returned bills identical to the previous "
            "company (%d bills, same parties). Tally is answering from the "
            "loaded company, not the requested one. Sync each company file in "
            "its own run, or with only that company open.",
            st.tally.company, len(bills),
        )
        return 0
    _last_bill_fetch = signature

    payload = [dataclasses.asdict(b) for b in bills]

    # Chunked like vouchers. This path only started returning rows on
    # 2026-08-15 — the parser had been iterating the wrong tag name, so every
    # run posted an empty list and the write side was never once exercised at
    # size. The live book yields ~4,600 open bills, and putting them through
    # one request means a multi-megabyte POST and a single long transaction on
    # Frappe Cloud.
    #
    # `replace` clears the company's snapshot and therefore belongs to the
    # FIRST batch alone; sent every time, each batch would wipe the one before
    # it and only the tail would survive.
    created = rejected = 0
    for i in range(0, len(payload), _BILL_BATCH):
        res = fc.upsert_bills(payload[i:i + _BILL_BATCH], st.tally.company,
                              replace=1 if i == 0 else 0) or {}
        msg = res.get("message", res) if isinstance(res, dict) else {}
        rejected += _report_rejects(msg, "bill")
        if isinstance(msg, dict):
            created += int(msg.get("created") or 0)
    # `created` is summed from what Frappe actually stored across the batches,
    # not from what was sent: it is the only figure that proves a row exists,
    # and len(payload) says nothing.
    overdue = sum(1 for b in bills if b.overdue_days > 0)
    log.info("  %d of %d bills stored (%d overdue), %s",
             created, len(payload), overdue,
             f"{rejected} rejected" if rejected else "none rejected")
    if created < len(payload) - rejected:
        log.error("  %d bills went missing without an error — check the "
                  "Frappe log for a rolled-back transaction.",
                  len(payload) - rejected - created)
    return created


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
        label = e.get("ledger") or e.get("voucher") or e.get("bill") or "?"
        log.warning("    %-45s %s", label[:45], e.get("error", "")[:120])
    if len(errs) > 10:
        log.warning("    ... and %d more", len(errs) - 10)
    return int(msg.get("failed") or len(errs))


def sync_ledgers(st: Settings, fc: FrappeClient) -> int:
    log.info("Syncing ledger masters...")

    # Groups first: real charts of accounts nest customers under sub-groups
    # (e.g. "AGENT XY" under "Sundry Debtors"), and classifying by immediate
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

    # Distributor-facing fields: credit limit/period, address, mobile, price
    # level. One extra request per company; merged into the same payload so
    # there is exactly one ledger pipeline, not two that can disagree.
    try:
        extras = distributor_fetch.fetch_ledger_extras(
            st.tally, date.today(),
            st.company_starts.get(st.tally.company) or date.today())
    except TallyError as exc:
        log.warning("Ledger distributor fields skipped for this run: %s", exc)
        extras = {}

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
    for row in payload:
        row.update(extras.get(row["name"], {}))
        # address_lines exists for the order importer (a voucher reproduces
        # the master's line breaks); the mirror keeps the flat `address`.
        row.pop("address_lines", None)
        # The agent (salesperson) is the immediate group under Sundry
        # Debtors in this book — "AGENT XY" and the like. No ledger UDF
        # carries it (inspected 2026-08-15: the export shows no UDF tags on
        # ledgers), so the group IS the answer; agent_source records that so
        # nothing downstream re-derives it.
        if (row.get("primary_group") == "Sundry Debtors"
                and row.get("parent")
                and row["parent"] != "Sundry Debtors"):
            row["agent"] = row["parent"]
            row["agent_source"] = "group"
        else:
            row["agent"] = ""
            row["agent_source"] = ""
        row["credit_days"] = _parse_credit_days(row.get("credit_period") or "")
    # Push in batches so a huge chart of accounts doesn't blow the request size.
    pushed = 0
    rejected = 0
    pruned = 0
    for i in range(0, len(payload), 500):
        batch = payload[i:i + 500]
        res = fc.upsert_ledgers(batch) or {}
        msg = res.get("message", res) if isinstance(res, dict) else {}
        rejected += _report_rejects(msg, "ledger")
        pruned += int(msg.get("pruned_duplicates") or 0)
        pushed += len(batch)
        log.info("  ledgers %d/%d", pushed, len(payload))
    if rejected:
        log.warning("%d ledger(s) were rejected by Frappe and NOT mirrored "
                    "(details above). The rest synced normally.", rejected)
    if pruned:
        # Deleting rows from the live mirror must never be silent. Frappe
        # prunes a duplicate ledger generation in place; without this line the
        # run would report plain success while thousands of rows disappeared.
        log.warning("Removed %d duplicate ledger row(s) left behind by an "
                    "earlier docname change — the mirror repaired itself. "
                    "Balances that read about double should now be correct.",
                    pruned)
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


def sync_distributor_docs(st: Settings, fc: FrappeClient,
                          frm: date, to: date) -> dict:
    """
    Mirror the distributor-facing documents for [frm, to]: sales orders,
    invoices, receipts, delivery notes (if the book has any), and the
    per-size stock balances harvested from those same payloads.

    Rides the voucher date window, so incremental runs stay incremental.
    Each document family is chunked like vouchers — this book's fortnight of
    Sales Orders alone is a 27 MB export.
    """
    counts = {"sales_orders": 0, "invoices": 0, "receipts": 0,
              "delivery_notes": 0, "size_balances": 0}
    harvest: list = []

    for c_from, c_to in date_chunks(frm, to, st.chunk_days):
        orders = distributor_fetch.fetch_sales_orders(st.tally, c_from, c_to)
        if orders:
            res = fc.upsert_sales_orders(orders) or {}
            msg = res.get("message", res) if isinstance(res, dict) else {}
            _report_rejects(msg, "sales order")
            counts["sales_orders"] += len(orders)
            harvest.extend(orders)

        invoices = distributor_fetch.fetch_invoices(st.tally, c_from, c_to)
        if invoices:
            res = fc.upsert_invoices(invoices) or {}
            msg = res.get("message", res) if isinstance(res, dict) else {}
            _report_rejects(msg, "invoice")
            counts["invoices"] += len(invoices)
            harvest.extend(invoices)

        receipts = distributor_fetch.fetch_receipts(st.tally, c_from, c_to)
        if receipts:
            res = fc.upsert_receipts(receipts) or {}
            msg = res.get("message", res) if isinstance(res, dict) else {}
            _report_rejects(msg, "receipt")
            counts["receipts"] += len(receipts)

        if st.delivery_types:
            notes = distributor_fetch.fetch_delivery_notes(
                st.tally, c_from, c_to, st.delivery_types)
            if notes:
                res = fc.upsert_delivery_notes(notes) or {}
                msg = res.get("message", res) if isinstance(res, dict) else {}
                _report_rejects(msg, "delivery note")
                counts["delivery_notes"] += len(notes)

    # Per-size stock, read out of the order-pad's own BlncQty UDF on the
    # vouchers just fetched. Zero extra Tally traffic; each figure carries
    # the voucher date it was true on.
    balances = distributor_fetch.harvest_size_balances(harvest)
    if balances:
        for b in balances:
            b["company"] = st.tally.company
        res = fc.upsert_stock_batches(balances, st.tally.company) or {}
        msg = res.get("message", res) if isinstance(res, dict) else {}
        _report_rejects(msg, "size balance")
        counts["size_balances"] = len(balances)

    log.info("Distributor docs %s..%s: %d orders, %d invoices, %d receipts, "
             "%d delivery notes, %d size balances",
             frm, to, counts["sales_orders"], counts["invoices"],
             counts["receipts"], counts["delivery_notes"],
             counts["size_balances"])
    return counts


def resolve_range(st: Settings, fc: FrappeClient, args, company: str = "") -> tuple[date, date]:
    today = date.today()
    if args.frm and args.to:
        return (
            datetime.strptime(args.frm, "%Y-%m-%d").date(),
            datetime.strptime(args.to, "%Y-%m-%d").date(),
        )

    # These books keep one company file per financial year, so each file's
    # range must be floored at ITS OWN start — flooring everything at the
    # current FY would leave every prior-year file permanently empty.
    book_start = st.company_starts.get(company)
    floor = book_start or fy_start(today, st.fy_start_month)

    # A one-year file's window must also be CAPPED at its year end. Without
    # the cap, --full on a 2022-23 file chunks week by week all the way to
    # today — hundreds of exports for dates its books cannot contain, against
    # a Tally that stops answering under load and is known to leak rows
    # across range boundaries on out-of-period requests.
    ceiling = today
    if book_start:
        book_end = (date(book_start.year + 1, book_start.month, book_start.day)
                    - timedelta(days=1))
        if book_end < today:
            ceiling = book_end

    if args.full:
        if book_start is None:
            # Guessing here is how a 2022-23 file gets asked for 2026 dates,
            # mirrors nothing, and still logs Success.
            sys.exit(
                f"Tally did not report a start date for {company!r}, so a "
                "--full window cannot be trusted. Re-run when Tally is "
                "responsive, or give the range explicitly with --from/--to."
            )
        return floor, ceiling

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
        # Defensive: the server may append a time component.
        last_date = datetime.strptime(str(last)[:10], "%Y-%m-%d").date()
        start = max(last_date - timedelta(days=st.overlap_days), floor)
        return start, ceiling
    return floor, ceiling


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
    p.add_argument("--no-bills", action="store_true",
                   help="skip the outstanding-bills snapshot")
    p.add_argument("--no-inventory", action="store_true",
                   help="skip inventory masters")
    p.add_argument("--no-distributor", action="store_true",
                   help="skip the distributor mirror (orders, invoices, "
                        "receipts, size balances)")
    p.add_argument("--company", metavar="NAME", default=None,
                   help="sync only this company, ignoring config.toml. "
                        "The name must match Tally exactly.")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main() -> int:
    # Windows consoles default to cp1252; a Hindi ledger name in any print()
    # would otherwise kill the run with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = build_parser().parse_args()

    if bool(args.frm) != bool(args.to):
        sys.exit("--from and --to must be given together, both as YYYY-MM-DD.")
    for label, val in (("--from", args.frm), ("--to", args.to)):
        if val:
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                sys.exit(f"{label} must be YYYY-MM-DD (e.g. 2025-04-01) — got {val!r}")

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

    if args.company:
        # Still learn the start dates: --company skips resolve_companies, and
        # that is the only place they were recorded. See learn_company_starts.
        open_now = learn_company_starts(st)
        if open_now and args.company not in open_now:
            # Record the failure in Frappe before exiting: an unattended run
            # that dies only to stderr leaves sync_health reporting the stale
            # Success forever. The names are printed !r so an invisible
            # whitespace difference from what the operator typed shows up.
            try:
                fc.log_sync("Failed", {
                    "company": args.company,
                    "error": "Not open in Tally. Open: "
                             + ", ".join(repr(c) for c in open_now),
                })
            except FrappeError:
                pass  # the exit message below still tells the operator
            sys.exit(
                f"{args.company!r} is not open in Tally, so it would sync "
                f"nothing and still report success.\n"
                "Open in Tally: " + ", ".join(repr(c) for c in open_now)
            )
        companies = [args.company]
    else:
        companies = resolve_companies(st)
    log.info("Syncing %d compan%s: %s",
             len(companies), "y" if len(companies) == 1 else "ies", ", ".join(companies))

    totals = {"ledgers": 0, "vouchers": 0, "bills": 0, "items": 0}
    failed: list = []
    skipped: list = []

    for company in companies:
        # Each company is its own Tally export scope.
        st.tally.company = company
        c_started = datetime.now()
        counts = {"company": company, "ledgers": 0, "vouchers": 0}
        log.info("--- %s ---", company)
        try:
            if not args.vouchers_only:
                counts["ledgers"] = sync_ledgers(st, fc)
                # Bills come after ledgers so party group and GSTIN are
                # already present to denormalise onto each bill.
                if not args.no_bills:
                    counts["bills"] = sync_bills(st, fc)
                if not args.no_inventory:
                    counts["items"] = sync_inventory(st, fc)
            if not args.ledgers_only:
                frm, to = resolve_range(st, fc, args, company)
                counts["vouchers"] = sync_vouchers(st, fc, frm, to)
                counts["range"] = f"{frm}..{to}"
                if not args.no_distributor:
                    counts["distributor"] = sync_distributor_docs(st, fc, frm, to)
        except TallyUnreachable as exc:
            # Tally is not running. On this hosted box that is the normal
            # state outside working hours, and it is not a fault: nothing was
            # attempted, nothing was half-written, and the next run will pick
            # the window up unchanged. Logging it as Failed put ~340 rows a
            # week into the failure list and hid the failures that mattered —
            # so it is recorded as Skipped, which sync_health counts apart.
            #
            # Still recorded, never swallowed: a mirror that stops advancing
            # because Tally has been closed for two days must be visible.
            log.warning("Tally not running, skipping %s: %s", company, exc)
            counts["error"] = str(exc)
            counts["seconds"] = round((datetime.now() - c_started).total_seconds(), 1)
            fc.log_sync("Skipped", counts)
            skipped.append(company)
            continue
        except (TallyError, FrappeError, ValueError) as exc:
            # One bad company must not abort the rest.
            log.error("Sync failed for %s: %s", company, exc)
            counts["error"] = str(exc)
            counts["seconds"] = round((datetime.now() - c_started).total_seconds(), 1)
            fc.log_sync("Failed", counts)
            failed.append(company)
            continue

        counts["seconds"] = round((datetime.now() - c_started).total_seconds(), 1)
        totals["ledgers"] += counts["ledgers"]
        totals["bills"] = totals.get("bills", 0) + counts.get("bills", 0)
        totals["items"] = totals.get("items", 0) + counts.get("items", 0)
        totals["vouchers"] += counts["vouchers"]
        log.info("%s: %d ledgers, %d vouchers in %.1fs",
                 company, counts["ledgers"], counts["vouchers"], counts["seconds"])
        fc.log_sync("Success", counts)

    elapsed = round((datetime.now() - started).total_seconds(), 1)
    log.info("All done in %.1fs — %d ledgers, %d bills, %d items, %d vouchers "
             "across %d compan%s%s",
             elapsed, totals["ledgers"], totals.get("bills", 0),
             totals.get("items", 0), totals["vouchers"], len(companies),
             "y" if len(companies) == 1 else "ies",
             (f" ({len(failed)} failed: {', '.join(failed)})" if failed else "")
             + (f" ({len(skipped)} skipped, Tally not running: "
                f"{', '.join(skipped)})" if skipped else ""))
    # Skipped is deliberately NOT a non-zero exit. The Windows task treats a
    # non-zero code as a failed run, and a closed Tally overnight is not a
    # failed run — making it one would turn the task's own history into the
    # same noise this change removes from the Frappe log.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
