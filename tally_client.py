"""
tally_client.py
----------------
Read-only client for TallyPrime's HTTP-XML gateway.

TallyPrime exposes an XML request/response server on the machine it runs on
(default port 9000). Enable it in TallyPrime:

    F1 (Help) > Settings > Connectivity > Client/Server configuration
        TallyPrime acts as  : Server
        Port                : 9000

This module ONLY reads from Tally (Export requests). It never sends Import
requests, so it can never modify your books. Tally stays the master.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import requests

log = logging.getLogger("tally")


class TallyError(RuntimeError):
    """Raised when Tally is unreachable or returns an error envelope."""


@dataclass
class TallyConfig:
    host: str = "localhost"
    port: int = 9000
    company: str = ""          # exact company name as shown in Tally
    timeout: int = 120         # seconds; large exports can be slow

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# Low-level request plumbing
# ---------------------------------------------------------------------------

def _post(cfg: TallyConfig, xml_body: str) -> str:
    """Send a raw XML envelope to Tally and return the raw XML response."""
    try:
        resp = requests.post(
            cfg.url,
            data=xml_body.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8"},
            timeout=cfg.timeout,
        )
    except requests.RequestException as exc:
        raise TallyError(
            f"Could not reach Tally at {cfg.url}. Is TallyPrime running with "
            f"'act as Server' enabled on port {cfg.port}? ({exc})"
        ) from exc

    if resp.status_code != 200:
        raise TallyError(f"Tally returned HTTP {resp.status_code}: {resp.text[:300]}")

    text = resp.text
    # Tally returns errors inside the body, not as HTTP codes.
    if "<LINEERROR>" in text:
        # Surface the first line error to make debugging painless.
        start = text.find("<LINEERROR>") + len("<LINEERROR>")
        end = text.find("</LINEERROR>", start)
        raise TallyError(f"Tally line error: {text[start:end].strip()}")
    return text


def _clean_xml(raw: str) -> str:
    """
    Tally emits invalid XML: raw '&' characters and control bytes that break
    strict parsers. Sanitise before handing to ElementTree.
    """
    raw = raw.replace("&#4;", "").replace("\x04", "")
    # Escape stray ampersands that are not part of a valid entity.
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "&":
            # Look ahead for a valid entity terminator ';' within 8 chars.
            semi = raw.find(";", i, i + 8)
            token = raw[i:semi] if semi != -1 else ""
            if token in ("&amp", "&lt", "&gt", "&quot", "&apos") or token.startswith("&#"):
                out.append(ch)
            else:
                out.append("&amp;")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _fmt_date(d: date) -> str:
    """Tally wants dates as YYYYMMDD."""
    return d.strftime("%Y%m%d")


def _xml_escape(s: str) -> str:
    """
    Escape a value being interpolated INTO a request.

    Company names really do contain ampersands ("S N Jain & Sons"), which would
    otherwise produce a malformed request that Tally answers unhelpfully.
    """
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def _company_tag(cfg: TallyConfig) -> str:
    """
    Render <SVCURRENTCOMPANY>, refusing to send an empty one.

    An empty tag does not mean "the current company" — it silently resolves to
    whatever is loaded, and a Collection request will happily return that
    company's rows with STATUS=1 and no error. Since the agent labels rows from
    config, that would file one company's ledgers under another company's name.
    Failing loudly here is the only safe option.
    """
    if not cfg.company or not cfg.company.strip():
        raise TallyError(
            "No company specified. Refusing to query Tally without one, because "
            "Tally would silently return whichever company happens to be loaded."
        )
    return f"<SVCURRENTCOMPANY>{_xml_escape(cfg.company)}</SVCURRENTCOMPANY>"


def assert_company_loaded(cfg: TallyConfig) -> None:
    """
    Verify the requested company is actually open in Tally before reading it.

    This is the guard against silent mis-binding: TallyPrime's Collection
    requests do NOT fail when the named company is closed. They return the
    loaded company's rows, or zero rows, with a success status either way.
    Neither is distinguishable from a legitimate result, so the load state has
    to be checked explicitly.
    """
    open_names = [c["name"] for c in list_companies(cfg)]
    if cfg.company in open_names:
        return
    raise TallyError(
        f"Company {cfg.company!r} is not open in Tally, so its data cannot be "
        f"read safely.\nOpen in Tally right now: "
        f"{', '.join(open_names) if open_names else '(none)'}\n"
        "Open it in Tally (K: Company > Open) and run again."
    )


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


# ---------------------------------------------------------------------------
# Public API — masters
# ---------------------------------------------------------------------------

@dataclass
class Group:
    """An account group. Groups nest, so `parent` may itself be a group."""
    name: str
    parent: str = ""
    primary_group: str = ""          # Tally's own root-group hint, when given


@dataclass
class Ledger:
    name: str
    company: str = ""                # which Tally company this belongs to
    parent: str = ""                 # immediate group, e.g. "AGENT RK"
    primary_group: str = ""          # resolved root, e.g. "Sundry Debtors"
    group_path: str = ""             # full chain, for auditing the resolution
    opening_balance: float = 0.0
    closing_balance: float = 0.0
    gstin: str = ""
    email: str = ""
    phone: str = ""
    bill_by_bill: bool = False       # maintains bill-wise details (outstanding)
    guid: str = ""
    master_id: str = ""              # Tally internal id (stable per company)
    alter_id: str = ""               # bumps on every edit -> incremental sync


def _ledgers_request(cfg: TallyConfig) -> str:
    return f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>TB_Ledgers</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    {_company_tag(cfg)}
   </STATICVARIABLES>
   <TDL><TDLMESSAGE>
    <COLLECTION NAME="TB_Ledgers" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
     <TYPE>Ledger</TYPE>
     <NATIVEMETHOD>Parent</NATIVEMETHOD>
     <NATIVEMETHOD>OpeningBalance</NATIVEMETHOD>
     <NATIVEMETHOD>ClosingBalance</NATIVEMETHOD>
     <NATIVEMETHOD>PartyGSTIN</NATIVEMETHOD>
     <NATIVEMETHOD>Email</NATIVEMETHOD>
     <NATIVEMETHOD>LedgerPhone</NATIVEMETHOD>
     <NATIVEMETHOD>IsBillWiseOn</NATIVEMETHOD>
     <NATIVEMETHOD>GUID</NATIVEMETHOD>
     <NATIVEMETHOD>MasterID</NATIVEMETHOD>
     <NATIVEMETHOD>AlterID</NATIVEMETHOD>
    </COLLECTION>
   </TDLMESSAGE></TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""


def _to_float(s: str) -> float:
    """
    Parse a raw Tally amount exactly as exported, e.g. '-1,234.50' or '1234.50 Dr'.

    This is the RAW value. Tally's XML uses the opposite sign convention to
    normal accounting: a Debit balance exports NEGATIVE and a Credit balance
    exports POSITIVE. Verified against a live book, where Sundry Debtors
    (Debit per Tally's own Group Summary) exported negative and Sundry
    Creditors exported positive. Use `_to_debit_positive` for anything that
    downstream code will reason about.
    """
    if not s:
        return 0.0
    s = s.replace(",", "").strip()
    sign = 1.0
    low = s.lower()
    if low.endswith("dr"):
        s = s[:-2].strip()
    elif low.endswith("cr"):
        s = s[:-2].strip()
        sign = -1.0
    try:
        return float(s) * sign
    except ValueError:
        return 0.0


def _to_debit_positive(s: str) -> float:
    """
    Normalise a Tally balance to the standard convention: positive = Debit.

    Tally exports Debit as negative, so this flips the sign. Everything stored
    in Frappe and everything Claude sees uses debit-positive, which is what an
    accountant expects: a customer who owes you money shows positive.
    """
    return -_to_float(s)


def _groups_request(cfg: TallyConfig) -> str:
    return f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>TB_Groups</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    {_company_tag(cfg)}
   </STATICVARIABLES>
   <TDL><TDLMESSAGE>
    <COLLECTION NAME="TB_Groups" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
     <TYPE>Group</TYPE>
     <NATIVEMETHOD>Parent</NATIVEMETHOD>
     <NATIVEMETHOD>_PrimaryGroup</NATIVEMETHOD>
     <NATIVEMETHOD>IsRevenue</NATIVEMETHOD>
     <NATIVEMETHOD>IsDeemedPositive</NATIVEMETHOD>
    </COLLECTION>
   </TDLMESSAGE></TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""


def fetch_groups(cfg: TallyConfig) -> list[Group]:
    """
    Export the account-group tree.

    Needed because real charts of accounts nest: this book files customers
    under groups like "AGENT RK" and "Sundry Debtors Online", which are
    themselves children of "Sundry Debtors". Classifying a ledger by its
    immediate parent alone misses them — in this book that was 92% of
    receivables.
    """
    assert_company_loaded(cfg)
    raw = _post(cfg, _groups_request(cfg))
    root = ET.fromstring(_clean_xml(raw))
    groups: list[Group] = []
    for el in root.iter("GROUP"):
        name = el.get("NAME") or _text(el.find("NAME"))
        if not name:
            continue
        groups.append(
            Group(
                name=name,
                parent=_text(el.find("PARENT")),
                primary_group=_text(el.find("_PRIMARYGROUP")),
            )
        )
    log.info("Fetched %d groups", len(groups))
    return groups


# TallyPrime's 28 reserved groups. Every custom group ultimately descends from
# one of these, so the nearest reserved ancestor is the level at which a ledger
# is meaningfully classified — "Sundry Debtors" rather than "AGENT RK" below it
# or "Primary" above it.
TALLY_RESERVED_GROUPS = frozenset({
    "Branch / Divisions", "Capital Account", "Current Assets", "Current Liabilities",
    "Direct Expenses", "Direct Incomes", "Fixed Assets", "Indirect Expenses",
    "Indirect Incomes", "Investments", "Loans (Liability)", "Misc. Expenses (ASSET)",
    "Purchase Accounts", "Sales Accounts", "Suspense A/c", "Bank Accounts",
    "Bank OD A/c", "Cash-in-Hand", "Deposits (Asset)", "Duties & Taxes",
    "Loans & Advances (Asset)", "Provisions", "Reserves & Surplus", "Secured Loans",
    "Stock-in-Hand", "Sundry Creditors", "Sundry Debtors", "Unsecured Loans",
})


def classify_group(chain: list[str]) -> str:
    """
    Pick the level at which a ledger should be reported, given its group chain.

    Returns the nearest reserved-group ancestor. For a customer filed under
    "AGENT RK", the chain is [AGENT RK, Sundry Debtors, Current Assets, Primary]
    and this returns "Sundry Debtors" — specific enough to be useful, general
    enough to aggregate. Falls back to the outermost group when nothing in the
    chain is a reserved name, which happens only with a non-standard chart.
    """
    for name in chain:
        if name in TALLY_RESERVED_GROUPS:
            return name
    return chain[-1] if chain else ""


def resolve_group_chain(group_name: str, by_name: dict) -> list[str]:
    """
    Walk from a group up to its root, returning the chain nearest-first.

    Tally guarantees no cycles, but a corrupt or partial export could still
    loop, so this is defensive: it stops on a repeat and caps the depth.
    """
    chain: list[str] = []
    seen = set()
    cur = group_name
    depth = 0
    while cur and cur not in seen and depth < 32:
        chain.append(cur)
        seen.add(cur)
        g = by_name.get(cur)
        if not g:
            break
        # Tally's own hint short-circuits the walk when it is present.
        if g.primary_group and g.primary_group not in seen:
            chain.append(g.primary_group)
            break
        cur = g.parent
        depth += 1
    return chain


def fetch_ledgers(cfg: TallyConfig) -> list[Ledger]:
    """Export every ledger master with balances and party details."""
    assert_company_loaded(cfg)
    raw = _post(cfg, _ledgers_request(cfg))
    root = ET.fromstring(_clean_xml(raw))
    ledgers: list[Ledger] = []
    for el in root.iter("LEDGER"):
        name = el.get("NAME") or _text(el.find("NAME"))
        if not name:
            continue
        ledgers.append(
            Ledger(
                name=name,
                company=cfg.company,
                parent=_text(el.find("PARENT")),
                # Normalised to debit-positive; Tally exports the opposite.
                opening_balance=_to_debit_positive(_text(el.find("OPENINGBALANCE"))),
                closing_balance=_to_debit_positive(_text(el.find("CLOSINGBALANCE"))),
                gstin=_text(el.find("PARTYGSTIN")),
                email=_text(el.find("EMAIL")),
                phone=_text(el.find("LEDGERPHONE")),
                bill_by_bill=_text(el.find("ISBILLWISEON")).lower() == "yes",
                guid=_text(el.find("GUID")),
                master_id=_text(el.find("MASTERID")),
                alter_id=_text(el.find("ALTERID")),
            )
        )
    if not ledgers:
        # Zero rows is indistinguishable from a mis-bound request, so treat it
        # as an error rather than mirroring an empty company over a real one.
        raise TallyError(
            f"Tally returned no ledgers for {cfg.company!r}. The company is "
            "open but produced nothing, which usually means the logged-in "
            "Tally user lacks permission to display Accounts Masters."
        )
    log.info("Fetched %d ledgers for %s", len(ledgers), cfg.company)
    return ledgers


# ---------------------------------------------------------------------------
# Public API — vouchers (transactions)
# ---------------------------------------------------------------------------

@dataclass
class VoucherEntry:
    ledger: str
    amount: float                    # normalised: +ve = Debit, -ve = Credit
    is_debit: bool = False


@dataclass
class Voucher:
    guid: str
    company: str
    voucher_type: str
    voucher_number: str
    date: str                        # ISO YYYY-MM-DD
    party: str = ""
    narration: str = ""
    amount: float = 0.0              # absolute voucher value
    is_cancelled: bool = False
    alter_id: str = ""
    entries: list[VoucherEntry] = field(default_factory=list)


def _daybook_request(cfg: TallyConfig, frm: date, to: date) -> str:
    return f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Data</TYPE>
  <ID>Day Book</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <SVFROMDATE>{_fmt_date(frm)}</SVFROMDATE>
    <SVTODATE>{_fmt_date(to)}</SVTODATE>
    {_company_tag(cfg)}
   </STATICVARIABLES>
   <TDL><TDLMESSAGE>
    <REPORT NAME="Day Book" ISMODIFY="No"><FORMS>Day Book</FORMS></REPORT>
   </TDLMESSAGE></TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""


def _tally_date_to_iso(s: str) -> str:
    """Tally voucher dates come as YYYYMMDD."""
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s


def fetch_vouchers(cfg: TallyConfig, frm: date, to: date) -> list[Voucher]:
    """
    Export all vouchers in [frm, to] via the Day Book report.
    Chunk your date ranges (e.g. one month at a time) for large books.
    """
    assert_company_loaded(cfg)
    raw = _post(cfg, _daybook_request(cfg, frm, to))
    root = ET.fromstring(_clean_xml(raw))
    vouchers: list[Voucher] = []

    for vel in root.iter("VOUCHER"):
        guid = _text(vel.find("GUID"))
        if not guid:
            continue
        v = Voucher(
            guid=guid,
            company=cfg.company,
            voucher_type=_text(vel.find("VOUCHERTYPENAME")),
            voucher_number=_text(vel.find("VOUCHERNUMBER")),
            date=_tally_date_to_iso(_text(vel.find("DATE"))),
            party=_text(vel.find("PARTYLEDGERNAME")),
            narration=_text(vel.find("NARRATION")),
            is_cancelled=_text(vel.find("ISCANCELLED")).lower() == "yes",
            alter_id=_text(vel.find("ALTERID")),
        )
        total_debit = 0.0
        for le in vel.iter("ALLLEDGERENTRIES.LIST"):
            ledger = _text(le.find("LEDGERNAME"))
            if not ledger:
                continue
            # ISDEEMEDPOSITIVE is Tally's own debit flag. Take direction from
            # it and magnitude from |AMOUNT|, so the export's sign convention
            # cannot invert anything downstream.
            is_debit = _text(le.find("ISDEEMEDPOSITIVE")).lower() == "yes"
            magnitude = abs(_to_float(_text(le.find("AMOUNT"))))
            amt = magnitude if is_debit else -magnitude
            v.entries.append(VoucherEntry(ledger=ledger, amount=amt, is_debit=is_debit))
            if is_debit:
                total_debit += magnitude
        v.amount = round(total_debit, 2)
        vouchers.append(v)

    log.info("Fetched %d vouchers for %s..%s", len(vouchers), frm, to)
    return vouchers


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------

def list_companies(cfg: TallyConfig) -> list[dict[str, Any]]:
    """Ping Tally and list open companies — used by `sync.py --check`."""
    req = """<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>TB_Companies</ID></HEADER>
 <BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="TB_Companies" ISMODIFY="No"><TYPE>Company</TYPE>
    <NATIVEMETHOD>Name</NATIVEMETHOD><NATIVEMETHOD>StartingFrom</NATIVEMETHOD></COLLECTION>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""
    raw = _post(cfg, req)
    root = ET.fromstring(_clean_xml(raw))
    companies = []
    for el in root.iter("COMPANY"):
        name = el.get("NAME") or _text(el.find("NAME"))
        if name:
            companies.append({"name": name, "starting_from": _text(el.find("STARTINGFROM"))})
    return companies
