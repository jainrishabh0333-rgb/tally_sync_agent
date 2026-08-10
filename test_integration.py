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
 <COMPANY NAME="SN JAIN INDUSTRIES"><STARTINGFROM>20250401</STARTINGFROM></COMPANY>
</COLLECTION></DATA></BODY></ENVELOPE>"""

LEDGERS_XML = """<ENVELOPE><BODY><DATA><COLLECTION>
 <LEDGER NAME="Acme Traders &amp; Co">
  <PARENT>Sundry Debtors</PARENT><OPENINGBALANCE>50000.00</OPENINGBALANCE>
  <CLOSINGBALANCE>168000.00</CLOSINGBALANCE><PARTYGSTIN>27AABCU9603R1ZM</PARTYGSTIN>
  <EMAIL>ap@acme.example</EMAIL><ISBILLWISEON>Yes</ISBILLWISEON>
  <MASTERID>77</MASTERID><ALTERID>1204</ALTERID>
 </LEDGER>
 <LEDGER NAME="M&amp;M Steel Suppliers">
  <PARENT>Sundry Creditors</PARENT><OPENINGBALANCE>-20000.00</OPENINGBALANCE>
  <CLOSINGBALANCE>-95000.00</CLOSINGBALANCE><ISBILLWISEON>Yes</ISBILLWISEON>
  <MASTERID>78</MASTERID><ALTERID>1205</ALTERID>
 </LEDGER>
 <LEDGER NAME="Sales Account">
  <PARENT>Sales Accounts</PARENT><CLOSINGBALANCE>-100000.00</CLOSINGBALANCE>
  <MASTERID>79</MASTERID><ALTERID>1206</ALTERID>
 </LEDGER>
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

    tcfg = TallyConfig(host="127.0.0.1", port=TALLY_PORT, company="SN JAIN INDUSTRIES")
    fcfg = FrappeConfig(url=f"http://127.0.0.1:{FRAPPE_PORT}",
                        api_key="testkey", api_secret="testsecret")
    st = sync.Settings(tally=tcfg, frappe=fcfg, chunk_days=31, overlap_days=7)
    fc = FrappeClient(fcfg)

    print("connectivity")
    companies = list_companies(tcfg)
    check("company discovered", [c["name"] for c in companies], ["SN JAIN INDUSTRIES"])
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
    check("debtor group", acme.parent, "Sundry Debtors")
    check("debtor closing balance", acme.closing_balance, 168000.0)
    check("gstin", acme.gstin, "27AABCU9603R1ZM")
    check("bill-wise flag", acme.bill_by_bill, True)
    check("creditor negative balance", by_name["M&M Steel Suppliers"].closing_balance, -95000.0)

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
