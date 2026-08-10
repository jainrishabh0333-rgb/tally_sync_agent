#!/usr/bin/env python3
"""
verify_install.py — prove the `tally_bridge` app really works on the site.

Run this once after installing the app on Frappe Cloud, before pointing the
sync agent at it. Checks, in order:

    1. the site answers at all (DNS, TLS, not a 502 or a parked page)
    2. the API key/secret are accepted, as the expected user
    3. tally_bridge is installed on THIS site, not just present on the bench
    4. all four DocTypes exist and are queryable
    5. every analytics endpoint responds without a server error
    6. the sync key CAN write, and a read-only key (if given) CANNOT

Credentials come from --url/--key/--secret, else the FRAPPE_URL /
FRAPPE_API_KEY / FRAPPE_API_SECRET environment variables, else config.toml
next to this file. The optional read-only key is read from
--readonly-key/--readonly-secret, FRAPPE_READONLY_API_KEY /
FRAPPE_READONLY_API_SECRET, or config.toml ([frappe] readonly_api_key, or the
[mcp] section — in this architecture the MCP server's key is the read-only one).

Nothing here writes real data: the write test posts an empty list.

Usage
-----
    python verify_install.py
    python verify_install.py --url https://yourco.frappe.cloud --key K --secret S
    python verify_install.py --readonly-key K2 --readonly-secret S2
    python verify_install.py --expect-user tally-sync@example.com

Exit code is 0 only when every check passed.
"""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import os
import re
import sys
import textwrap
from pathlib import Path
from urllib.parse import quote

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("The 'requests' package is missing. Run: pip install -r requirements.txt")

try:  # Python 3.11+ has tomllib; fall back to tomli if present.
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore

HERE = Path(__file__).resolve().parent
WIDTH = 78

# Guest-accessible liveness endpoints. The name has moved between Frappe
# versions, so try each — any one of them answering proves a Frappe site.
PING_PATHS = (
    "/api/method/ping",
    "/api/method/frappe.ping",
    "/api/method/frappe.handler.ping",
)
# Keys that only ever appear in a Frappe JSON response, error or not.
FRAPPE_KEYS = ("message", "data", "exc_type", "exception", "_server_messages")

# (DocType, parent DocType if it is a child table)
DOCTYPES = [
    ("Tally Ledger", None),
    ("Tally Voucher", None),
    ("Tally Voucher Entry", "Tally Voucher"),
    ("Tally Sync Log", None),
]

# Analytics endpoints, called with harmless arguments. `expect_error` marks the
# probe that is SUPPOSED to come back with a friendly error rather than data.
BOGUS_LEDGER = "__tally_bridge_probe_no_such_ledger__"
ANALYTICS = [
    ("outstanding", {"party_type": "receivable", "limit": 5}, False),
    ("ledger_statement", {"ledger": BOGUS_LEDGER, "limit": 5}, True),
    ("day_book", {"limit": 5}, False),
    ("trial_balance", {}, False),
    ("summary_by_voucher_type", {}, False),
    ("search_ledgers", {"query": "a", "limit": 5}, False),
    ("unbalanced_vouchers", {"limit": 5}, False),
    ("sync_health", {}, False),
    ("get_sync_state", {}, False),
]

# Repeated advice, kept in one place so the wording stays consistent.
FIX_NOT_INSTALLED = (
    "The app's code is not serving on this site. In Frappe Cloud open your "
    "site > Apps > Add App and pick tally_bridge, then wait for the deploy to "
    "finish. If it is already listed there, the site needs a migrate: "
    "site > Migrate (or 'bench --site <site> migrate' on a self-hosted bench)."
)
FIX_SERVER_ERROR = (
    "The endpoint crashed. Open Frappe desk > Error Log for the traceback. The "
    "usual cause is a missing database table, which a migrate fixes: "
    "site > Migrate in Frappe Cloud."
)
FIX_WRITE_ROLE = (
    "Ingestion is restricted to System Manager. In Frappe desk open User > the "
    "sync user > Roles, tick 'System Manager', save, then re-run this script."
)
FIX_READ_ROLE = (
    "The key is valid but its user cannot read the mirrored data. In Frappe "
    "desk open User > that user > Roles and add a role the Tally DocTypes "
    "grant read to — 'System Manager' for the sync user, 'Accounts User' for "
    "the read-only MCP user."
)
FIX_MISSING_DOCTYPE = (
    "The app is on the bench but this site has no tables for it. Open the site "
    "in Frappe Cloud, go to Apps > Install App and choose tally_bridge, then "
    "run Site > Migrate."
)


# ---------------------------------------------------------------------------
# HTTP — every failure comes back as data, nothing raises at the operator
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Res:
    status: int | None = None      # None means we never got an HTTP response
    body: object = None            # parsed JSON when possible, else raw text
    network_error: str = ""        # non-empty when the request never completed
    exc: object = None             # the requests exception, for classification

    @property
    def ok(self) -> bool:
        return self.status is not None and self.status < 400

    @property
    def message(self):
        """Unwrap Frappe's {"message": ...} envelope."""
        if isinstance(self.body, dict) and "message" in self.body:
            return self.body["message"]
        return self.body

    @property
    def exc_type(self) -> str:
        if isinstance(self.body, dict):
            return str(self.body.get("exc_type") or "")
        return ""

    def is_missing(self) -> bool:
        """404, or any status carrying Frappe's DoesNotExistError."""
        return self.status == 404 or "DoesNotExist" in self.exc_type

    def is_denied(self) -> bool:
        return self.status == 403 or "PermissionError" in self.exc_type


class Api:
    """Thin, non-raising client. Same auth header as frappe_client.py."""

    def __init__(self, url: str, key: str = "", secret: str = "", timeout: int = 30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if key and secret:
            self.session.headers["Authorization"] = f"token {key}:{secret}"

    def request(self, method: str, path: str, params=None, json_body=None) -> Res:
        try:
            resp = self.session.request(
                method, f"{self.url}{path}",
                params=params, json=json_body, timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            return Res(network_error=str(exc), exc=exc)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return Res(status=resp.status_code, body=body)

    def get(self, path: str, params=None) -> Res:
        return self.request("GET", path, params=params)

    def post(self, path: str, json_body=None) -> Res:
        return self.request("POST", path, json_body=json_body)

    def method(self, name: str, params=None) -> Res:
        return self.get(f"/api/method/tally_bridge.api.{name}", params=params)


def clean(text: str, limit: int = 220) -> str:
    """Frappe error text is HTML. Flatten it into one readable line."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(text)))
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def why(res: Res) -> str:
    """Best human-readable explanation of a failed response."""
    if res.network_error:
        return clean(res.network_error)
    body = res.body
    if isinstance(body, str):
        return clean(body) or f"HTTP {res.status} with an empty body"
    if not isinstance(body, dict):
        return f"HTTP {res.status}: {clean(json.dumps(body, default=str))}"

    raw = body.get("_server_messages")
    if raw:
        parts = []
        try:
            for item in json.loads(raw):
                if isinstance(item, str):
                    try:
                        item = json.loads(item)
                    except ValueError:
                        parts.append(item)
                        continue
                if isinstance(item, dict):
                    parts.append(str(item.get("message", item)))
                else:
                    parts.append(str(item))
        except ValueError:
            pass
        if parts:
            return clean("; ".join(parts))

    for key in ("exception", "message", "error", "exc_type"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return clean(value)
    return clean(json.dumps(body, default=str))


def network_fix(res: Res, url: str) -> tuple[str, str]:
    """Turn a requests exception into (what happened, what to do)."""
    exc, text = res.exc, res.network_error.lower()
    if isinstance(exc, requests.exceptions.SSLError):
        return (
            "The HTTPS certificate could not be verified.",
            "If this is a custom domain, its certificate is missing or expired "
            "— check Site > Domains in Frappe Cloud. Try the site's default "
            "*.frappe.cloud address to confirm the site itself is fine.",
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return (
            f"No response within the timeout while connecting to {url}.",
            "The site may be asleep or overloaded. Open the URL in a browser; "
            "if it loads slowly, re-run with a longer --timeout.",
        )
    dns_markers = (
        "name or service not known", "nodename nor servname", "getaddrinfo failed",
        "failed to resolve", "name resolution", "temporary failure in name resolution",
        "nameresolutionerror",
    )
    if any(m in text for m in dns_markers):
        return (
            "The site name does not resolve in DNS.",
            "Check the URL for typos. It should look like "
            "https://yourcompany.frappe.cloud with no trailing slash and no /app "
            "on the end. A brand-new custom domain can take a few hours to "
            "propagate.",
        )
    if "connection refused" in text:
        return (
            "The server refused the connection.",
            "Nothing is listening on that address. Confirm the URL, and that "
            "this machine's firewall or proxy allows outbound HTTPS.",
        )
    if "proxy" in text:
        return (
            "A network proxy rejected the request.",
            "This machine routes through a proxy. Set HTTPS_PROXY correctly, or "
            "ask IT to allow outbound HTTPS to the Frappe site.",
        )
    return (
        f"Could not reach {url}.",
        "Check the URL and this machine's internet connection. The agent only "
        "needs outbound HTTPS (port 443) — no inbound rules.",
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.warned = 0
        self.skipped = 0
        self.failed_steps: list[str] = []
        self.step_title = ""
        self.shown_fixes: list[str] = []

    def step(self, n: int, total: int, title: str) -> None:
        self.step_title = title
        self.shown_fixes = []  # the same advice is only worth printing once per step
        print()
        print(f"[{n}/{total}] {title}")

    def _fix(self, text: str) -> None:
        if not text:
            return
        if text in self.shown_fixes:
            print("        (same fix as above)")
            return
        self.shown_fixes.append(text)
        print(textwrap.fill(
            "What to do: " + text, WIDTH,
            initial_indent="        ", subsequent_indent="        ",
        ))

    def ok(self, msg: str) -> None:
        self.passed += 1
        print(f"  PASS  {msg}")

    def fail(self, msg: str, fix: str = "") -> None:
        self.failed += 1
        if self.step_title and self.step_title not in self.failed_steps:
            self.failed_steps.append(self.step_title)
        print(f"  FAIL  {msg}")
        self._fix(fix)

    def warn(self, msg: str, fix: str = "") -> None:
        self.warned += 1
        print(f"  WARN  {msg}")
        self._fix(fix)

    def skip(self, msg: str) -> None:
        self.skipped += 1
        print(f"  SKIP  {msg}")

    def note(self, msg: str) -> None:
        print(textwrap.fill(msg, WIDTH,
                            initial_indent="        ", subsequent_indent="        "))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Creds:
    url: str = ""
    key: str = ""
    secret: str = ""
    ro_key: str = ""
    ro_secret: str = ""
    expect_user: str = ""
    timeout: int = 30
    source: str = ""


def load_creds(args) -> Creds:
    """Precedence: command line, then environment, then config.toml."""
    data: dict = {}
    cfg_path = args.config or (HERE / "config.toml")
    if cfg_path.exists():
        if tomllib is None:
            sys.exit(
                f"{cfg_path} found but no TOML parser available.\n"
                "Run: pip install tomli   (or use Python 3.11+)"
            )
        try:
            with cfg_path.open("rb") as fh:
                data = tomllib.load(fh)
        except Exception as exc:  # noqa: BLE001 - a broken config must not traceback
            sys.exit(f"Could not read {cfg_path}: {exc}")

    f = data.get("frappe", {}) if isinstance(data.get("frappe"), dict) else {}
    m = data.get("mcp", {}) if isinstance(data.get("mcp"), dict) else {}

    def pick(cli, env, *fallbacks):
        for value in (cli, os.getenv(env, "")) + fallbacks:
            if value:
                return str(value).strip()
        return ""

    creds = Creds(
        url=pick(args.url, "FRAPPE_URL", f.get("url")),
        key=pick(args.key, "FRAPPE_API_KEY", f.get("api_key")),
        secret=pick(args.secret, "FRAPPE_API_SECRET", f.get("api_secret")),
        ro_key=pick(args.readonly_key, "FRAPPE_READONLY_API_KEY",
                    f.get("readonly_api_key"), m.get("api_key")),
        ro_secret=pick(args.readonly_secret, "FRAPPE_READONLY_API_SECRET",
                       f.get("readonly_api_secret"), m.get("api_secret")),
        expect_user=pick(args.expect_user, "FRAPPE_EXPECT_USER", f.get("user")),
        timeout=args.timeout,
        source=str(cfg_path) if data else "command line / environment",
    )

    missing = [n for n, v in (("url", creds.url), ("api key", creds.key),
                              ("api secret", creds.secret)) if not v]
    if missing:
        sys.exit(
            "Missing the Frappe " + ", ".join(missing) + ".\n"
            f"Either fill in {cfg_path} (copy config.example.toml), set "
            "FRAPPE_URL / FRAPPE_API_KEY / FRAPPE_API_SECRET, or pass "
            "--url --key --secret.\n"
            "Keys are generated in Frappe desk: your user > Settings > "
            "API Access > Generate Keys."
        )

    if "://" not in creds.url:
        creds.url = "https://" + creds.url
    creds.url = creds.url.rstrip("/")
    for suffix in ("/app", "/api"):
        if creds.url.endswith(suffix):
            creds.url = creds.url[: -len(suffix)]
    return creds


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def is_frappe_json(res: Res) -> bool:
    return isinstance(res.body, dict) and any(k in res.body for k in FRAPPE_KEYS)


def check_site(rep: Report, api: Api, url: str) -> bool:
    """
    1. Is there a live Frappe site at this address?

    Probed WITHOUT credentials, so a bad API key cannot be mistaken for a dead
    site. Frappe rejects invalid keys on guest endpoints too, which is why a
    401 here still counts as "the site is alive" — check 2 judges the key.
    """
    res = Res()
    for path in PING_PATHS:
        res = api.get(path)
        if res.network_error or res.status in (502, 503, 504):
            break
        if res.ok and res.message == "pong":
            break
        if res.status in (401, 403) and is_frappe_json(res):
            break

    if res.network_error:
        what, fix = network_fix(res, url)
        rep.fail(what, fix)
        return False

    if res.status in (502, 503, 504):
        rep.fail(
            f"{url} returned HTTP {res.status} — the site is not serving.",
            "On Frappe Cloud this usually means the site is suspended, asleep, "
            "or mid-deploy. Open the site in the Frappe Cloud dashboard and "
            "check its status, then re-run this script.",
        )
        return False

    alive = (res.ok and res.message == "pong") or is_frappe_json(res)
    if alive:
        rep.ok(f"{url} is a live Frappe site (HTTP {res.status}).")
        if url.startswith("http://"):
            rep.warn(
                "The URL is plain http:// — the API key would travel unencrypted.",
                "Use the https:// address of the site.",
            )
        return True

    if res.status == 404:
        rep.fail(
            f"{url} answered HTTP 404 and not like Frappe.",
            "That address is serving something, but it is not a Frappe site "
            "(often a parked domain or the wrong host). Double-check the URL — "
            "it should be the site root, e.g. https://yourcompany.frappe.cloud.",
        )
        return False

    if res.ok:
        # 200, but not Frappe's "pong" — a holding page or a proxy in the way.
        rep.fail(
            f"{url} replied HTTP {res.status}, but not like a Frappe site.",
            "This looks like a parked or placeholder page. On Frappe Cloud an "
            "address that has not been provisioned yet still answers, with the "
            "dashboard page — so check the site really exists and that this is "
            "its exact URL, with no /app or trailing path.",
        )
        rep.note("Got instead: " + clean(why(res), 120))
        return False

    rep.fail(f"{url} returned HTTP {res.status}. {why(res)}",
             "Open that URL in a browser to see what the site is doing.")
    return False


def check_auth(rep: Report, api: Api, expect_user: str) -> str:
    """2. Are the API key and secret accepted, and by whom?"""
    res = api.get("/api/method/frappe.auth.get_logged_user")
    if res.network_error:
        what, fix = network_fix(res, api.url)
        rep.fail(what, fix)
        return ""

    if res.status in (401, 403) or res.is_denied():
        rep.fail(
            f"Frappe rejected the credentials (HTTP {res.status}).",
            "The api_key/api_secret pair is wrong, expired, or belongs to a "
            "disabled user. In Frappe desk go to User > the sync user > "
            "Settings > API Access > Generate Keys, and copy BOTH values into "
            "config.toml. The secret is shown only once.",
        )
        return ""

    if not res.ok:
        rep.fail(f"Auth check returned HTTP {res.status}. {why(res)}",
                 "Re-run in a minute; if it persists, check Frappe's Error Log.")
        return ""

    user = res.message if isinstance(res.message, str) else ""
    if not user or user == "Guest":
        rep.fail(
            "The site answered as 'Guest' — the API key was ignored.",
            "Something is stripping the Authorization header, usually a proxy "
            "or a URL that redirects. Use the site's canonical https:// URL.",
        )
        return ""

    if expect_user and user.lower() != expect_user.lower():
        rep.fail(
            f"Authenticated as '{user}', but expected '{expect_user}'.",
            "These keys belong to a different user than intended. Generate the "
            "keys again from the correct user's record, or drop --expect-user "
            "if this user is in fact the right one.",
        )
        return user

    rep.ok(f"Authenticated as '{user}'.")
    if user == "Administrator":
        rep.warn(
            "That is the Administrator account.",
            "Prefer a dedicated sync user with the System Manager role, so the "
            "key can be revoked without locking anyone out.",
        )
    return user


def check_app_installed(rep: Report, api: Api) -> bool:
    """3. Is tally_bridge installed on THIS site, not just built on the bench?"""
    res = api.get("/api/method/frappe.utils.change_log.get_versions")
    if res.ok and isinstance(res.message, dict):
        apps = res.message
        if "tally_bridge" in apps:
            info = apps["tally_bridge"] if isinstance(apps["tally_bridge"], dict) else {}
            version = info.get("version") or "unknown version"
            branch = info.get("branch")
            rep.ok(f"tally_bridge is installed on this site ({version}"
                   + (f", branch {branch}" if branch else "") + ").")
            return True
        rep.fail(
            "tally_bridge is NOT installed on this site.",
            FIX_NOT_INSTALLED,
        )
        rep.note("Apps this site does have: " + ", ".join(sorted(apps)))
        return False

    # Fall back to the Installed Application child table of Installed Applications.
    res = api.get(
        "/api/resource/" + quote("Installed Application"),
        params={"parent": "Installed Applications",
                "fields": '["app_name"]', "limit_page_length": 0},
    )
    if res.ok and isinstance(res.body, dict) and isinstance(res.body.get("data"), list):
        names = [r.get("app_name") for r in res.body["data"] if isinstance(r, dict)]
        if "tally_bridge" in names:
            rep.ok("tally_bridge is listed in this site's installed applications.")
            return True
        rep.fail("tally_bridge is NOT in this site's installed applications.",
                 FIX_NOT_INSTALLED)
        rep.note("Apps this site does have: " + ", ".join(sorted(n for n in names if n)))
        return False

    # Last resort: one of the app's own DocTypes existing proves it installed here.
    res = api.get("/api/resource/DocType/" + quote("Tally Ledger"))
    if res.ok:
        rep.ok("tally_bridge is installed (its DocTypes exist on this site).")
        rep.note("Could not read the app list directly; inferred from the DocTypes.")
        return True
    if res.is_missing():
        rep.fail("tally_bridge is NOT installed on this site.", FIX_NOT_INSTALLED)
        return False

    rep.fail(f"Could not determine whether the app is installed. {why(res)}",
             "Check the app list by hand: Frappe desk > Help (?) > About.")
    return False


def check_doctypes(rep: Report, api: Api) -> bool:
    """4. Do the four DocTypes exist, and can this key query them?"""
    all_ok = True
    for doctype, parent in DOCTYPES:
        meta = api.get("/api/resource/DocType/" + quote(doctype))
        if meta.network_error:
            rep.fail(f"{doctype}: lost the connection mid-check. {why(meta)}")
            all_ok = False
            continue
        if meta.is_missing():
            rep.fail(f"{doctype}: does not exist on this site.", FIX_MISSING_DOCTYPE)
            all_ok = False
            continue

        params = {"limit_page_length": 1}
        if parent:
            params["parent"] = parent  # child tables are only queryable via a parent
        rows = api.get("/api/resource/" + quote(doctype), params=params)

        if rows.network_error:
            rep.fail(f"{doctype}: lost the connection mid-check. {why(rows)}")
            all_ok = False
        elif rows.ok:
            data = rows.body.get("data") if isinstance(rows.body, dict) else None
            count = len(data) if isinstance(data, list) else 0
            state = f"{count} row(s) returned" if count else "queryable, currently empty"
            rep.ok(f"{doctype}: present and {state}.")
        elif rows.is_missing():
            rep.fail(
                f"{doctype}: the DocType record exists but the list endpoint "
                "returned 404.",
                "The app is half-installed. Run Site > Migrate, then re-run this "
                "script.",
            )
            all_ok = False
        elif rows.is_denied():
            if parent:
                # Expected: a child table is readable only through its parent.
                rep.ok(f"{doctype}: present (child table, read via {parent}).")
            else:
                rep.fail(f"{doctype}: exists, but this key may not read it "
                         f"(HTTP {rows.status}).", FIX_READ_ROLE)
                all_ok = False
        elif rows.status is not None and rows.status >= 500:
            rep.fail(
                f"{doctype}: the query crashed with HTTP {rows.status}. {why(rows)}",
                "The DocType is defined but its database table is missing. Run "
                "Site > Migrate in Frappe Cloud, then re-run this script.",
            )
            all_ok = False
        else:
            rep.fail(f"{doctype}: unexpected HTTP {rows.status}. {why(rows)}",
                     "Check Frappe desk > Error Log for the matching entry.")
            all_ok = False
    return all_ok


def check_analytics(rep: Report, api: Api) -> bool:
    """5. Does every read-only endpoint answer without blowing up?"""
    all_ok = True
    for name, params, expect_error in ANALYTICS:
        res = api.method(name, params=params)

        if res.network_error:
            rep.fail(f"{name}: lost the connection. {why(res)}")
            all_ok = False
            continue

        if res.is_missing():
            rep.fail(f"{name}: no such method on this site (HTTP {res.status}).",
                     FIX_NOT_INSTALLED)
            all_ok = False
            continue

        if res.is_denied():
            rep.fail(f"{name}: refused for this key (HTTP {res.status}). {why(res)}",
                     FIX_READ_ROLE)
            all_ok = False
            continue

        if res.status is not None and res.status >= 500:
            rep.fail(f"{name}: server error HTTP {res.status}. {why(res)}",
                     FIX_SERVER_ERROR)
            all_ok = False
            continue

        if not res.ok:
            rep.fail(f"{name}: HTTP {res.status}. {why(res)}", FIX_SERVER_ERROR)
            all_ok = False
            continue

        body = res.message
        if expect_error:
            # A bogus ledger must produce a helpful answer, never a crash.
            if isinstance(body, dict) and body.get("error"):
                rep.ok(f"{name}: returned a helpful error for an unknown ledger, "
                       "not a crash.")
            else:
                rep.ok(f"{name}: responded (HTTP {res.status}).")
            continue

        if isinstance(body, dict) and body.get("error"):
            rep.fail(f"{name}: responded with an error. {clean(body['error'])}",
                     FIX_SERVER_ERROR)
            all_ok = False
            continue

        rep.ok(f"{name}: OK ({summarise(body)}).")
    return all_ok


def summarise(body) -> str:
    """One-glance description of an endpoint's payload."""
    if isinstance(body, dict):
        for key in ("count", "voucher_count", "total_debit"):
            if key in body:
                return f"{key}={body[key]}"
        rows = body.get("rows")
        if isinstance(rows, list):
            return f"{len(rows)} row(s)"
        return f"{len(body)} field(s)"
    if isinstance(body, list):
        return f"{len(body)} item(s)"
    return "responded"


def check_write(rep: Report, api: Api, ro: Api | None, ro_same: bool = False) -> bool:
    """6. The two-user model: the sync key writes, the read-only key must not."""
    all_ok = True

    # Empty list is a genuine no-op: the endpoint loops over nothing.
    res = api.post("/api/method/tally_bridge.api.upsert_ledgers", json_body={"ledgers": []})
    if res.network_error:
        rep.fail(f"Write test: lost the connection. {why(res)}")
        all_ok = False
    elif res.ok:
        rep.ok("Sync key CAN write (upsert_ledgers accepted an empty batch; "
               "no data was created).")
    elif res.is_denied():
        rep.fail(
            f"Sync key CANNOT write (HTTP {res.status}). {why(res)}",
            FIX_WRITE_ROLE + " Without it the agent fails on its first push.",
        )
        all_ok = False
    elif res.is_missing():
        rep.fail(f"Write test: upsert_ledgers does not exist (HTTP {res.status}).",
                 FIX_NOT_INSTALLED)
        all_ok = False
    else:
        rep.fail(f"Write test: HTTP {res.status}. {why(res)}", FIX_SERVER_ERROR)
        all_ok = False

    if ro is None:
        if ro_same:
            rep.skip("Read-only key not tested: the same key was given for both "
                     "roles, so there is no second user to check.")
        else:
            rep.skip("No read-only key supplied, so the MCP server's key was not "
                     "tested. Pass --readonly-key/--readonly-secret to check it.")
        return all_ok

    probe = ro.get("/api/method/frappe.auth.get_logged_user")
    ro_user = probe.message if probe.ok and isinstance(probe.message, str) else ""
    if not ro_user or ro_user == "Guest":
        rep.fail(
            "The read-only key was not accepted by the site, so its write "
            f"restriction could not be proven. {why(probe)}",
            "Regenerate that key (Frappe desk > the MCP user > Settings > API "
            "Access), or leave --readonly-key off until it works.",
        )
        return False
    rep.ok(f"Read-only key authenticates as '{ro_user}'.")

    res = ro.post("/api/method/tally_bridge.api.upsert_ledgers", json_body={"ledgers": []})
    if res.network_error:
        rep.fail(f"Read-only write test: lost the connection. {why(res)}")
        return False
    if res.is_denied():
        rep.ok(f"Read-only key CANNOT write (HTTP {res.status}) — the two-user "
               "security model holds.")
        return all_ok
    if res.ok:
        rep.fail(
            f"Read-only key CAN write. '{ro_user}' is not actually read-only.",
            "That key is what Claude uses through the MCP server, so it must "
            "never be able to change the books. In Frappe desk open User > "
            f"{ro_user} > Roles and remove 'System Manager', leaving only a "
            "read role such as 'Accounts User'. Then re-run this script.",
        )
        return False
    rep.warn(
        f"Read-only key was rejected with HTTP {res.status} rather than a clean "
        f"permission error. {why(res)}",
        "Probably still safe, but confirm the MCP user's roles by hand.",
    )
    return all_ok


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Verify that the tally_bridge app is installed and working.",
    )
    p.add_argument("--url", default="", help="https://yourcompany.frappe.cloud")
    p.add_argument("--key", default="", help="API key of the sync (write) user")
    p.add_argument("--secret", default="", help="API secret of the sync user")
    p.add_argument("--readonly-key", default="", help="optional read-only API key")
    p.add_argument("--readonly-secret", default="", help="optional read-only API secret")
    p.add_argument("--expect-user", default="", help="user the sync key should map to")
    p.add_argument("--config", type=Path, default=None, help="path to config.toml")
    p.add_argument("--timeout", type=int, default=30, help="seconds per request")
    args = p.parse_args()

    # A console that cannot render a dash must not kill the run.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    creds = load_creds(args)
    total = 6

    print("=" * WIDTH)
    print("  Tally Bridge — installation check")
    print("=" * WIDTH)
    print(f"  Site:   {creds.url}")
    print(f"  Config: {creds.source}")

    rep = Report()
    api = Api(creds.url, creds.key, creds.secret, creds.timeout)

    rep.step(1, total, "Site reachable")
    anon = Api(creds.url, timeout=creds.timeout)  # no key: liveness, not access
    if not check_site(rep, anon, creds.url):
        return finish(rep, total, stopped_at=1)

    rep.step(2, total, "Authentication")
    user = check_auth(rep, api, creds.expect_user)
    if not user:
        return finish(rep, total, stopped_at=2)

    rep.step(3, total, "App installed on this site")
    installed = check_app_installed(rep, api)

    rep.step(4, total, "DocTypes exist and are queryable")
    doctypes_ok = check_doctypes(rep, api)

    rep.step(5, total, "Analytics endpoints respond")
    if installed or doctypes_ok:
        check_analytics(rep, api)
    else:
        rep.skip("The app is not installed, so there is nothing to call yet.")

    rep.step(6, total, "Write permissions (two-user security model)")
    ro = None
    ro_same = bool(creds.ro_key) and creds.ro_key == creds.key
    if ro_same:
        rep.warn(
            "The read-only key is the same as the sync key.",
            "Create a separate Frappe user for the MCP server with read-only "
            "roles, so Claude can never write to the books.",
        )
    elif creds.ro_key and creds.ro_secret:
        ro = Api(creds.url, creds.ro_key, creds.ro_secret, creds.timeout)
    check_write(rep, api, ro, ro_same)

    return finish(rep, total)


def finish(rep: Report, total: int, stopped_at: int = 0) -> int:
    print()
    print("=" * WIDTH)
    counts = f"  {rep.passed} passed, {rep.failed} failed"
    if rep.warned:
        counts += f", {rep.warned} warning(s)"
    if rep.skipped:
        counts += f", {rep.skipped} skipped"
    print(counts)

    if stopped_at:
        print(f"  Stopped at check {stopped_at} of {total} — later checks cannot "
              "run until it passes.")

    if rep.failed:
        print()
        print("  NOT ready to sync. Problem area(s):")
        for title in rep.failed_steps:
            print(f"    - {title}")
        print("  Fix the FAIL items above, then run this script again:")
        print("      python verify_install.py")
        print("=" * WIDTH)
        return 1

    print()
    print("  All checks passed. The site is ready for the sync agent.")
    if rep.warned:
        print("  The warning(s) above are worth a look, but nothing is blocking.")
    print("  Next: python sync.py --check, then python sync.py --full")
    print("=" * WIDTH)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
