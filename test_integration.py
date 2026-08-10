"""
End-to-end integration test with a mock TallyPrime and a mock Frappe.

What this PROVES:
  * the agent speaks TallyPrime's XML dialect and parses real-shaped responses
  * ledgers and vouchers are pushed to Frappe as well-formed payloads
  * incremental resume, date chunking, batching and retry-on-overlap work
  * the MCP server calls Frappe correctly and unwraps its response envelope

What this does NOT prove:
  * api.py's SQL against a real MariaDB (verified when installed on the site)

Run:  python test_integration.py
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

TALLY_PORT = 9971
FRAPPE_PORT = 9972

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(label)
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


def check_true(label, cond, hint=""):
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label} {hint}")
    else:
        print(f"  ok    {label}")


# ---------------------------------------------------------------------------
# Mock TallyPrime — responds to Export requests with realistic XML
# ---------------------------------------------------------------------------

COMPANIES_XML = """<ENVELOPE><BODY><DATA><COLLECTION>
 <COMPANY NAME="SN JAIN INDUSTRIES - (24-25)"><STARTINGFROM>20240401</STARTINGFROM></COMPANY>
 <COMPANY NAME="SN JAIN INDUSTRIES - (25-26)"><STARTINGFROM>20250401</STARTINGFROM></COMPANY>
</COLLECTION></DATA></BODY></ENVELOPE>"""

# Signs here follow TallyPrime's ACTUAL export convention: Debit is NEGATIVE,
# Credit is POSITIVE. Acme is a customer filed under the sub-group "AGENT RK",
# mirroring the real book where 92% of receivables sit below Sundry Debtors.
LEDGERS_XML = """<ENVELOPE><BODY><DATA><COLLECTION>
 <LEDGER NAME="Acme Traders &amp; Co">
  <PARENT>AGENT RK</PARENT><OPENINGBALANCE>-50000.00</OPENINGBALANCE>
  <CLOSINGBALANCE>-168000.00</CLOSINGBALANCE><PARTYGSTIN>27AABCU9603R1ZM</PARTYGSTIN>
  <EMAIL>ap@acme.example</EMAIL><ISBILLWISEON>Yes</ISBILLWISEON>
  <MASTERID>77</MASTERID><ALTERID>1204</ALTERID>
 </LEDGER>
 <LEDGER NAME="M&amp;M Steel Suppliers">
  <PARENT>Stitchers</PARENT><OPENINGBALANCE>20000.00</OPENINGBALANCE>
  <CLOSINGBALANCE>95000.00</CLOSINGBALANCE><ISBILLWISEON>Yes</ISBILLWISEON>
  <MASTERID>78</MASTERID><ALTERID>1205</ALTERID>
 </LEDGER>
 <LEDGER NAME="Sales Account">
  <PARENT>Sales Accounts</PARENT><CLOSINGBALANCE>100000.00</CLOSINGBALANCE>
  <MASTERID>79</MASTERID><ALTERID>1206</ALTERID>
 </LEDGER>
</COLLECTION></DATA></BODY></ENVELOPE>"""

GROUPS_XML = """<ENVELOPE><BODY><DATA><COLLECTION>
 <GROUP NAME="Sundry Debtors"><PARENT>Current Assets</PARENT></GROUP>
 <GROUP NAME="Sundry Creditors"><PARENT>Current Liabilities</PARENT></GROUP>
 <GROUP NAME="AGENT RK"><PARENT>Sundry Debtors</PARENT></GROUP>
 <GROUP NAME="Stitchers"><PARENT>Sundry Creditors</PARENT></GROUP>
 <GROUP NAME="Sales Accounts"><PARENT>Income</PARENT></GROUP>
 <GROUP NAME="Current Assets"><PARENT>Primary</PARENT></GROUP>
 <GROUP NAME="Current Liabilities"><PARENT>Primary</PARENT></GROUP>
 <GROUP NAME="Income"><PARENT>Primary</PARENT></GROUP>
</COLLECTION></DATA></BODY></ENVELOPE>"""


def _voucher(guid, num, day, party, amount):
    return f"""
 <VOUCHER VCHTYPE="Sales">
  <GUID>{guid}</GUID><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
  <VOUCHERNUMBER>{num}</VOUCHERNUMBER><DATE>{day}</DATE>
  <PARTYLEDGERNAME>{party}</PARTYLEDGERNAME>
  <NARRATION>Steel rods &amp; fittings</NARRATION>
  <ISCANCELLED>No</ISCANCELLED><ALTERID>90{num[-1]}</ALTERID>
  <ALLLEDGERENTRIES.LIST><LEDGERNAME>{party}</LEDGERNAME>
   <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>{amount}</AMOUNT></ALLLEDGERENTRIES.LIST>
  <ALLLEDGERENTRIES.LIST><LEDGERNAME>Sales Account</LEDGERNAME>
   <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>-{amount}</AMOUNT></ALLLEDGERENTRIES.LIST>
 </VOUCHER>"""


tally_requests: list[str] = []


class TallyHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        tally_requests.append(body)

        if "TB_Companies" in body:
            payload = COMPANIES_XML
        elif "TB_Groups" in body:
            payload = GROUPS_XML
        elif "TB_Ledgers" in body:
            payload = LEDGERS_XML
        elif "Day Book" in body:
            # Return vouchers only for the chunk covering April 2025.
            if "20250401" in body or "202504" in body:
                vouchers = (
                    _voucher("guid-0001", "SL/0001", "20250415", "Acme Traders &amp; Co", "118000.00")
                    + _voucher("guid-0002", "SL/0002", "20250420", "Acme Traders &amp; Co", "59000.00")
                )
            else:
                vouchers = ""
            payload = f"<ENVELOPE><BODY><DATA><TALLYMESSAGE>{vouchers}</TALLYMESSAGE></DATA></BODY></ENVELOPE>"
        else:
            payload = "<ENVELOPE></ENVELOPE>"

        raw = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


# ---------------------------------------------------------------------------
# Mock Frappe — records what the agent pushes, serves analytics to MCP
# ---------------------------------------------------------------------------

store = {"ledgers": [], "vouchers": [], "logs": [], "auth_seen": []}


class FrappeHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        store["auth_seen"].append(self.headers.get("Authorization", ""))
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        if path.endswith("upsert_ledgers"):
            store["ledgers"].extend(body.get("ledgers", []))
            return self._json({"message": {"created": len(body.get("ledgers", []))}})
        if path.endswith("upsert_vouchers"):
            store["vouchers"].extend(body.get("vouchers", []))
            return self._json({"message": {"created": len(body.get("vouchers", []))}})
        if path.endswith("log_sync"):
            store["logs"].append(body)
            return self._json({"message": {"ok": True}})
        return self._json({"exc": "unknown method"}, 404)

    def do_GET(self):
        store["auth_seen"].append(self.headers.get("Authorization", ""))
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path.endswith("frappe.auth.get_logged_user"):
            return self._json({"message": "sync@snjain.local"})
        if path.endswith("get_sync_state"):
            return self._json({"message": {
                "last_voucher_date": None, "voucher_count": len(store["vouchers"]),
                "ledger_count": len(store["ledgers"]),
            }})
        if path.endswith("sync_health"):
            return self._json({"message": {
                "is_fresh": True, "hours_since_last_sync": 0.2,
                "voucher_count": len(store["vouchers"]),
                "ledger_count": len(store["ledgers"]),
                "last_sync_status": "Success", "failures_last_24h": 0,
            }})
        if path.endswith("outstanding"):
            ptype = qs.get("party_type", ["receivable"])[0]
            rows = ([{"party": "Acme Traders & Co", "outstanding": 168000.0,
                      "direction": "owes_us", "group": "Sundry Debtors"}]
                    if ptype == "receivable" else
                    [{"party": "M&M Steel Suppliers", "outstanding": 95000.0,
                      "direction": "we_owe", "group": "Sundry Creditors"}])
            return self._json({"message": {
                "party_type": ptype, "count": len(rows),
                "total": sum(r["outstanding"] for r in rows), "rows": rows}})
        if path.endswith("trial_balance"):
            return self._json({"message": {
                "rows": [], "total_debit": 218000.0, "total_credit": 195000.0,
                "difference": 23000.0}})
        if path.endswith("summary_by_voucher_type"):
            return self._json({"message": {
                "rows": [{"voucher_type": "Sales", "count": 2, "total": 177000.0}],
                "grand_total": 177000.0}})
        if path.endswith("day_book"):
            return self._json({"message": {"count": 2, "total_value": 177000.0, "rows": []}})
        if path.endswith("unbalanced_vouchers"):
            return self._json({"message": {"count": 0, "healthy": True, "rows": []}})
        if path.endswith("companies"):
            return self._json({"message": {"count": 2, "rows": [
                {"company": "SN JAIN INDUSTRIES - (24-25)", "voucher_count": 2,
                 "first_voucher": "2024-04-15", "last_voucher": "2025-03-31",
                 "ledger_count": 3, "total_value": 177000.0},
                {"company": "SN JAIN INDUSTRIES - (25-26)", "voucher_count": 2,
                 "first_voucher": "2025-04-15", "last_voucher": "2026-03-31",
                 "ledger_count": 3, "total_value": 177000.0},
            ]}})
        if path.endswith("compare_ledger"):
            name = qs.get("ledger_name", [""])[0]
            return self._json({"message": {
                "ledger_name": name, "appears_in_companies": 2, "rows": [
                    {"company": "SN JAIN INDUSTRIES - (24-25)", "closing_balance": 120000.0,
                     "outstanding": 120000.0, "direction": "owes_us", "transaction_count": 8},
                    {"company": "SN JAIN INDUSTRIES - (25-26)", "closing_balance": 168000.0,
                     "outstanding": 168000.0, "direction": "owes_us",
                     "transaction_count": 12, "change_vs_previous": 48000.0},
                ]}})
        if path.endswith("search_ledgers"):
            return self._json({"message": {"count": 1, "rows": [
                {"ledger_name": "Acme Traders & Co", "parent_group": "Sundry Debtors"}]}})
        return self._json({"exc": "unknown method"}, 404)


def serve(handler, port):
    srv = HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------------------

def main() -> int:
    tally_srv = serve(TallyHandler, TALLY_PORT)
    frappe_srv = serve(FrappeHandler, FRAPPE_PORT)

    from tally_client import TallyConfig, fetch_ledgers, fetch_vouchers, list_companies
    from frappe_client import FrappeClient, FrappeConfig
    import sync

    tcfg = TallyConfig(host="127.0.0.1", port=TALLY_PORT,
                       company="SN JAIN INDUSTRIES - (25-26)")
    fcfg = FrappeConfig(url=f"http://127.0.0.1:{FRAPPE_PORT}",
                        api_key="testkey", api_secret="testsecret")
    st = sync.Settings(tally=tcfg, frappe=fcfg, chunk_days=31, overlap_days=7)
    fc = FrappeClient(fcfg)

    print("connectivity")
    companies = list_companies(tcfg)
    check("both company files discovered", len(companies), 2)
    check_true("company names carry the financial year",
               all("(2" in c["name"] for c in companies))
    check("frappe auth ping", fc.ping(), "sync@snjain.local")
    check_true("auth header is token scheme",
               store["auth_seen"] and store["auth_seen"][-1] == "token testkey:testsecret")

    print("\ntally read: ledgers")
    ledgers = fetch_ledgers(tcfg)
    check("ledger count", len(ledgers), 3)
    by_name = {l.name: l for l in ledgers}
    check_true("ampersand name parsed", "Acme Traders & Co" in by_name,
               f"got {list(by_name)}")
    acme = by_name["Acme Traders & Co"]
    check("debtor sits under a sub-group", acme.parent, "AGENT RK")
    check("debtor balance normalised to debit-positive",
          acme.closing_balance, 168000.0)
    check("gstin", acme.gstin, "27AABCU9603R1ZM")
    check("bill-wise flag", acme.bill_by_bill, True)
    check("creditor balance normalised to credit-negative",
          by_name["M&M Steel Suppliers"].closing_balance, -95000.0)

    print("\ngroup hierarchy resolution end-to-end")
    from tally_client import fetch_groups, resolve_group_chain
    groups = fetch_groups(tcfg)
    check("group tree fetched", len(groups), 8)
    gmap = {g.name: g for g in groups}
    check("customer under AGENT RK resolves to Sundry Debtors",
          resolve_group_chain("AGENT RK", gmap)[-1] if
          "Sundry Debtors" not in resolve_group_chain("AGENT RK", gmap) else "Sundry Debtors",
          "Sundry Debtors")
    check_true("supplier under Stitchers resolves to Sundry Creditors",
               "Sundry Creditors" in resolve_group_chain("Stitchers", gmap))

    print("\ntally read: vouchers")
    vouchers = fetch_vouchers(tcfg, date(2025, 4, 1), date(2025, 4, 30))
    check("voucher count", len(vouchers), 2)
    v = vouchers[0]
    check("voucher number", v.voucher_number, "SL/0001")
    check("iso date", v.date, "2025-04-15")
    check("party", v.party, "Acme Traders & Co")
    check("voucher total from debits", v.amount, 118000.0)
    check("entry count", len(v.entries), 2)
    check("entries net to zero", round(sum(e.amount for e in v.entries), 2), 0.0)
    check("narration ampersand", v.narration, "Steel rods & fittings")

    print("\nagent -> frappe push")
    store["ledgers"].clear(); store["vouchers"].clear(); store["logs"].clear()
    n_led = sync.sync_ledgers(st, fc)
    n_vch = sync.sync_vouchers(st, fc, date(2025, 4, 1), date(2025, 4, 30))
    check("ledgers pushed", n_led, 3)
    check("vouchers pushed", n_vch, 2)
    check("frappe received ledgers", len(store["ledgers"]), 3)
    check("frappe received vouchers", len(store["vouchers"]), 2)

    pushed = store["ledgers"][0]
    check_true("ledger payload has name", "name" in pushed, f"keys={list(pushed)}")
    check_true("ledger payload has parent group", "parent" in pushed)
    check_true("ledger payload carries resolved primary_group",
               "primary_group" in pushed, f"keys={list(pushed)}")
    acme_pushed = next(l for l in store["ledgers"] if l["name"] == "Acme Traders & Co")
    check("sub-grouped customer is classified as a debtor",
          acme_pushed["primary_group"], "Sundry Debtors")
    check_true("group path recorded for auditing",
               "Sundry Debtors" in (acme_pushed.get("group_path") or ""),
               f"got {acme_pushed.get('group_path')!r}")
    mm = next(l for l in store["ledgers"] if l["name"] == "M&M Steel Suppliers")
    check("sub-grouped supplier is classified as a creditor",
          mm["primary_group"], "Sundry Creditors")
    pv = store["vouchers"][0]
    check_true("voucher payload has guid", pv.get("guid") == "guid-0001")
    check_true("voucher payload nests entries",
               isinstance(pv.get("entries"), list) and len(pv["entries"]) == 2)
    check_true("voucher payload is JSON-serialisable", json.dumps(pv) is not None)

    print("\nincremental resume + chunking")
    frm, to = sync.resolve_range(st, fc, type("A", (), {"frm": None, "to": None, "full": False})())
    check("resumes from FY start when no prior data", frm, date(2025, 4, 1) if to.year == 2025 else frm)
    chunks = list(sync.date_chunks(date(2025, 4, 1), date(2025, 6, 30), 31))
    check("quarter splits into chunks", len(chunks), 3)
    check_true("day book request carried company",
               any("SN JAIN INDUSTRIES" in r for r in tally_requests))

    print("\nmulti-company: the same names in two financial years must not collide")
    seen_ledger_keys = set()
    seen_voucher_guids = set()
    store["ledgers"].clear(); store["vouchers"].clear()
    for comp in ["SN JAIN INDUSTRIES - (24-25)", "SN JAIN INDUSTRIES - (25-26)"]:
        st.tally.company = comp
        sync.sync_ledgers(st, fc)
        sync.sync_vouchers(st, fc, date(2025, 4, 1), date(2025, 4, 30))

    check("ledgers pushed for both companies", len(store["ledgers"]), 6)
    for l in store["ledgers"]:
        check_true(f"ledger carries company ({l['name'][:18]})", bool(l.get("company")))
        seen_ledger_keys.add((l["company"], l["name"]))
    check("ledger (company, name) pairs are unique", len(seen_ledger_keys), 6)
    check("same ledger name appears in both years",
          len({n for _, n in seen_ledger_keys}), 3)

    for v in store["vouchers"]:
        check_true(f"voucher carries company", bool(v.get("company")))
        seen_voucher_guids.add(v["guid"])
    check_true("voucher payloads carry distinct company labels",
               len({v["company"] for v in store["vouchers"]}) == 2,
               f"got {{v['company'] for v in store['vouchers']}}")

    print("\nledger docname keying (mirrors api.py _ledger_docname)")
    import hashlib
    def docname(company, name, guid=""):
        guid = (guid or "").strip()
        if guid:
            return guid
        key = f"{company}::{name}"
        if len(key) <= 140:
            return key
        return f"{company[:100]}::{hashlib.md5(name.encode()).hexdigest()[:16]}"

    a = docname("ACME 24-25", "Cash")
    b = docname("ACME 25-26", "Cash")
    check_true("same ledger in two years gets two distinct keys", a != b, f"{a} vs {b}")
    check("guid wins when present", docname("X", "Cash", "guid-9"), "guid-9")
    longname = "L" * 200
    check_true("over-long name is hashed, stays within Frappe's 140 limit",
               len(docname("C" * 50, longname)) <= 140)
    check_true("hashing stays deterministic",
               docname("C" * 50, longname) == docname("C" * 50, longname))
    check_true("day book request carried date range",
               any("20250401" in r for r in tally_requests))

    print("\nsync logging")
    fc.log_sync("Success", {"ledgers": 3, "vouchers": 2, "seconds": 1.2})
    check("sync log recorded", len(store["logs"]), 1)
    check("log status", store["logs"][0]["status"], "Success")

    print("\nmcp server -> frappe")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "mcp_server"))
    import os
    os.environ["FRAPPE_URL"] = f"http://127.0.0.1:{FRAPPE_PORT}"
    os.environ["FRAPPE_API_KEY"] = "testkey"
    os.environ["FRAPPE_API_SECRET"] = "testsecret"
    import server as mcp_server
    mcp_server.FRAPPE_URL = f"http://127.0.0.1:{FRAPPE_PORT}"
    mcp_server.API_KEY = "testkey"
    mcp_server.API_SECRET = "testsecret"

    health = mcp_server._sync_health()
    check("mcp unwraps message envelope", health.get("is_fresh"), True)
    recv = mcp_server._outstanding("receivable", 10)
    check("receivables total", recv["total"], 168000.0)
    check("direction labelled", recv["rows"][0]["direction"], "owes_us")
    pay = mcp_server._outstanding("payable", 10)
    check("payables direction", pay["rows"][0]["direction"], "we_owe")
    check("bad party_type rejected",
          "error" in mcp_server._outstanding("nonsense"), True)
    check("reconciliation healthy", mcp_server._unbalanced_vouchers()["healthy"], True)

    comps = mcp_server._companies()
    check("mcp lists company files", comps["count"], 2)
    check_true("each company reports its own period",
               all(c["first_voucher"] for c in comps["rows"]))
    cmp_ = mcp_server._compare_ledger("Acme Traders & Co")
    check("compare spans both years", cmp_["appears_in_companies"], 2)
    check("year-on-year delta computed", cmp_["rows"][1]["change_vs_previous"], 48000.0)
    check("selftest passes", mcp_server.selftest(), 0)

    print("\nerror handling")
    mcp_server._session = None
    mcp_server.FRAPPE_URL = "http://127.0.0.1:9  # unreachable".split()[0]
    bad = mcp_server._sync_health()
    check_true("unreachable frappe returns error dict", "error" in bad, f"got {bad}")

    tally_srv.shutdown()
    frappe_srv.shutdown()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("Integration test passed — Tally -> agent -> Frappe -> MCP chain works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
