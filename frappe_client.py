"""
frappe_client.py
-----------------
Thin client for pushing Tally data into the `tally_bridge` Frappe app.

Auth uses a Frappe API key/secret pair (Settings > My Settings > API Access
on your Frappe Cloud site). All calls are outbound HTTPS from your LAN — no
inbound firewall rules needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger("frappe")


class FrappeError(RuntimeError):
    pass


@dataclass
class FrappeConfig:
    url: str            # e.g. https://yourco.frappe.cloud  (no trailing slash)
    api_key: str
    api_secret: str
    timeout: int = 60

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self.api_key}:{self.api_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


class FrappeClient:
    def __init__(self, cfg: FrappeConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(cfg.headers())

    # -- generic helpers ----------------------------------------------------

    def _call(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.cfg.url.rstrip('/')}{path}"
        try:
            resp = self.session.request(method, url, timeout=self.cfg.timeout, **kwargs)
        except requests.RequestException as exc:
            raise FrappeError(f"Cannot reach Frappe at {url}: {exc}") from exc
        ctype = (resp.headers.get("Content-Type") or "").lower()
        body = resp.text or ""
        looks_html = "html" in ctype or body.lstrip()[:15].lower().startswith(
            ("<!doctype", "<html", "<link", "<script")
        )

        # A web page where JSON belongs almost always means the URL points at
        # something other than the site's API — most often the Frappe Cloud
        # dashboard rather than the site itself. Say that in one line instead
        # of printing the whole page.
        if looks_html:
            hint = ""
            if "press/dashboard" in body or "frappecloud" in body:
                hint = (
                    "\n  That is the Frappe Cloud DASHBOARD, not your site.\n"
                    "  In config.toml the url must be your own site, e.g.\n"
                    "      url = \"https://snjpr.nvi.frappe.cloud\"\n"
                    "  not cloud.frappe.io or frappecloud.com."
                )
            elif resp.status_code == 404:
                hint = ("\n  The site answered but has no such endpoint. Is the "
                        "tally_bridge app installed on THIS site?")
            raise FrappeError(
                f"Expected JSON from {url} but got a web page "
                f"(HTTP {resp.status_code}, {len(body)} bytes).{hint}"
            )

        if resp.status_code >= 400:
            raise FrappeError(f"Frappe {resp.status_code} on {path}: {body[:300]}")
        try:
            return resp.json()
        except ValueError:
            raise FrappeError(
                f"Frappe returned a non-JSON response from {path} "
                f"(HTTP {resp.status_code}): {body[:200]}"
            )

    # -- bulk upsert via whitelisted endpoints ------------------------------
    # These hit custom endpoints in the tally_bridge app (see api.py there),
    # which upsert in bulk — far faster than one REST call per document.

    def upsert_ledgers(self, ledgers: list[dict[str, Any]]) -> dict:
        return self._call(
            "POST",
            "/api/method/tally_bridge.api.upsert_ledgers",
            json={"ledgers": ledgers},
        )

    def upsert_vouchers(self, vouchers: list[dict[str, Any]]) -> dict:
        return self._call(
            "POST",
            "/api/method/tally_bridge.api.upsert_vouchers",
            json={"vouchers": vouchers},
        )

    def log_sync(self, status: str, detail: dict[str, Any]) -> None:
        try:
            self._call(
                "POST",
                "/api/method/tally_bridge.api.log_sync",
                json={"status": status, "detail": detail},
            )
        except FrappeError as exc:  # logging must never kill the sync
            log.warning("Could not write sync log to Frappe: %s", exc)

    def get_sync_state(self, company: str = "") -> dict[str, Any]:
        """
        Returns {'last_voucher_date': 'YYYY-MM-DD' | None, ...}.

        Each company file tracks its own high-water mark, so a newly added
        financial year resumes from its own start rather than inheriting
        another year's progress.
        """
        out = self._call(
            "GET", "/api/method/tally_bridge.api.get_sync_state",
            params={"company": company} if company else None,
        )
        return out.get("message", {}) if isinstance(out, dict) else {}

    def ping(self) -> str:
        out = self._call("GET", "/api/method/frappe.auth.get_logged_user")
        return out.get("message", "?") if isinstance(out, dict) else str(out)
