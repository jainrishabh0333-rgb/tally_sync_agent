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
import time
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

        # Frappe Cloud restarts workers routinely, so a lone 502/503 or a
        # dropped keep-alive connection is normal weather, not a failure.
        # Retrying is safe here: the upsert endpoints are GUID-keyed and
        # idempotent by design. Without this, one platform hiccup costs the
        # operator an entire run.
        resp = None
        last_exc: Exception | None = None
        for attempt in range(4):
            if attempt:
                wait = 2 ** attempt          # 2s, 4s, 8s
                log.warning("Frappe unavailable (%s), retrying in %ds "
                            "(attempt %d/4)...",
                            last_exc or f"HTTP {resp.status_code}", wait, attempt + 1)
                time.sleep(wait)
            try:
                resp = self.session.request(method, url,
                                            timeout=self.cfg.timeout, **kwargs)
                last_exc = None
            except requests.RequestException as exc:
                last_exc = exc
                continue
            if resp.status_code not in (502, 503, 504):
                break
        if last_exc is not None:
            raise FrappeError(f"Cannot reach Frappe at {url}: {last_exc}") from last_exc
        assert resp is not None
        if resp.status_code in (502, 503, 504):
            raise FrappeError(
                f"Frappe is temporarily unavailable (HTTP {resp.status_code} "
                f"after 4 attempts). The site is likely restarting — wait a "
                "minute and run again; already-synced data is kept."
            )
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
            if resp.status_code < 400 and ("press/dashboard" in body or "frappecloud" in body):
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

    def upsert_bills(self, bills: list[dict[str, Any]], company: str,
                     replace: int = 1) -> dict:
        """
        Send one batch of bills.

        `replace` clears the company's existing snapshot first and must be set
        on the FIRST batch only — passing it on every batch would leave just
        the last batch standing.
        """
        return self._call(
            "POST",
            "/api/method/tally_bridge.api.upsert_bills",
            json={"bills": bills, "company": company,
                  "replace": 1 if replace else 0},
        )

    def upsert_inventory(self, company: str, **payload) -> dict:
        return self._call(
            "POST",
            "/api/method/tally_bridge.api.upsert_inventory",
            json={"company": company, **payload},
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

    # -- sales-order queue (the ONE write path towards Tally) ---------------
    # Approved orders sit in a queue DocType on Frappe; order_importer.py on
    # the Tally server drains it. Both calls below ride _call's retry loop,
    # and that is SAFE for them specifically: reading the queue changes
    # nothing, and writing the same status twice lands in the same place.
    # The caveat lives one layer up — the Tally IMPORT itself must never be
    # retried blindly (a lost response may mean the voucher already exists),
    # which is why order_importer.py sends each envelope exactly once and
    # only records the outcome here.

    def get_pending_sales_orders(self, company: str = "") -> list:
        """Orders with status=Pending, oldest first. Read-only, retry-safe."""
        # No query params: the endpoint takes none (an unexpected kwarg is a
        # server-side error in Frappe, not an ignored extra). The company
        # filter is applied here instead.
        out = self._call(
            "GET", "/api/method/tally_bridge.api.pending_sales_orders",
        )
        msg = out.get("message", out) if isinstance(out, dict) else out
        if isinstance(msg, dict):
            stuck = msg.get("stuck_importing") or 0
            if stuck:
                # An Importing row over 30 minutes old means an importer died
                # mid-send. Deliberately NOT auto-retried — Tally may or may
                # not hold the voucher — so make sure a human hears about it.
                log.warning("%d order(s) stuck in Importing for over 30 "
                            "minutes — an import died mid-send. Check Tally "
                            "for those orders before re-queueing them.", stuck)
            # The endpoint returns the list under "rows". The first cut of
            # this method read "orders" and silently saw an empty queue
            # forever — reading BOTH keys keeps either side free to be fixed
            # without re-breaking the other.
            msg = msg.get("rows") or msg.get("orders") or []
        rows = msg if isinstance(msg, list) else []
        if company:
            rows = [r for r in rows if (r.get("company") or "") == company]
        return rows

    def mark_order_result(self, order_key: str, status: str,
                          error: str = "", tally_vch_no: str = "") -> dict:
        """
        Record an order's state transition (Importing / Imported / Failed).

        Idempotent on the Frappe side (keyed by order_key), so _call's
        transient-error retries cannot double anything. `error` carries the
        failure text shown to the operator; `tally_vch_no` the voucher
        number/id Tally reported on success.
        """
        # The endpoint's parameter is `tally_vch_number`. This used to send
        # `tally_vch_no`, which Frappe's argument filtering silently DROPS —
        # every import was recorded without its Tally voucher number, and
        # nothing errored to say so.
        return self._call(
            "POST", "/api/method/tally_bridge.api.mark_order_result",
            json={"order_key": order_key, "status": status,
                  "error": error, "tally_vch_number": tally_vch_no},
        )

    # -- distributor mirror -------------------------------------------------

    def upsert_sales_orders(self, orders: list[dict[str, Any]]) -> dict:
        return self._call(
            "POST", "/api/method/tally_bridge.api.upsert_sales_orders",
            json={"orders": orders},
        )

    def upsert_invoices(self, invoices: list[dict[str, Any]]) -> dict:
        return self._call(
            "POST", "/api/method/tally_bridge.api.upsert_invoices",
            json={"invoices": invoices},
        )

    def upsert_delivery_notes(self, notes: list[dict[str, Any]]) -> dict:
        return self._call(
            "POST", "/api/method/tally_bridge.api.upsert_delivery_notes",
            json={"notes": notes},
        )

    def upsert_receipts(self, receipts: list[dict[str, Any]]) -> dict:
        return self._call(
            "POST", "/api/method/tally_bridge.api.upsert_receipts",
            json={"receipts": receipts},
        )

    def upsert_stock_batches(self, batches: list[dict[str, Any]],
                             company: str) -> dict:
        return self._call(
            "POST", "/api/method/tally_bridge.api.upsert_stock_batches",
            json={"batches": batches, "company": company},
        )

    def ping(self) -> str:
        out = self._call("GET", "/api/method/frappe.auth.get_logged_user")
        return out.get("message", "?") if isinstance(out, dict) else str(out)
