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
        if resp.status_code >= 400:
            raise FrappeError(f"Frappe {resp.status_code} on {path}: {resp.text[:500]}")
        try:
            return resp.json()
        except ValueError:
            return resp.text

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

    def get_sync_state(self) -> dict[str, Any]:
        """Returns {'last_voucher_date': 'YYYY-MM-DD' | None, ...}."""
        out = self._call("GET", "/api/method/tally_bridge.api.get_sync_state")
        return out.get("message", {}) if isinstance(out, dict) else {}

    def ping(self) -> str:
        out = self._call("GET", "/api/method/frappe.auth.get_logged_user")
        return out.get("message", "?") if isinstance(out, dict) else str(out)
