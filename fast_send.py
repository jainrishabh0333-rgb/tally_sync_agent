#!/usr/bin/env python3
"""Send one priced Sales Order with the minimum possible Tally round trips.

The standard path re-fetches every ledger and stock item from Tally before
each send (2 heavy pulls, minutes on a slow gateway). This path validates
against a LOCAL masters cache and prices from rates_master.json, so a send
is exactly ONE Tally post, plus ONE narrow read-back to verify.

  Refresh the cache (run daily / after new masters):
      python3 fast_send.py --host <tally-host> --refresh-masters
  Send:
      python3 fast_send.py --host <tally-host> --hold ../order_console/hold.json
  Options: --rates <file> (default rates_master.json), --dry-run, --no-verify

Refuses to send if the cache is missing or older than --max-cache-age days
(default 7): a stale cache could wave through a renamed master and Tally
would silently CREATE a junk item.
"""
import argparse, json, sys, time
from datetime import date, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import re as _re
from tally_client import TallyConfig, TallyError, _post, fetch_ledgers, fetch_stock_items
from order_importer import build_envelope, load_order_settings, normalise_order
from distributor_fetch import fetch_ledger_extras, fetch_sales_orders
from send_priced_order import build_order

HERE = Path(__file__).resolve().parent
CACHE = HERE / "masters_cache.json"


# Per-party fields a voucher must carry itself — Tally fills in NONE of them
# on import, so a cache without them produces addressless vouchers.
_PARTY_FIELDS = ("address_lines", "state", "pincode", "mailing_name",
                 "country", "gst_registration_type")


def refresh_masters(cfg) -> dict:
    ledgers = fetch_ledgers(cfg)
    items = fetch_stock_items(cfg, date.today())
    extras = fetch_ledger_extras(cfg, date.today(), date.today())
    party_details = {
        name: {k: v for k, v in row.items() if k in _PARTY_FIELDS and v}
        for name, row in extras.items()
    }
    data = {
        "company": cfg.company,
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "parties": sorted(l.name for l in ledgers),
        "gstins": {l.name: (l.gstin or "").strip() for l in ledgers if l.gstin},
        "items": sorted(i.name for i in items),
        "party_details": {k: v for k, v in party_details.items() if v},
    }
    CACHE.write_text(json.dumps(data, indent=1))
    print(f"masters cache: {len(data['parties'])} ledgers, "
          f"{len(data['items'])} items, "
          f"{len(data['party_details'])} party address blocks -> {CACHE}")
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--company", default="SN JAIN INDUSTRIES PVT LTD - (26-27)")
    ap.add_argument("--refresh-masters", action="store_true")
    ap.add_argument("--hold", type=Path)
    ap.add_argument("--rates", type=Path, default=HERE / "rates_master.json")
    ap.add_argument("--max-cache-age", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args()

    cfg = TallyConfig(host=a.host, port=a.port, timeout=300)
    cfg.company = a.company

    if a.refresh_masters:
        refresh_masters(cfg)
        if not a.hold:
            return 0
    if not a.hold:
        ap.error("--hold is required to send (or use --refresh-masters)")

    if not CACHE.exists():
        print("No masters cache. Run --refresh-masters first.", file=sys.stderr)
        return 2
    cache = json.loads(CACHE.read_text())
    age_days = (datetime.now()
                - datetime.fromisoformat(cache["as_of"])).days
    if cache.get("company") != a.company or age_days > a.max_cache_age:
        print(f"Masters cache is {age_days}d old (max {a.max_cache_age}) or "
              f"wrong company. Run --refresh-masters.", file=sys.stderr)
        return 2
    if "party_details" not in cache:
        # Caches written before party details existed would send an
        # addressless voucher to the wrong sales ledger — the exact pair of
        # defects this guard was added for.
        print("Masters cache predates party address details. Run "
              "--refresh-masters before sending.", file=sys.stderr)
        return 2

    hold = json.loads(a.hold.read_text())
    rates = json.loads(a.rates.read_text())
    raw, missing = build_order(hold, rates)
    if missing:
        print("No rate for these items — nothing sent:", *missing,
              sep="\n  ", file=sys.stderr)
        return 2
    order = normalise_order(raw)

    parties, items = set(cache["parties"]), set(cache["items"])
    if order["party"] not in parties:
        print(f"Party {order['party']!r} not in masters cache "
              f"(as of {cache['as_of']}).", file=sys.stderr)
        return 2
    unknown = [l["item"] for l in order["lines"] if l["item"] not in items]
    if unknown:
        print("Unknown stock items — nothing sent:", *unknown,
              sep="\n  ", file=sys.stderr)
        return 2

    ocfg = load_order_settings(None)
    party_info = cache["party_details"].get(order["party"])
    xml = build_envelope(order, ocfg, cache["gstins"].get(order["party"], ""),
                         party=party_info)
    total = sum(float(x) for x in _re.findall(
        r"<ACCOUNTINGALLOCATIONS\.LIST>(?:(?!</ACCOUNTING).)*?"
        r"<AMOUNT>([^<]*)</AMOUNT>", xml, _re.S))
    ledgers = sorted(set(_re.findall(
        r"<LEDGERNAME>(Sale[^<]*)</LEDGERNAME>", xml)))
    print(f"{len(order['lines'])} lines, order value {total:,.2f}, "
          f"sales ledger {'/'.join(ledgers)}, "
          f"party state {(party_info or {}).get('state') or 'UNKNOWN'}")
    if not (party_info or {}).get("address_lines"):
        print(f"  WARNING: no address on file for {order['party']!r} — the "
              f"voucher will show a blank party address.", file=sys.stderr)
    if a.dry_run:
        return 0

    t0 = time.time()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = HERE / "sent_orders"
    out.mkdir(exist_ok=True)
    (out / f"sent-{order['order_key']}-{stamp}.xml").write_text(xml)
    try:
        resp = _post(cfg, xml)
    except TallyError as exc:
        print(f"SEND FAILED (may or may not have imported): {exc}",
              file=sys.stderr)
        return 1
    (out / f"resp-{order['order_key']}-{stamp}.xml").write_text(resp)
    created = "<CREATED>1</CREATED>" in resp and "<ERRORS>0</ERRORS>" in resp
    print(f"send took {time.time()-t0:.0f}s; created={created}")
    if not created:
        print(resp[:800], file=sys.stderr)
        return 1

    if a.no_verify:
        return 0
    t0 = time.time()
    od = order["order_date"]
    day = od if isinstance(od, date) else date.fromisoformat(str(od))
    mine = [v for v in fetch_sales_orders(cfg, day, day)
            if v["voucher_number"] == order["order_no"]]
    if not mine:
        print("VERIFY: voucher not found on read-back!", file=sys.stderr)
        return 1
    got = sum(l["qty"] for l in mine[0]["lines"])
    want = sum(s["qty"] for l in order["lines"] for s in l["sizes"])
    print(f"verify took {time.time()-t0:.0f}s; qty {got:g} vs {want:g} "
          f"{'OK' if abs(got-want) < 0.01 else 'MISMATCH'}; "
          f"amount {abs(mine[0]['amount']):,.2f}")
    return 0 if abs(got - want) < 0.01 else 1


if __name__ == "__main__":
    sys.exit(main())
