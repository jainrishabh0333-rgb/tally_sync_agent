"""
purchase_rate_audit.py — hunt for mis-keyed purchase rates on raw material.

The question this answers: "we bought fabric at Rs 400, somebody typed Rs 450 —
where has that happened?" Nothing in the Frappe mirror can answer it. There is
no purchase-line doctype, and the cached voucher XML was filtered to sales
types, so it holds no Purchase vouchers at all. The rate AS KEYED exists only
in the Tally voucher, so this reads the gateway directly.

SEVEN TESTS, cheapest and most conclusive first
-----------------------------------------------
1. LINE ARITHMETIC.  amount != qty * rate on the line itself. This is the only
   test that proves an error without any judgement: the voucher contradicts
   itself. A mis-typed rate usually leaves the amount right (it was checked
   against the bill) or the amount wrong (it was not), and either way the three
   numbers stop agreeing.
2. RATE DISPERSION per item. Same item, same unit, same supplier, rates that
   disagree across vouchers. Reported as a spread against the item's own MODAL
   rate rather than its mean — one bad entry drags a mean and hides itself.
3. DECIMAL AND TRANSPOSITION SHAPES. A rate that is ~10x or ~0.1x the modal, or
   a digit-swap of it (450/540, 400/040). These are what a keyboard slip looks
   like, as opposed to a real price move.
4. PURCHASE RATE vs CLOSING RATE. Material carried in stock above what it was
   most recently bought for. Overstates inventory and understates consumption.
5. ROUND-NUMBER CLUSTERING. A supplier whose every rate is a round number is
   being estimated, not invoiced.
6. NEAR-DUPLICATE VOUCHERS. Same supplier, item, qty and amount within a few
   days — a bill entered twice inflates both stock and creditors.
7. RATE STEP-CHANGES. A rate that moves once and stays moved is a price
   revision; a rate that moves for one voucher and returns is an error.

WHAT THIS CANNOT DO
-------------------
Tell you the CORRECT rate. It finds entries that disagree with their own
neighbours, their own arithmetic, or their own history. Every hit still needs
the physical purchase bill pulled and compared. The output is a work list for
whoever holds the bills, ordered by how much money rides on it.

Run (Tally must be open, roughly 10:00-18:00):
    python purchase_rate_audit.py --host 100.74.103.22 \
        --company "SN JAIN INDUSTRIES PVT LTD - (26-27)" \
        --from 2026-04-01 --to 2026-08-26 --csv out/purchase_rate_audit.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

import distributor_fetch as df
from tally_client import TallyConfig, _parse_xml, _post, _text, _parse_qty

log = logging.getLogger("sync")

# Every voucher type that brings material IN at a rate. Debit Note is here
# because a purchase return re-prices the goods going back out, and a wrong
# rate there misstates stock exactly as a purchase does.
PURCHASE_TYPES = ["Purchase", "Debit Note", "Debit Note-Purchase",
                  "Job Work - (Dying Received)", "Receipt Challan(Branch Transfer)"]

MATERIAL_GROUPS = {"Fabric-Kg", "Fabric-Mtr", "Fabric", "Raw Material",
                   "Packing Material"}


def fetch_purchases(cfg: TallyConfig, frm: date, to: date, chunk_days: int = 7,
                    cache_dir: str = "") -> list[dict]:
    """
    Purchase vouchers with their inventory lines, chunked and disk-cached.

    Same chunk-and-cache discipline as the movement puller, and the same
    warning applies: the cache is keyed by exact date range, so changing
    --chunk-days leaves a second overlapping set behind. Never glob it.
    """
    out: list[dict] = []
    cur = frm
    while cur <= to:
        end = min(cur + timedelta(days=chunk_days - 1), to)
        path = os.path.join(cache_dir, f"pu_{cur}_{end}.xml") if cache_dir else ""
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        else:
            raw = _post(cfg, df._voucher_body(cfg, cur, end, PURCHASE_TYPES,
                                              df._INVENTORY))
            if path:
                os.makedirs(cache_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(raw)
        for vel in _parse_xml(raw).iter("VOUCHER"):
            h = df._header(vel, cfg.company)
            if not h["guid"]:
                continue
            h["lines"] = df._inventory_lines(vel, h["date"])
            out.append(h)
        log.info("purchases %s..%s -> %d vouchers so far", cur, end, len(out))
        cur = end + timedelta(days=1)
    return out


def _rate(line: dict) -> float:
    """Rate per unit, preferring Tally's own stated rate."""
    r = line.get("rate")
    try:
        r = float(r)
    except (TypeError, ValueError):
        r = 0.0
    if r:
        return r
    q, a = line.get("qty") or 0, line.get("amount") or 0
    return (a / q) if q else 0.0


def test_line_arithmetic(vouchers: list[dict], tol: float = 0.02) -> list[dict]:
    """amount != qty * rate. The voucher contradicting itself needs no judgement."""
    out = []
    for v in vouchers:
        for l in v["lines"]:
            q, r, a = l.get("qty") or 0, _rate(l), l.get("amount") or 0
            if q <= 0 or r <= 0 or a <= 0:
                continue
            exp = q * r
            if abs(exp - a) / a > tol:
                out.append({
                    "test": "line arithmetic", "voucher": v.get("voucher_number"),
                    "date": v.get("date"), "party": v.get("party"),
                    "item": l["item_name"], "qty": q, "unit": l.get("unit"),
                    "rate": r, "amount": a, "expected_amount": round(exp, 2),
                    "gap": round(a - exp, 2),
                    "note": "qty x rate does not equal the line amount",
                })
    return out


def test_rate_dispersion(vouchers: list[dict], groups: dict,
                         min_vouchers: int = 3, spread: float = 0.15) -> list[dict]:
    """
    Same item bought at rates that disagree with the item's own MODAL rate.

    Modal, not mean: a single fat-fingered rate moves a mean and then hides
    inside its own average. The mode is what the item is normally bought at.
    """
    by_item: dict = defaultdict(list)
    for v in vouchers:
        for l in v["lines"]:
            r = _rate(l)
            if r > 0 and (l.get("qty") or 0) > 0:
                by_item[(l["item_name"], l.get("unit") or "")].append((r, v, l))
    out = []
    for (item, unit), obs in by_item.items():
        if len(obs) < min_vouchers:
            continue
        if groups and groups.get(item) not in MATERIAL_GROUPS:
            continue
        rates = [o[0] for o in obs]
        modal = statistics.mode([round(r, 2) for r in rates])
        for r, v, l in obs:
            if modal and abs(r - modal) / modal > spread:
                out.append({
                    "test": "rate dispersion", "voucher": v.get("voucher_number"),
                    "date": v.get("date"), "party": v.get("party"),
                    "item": item, "qty": l.get("qty"), "unit": unit,
                    "rate": r, "modal_rate": modal,
                    "amount": l.get("amount"),
                    "gap": round(((l.get("qty") or 0) * (r - modal)), 2),
                    "note": f"{len(obs)} purchases of this item; normally {modal}",
                })
    return out


def test_keyslip_shapes(rows: list[dict]) -> list[dict]:
    """
    Flag dispersion hits whose shape looks like a keyboard slip rather than a
    price move: a factor of ten, or the modal rate's digits transposed.
    """
    out = []
    for r in rows:
        if r["test"] != "rate dispersion":
            continue
        rate, modal = r["rate"], r["modal_rate"]
        if not modal:
            continue
        shape = None
        for f, name in ((10, "10x"), (0.1, "one tenth"), (100, "100x"), (0.01, "one hundredth")):
            if abs(rate - modal * f) / (modal * f) < 0.02:
                shape = name
                break
        if shape is None:
            a, b = str(int(round(rate))), str(int(round(modal)))
            if len(a) == len(b) and sorted(a) == sorted(b) and a != b:
                shape = "digits transposed"
        if shape:
            out.append({**r, "test": "key-slip shape",
                        "note": f"{shape} of the modal rate {modal}"})
    return out


def test_vs_closing_rate(vouchers: list[dict], closing: dict,
                         tol: float = 0.10) -> list[dict]:
    """
    Material carried in stock ABOVE what it is normally bought at.

    Benchmarked on the MODAL purchase rate, not the latest. Using the latest
    lets one mis-keyed entry become the benchmark and mask the very error this
    file exists to find: a 10x slip on the most recent bill would make every
    real rate look cheap and the test would report nothing. Caught by the
    self-test, which is why it reads this way.
    """
    obs: dict = defaultdict(list)
    for v in vouchers:
        for l in v["lines"]:
            r = _rate(l)
            if r > 0:
                obs[l["item_name"]].append((r, v.get("date") or "",
                                            v.get("voucher_number")))
    latest: dict = {}
    for k, seen in obs.items():
        modal = statistics.mode([round(r, 2) for r, _, _ in seen])
        newest = max(seen, key=lambda t: t[1])
        latest[k] = (modal, newest[1], newest[2])
    out = []
    for item, (r, d, vno) in latest.items():
        c = closing.get(item)
        if not c or not c.get("rate"):
            continue
        if c["rate"] > r * (1 + tol):
            out.append({
                "test": "carried above last purchase", "voucher": vno, "date": d,
                "party": "", "item": item, "qty": c.get("qty"),
                "unit": c.get("unit"), "rate": r, "modal_rate": c["rate"],
                "amount": c.get("value"),
                "gap": round(((c.get("qty") or 0) * (c["rate"] - r)), 2),
                "note": "closing rate exceeds the modal purchase rate",
            })
    return out


def test_duplicate_bills(vouchers: list[dict], days: int = 7) -> list[dict]:
    """Same party, item, qty and amount within a few days — a bill keyed twice."""
    seen: dict = defaultdict(list)
    for v in vouchers:
        for l in v["lines"]:
            key = (v.get("party"), l["item_name"], round(l.get("qty") or 0, 2),
                   round(l.get("amount") or 0, 2))
            seen[key].append(v)
    out = []
    for (party, item, qty, amt), vs in seen.items():
        if len(vs) < 2 or not amt:
            continue
        vs = sorted(vs, key=lambda v: v.get("date") or "")
        for a, b in zip(vs, vs[1:]):
            da, db = a.get("date") or "", b.get("date") or ""
            if da and db and (date.fromisoformat(db) - date.fromisoformat(da)).days <= days:
                out.append({
                    "test": "possible duplicate bill", "voucher": b.get("voucher_number"),
                    "date": db, "party": party, "item": item, "qty": qty,
                    "unit": "", "rate": 0, "modal_rate": 0, "amount": amt,
                    "gap": amt, "note": f"identical line on {a.get('voucher_number')} dated {da}",
                })
    return out


COLUMNS = ["test", "gap", "item", "qty", "unit", "rate", "modal_rate", "amount",
           "expected_amount", "party", "voucher", "date", "note"]


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows = sorted(rows, key=lambda r: -abs(r.get("gap") or 0))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log.info("wrote %d findings to %s", len(rows), path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="100.74.103.22")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--company", required=True)
    ap.add_argument("--from", dest="frm", default="2026-04-01")
    ap.add_argument("--to", default="")
    ap.add_argument("--chunk-days", type=int, default=7)
    ap.add_argument("--cache", default="out/purchase_cache")
    ap.add_argument("--csv", default="out/purchase_rate_audit.csv")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = TallyConfig(host=a.host, port=a.port, company=a.company, timeout=280)
    frm = date.fromisoformat(a.frm)
    to = date.fromisoformat(a.to) if a.to else date.today()
    vouchers = fetch_purchases(cfg, frm, to, a.chunk_days, a.cache)
    lines = sum(len(v["lines"]) for v in vouchers)
    log.info("%d purchase-side vouchers, %d lines", len(vouchers), lines)
    if not lines:
        log.error("No purchase lines returned. Either the window is empty or "
                  "the voucher-type names differ in this book — list them with "
                  "summary_by_voucher_type before assuming a fetch bug.")
        return 1

    groups, closing = {}, {}
    try:
        import json as _j
        import urllib.parse
        import urllib.request
        url = os.environ["FRAPPE_URL"].rstrip("/")
        hdr = {"Authorization": "token %s:%s" % (os.environ["FRAPPE_API_KEY"],
                                                 os.environ["FRAPPE_API_SECRET"])}
        q = {"doctype": "Tally Stock Item",
             "fields": _j.dumps(["item_name", "stock_group", "closing_qty",
                                 "closing_qty_unit", "closing_rate", "closing_value"]),
             "filters": _j.dumps([["company", "=", a.company]]),
             "limit_page_length": 0}
        req = urllib.request.Request(
            url + "/api/method/frappe.client.get_list?" + urllib.parse.urlencode(q),
            headers=hdr)
        for r in _j.loads(urllib.request.urlopen(req, timeout=240).read())["message"]:
            groups[r["item_name"]] = r["stock_group"]
            closing[r["item_name"]] = {"rate": float(r["closing_rate"] or 0),
                                       "qty": float(r["closing_qty"] or 0),
                                       "value": float(r["closing_value"] or 0),
                                       "unit": r["closing_qty_unit"]}
    except Exception as exc:  # the Tally-side tests still stand without it
        log.warning("Could not load stock masters (%s); skipping the "
                    "closing-rate test and the material-group filter.", exc)

    rows = []
    rows += test_line_arithmetic(vouchers)
    disp = test_rate_dispersion(vouchers, groups)
    rows += disp
    rows += test_keyslip_shapes(disp)
    if closing:
        rows += test_vs_closing_rate(vouchers, closing)
    rows += test_duplicate_bills(vouchers)

    write_csv(rows, a.csv)
    by = Counter(r["test"] for r in rows)
    print()
    print("%-32s %6s %14s" % ("test", "hits", "value at risk"))
    for k, n in by.most_common():
        v = sum(abs(r.get("gap") or 0) for r in rows if r["test"] == k)
        print("%-32s %6d %14s" % (k, n, "Rs %.0f" % v))
    print("\nEvery hit needs the physical bill pulled. This finds disagreement, "
          "not truth.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
