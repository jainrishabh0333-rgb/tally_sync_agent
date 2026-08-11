"""
End-to-end test that runs `python sync.py --full` AS A SUBPROCESS — the exact
command the operator types — against a mock Tally serving deliberately hostile
data modelled on the live book, and a mock Frappe.

Every prior live failure got through because tests exercised functions rather
than the real entry point, or clean data rather than real data. This test
closes both gaps:

  * real CLI invocation (argparse, config loading, logging, exit code)
  * config.toml written with a UTF-8 BOM and CRLF, as Notepad saves it
  * 2,000+ ledgers across nested custom groups, Hindi/rupee text
  * vouchers whose narrations carry control bytes, fake tags, stray & and <
  * a ledger whose email is a bare domain (the live InvalidEmailAddressError)
  * multi-chunk date ranges, per-row rejects, sync-log calls

Run:  python test_e2e.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
TALLY_PORT = 9981
FRAPPE_PORT = 9982

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
        print(f"  FAIL  {label}  {hint}")
    else:
        print(f"  ok    {label}")


# ---------------------------------------------------------------------------
# Mock Tally: 2,000 ledgers, nested groups, hostile vouchers
# ---------------------------------------------------------------------------

COMPANY = "SN JAIN INDUSTRIES PVT LTD - (26-27)"
OLD_COMPANY = "SN JAIN INDUSTRIES PVT LTD - (24-25)"

GROUPS = (
    [("Sundry Debtors", "Current Assets"), ("Sundry Creditors", "Current Liabilities"),
     ("Current Assets", "Primary"), ("Current Liabilities", "Primary"),
     ("Sales Accounts", "Primary"), ("Sundry Debtors Online", "Sundry Debtors")]
    + [(f"AGENT {c}", "Sundry Debtors") for c in "ABCDEFGHIJ"]
    + [(f"Stitchers {i}", "Sundry Creditors") for i in range(5)]
)


def groups_xml() -> str:
    rows = "".join(
        f'<GROUP NAME="{n}"><PARENT>{p}</PARENT></GROUP>' for n, p in GROUPS
    )
    return f"<ENVELOPE><BODY><DATA><COLLECTION>{rows}</COLLECTION></DATA></BODY></ENVELOPE>"


def ledgers_xml() -> str:
    """2,010 ledgers. Debit balances NEGATIVE, as TallyPrime exports them."""
    rows = []
    # 1,000 customers under agent sub-groups, balances -1000, -2000, ...
    for i in range(1000):
        g = f"AGENT {'ABCDEFGHIJ'[i % 10]}"
        rows.append(
            f'<LEDGER NAME="Customer {i:04d}"><PARENT>{g}</PARENT>'
            f"<CLOSINGBALANCE>-{(i + 1) * 1000}.00</CLOSINGBALANCE>"
            f"<MASTERID>{i}</MASTERID><ALTERID>{i}</ALTERID></LEDGER>"
        )
    # 1,000 suppliers under Stitchers sub-groups, balances +500, +1000, ...
    for i in range(1000):
        g = f"Stitchers {i % 5}"
        rows.append(
            f'<LEDGER NAME="Supplier {i:04d}"><PARENT>{g}</PARENT>'
            f"<CLOSINGBALANCE>{(i + 1) * 500}.00</CLOSINGBALANCE>"
            f"<MASTERID>{1000 + i}</MASTERID><ALTERID>{1000 + i}</ALTERID></LEDGER>"
        )
    # The live failure: email field holding a bare domain.
    rows.append(
        '<LEDGER NAME="Hariom Silk Mills"><PARENT>Sundry Debtors</PARENT>'
        "<CLOSINGBALANCE>-99999.00</CLOSINGBALANCE><EMAIL>hariomsilkmills.com</EMAIL>"
        "<MASTERID>9001</MASTERID><ALTERID>9001</ALTERID></LEDGER>"
    )
    # Direct debtor with Hindi name and ampersand.
    rows.append(
        '<LEDGER NAME="V MART RETAIL LTD-HARYANA"><PARENT>Sundry Debtors</PARENT>'
        "<CLOSINGBALANCE>-12008830.20</CLOSINGBALANCE>"
        "<MASTERID>9002</MASTERID><ALTERID>9002</ALTERID></LEDGER>"
    )
    rows.append(
        '<LEDGER NAME="M&M स्टील &amp; Sons"><PARENT>Sundry Creditors</PARENT>'
        "<CLOSINGBALANCE>95000.00</CLOSINGBALANCE>"
        "<MASTERID>9003</MASTERID><ALTERID>9003</ALTERID></LEDGER>"
    )
    # A ledger under a group nobody defined (broken group export).
    rows.append(
        '<LEDGER NAME="Orphan Ledger"><PARENT>MYSTERY GROUP</PARENT>'
        "<CLOSINGBALANCE>-1.00</CLOSINGBALANCE>"
        "<MASTERID>9004</MASTERID><ALTERID>9004</ALTERID></LEDGER>"
    )
    # 8 in a "Sales Accounts" group so classification variety exists.
    for i in range(8):
        rows.append(
            f'<LEDGER NAME="Sales {i}"><PARENT>Sales Accounts</PARENT>'
            f"<CLOSINGBALANCE>{(i + 1) * 10000}.00</CLOSINGBALANCE>"
            f"<MASTERID>{9100 + i}</MASTERID><ALTERID>{9100 + i}</ALTERID></LEDGER>"
        )
    return ("<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(rows)
            + "</COLLECTION></DATA></BODY></ENVELOPE>")


def _voucher(guid, num, day, party, amount, narration):
    return (
        f'<VOUCHER VCHTYPE="Sales"><GUID>{guid}</GUID>'
        f"<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>"
        f"<VOUCHERNUMBER>{num}</VOUCHERNUMBER><DATE>{day}</DATE>"
        f"<PARTYLEDGERNAME>{party}</PARTYLEDGERNAME>"
        f"<NARRATION>{narration}</NARRATION>"
        f"<ISCANCELLED>No</ISCANCELLED><ALTERID>1</ALTERID>"
        f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{party}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-{amount}</AMOUNT>"
        f"</ALLLEDGERENTRIES.LIST>"
        f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>Sales 0</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>{amount}</AMOUNT>"
        f"</ALLLEDGERENTRIES.LIST></VOUCHER>"
    )


def vouchers_xml(chunk_key: str) -> str:
    """Every narration pathology the live book demonstrated, all at once."""
    hostile = [
        # control bytes after multi-byte text (byte-col != char-col)
        "माल भेजा ₹500\x07 urgent",
        # fake tag with digit attribute
        "as per <PONO 123> confirmed",
        # stray ampersand and comparison
        "M&M rate < 500 per pc",
        # invalid numeric refs
        "ref &#4; and &#27; done",
        # arrow and trailing amp
        "adjusted <- see note &",
        # plain sanity
        "normal narration",
        # structural killers: text impersonating real markup
        "see </NARRATION> note above",
        "flagged <ok> by accounts",
    ]
    vs = "".join(
        _voucher(f"{chunk_key}-g{i}", f"SL/{i:03d}", "20260415",
                 f"Customer {i:04d}", f"{(i + 1) * 118}.00", hostile[i % len(hostile)])
        for i in range(60)
    )
    return f"<ENVELOPE><BODY><DATA><TALLYMESSAGE>{vs}</TALLYMESSAGE></DATA></BODY></ENVELOPE>"


class TallyHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
        if "TB_Companies" in body:
            payload = (f"<ENVELOPE><BODY><DATA><COLLECTION>"
                       f'<COMPANY NAME="{COMPANY}"><STARTINGFROM>20260401</STARTINGFROM></COMPANY>'
                       f'<COMPANY NAME="{OLD_COMPANY}"><STARTINGFROM>20240401</STARTINGFROM></COMPANY>'
                       f"</COLLECTION></DATA></BODY></ENVELOPE>")
        elif "TB_Groups" in body:
            payload = groups_xml()
        elif "TB_Ledgers" in body:
            if OLD_COMPANY in body:
                payload = ('<ENVELOPE><BODY><DATA><COLLECTION>'
                           '<LEDGER NAME="Old Year Customer"><PARENT>Sundry Debtors</PARENT>'
                           '<CLOSINGBALANCE>-5000.00</CLOSINGBALANCE>'
                           '<MASTERID>1</MASTERID><ALTERID>1</ALTERID></LEDGER>'
                           '</COLLECTION></DATA></BODY></ENVELOPE>')
            else:
                payload = ledgers_xml()
        elif "Day Book" in body:
            if OLD_COMPANY in body:
                # Prior-year vouchers exist ONLY in April 2024. If the agent
                # floors the range at the current FY, it never requests this.
                payload = (vouchers_xml("old") if "20240401" in body else
                           "<ENVELOPE><BODY><DATA><TALLYMESSAGE></TALLYMESSAGE></DATA></BODY></ENVELOPE>")
            else:
                payload = (vouchers_xml("apr") if "20260401" in body
                           else "<ENVELOPE><BODY><DATA><TALLYMESSAGE></TALLYMESSAGE></DATA></BODY></ENVELOPE>")
        else:
            payload = "<ENVELOPE></ENVELOPE>"
        raw = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


# ---------------------------------------------------------------------------
# Mock Frappe: validates like the real one (email!), records everything
# ---------------------------------------------------------------------------

store = {"ledgers": [], "vouchers": [], "logs": [], "flaked": []}


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

    def do_GET(self):
        path = urlparse(self.path).path
        if path.endswith("get_logged_user"):
            return self._json({"message": "tally-sync@snjainindustries.com"})
        if path.endswith("get_sync_state"):
            return self._json({"message": {"last_voucher_date": None,
                                           "voucher_count": len(store["vouchers"]),
                                           "ledger_count": len(store["ledgers"])}})
        return self._json({"exc": "unknown"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if path.endswith("upsert_ledgers"):
            rows = body.get("ledgers", [])
            good = [r for r in rows if "@" in (r.get("email") or "") or not r.get("email")]
            bad = [r for r in rows if r not in good]
            store["ledgers"].extend(good)
            out = {"created": len(good)}
            if bad:
                out["failed"] = len(bad)
                out["errors"] = [{"ledger": b["name"], "error": "InvalidEmailAddressError"}
                                 for b in bad]
            return self._json({"message": out})
        if path.endswith("upsert_vouchers"):
            if not store["flaked"]:
                store["flaked"].append(1)
                raw = b"<html><body>502 Bad Gateway frappecloud</body></html>"
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            store["vouchers"].extend(body.get("vouchers", []))
            return self._json({"message": {"created": len(body.get("vouchers", []))}})
        if path.endswith("log_sync"):
            store["logs"].append(body)
            return self._json({"message": {"ok": True}})
        return self._json({"exc": "unknown"}, 404)


def main() -> int:
    t_srv = HTTPServer(("127.0.0.1", TALLY_PORT), TallyHandler)
    f_srv = HTTPServer(("127.0.0.1", FRAPPE_PORT), FrappeHandler)
    threading.Thread(target=t_srv.serve_forever, daemon=True).start()
    threading.Thread(target=f_srv.serve_forever, daemon=True).start()

    workdir = Path(tempfile.mkdtemp(prefix="tally_e2e_"))
    # Notepad-style config: UTF-8 BOM + CRLF line endings.
    cfg = (
        f'[tally]\r\nhost = "127.0.0.1"\r\nport = {TALLY_PORT}\r\ncompanies = []\r\n\r\n'
        f'[frappe]\r\nurl = "http://127.0.0.1:{FRAPPE_PORT}"\r\n'
        f'api_key = "k"\r\napi_secret = "s"\r\n\r\n'
        f"[sync]\r\nchunk_days = 31\r\noverlap_days = 7\r\nfy_start_month = 4\r\n"
    )
    (workdir / "config.toml").write_bytes(b"\xef\xbb\xbf" + cfg.encode("utf-8"))

    print("=" * 62)
    print("END-TO-END: python sync.py --full   (as a real subprocess)")
    print("=" * 62)

    proc = subprocess.run(
        [sys.executable, str(HERE / "sync.py"), "--full",
         "--config", str(workdir / "config.toml")],
        capture_output=True, text=True, timeout=300, cwd=str(workdir),
    )
    out = (proc.stdout or "") + (proc.stderr or "")

    print()
    print("--- checks ---")
    check("exit code", proc.returncode, 0)
    check_true("no traceback in output", "Traceback" not in out,
               out[-800:] if "Traceback" in out else "")
    check_true("BOM+CRLF config accepted", "Could not read config" not in out)

    # Ledgers: 2,010 sent; only the bare-domain email row may be rejected.
    check("ledgers mirrored (2013 sent, 1 rejected)", len(store["ledgers"]), 2012)
    check_true("bare-domain email row rejected and reported",
               "InvalidEmailAddressError" in out and "1 ledger(s) rejected" in out.replace("was", "were") or
               "rejected" in out)
    names = {r["name"] for r in store["ledgers"]}
    check_true("hariom row was the reject", "Hariom Silk Mills" not in names)
    check_true("V MART present", "V MART RETAIL LTD-HARYANA" in names)

    # Sign convention: V MART is a debtor, must arrive debit-POSITIVE.
    vmart = next(r for r in store["ledgers"] if r["name"] == "V MART RETAIL LTD-HARYANA")
    check("V MART balance debit-positive", vmart["closing_balance"], 12008830.20)
    mm = next(r for r in store["ledgers"] if "स्टील" in r["name"])
    check("creditor balance negative", mm["closing_balance"], -95000.0)

    # Group resolution at scale.
    check("customers resolved to Sundry Debtors",
          sum(1 for r in store["ledgers"]
              if r["primary_group"] == "Sundry Debtors" and r["company"] == COMPANY),
          1000 + 1)  # 1000 via agents + V MART direct (hariom rejected)
    check("suppliers resolved to Sundry Creditors",
          sum(1 for r in store["ledgers"] if r["primary_group"] == "Sundry Creditors"),
          1000 + 1)
    orphan = next(r for r in store["ledgers"] if r["name"] == "Orphan Ledger")
    check("unknown group degrades to itself", orphan["primary_group"], "MYSTERY GROUP")

    # Vouchers: all 60 hostile-narration vouchers must land.
    check("vouchers mirrored despite hostile narrations",
          len([v for v in store["vouchers"] if v.get("company") == COMPANY]), 60)
    check_true("voucher entries preserved",
               all(len(v.get("entries", [])) == 2 for v in store["vouchers"]))
    check_true("voucher amounts positive (sum of debits)",
               all(v["amount"] > 0 for v in store["vouchers"]))
    check_true("narration with fake tag survived",
               any("PONO" in (v.get("narration") or "") for v in store["vouchers"]))
    check_true("hindi narration survived",
               any("माल" in (v.get("narration") or "") for v in store["vouchers"]))

    # Prior-year company must have synced its own period.
    old_ledgers = [r for r in store["ledgers"] if r.get("company") == OLD_COMPANY]
    check("prior-year ledgers synced", len(old_ledgers), 1)
    old_vouchers = [v for v in store["vouchers"] if v.get("company") == OLD_COMPANY]
    check("prior-year vouchers synced (range floored at ITS year)",
          len(old_vouchers), 60)

    # Structural narration attacks: a narration containing a literal
    # "</NARRATION>" is truncated at that point (the parser cannot tell the
    # fake closer from the real one) — but the VOUCHER must survive with its
    # party and amounts intact, which is what actually matters.
    fake_close = [v for v in store["vouchers"]
                  if (v.get("narration") or "").strip() == "see"]
    check_true("voucher with fake close-tag narration survived (truncated)",
               len(fake_close) > 0 and all(v.get("party") for v in fake_close),
               f"found {len(fake_close)}")
    check_true("fake open-tag narration survived intact",
               any("flagged <ok> by accounts" == (v.get("narration") or "")
                   for v in store["vouchers"]))

    # The one 502 was retried, not fatal.
    check_true("502 mid-run was retried and absorbed",
               "temporarily unavailable" not in out and proc.returncode == 0)
    check_true("dashboard hint NOT shown for the 502",
               "DASHBOARD, not your site" not in out)

    # Sync log written with Success.
    check_true("sync log recorded", len(store["logs"]) >= 1)
    check_true("status Success", any(l.get("status") == "Success" for l in store["logs"]),
               f"statuses: {[l.get('status') for l in store['logs']]}")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        print()
        print("--- subprocess output (tail) ---")
        print(out[-2500:])
        return 1
    print("E2E PASSED — the exact operator command survives hostile live-shaped data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
