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
import os
import re
import time
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


# Tally serves XML from the same single-threaded engine that draws its UI, and
# a hosted box shares it with live operators. A heavy collection leaves it
# unable to accept connections for a while — study.py pulled two months of
# vouchers and every one of the next eighteen requests died on connect, for
# twelve minutes straight. So: never fire two requests back to back, and treat
# a refused connection as "busy, ask again" rather than "absent".
# Overridable so the test mock — which is a local HTTP server with no engine
# behind it — is not throttled to a crawl for no reason.
_MIN_REQUEST_GAP = float(os.environ.get("TALLY_MIN_REQUEST_GAP", "1.5"))
_last_request_at = 0.0


def _post(cfg: TallyConfig, xml_body: str, *, attempts: int = 4) -> str:
    """Send a raw XML envelope to Tally and return the raw XML response."""
    global _last_request_at
    delay = 3.0
    for attempt in range(1, attempts + 1):
        pause = _MIN_REQUEST_GAP - (time.monotonic() - _last_request_at)
        if pause > 0:
            time.sleep(pause)
        try:
            resp = requests.post(
                cfg.url,
                data=xml_body.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8"},
                timeout=cfg.timeout,
            )
            _last_request_at = time.monotonic()
            break
        except requests.RequestException as exc:
            _last_request_at = time.monotonic()
            if attempt == attempts:
                raise TallyError(
                    f"Could not reach Tally at {cfg.url} after {attempts} "
                    f"attempts. Tally answers on this port but stops accepting "
                    f"connections while it digests a large export — if this "
                    f"persists, reduce sync.chunk_days. ({exc})"
                ) from exc
            log.warning("Tally did not answer (attempt %d/%d: %s) — "
                        "waiting %.0fs and trying again.", attempt, attempts,
                        type(exc).__name__, delay)
            time.sleep(delay)
            delay *= 2

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
# How far ahead to look for a tag's closing '>', and how many candidate
# closers to try. Both were far too small: real Tally tags carry UDF
# attributes that run past 300 characters.
_TAG_SCAN = 4000
_TAG_CLOSER_TRIES = 40

_TAG_SHAPE = re.compile(
    r"^/?[A-Za-z_][\w.:-]*"                       # element name
    r"(\s+[A-Za-z_][\w.:-]*\s*=\s*\"[^\"]*\")*"     # attributes; value may hold > or <
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
            # The scan window must be generous. Tally emits User Defined Field
            # tags that run to several hundred characters:
            #   <UDF:CMPGSTREGNUMBER.LIST DESC="`CMPGSTREGNUMBER`" ISLIST="YES"
            #    TYPE="String" INDEX="7">
            # A short window makes the closing '>' invisible, the tag gets
            # escaped as text, and the repair loop then eats "<UDF" one letter
            # at a time until the whole document is unparseable — which is
            # exactly what happened on the live book.
            #
            # An attribute value may itself contain '>' (a ledger named
            # "A > B"), so try successive candidate closers before giving up.
            kept = False
            close = raw.find(">", i + 1, i + _TAG_SCAN)
            for _ in range(_TAG_CLOSER_TRIES):
                if close == -1:
                    break
                if _TAG_SHAPE.match(raw[i + 1:close]):
                    kept = True
                    break
                close = raw.find(">", close + 1, i + _TAG_SCAN)
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


# Tally emits User Defined Fields as <UDF:FIELDNAME>, which LOOKS like an XML
# namespace prefix but is never declared. ElementTree rejects the entire
# document with "unbound prefix", and a character-level repair then eats the
# tag one letter at a time until nothing parses — the exact failure seen on
# the live book. These are not namespaces, so the colon is simply flattened.
_PREFIXED_TAG = re.compile(r"(</?)([A-Za-z_][\w.-]*):([A-Za-z_][\w.-]*)")


def _flatten_prefixes(text: str) -> str:
    """Turn <UDF:NAME> into <UDF_NAME> so ElementTree will accept it."""
    prev = None
    # Repeat: a tag may carry more than one colon (UDF:A:B).
    while prev != text:
        prev = text
        text = _PREFIXED_TAG.sub(r"\1\2_\3", text)
    return text


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

    text = _repair_structure(_flatten_prefixes(_clean_xml(raw)))
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
class Unit:
    """A unit of measure. Compound units carry a conversion, e.g. Dzn = 12 Pcs."""
    name: str
    company: str = ""
    formal_name: str = ""
    is_simple: bool = True
    base_units: str = ""          # for a compound unit, the smaller unit
    additional_units: str = ""    # the larger unit
    conversion: float = 0.0       # how many base units make one additional
    decimal_places: int = 0
    guid: str = ""
    alter_id: str = ""


@dataclass
class Godown:
    """A storage location. These usually mirror the physical units."""
    name: str
    company: str = ""
    parent: str = ""
    has_no_stock: bool = False
    is_external: bool = False
    guid: str = ""
    alter_id: str = ""


@dataclass
class StockGroup:
    name: str
    company: str = ""
    parent: str = ""
    primary_group: str = ""       # resolved root, e.g. "Hosiery"
    guid: str = ""
    alter_id: str = ""


@dataclass
class StockItem:
    """
    A product. Quantities are kept in three forms deliberately — see
    _parse_qty for why a bare float loses information.
    """
    name: str
    company: str = ""
    parent: str = ""              # stock group
    primary_group: str = ""       # resolved root product family
    category: str = ""
    part_no: str = ""
    description: str = ""
    base_units: str = ""
    additional_units: str = ""
    conversion: float = 0.0
    opening_qty: float = 0.0
    opening_qty_raw: str = ""
    opening_value: float = 0.0
    closing_qty: float = 0.0
    closing_qty_raw: str = ""
    closing_qty_unit: str = ""
    closing_rate: float = 0.0
    closing_rate_unit: str = ""
    closing_value: float = 0.0
    costing_method: str = ""
    is_batchwise: bool = False
    hsn_code: str = ""
    hsn_description: str = ""
    gst_rate: float = 0.0
    taxability: str = ""
    guid: str = ""
    master_id: str = ""
    alter_id: str = ""


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


_QTY_RE = re.compile(r"(-?[\d,]+(?:\.\d+)?)\s*([A-Za-z%]*)")


def _qty_pairs(text: str) -> list:
    """Split a Tally quantity string into its (value, unit) components."""
    return [(float(v.replace(",", "")), u)
            for v, u in _QTY_RE.findall((text or "").strip()) if v]


def _parse_qty(text: str, units: "dict | None" = None) -> tuple:
    """
    Parse a Tally quantity into (value, unit, raw), resolving compound units.

    Tally renders quantities for display, not arithmetic. A compound unit
    prints as several parts — "3 Dzn 6 Pcs" — and taking any single number
    is wrong: that is 42 pieces, not 3 and not 6. Hosiery books use Dzn and
    Box constantly, so this resolves the parts through the unit conversion
    table into the smallest unit present.

    Without a conversion table the parts cannot be combined safely, so the
    largest component is reported and the raw string is ALWAYS preserved —
    an unresolvable quantity should be visibly approximate, never silently
    wrong.
    """
    raw = (text or "").strip()
    if not raw:
        return 0.0, "", ""
    pairs = _qty_pairs(raw)
    if not pairs:
        return 0.0, "", raw
    if len(pairs) == 1:
        return pairs[0][0], pairs[0][1], raw

    smallest_unit = pairs[-1][1]
    # Tally signs the leading component only: "-2 Dzn 6 Pcs" means minus two
    # dozen AND six, i.e. -30, not -24+6. Apply the sign to every part.
    negative = pairs[0][0] < 0
    if units:
        total = 0.0
        resolved = True
        for value, unit in pairs:
            factor = _conversion_to(unit, smallest_unit, units)
            if factor is None:
                resolved = False
                break
            total += abs(value) * factor
        if resolved:
            return (-total if negative else total), smallest_unit, raw

    log.debug("Compound quantity %r could not be resolved; reporting the "
              "largest component.", raw)
    return pairs[0][0], pairs[0][1], raw


def _conversion_to(unit: str, target: str, units: dict) -> "float | None":
    """
    How many `target` units make one `unit`. 1.0 when they are the same.

    Follows the chain of compound-unit definitions (Box = 10 Dzn, Dzn = 12
    Pcs) up to a small depth, so "12 Box 3 Dzn 4 Pcs" resolves correctly.
    """
    if unit == target:
        return 1.0
    factor = 1.0
    cur = unit
    for _ in range(6):
        u = units.get(cur)
        if u is None or not u.base_units or not u.conversion:
            return None
        factor *= u.conversion
        cur = u.base_units
        if cur == target:
            return factor
    return None


_RATE_RE = re.compile(r"(-?[\d,]+(?:\.\d+)?)\s*/?\s*([A-Za-z]*)")


def _parse_rate(text: str) -> tuple:
    """Parse "45.50/Pcs" into (45.50, "Pcs"). The unit may differ from the
    quantity's unit, which is why amount is never recomputed from qty x rate."""
    m = _RATE_RE.search((text or "").strip())
    if not m:
        return 0.0, ""
    return float(m.group(1).replace(",", "")), m.group(2) or ""


def _master_request(cfg: TallyConfig, coll_id: str, tally_type: str,
                    methods: list, frm: "date | None" = None,
                    to: "date | None" = None) -> str:
    """A Collection request for any master object."""
    lines = "\n     ".join(f"<NATIVEMETHOD>{m}</NATIVEMETHOD>" for m in methods)
    dates = ""
    if frm and to:
        # Closing figures are evaluated as at SVTODATE. Omitting the dates does
        # not mean "now" — it means whatever period the Tally session happens
        # to hold, which makes the export non-deterministic between runs.
        dates = (f"<SVFROMDATE TYPE=\"Date\">{_fmt_date(frm)}</SVFROMDATE>"
                 f"<SVTODATE TYPE=\"Date\">{_fmt_date(to)}</SVTODATE>")
    return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>{coll_id}</ID></HEADER>
 <BODY><DESC><STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   {dates}
   {_company_tag(cfg)}
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="{coll_id}" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
    <TYPE>{tally_type}</TYPE>
     {lines}
   </COLLECTION>
  </TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""


def _fetch_master(cfg: TallyConfig, coll_id: str, tally_type: str, tag: str,
                  core: list, extra: list, frm=None, to=None) -> list:
    """
    Fetch a master collection, dropping optional fields a build rejects.

    An unrecognised NATIVEMETHOD makes Tally return a LINEERROR that kills the
    whole request, so optional fields are shed one at a time rather than
    costing the entire object.
    """
    methods = core + extra
    raw = None
    while raw is None:
        try:
            raw = _post(cfg, _master_request(cfg, coll_id, tally_type, methods, frm, to))
        except TallyError as exc:
            if len(methods) > len(core):
                dropped = methods.pop()
                log.info("Tally rejected %r on %s; retrying without it.", dropped, tally_type)
                continue
            raise TallyError(f"Could not read {tally_type}: {exc}") from exc
    return list(_parse_xml(raw).iter(tag))


def fetch_units(cfg: TallyConfig) -> list:
    """
    Units of measure. Fetched FIRST: the conversion table is what makes every
    quantity string in the inventory domain interpretable.
    """
    assert_company_loaded(cfg)
    els = _fetch_master(
        cfg, "TB_Units", "Unit", "UNIT",
        ["Name"],
        ["FormalName", "IsSimpleUnit", "BaseUnits", "AdditionalUnits",
         "Conversion", "DecimalPlaces", "GUID", "AlterId"],
    )
    out = []
    for el in els:
        name = el.get("NAME") or _text(el.find("NAME"))
        if not name:
            continue
        out.append(Unit(
            name=name, company=cfg.company,
            formal_name=_text(el.find("FORMALNAME")),
            is_simple=_text(el.find("ISSIMPLEUNIT")).lower() != "no",
            base_units=_text(el.find("BASEUNITS")),
            additional_units=_text(el.find("ADDITIONALUNITS")),
            conversion=_to_float(_text(el.find("CONVERSION"))),
            decimal_places=int(_to_float(_text(el.find("DECIMALPLACES")))),
            guid=_text(el.find("GUID")), alter_id=_text(el.find("ALTERID")),
        ))
    log.info("Fetched %d units", len(out))
    return out


def fetch_godowns(cfg: TallyConfig) -> list:
    assert_company_loaded(cfg)
    els = _fetch_master(
        cfg, "TB_Godowns", "Godown", "GODOWN",
        ["Name"], ["Parent", "HasNoStock", "IsExternal", "GUID", "AlterId"],
    )
    out = []
    for el in els:
        name = el.get("NAME") or _text(el.find("NAME"))
        if not name:
            continue
        out.append(Godown(
            name=name, company=cfg.company, parent=_text(el.find("PARENT")),
            has_no_stock=_text(el.find("HASNOSTOCK")).lower() == "yes",
            is_external=_text(el.find("ISEXTERNAL")).lower() == "yes",
            guid=_text(el.find("GUID")), alter_id=_text(el.find("ALTERID")),
        ))
    log.info("Fetched %d godowns", len(out))
    return out


def fetch_stock_groups(cfg: TallyConfig) -> list:
    assert_company_loaded(cfg)
    els = _fetch_master(
        cfg, "TB_StockGroups", "StockGroup", "STOCKGROUP",
        ["Name"], ["Parent", "GUID", "AlterId"],
    )
    out = []
    for el in els:
        name = el.get("NAME") or _text(el.find("NAME"))
        if not name:
            continue
        out.append(StockGroup(
            name=name, company=cfg.company, parent=_text(el.find("PARENT")),
            guid=_text(el.find("GUID")), alter_id=_text(el.find("ALTERID")),
        ))
    log.info("Fetched %d stock groups", len(out))
    return out


def fetch_stock_items(cfg: TallyConfig, as_on: "date | None" = None,
                      units: "dict | None" = None) -> list:
    """
    Product masters with closing stock as at `as_on`.

    Closing figures depend on the date window, so it is always sent explicitly
    rather than inherited from whatever period the session holds.
    """
    assert_company_loaded(cfg)
    as_on = as_on or date.today()
    start = _company_start(cfg) or date(as_on.year, 4, 1)
    els = _fetch_master(
        cfg, "TB_StockItems", "StockItem", "STOCKITEM",
        ["Name", "Parent", "BaseUnits", "ClosingBalance", "ClosingValue"],
        ["Category", "PartNo", "Description", "AdditionalUnits", "Conversion",
         "OpeningBalance", "OpeningValue", "ClosingRate", "CostingMethod",
         "IsBatchWiseOn", "GUID", "MasterId", "AlterId",
         "InfGSTHSNCode", "InfGSTHSNDescription", "InfGSTIGSTRate",
         "InfGSTTaxablility"],
        frm=start, to=as_on,
    )
    out = []
    for el in els:
        name = el.get("NAME") or _text(el.find("NAME"))
        if not name:
            continue
        cq, cu, craw = _parse_qty(_text(el.find("CLOSINGBALANCE")), units)
        oq, _, oraw = _parse_qty(_text(el.find("OPENINGBALANCE")), units)
        rate, runit = _parse_rate(_text(el.find("CLOSINGRATE")))
        out.append(StockItem(
            name=name, company=cfg.company,
            parent=_text(el.find("PARENT")),
            category=_text(el.find("CATEGORY")),
            part_no=_text(el.find("PARTNO")),
            description=_text(el.find("DESCRIPTION")),
            base_units=_text(el.find("BASEUNITS")),
            additional_units=_text(el.find("ADDITIONALUNITS")),
            conversion=_to_float(_text(el.find("CONVERSION"))),
            opening_qty=oq, opening_qty_raw=oraw,
            opening_value=_to_float(_text(el.find("OPENINGVALUE"))),
            closing_qty=cq, closing_qty_raw=craw, closing_qty_unit=cu,
            closing_rate=rate, closing_rate_unit=runit,
            closing_value=_to_float(_text(el.find("CLOSINGVALUE"))),
            costing_method=_text(el.find("COSTINGMETHOD")),
            is_batchwise=_text(el.find("ISBATCHWISEON")).lower() == "yes",
            hsn_code=_text(el.find("INFGSTHSNCODE")),
            hsn_description=_text(el.find("INFGSTHSNDESCRIPTION")),
            gst_rate=_to_float(_text(el.find("INFGSTIGSTRATE"))),
            taxability=_text(el.find("INFGSTTAXABLILITY")),
            guid=_text(el.find("GUID")),
            master_id=_text(el.find("MASTERID")),
            alter_id=_text(el.find("ALTERID")),
        ))
    log.info("Fetched %d stock items for %s", len(out), cfg.company)
    return out


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


# The narrow set study.py proved on TallyPrime Edit Log 7.0. Enough for dates,
# parties, voucher types and full double-entry lines — so balances reconcile.
_FIELDS_PROVEN = ("Guid,Date,VoucherTypeName,VoucherNumber,PartyLedgerName,"
                  "AllLedgerEntries.LedgerName,AllLedgerEntries.Amount,"
                  "AllLedgerEntries.IsDeemedPositive")

# Everything worth having. Reference/ReferenceDate feed bill allocations and
# AlterID would enable incremental sync, so this is tried where it works.
_FIELDS_RICH = ("Guid,Date,VoucherTypeName,VoucherNumber,Reference,ReferenceDate,"
                "Narration,PartyLedgerName,IsInvoice,IsCancelled,AlterID,"
                "AllLedgerEntries.LedgerName,AllLedgerEntries.Amount,"
                "AllLedgerEntries.IsDeemedPositive")


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

    if variant.startswith("filter"):
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
        # Every extra filter is another way to get an empty result: if a
        # build lacks $IsOptional, the whole expression evaluates false and
        # Tally returns nothing at all — indistinguishable from "no data".
        # So the plain variant filters on date ONLY.
        # Two date literal forms. YYYYMMDD is what the most-deployed Tally
        # extractor uses and should be locale-independent — but this build
        # answered it with zero rows in 142ms, so the alternate is offered
        # rather than assumed away.
        if variant.endswith("_dmy"):
            lo, hi = frm.strftime("%d-%b-%Y"), to.strftime("%d-%b-%Y")
        else:
            lo, hi = fd, td
        cond = (f'$Date &gt;= $$Date:"{lo}" and $Date &lt;= $$Date:"{hi}"')
        # A build that does not recognise one FETCH field answers the whole
        # collection with zero rows — no error, no clue which field. That is
        # indistinguishable from "the filter matched nothing", and it is what
        # sent this agent down the whole-company fallback for two days: the
        # rich set below differs from the proven one ONLY by Reference,
        # ReferenceDate, Narration, IsInvoice, IsCancelled and AlterID, and on
        # TallyPrime Edit Log 7.0 that difference is the whole failure. So the
        # narrow set that study.py proved is tried FIRST, and the richer one
        # only as an upgrade on builds that accept it.
        plain = variant.startswith(("filter_plain", "filter_dotted"))
        fields = _FIELDS_PROVEN if variant.startswith("filter_dotted") else _FIELDS_RICH
        names = ("TBFltrPeriod" if plain
                 else "TBFltrPeriod,TBFltrNotCancelled,TBFltrNotOptional")
        hygiene = ("" if plain else
                   '<SYSTEM TYPE="Formulae" NAME="TBFltrNotCancelled">NOT $IsCancelled</SYSTEM>'
                   '<SYSTEM TYPE="Formulae" NAME="TBFltrNotOptional">NOT $IsOptional</SYSTEM>')
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
    <FETCH>{fields}</FETCH>
    <FILTER>{names}</FILTER>
   </COLLECTION>
   <SYSTEM TYPE="Formulae" NAME="TBFltrPeriod">{cond}</SYSTEM>
   {hygiene}
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

# Ordered best-first: proven before plausible, precise before broad and heavy.
# filter_dotted is the shape study.py verified against the live server on
# 2026-08-12 — 4,595 vouchers for April 2026, every one inside the window.
_VARIANTS = ("filter_dotted", "filter_dotted_dmy",
             "filter_plain", "filter_plain_dmy", "filter",
             "daybook", "daybook_typed", "register")


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


def _has_any_vouchers(cfg: TallyConfig) -> bool:
    """
    Does this company hold ANY vouchers at all?

    Needed to interpret an empty filtered result: a filter that matches
    nothing looks identical to a company with no transactions, and treating a
    broken filter as "no data" would quietly mirror an empty book.
    """
    key = (cfg.url, cfg.company, "any")
    if key in _variant_cache:
        return _variant_cache[key]
    probe = TallyConfig(host=cfg.host, port=cfg.port, company=cfg.company,
                        timeout=min(cfg.timeout, 90))
    try:
        raw = _post(probe, _voucher_request(cfg, date(1900, 1, 1), date(2100, 1, 1), "all"))
        found = next(_parse_xml(raw).iter("VOUCHER"), None) is not None
    except TallyError:
        found = True      # unknown: assume yes, so an empty filter is suspect
    _variant_cache[key] = found
    return found


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
    #
    # They must also be TINY. A month of a real book is a heavy export: the
    # live run spent 90 seconds per shape and timed out on every one, so the
    # probe learned nothing except that the book is large. A few days answers
    # the same question — does this shape honour the range — for a fraction
    # of the work.
    start = _company_start(cfg) or date(date.today().year, 4, 1)
    windows = [(start, start + timedelta(days=2)),
               (start + timedelta(days=45), start + timedelta(days=47))]
    # Probe with a short timeout: a correct one-month request answers quickly,
    # and a shape that hangs is not one worth waiting four minutes to reject.
    probe = TallyConfig(host=cfg.host, port=cfg.port, company=cfg.company,
                        timeout=min(cfg.timeout, 90))
    seen = []
    for frm, to in windows:
        try:
            # Probe with the EXACT request the sync will send. This used to
            # substitute a slim <FETCH>Guid,Date</FETCH> to save bandwidth,
            # which quietly made the probe test a different request from the
            # one it was choosing between: against the live server the slim
            # form returns nothing at all, while the full field list returns
            # the whole month correctly. Every shape was therefore rejected on
            # evidence that did not apply to it. The windows below are three
            # days wide, so the fields cost little and prove much more.
            body = _voucher_request(cfg, frm, to, variant)
            root = _parse_xml(_post(probe, body))
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
        # Empty is ambiguous. If the company demonstrably HAS vouchers, an
        # empty filtered result means the filter is wrong, not the book.
        return "filters everything out" if _has_any_vouchers(cfg) else "empty"
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
        if verdict == "filters everything out":
            log.warning("  (%s: Tally answered quickly but matched no vouchers, "
                        "while the company demonstrably has some — that shape's "
                        "filter is not being applied as intended.)", variant)

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
        big = TallyConfig(host=cfg.host, port=cfg.port, company=cfg.company,
                          timeout=max(cfg.timeout, 900))
        _all_cache[key] = _post(big, _voucher_request(cfg, date(1900, 1, 1),
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
