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


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


# ---------------------------------------------------------------------------
# Public API — masters
# ---------------------------------------------------------------------------

@dataclass
class Ledger:
    name: str
    company: str = ""                # which Tally company this belongs to
    parent: str = ""                 # ledger group
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
    <SVCURRENTCOMPANY>{cfg.company}</SVCURRENTCOMPANY>
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
    """Tally amounts look like '-1,234.50' or '1234.50 Dr'. Normalise."""
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


def fetch_ledgers(cfg: TallyConfig) -> list[Ledger]:
    """Export every ledger master with balances and party details."""
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
                opening_balance=_to_float(_text(el.find("OPENINGBALANCE"))),
                closing_balance=_to_float(_text(el.find("CLOSINGBALANCE"))),
                gstin=_text(el.find("PARTYGSTIN")),
                email=_text(el.find("EMAIL")),
                phone=_text(el.find("LEDGERPHONE")),
                bill_by_bill=_text(el.find("ISBILLWISEON")).lower() == "yes",
                guid=_text(el.find("GUID")),
                master_id=_text(el.find("MASTERID")),
                alter_id=_text(el.find("ALTERID")),
            )
        )
    log.info("Fetched %d ledgers", len(ledgers))
    return ledgers


# ---------------------------------------------------------------------------
# Public API — vouchers (transactions)
# ---------------------------------------------------------------------------

@dataclass
class VoucherEntry:
    ledger: str
    amount: float                    # +ve = debit, -ve = credit (Tally sign)
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
    <SVCURRENTCOMPANY>{cfg.company}</SVCURRENTCOMPANY>
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
            amt = _to_float(_text(le.find("AMOUNT")))
            is_debit = _text(le.find("ISDEEMEDPOSITIVE")).lower() == "yes"
            if not ledger:
                continue
            v.entries.append(VoucherEntry(ledger=ledger, amount=amt, is_debit=is_debit))
            if amt > 0:
                total_debit += amt
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
