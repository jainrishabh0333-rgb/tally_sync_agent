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
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
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

def _decode_response(content: bytes) -> str:
    """
    Decode Tally's response bytes without trusting HTTP headers.

    TallyPrime can answer in UTF-8 or UTF-16 and usually omits the charset
    from Content-Type. Left to its defaults, requests decodes text/* as
    Latin-1, which does not error — it just turns every multi-byte character
    into garbage. Order: BOM, XML declaration, UTF-8, then a lossy fallback.
    """
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16", "replace")
    if content.startswith(b"\xef\xbb\xbf"):
        return content[3:].decode("utf-8", "replace")
    m = re.search(rb'encoding=["\']([A-Za-z0-9_.\-]+)["\']', content[:200])
    if m:
        try:
            return content.decode(m.group(1).decode("ascii"), "replace")
        except LookupError:
            pass
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", "replace")


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

    # Decode explicitly. Tally rarely declares a charset, and requests then
    # assumes Latin-1 for text/* — which silently mangles every Devanagari
    # narration and rupee sign into mojibake. Sniff the BOM and the XML
    # declaration, then default to UTF-8.
    text = _decode_response(resp.content)
    # Tally returns errors inside the body, not as HTTP codes.
    if "<LINEERROR>" in text:
        # Surface the first line error to make debugging painless.
        start = text.find("<LINEERROR>") + len("<LINEERROR>")
        end = text.find("</LINEERROR>", start)
        raise TallyError(f"Tally line error: {text[start:end].strip()}")
    return text


# Characters XML 1.0 forbids outright: C0 controls except tab/newline/CR,
# plus a few oddities. Voucher narrations accumulate these — people paste
# text into Tally from anywhere — and one such byte kills the whole parse.
_INVALID_XML_CHARS = re.compile(
    "[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD"
    "\U00010000-\U0010FFFF]"
)
_NUMERIC_REF = re.compile(r"&#(x[0-9a-fA-F]+|[0-9]+);")
_VALID_NUMERIC = re.compile(r"&#(x[0-9a-fA-F]+|[0-9]+)$")

# What may legitimately sit between '<' and '>' in Tally's export: an element
# name (letters, digits, dots, colons, hyphens, underscores — ALLLEDGERENTRIES.LIST,
# UDF:_UDF_FIELD), optionally attributes with double-quoted values, optional
# self-close. Anything else is narration text wearing angle brackets.
_TAG_SHAPE = re.compile(
    r"^/?[A-Za-z_][\w.:-]*"                       # element name
    r"(\s+[A-Za-z_][\w.:-]*\s*=\s*\"[^\"<]*\")*"   # attributes; '>' may occur in a value
    r"\s*/?$"
    r"|^[!?].*$"                                   # <?xml ...?>, <!-- -->
)


def _drop_invalid_refs(m: "re.Match[str]") -> str:
    """Keep a numeric character reference only if it names a legal XML char."""
    v = m.group(1)
    code = int(v[1:], 16) if v[0] in "xX" else int(v)
    if code in (0x9, 0xA, 0xD) or 0x20 <= code <= 0xD7FF \
            or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF:
        return m.group(0)
    return ""


def _clean_xml(raw: str) -> str:
    """
    Tally's XML export is not well-formed XML. Observed in live books:
    raw control bytes in narrations, invalid numeric references like &#4;,
    unescaped ampersands, and unescaped '<' in text ("qty < 100").
    Sanitise all of it before handing to ElementTree.
    """
    raw = _INVALID_XML_CHARS.sub("", raw)
    raw = _NUMERIC_REF.sub(_drop_invalid_refs, raw)

    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "&":
            # Keep only well-formed entities; escape every other ampersand.
            semi = raw.find(";", i, i + 8)
            token = raw[i:semi] if semi != -1 else ""
            if token in ("&amp", "&lt", "&gt", "&quot", "&apos") \
                    or (token.startswith("&#") and _VALID_NUMERIC.match(token)):
                out.append(ch)
            else:
                out.append("&amp;")
        elif ch == "<":
            # Keep '<' only when what follows is a genuinely well-formed tag.
            # Narrations contain things like "rate < 500", "<- pending" and
            # "<PONO 123>" — the last LOOKS tag-like but has an attribute
            # starting with a digit, which no repair-by-character can fix.
            # An attribute value may itself contain '>' (a ledger named
            # "A > B"), so try successive candidate closers before giving up.
            kept = False
            close = raw.find(">", i + 1, i + 300)
            for _ in range(5):
                if close == -1:
                    break
                if _TAG_SHAPE.match(raw[i + 1:close]):
                    kept = True
                    break
                close = raw.find(">", close + 1, i + 300)
            out.append(ch if kept else "&lt;")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# A complete tag: optional /, name, attributes (quoted values may hold '>'),
# optional self-close. Comments/PIs don't match (name must start with a letter).
_TAG_TOKEN = re.compile(
    r"<(/?)([A-Za-z_][\w.:-]*)((?:[^<>\"]|\"[^\"<]*\")*?)(/?)>"
)


def _repair_structure(text: str) -> str:
    """
    Neutralise narration text that impersonates markup.

    A narration containing "</NARRATION>" or "<ok>" passes every character-
    level check yet derails the element tree: the fake closer ends the real
    element early, or the fake opener never closes. Character-by-character
    repair makes that worse — it eats the REAL tags one letter at a time.

    So repair structurally: walk the tags with a stack; a closer that matches
    nothing is text (escape it), a closer that matches deeper in the stack
    means the tags above it are fake openers (escape those). Escaping turns
    them back into what they are — narration text — and every legitimate
    element keeps its content.
    """
    stack: list = []          # (name, span)
    to_escape: list = []
    for m in _TAG_TOKEN.finditer(text):
        closing, name, self_close = m.group(1), m.group(2), m.group(4)
        if self_close:
            continue
        if not closing:
            stack.append((name, m.span()))
        elif stack and stack[-1][0] == name:
            stack.pop()
        elif any(n == name for n, _ in stack):
            while stack and stack[-1][0] != name:
                to_escape.append(stack.pop()[1])
            stack.pop()
        else:
            to_escape.append(m.span())

    # Leftover openers are fake tags from narrations — unless there are LOTS,
    # which smells like a truncated response that should fail loudly instead
    # of parsing with half the data silently missing.
    if stack and len(stack) <= 20:
        to_escape.extend(span for _, span in stack)

    if not to_escape:
        return text
    parts: list = []
    prev = 0
    for a, b in sorted(to_escape):
        parts.append(text[prev:a])
        parts.append(text[a:b].replace("<", "&lt;").replace(">", "&gt;"))
        prev = b
    parts.append(text[prev:])
    return "".join(parts)


def _parse_xml(raw: str) -> ET.Element:
    """
    Parse Tally's response, surviving garbage _clean_xml didn't anticipate.

    If parsing still fails, the offending character (ElementTree reports its
    exact position) is replaced and the parse retried, up to a bounded number
    of times. Losing a character of a narration is acceptable; losing a whole
    day's voucher export to one byte is not. Every failure mode raises
    TallyError, never a raw parser exception, so one bad company cannot abort
    the others.
    """
    stripped = raw.lstrip()[:200].lower()
    if not raw.strip():
        raise TallyError(
            "Tally returned an empty response. The company may have been "
            "closed mid-request, or Tally is still starting up — retry shortly."
        )
    if stripped.startswith(("<!doctype", "<html")):
        raise TallyError(
            "Port 9000 answered with a web page, not Tally XML. On a shared "
            "server another program may own the port — check with the provider."
        )

    text = _repair_structure(_clean_xml(raw))
    repairs = 0
    last_pos = None
    for _ in range(200):
        try:
            root = ET.fromstring(text)
            if repairs:
                log.warning("Repaired %d invalid character(s) in Tally's XML "
                            "response; the affected narration text lost those "
                            "characters, everything else is intact.", repairs)
            return root
        except ET.ParseError as exc:
            lineno, col = getattr(exc, "position", (None, None))
            if lineno is None:
                raise TallyError(f"Tally returned unparseable XML: {exc}") from exc
            lines = text.split("\n")
            if not (1 <= lineno <= len(lines)):
                raise TallyError(f"Tally returned unparseable XML: {exc}") from exc
            line = lines[lineno - 1]

            # expat counts columns in UTF-8 BYTES; the string is indexed in
            # characters. On a line holding multi-byte text those differ, and
            # editing at the byte column mutilates an innocent character while
            # the real offender survives — the loop then spins in place.
            char_idx = len(line.encode("utf-8")[:col].decode("utf-8", "ignore"))
            if char_idx >= len(line):
                # Blamed past the end of the line: trim its tail so the text
                # still shrinks, rather than giving up on the whole response.
                if not line:
                    raise TallyError(
                        f"Tally returned unparseable XML: {exc}") from exc
                char_idx = len(line) - 1

            # Deleting (not substituting) guarantees the text shrinks, so the
            # loop must terminate even when the parser keeps blaming the same
            # spot. A substituted placeholder can itself be blamed, forever.
            if last_pos == (lineno, col, len(line)):
                char_idx = max(0, char_idx - 1)   # blamed char unchanged? widen
            last_pos = (lineno, col, len(line))

            if repairs < 5:
                log.warning("Removing invalid XML character %r at line %d",
                            line[char_idx], lineno)
            repairs += 1
            lines[lineno - 1] = line[:char_idx] + line[char_idx + 1:]
            text = "\n".join(lines)
    raise TallyError(
        "Tally's XML response could not be repaired after 200 attempts — "
        "the export appears badly corrupted. Re-run with -v and report the "
        "date range being fetched."
    )


def _fmt_date(d: date) -> str:
    """Tally wants dates as YYYYMMDD."""
    return d.strftime("%Y%m%d")


def _tally_date_to_iso(s: str) -> str:
    """Tally voucher dates come back as YYYYMMDD; normalise to ISO."""
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s


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
class Bill:
    """
    One outstanding bill (invoice) against a party.

    Tally's Bills collection returns only bills with a balance still open — a
    fully paid invoice disappears from it. So this mirrors what is UNPAID, not
    a full invoice history.
    """
    name: str                        # bill reference, e.g. "SL/1234"
    party: str = ""                  # the ledger it belongs to
    company: str = ""
    bill_date: str = ""              # ISO
    credit_period: str = ""          # raw Tally text: "45 Days", "2 Months"
    due_date: str = ""               # computed
    overdue_days: int = 0            # computed, negative = not yet due
    opening: float = 0.0             # debit-positive
    closing: float = 0.0             # debit-positive; the amount still unpaid
    is_advance: bool = False


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


# Bill-wise fields, most valuable first. Tally answers an unrecognised
# NATIVEMETHOD with a LINEERROR that kills the whole request, so the optional
# ones are dropped progressively rather than assumed.
_BILL_CORE = ["Name", "Parent", "BillDate", "ClosingBalance"]
_BILL_EXTRA = ["BillCreditPeriod", "OpeningBalance", "IsAdvance", "BillFixed", "BaseClosing"]


def _bills_request(cfg: TallyConfig, frm: date, to: date, methods: list) -> str:
    lines = "\n     ".join(f"<NATIVEMETHOD>{m}</NATIVEMETHOD>" for m in methods)
    return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>TB_Bills</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVFROMDATE TYPE="Date">{_fmt_date(frm)}</SVFROMDATE>
   <SVTODATE TYPE="Date">{_fmt_date(to)}</SVTODATE>
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="TB_Bills" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <TYPE>Bills</TYPE>
     {lines}
   </COLLECTION>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


_CREDIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-z]*)")


def _parse_credit_days(text: str) -> int:
    """
    Turn Tally's free-text credit period into days.

    Accountants type "45 Days", "2 Months", "5W", "60", or leave it blank.
    Blank means due on the bill date, which is Tally's own default.
    """
    m = _CREDIT_RE.search(text or "")
    if not m:
        return 0
    n = float(m.group(1))
    unit = (m.group(2) or "d").lower()[:1]
    return int(n * {"d": 1, "w": 7, "m": 30, "y": 365}.get(unit, 1))


def fetch_bills(cfg: TallyConfig, as_on: date) -> list:
    """
    Outstanding bills for the current company, with ageing computed here.

    Tally returns no ageing buckets — only a bill date and a free-text credit
    period — so the due date and overdue days are derived. Optional fields are
    dropped one at a time if this build rejects them, so a single unsupported
    method cannot cost the whole request.
    """
    assert_company_loaded(cfg)
    start = _company_start(cfg) or date(as_on.year, 4, 1)

    methods = _BILL_CORE + _BILL_EXTRA
    raw = None
    while raw is None:
        try:
            raw = _post(cfg, _bills_request(cfg, start, as_on, methods))
        except TallyError as exc:
            if len(methods) > len(_BILL_CORE):
                dropped = methods.pop()
                log.info("Tally rejected the %r bill field; retrying without it.", dropped)
                continue
            raise TallyError(
                f"Could not read outstanding bills: {exc}\n"
                "If this build has no Bills collection, bill-wise details may "
                "be switched off for this company (F11 > Accounting > Maintain "
                "bill-wise details)."
            ) from exc

    root = _parse_xml(raw)
    bills: list = []
    for el in root.iter("BILLS"):
        name = el.get("NAME") or _text(el.find("NAME"))
        party = _text(el.find("PARENT"))
        if not name or not party:
            continue
        bill_date = _tally_date_to_iso(_text(el.find("BILLDATE")))
        credit = _text(el.find("BILLCREDITPERIOD"))
        due = ""
        overdue = 0
        if len(bill_date) == 10:
            try:
                d = date.fromisoformat(bill_date) + timedelta(days=_parse_credit_days(credit))
                due = d.isoformat()
                overdue = (as_on - d).days
            except ValueError:
                pass
        bills.append(Bill(
            name=name,
            party=party,
            company=cfg.company,
            bill_date=bill_date,
            credit_period=credit,
            due_date=due,
            overdue_days=overdue,
            opening=_to_debit_positive(_text(el.find("OPENINGBALANCE"))),
            closing=_to_debit_positive(_text(el.find("CLOSINGBALANCE"))),
            is_advance=_text(el.find("ISADVANCE")).lower() == "yes",
        ))
    log.info("Fetched %d outstanding bills for %s", len(bills), cfg.company)
    return bills


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
    root = _parse_xml(raw)
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
    root = _parse_xml(raw)
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


def _voucher_request(cfg: TallyConfig, frm: date, to: date, variant: str) -> str:
    """
    Build a voucher export in one of several shapes.

    TallyPrime builds disagree about which export honours SVFROMDATE/SVTODATE.
    On this server the Day Book report ignored them entirely and answered every
    monthly window with the current day's data — five identical vouchers filed
    as five different months. The working shape is detected at runtime by
    _pick_voucher_variant, which verifies rather than assumes.
    """
    fd, td = _fmt_date(frm), _fmt_date(to)

    if variant in ("daybook", "daybook_typed", "register"):
        report = "Voucher Register" if variant == "register" else "Day Book"
        attr = ' TYPE="Date"' if variant == "daybook_typed" else ""
        return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Data</TYPE><ID>{report}</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVFROMDATE{attr}>{fd}</SVFROMDATE><SVTODATE{attr}>{td}</SVTODATE>
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <REPORT NAME="{report}" ISMODIFY="No"><FORMS>{report}</FORMS></REPORT>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""

    if variant == "filter":
        # Verified against the most widely deployed Tally extractor
        # (tally-database-loader, src/tally.mts:506 and 1104-1124).
        #
        # Three things here are load-bearing and were each wrong before:
        #   * <FILTER> is SINGULAR and comma-joined. <FILTERS> is silently
        #     ignored, which is exactly how an unfiltered result came back.
        #   * ONE <FETCH>, comma-joined. The multi-tag form belongs to
        #     <FETCHLIST> at DESC level — a different construct.
        #   * NO SVFROMDATE/SVTODATE. A Collection has no report context, so
        #     they bind to nothing; the FILTER is the only scoping mechanism.
        #
        # The comparison operators must reach Tally as the entity sequences
        # below — this is ordinary XML escaping of < and > inside element
        # text. Never pass this formula through _xml_escape(): doing so would
        # double-escape it and the filter would silently mis-evaluate.
        cond = (f'$Date &gt;= $$Date:"{fd}" and $Date &lt;= $$Date:"{td}"')
        return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>TB_Vouchers</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="TB_Vouchers" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <TYPE>Voucher</TYPE>
    <FETCH>Guid,Date,VoucherTypeName,VoucherNumber,Reference,ReferenceDate,Narration,PartyLedgerName,IsInvoice,IsCancelled,AlterID,AllLedgerEntries.LedgerName,AllLedgerEntries.Amount,AllLedgerEntries.IsDeemedPositive</FETCH>
    <FILTER>TBFltrPeriod,TBFltrNotCancelled,TBFltrNotOptional</FILTER>
   </COLLECTION>
   <SYSTEM TYPE="Formulae" NAME="TBFltrPeriod">{cond}</SYSTEM>
   <SYSTEM TYPE="Formulae" NAME="TBFltrNotCancelled">NOT $IsCancelled</SYSTEM>
   <SYSTEM TYPE="Formulae" NAME="TBFltrNotOptional">NOT $IsOptional</SYSTEM>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""

    # variant == "all": no date scoping at all. Used as the guaranteed
    # fallback — fetch the company once, then filter in Python. Slower and
    # heavier, but it cannot be defeated by a build that ignores ranges.
    return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>TB_VchAll</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="TB_VchAll" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <TYPE>Voucher</TYPE>
    <FETCH>Guid,Date,VoucherTypeName,VoucherNumber,Reference,ReferenceDate,Narration,PartyLedgerName,IsInvoice,IsCancelled,AlterID,AllLedgerEntries.LedgerName,AllLedgerEntries.Amount,AllLedgerEntries.IsDeemedPositive</FETCH>
    <FILTER>TBFltrNotCancelled,TBFltrNotOptional</FILTER>
   </COLLECTION>
   <SYSTEM TYPE="Formulae" NAME="TBFltrNotCancelled">NOT $IsCancelled</SYSTEM>
   <SYSTEM TYPE="Formulae" NAME="TBFltrNotOptional">NOT $IsOptional</SYSTEM>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


# Probe result and whole-company cache, keyed per Tally endpoint + company.
_variant_cache: dict = {}
_all_cache: dict = {}

# Ordered best-first: precise and cheap before broad and heavy.
_VARIANTS = ("filter", "daybook", "daybook_typed", "register")


def _company_start(cfg: TallyConfig) -> "date | None":
    """The first date this company's books cover, per Tally itself."""
    try:
        for c in list_companies(cfg):
            if c["name"] == cfg.company:
                iso = _tally_date_to_iso(c.get("starting_from") or "")
                if len(iso) == 10:
                    return date.fromisoformat(iso)
    except (TallyError, ValueError):
        pass
    return None


def _probe_variant(cfg: TallyConfig, variant: str) -> str:
    """
    Classify one variant by EXPERIMENT: 'works', 'ignores', 'empty', 'error'.

    Two different real windows are requested and their results compared. A
    request that honours dates returns different vouchers for different
    months; one that ignores them returns the same rows both times — which is
    precisely the failure that filed one Receipt under five months.

    An absurd window (1901) is deliberately NOT used: Tally clamps requests to
    the company's own book period, so an empty answer there proves nothing.
    """
    # Windows must sit INSIDE this company's own book period — a file for
    # 2024-25 holds nothing in 2026, and probing there would prove nothing.
    start = _company_start(cfg) or date(date.today().year, 4, 1)
    windows = [(start, start + timedelta(days=29)),
               (start + timedelta(days=90), start + timedelta(days=119))]
    seen = []
    for frm, to in windows:
        try:
            root = _parse_xml(_post(cfg, _voucher_request(cfg, frm, to, variant)))
        except TallyError:
            return "error"
        guids, out_of_range = set(), 0
        for vel in root.iter("VOUCHER"):
            g = _text(vel.find("GUID"))
            d = _tally_date_to_iso(_text(vel.find("DATE")))
            if g:
                guids.add(g)
            if d and not (str(frm) <= d <= str(to)):
                out_of_range += 1
        if out_of_range:
            return "ignores"          # returned rows outside the window asked for
        seen.append(guids)

    if not seen[0] and not seen[1]:
        return "empty"                # nothing in either window
    if seen[0] and seen[0] == seen[1]:
        return "ignores"              # identical rows for two different months
    return "works"


def _pick_voucher_variant(cfg: TallyConfig) -> str:
    """
    Choose how to fetch vouchers from THIS Tally, verifying by experiment.

    Tries each request shape in order and keeps the first that demonstrably
    honours a date window. If none does, falls back to fetching the whole
    company once and filtering here — slower, but it cannot be defeated by a
    build that ignores ranges, so the mirror is correct either way.
    """
    key = (cfg.url, cfg.company)
    if key in _variant_cache:
        return _variant_cache[key]

    for variant in _VARIANTS:
        verdict = _probe_variant(cfg, variant)
        if verdict == "works":
            log.info("Voucher requests: using %r (verified against two "
                     "different months).", variant)
            _variant_cache[key] = variant
            return variant
        log.info("Voucher request %r: %s — trying the next shape.",
                 variant, verdict)

    log.warning(
        "No voucher request on this Tally build honours date ranges. Falling "
        "back to fetching the whole company once and filtering by date here — "
        "slower and heavier, but the result is correct either way."
    )
    _variant_cache[key] = "all"
    return "all"


def _fetch_all_once(cfg: TallyConfig) -> str:
    """Fetch (and cache for this run) every voucher in the current company."""
    key = (cfg.url, cfg.company)
    if key not in _all_cache:
        log.info("Fetching the complete voucher list for %s (one-off, may take "
                 "a few minutes)...", cfg.company)
        _all_cache[key] = _post(cfg, _voucher_request(cfg, date(1900, 1, 1),
                                                      date(2100, 1, 1), "all"))
        log.info("  received %.1f MB", len(_all_cache[key]) / 1_048_576)
    return _all_cache[key]


def fetch_vouchers(cfg: TallyConfig, frm: date, to: date) -> list[Voucher]:
    """
    Export all vouchers in [frm, to].

    The request shape is picked by _pick_voucher_variant, because TallyPrime
    builds differ in which export actually honours SVFROMDATE/SVTODATE — the
    live server ignored them on the Day Book report entirely. Chunk your date
    ranges (e.g. one month at a time) for large books.
    """
    assert_company_loaded(cfg)
    variant = _pick_voucher_variant(cfg)
    raw = _fetch_all_once(cfg) if variant == "all" else _post(
        cfg, _voucher_request(cfg, frm, to, variant))
    root = _parse_xml(raw)
    vouchers: list[Voucher] = []
    out_of_range = 0

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

        # Belt and braces: even a range-honouring variant must never smuggle
        # in rows from outside the window — mislabelled periods are worse
        # than missing ones.
        try:
            v_date = date.fromisoformat(v.date)
        except ValueError:
            v_date = None
        if v_date is not None and not (frm <= v_date <= to):
            out_of_range += 1
            continue
        vouchers.append(v)

    if out_of_range:
        log.warning("Dropped %d voucher(s) dated outside %s..%s — this Tally "
                    "build leaks rows across range boundaries.",
                    out_of_range, frm, to)
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
    root = _parse_xml(raw)
    companies = []
    for el in root.iter("COMPANY"):
        name = el.get("NAME") or _text(el.find("NAME"))
        if name:
            companies.append({"name": name, "starting_from": _text(el.find("STARTINGFROM"))})
    return companies
