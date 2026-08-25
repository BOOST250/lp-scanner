"""
Your live resting orders, and what they are earning per minute and per hour.

WHY THIS RUNS LOCALLY AND NOT IN THE DASHBOARD

  Open orders are private: the CLOB requires L2 authentication - an API key,
  passphrase and an HMAC-SHA256 signature over the request - and there is no
  read-only mode. The dashboard is a public GitHub Pages site, so credentials
  cannot live there, cannot live in the repository, and should not live in a
  browser's localStorage on a public origin.

  So this runs on your machine, reads the credentials from the environment, and
  serves the result to the dashboard over localhost. The keys never leave the
  machine and never touch the repository.

    setx POLY_API_KEY     ...        (or export, on a shell)
    setx POLY_API_SECRET  ...
    setx POLY_PASSPHRASE  ...
    setx POLY_ADDRESS     0x...

  Create the credentials on Polymarket, not here. This file never prints them.

THE MATH IS NOT THE SCANNER'S MATH

  scanner.py answers "if I *added* an order here, what would I earn", so it uses
  share = mine / (existing + mine). Your live orders are already resting in the
  public book, so the book score it sees *includes* them. Using the prospective
  formula on live orders would count your own size twice and overstate your
  earnings. Here:

      share = your_Q_min / total_book_score

  One honest approximation remains. Polymarket computes each maker's Q_min
  separately - one-sided quoting is divided by three - and then normalises across
  makers. The public book is anonymous, so other makers' Q_min cannot be
  reconstructed; the denominator is the raw book score instead. Where others quote
  one side only, their true score is lower than that, so this *understates* your
  share. The error runs in the safe direction.

ACCRUAL

  Scores are sampled once a minute and the daily pool is split by normalised
  score, so a resting order earns rate * share / 1440 per minute - but only while
  it rests. The per-minute figure is a rate, not a promise: cancel the order and
  it stops immediately.

USAGE
  python orders.py                 # print once
  python orders.py --watch         # refresh and serve on http://127.0.0.1:8787
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import scoring

CLOB = "https://clob.polymarket.com"
PROXY = os.environ.get("POLYMARKET_PROXY_URL", "socks5://127.0.0.1:40000")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("LP_ORDERS_PORT", "8787"))
REFRESH_SECONDS = 30

CREDS = ("POLY_API_KEY", "POLY_API_SECRET", "POLY_PASSPHRASE", "POLY_ADDRESS")


def load_env_file(path):
    """
    Read KEY=value lines into the environment, without overwriting what is
    already set there.

    This exists so the credentials can stay in ONE file. Copying them into a
    second location doubles the number of places that can leak, and the copies
    drift apart when you rotate. Point POLY_ENV_FILE at the file you already
    have and nothing new is written to disk.

    Values are never printed - not by this function, not by anything downstream.
    """
    found = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k in CREDS and v and not os.environ.get(k):
                    os.environ[k] = v
                    found.append(k)
    except OSError as e:
        sys.exit(f"Nem olvashato: {path} ({e.strerror})")
    return found


def creds():
    # env vars win; a file is the fallback so the secrets can live in one place
    env_file = os.environ.get("POLY_ENV_FILE") or os.path.join(HERE, ".env")
    if any(not os.environ.get(c) for c in CREDS) and os.path.exists(env_file):
        got = load_env_file(env_file)
        if got:
            print(f"  {len(got)} kulcs betoltve innen: {env_file}")
            print(f"  (nevek: {', '.join(got)} — az ertekek nem jelennek meg sehol)")

    missing = [c for c in CREDS if not os.environ.get(c)]
    if missing:
        sys.exit(
            "Hianyzo ertekek: " + ", ".join(missing) + "\n\n"
            "Ket lehetoseg, mindketto a te gepeden marad:\n"
            f"  1) mutass ra a meglevo fajlodra:  set POLY_ENV_FILE=<utvonal>\\.env\n"
            f"  2) vagy tedd oket ide:            {env_file}\n\n"
            "A fajlban KULCS=ertek soronkent. Ez a szkript soha nem irja ki az ertekeket."
        )
    return {c: os.environ[c] for c in CREDS}


def explain(err):
    """
    Turn an upstream failure into something actionable, without ever echoing a
    credential back - not even a truncated one, since a prefix still narrows a
    brute force.
    """
    msg = str(err)
    if "Unauthorized" in msg or "Invalid api key" in msg:
        return (
            "A CLOB elutasitotta a hitelesitest.\n\n"
            "Gyakori okok, sorrendben:\n"
            "  - a POLY_API_SECRET nem base64 (a szkript base64-kent dekodolja)\n"
            "  - a POLY_ADDRESS nem ahhoz a kulcshoz tartozik\n"
            "  - a kulcs mas kornyezethez keszult, vagy visszavontad\n"
            "  - a gep oraja elcsuszott: az alairas idobelyeget a szerver ellenorzi\n\n"
            "Az ertekeket sem ez a szkript, sem a hibauzenet nem irja ki."
        )
    if "ures valasz" in msg or "timed out" in msg:
        return (
            "Nem jott valasz a Polymarkettol.\n"
            "Fut a SOCKS alagut? A magyar szolgaltatok a TLS kezfogasnal dobjak a *.polymarket.com SNI-t.\n"
            "  POLYMARKET_PROXY_URL=" + (PROXY or "(nincs beallitva)")
        )
    return f"Lekeres sikertelen: {msg}"


def l2_headers(c, method, path):
    """
    HMAC-SHA256 over <timestamp><METHOD><path>, keyed with the base64-decoded
    secret, encoded url-safe base64. Documented as Polymarket L2 auth.
    """
    ts = str(int(time.time()))
    msg = f"{ts}{method}{path}".encode()
    key = base64.urlsafe_b64decode(c["POLY_API_SECRET"] + "=" * (-len(c["POLY_API_SECRET"]) % 4))
    sig = base64.urlsafe_b64encode(hmac.new(key, msg, hashlib.sha256).digest()).decode()
    return {
        "POLY_ADDRESS": c["POLY_ADDRESS"],
        "POLY_API_KEY": c["POLY_API_KEY"],
        "POLY_PASSPHRASE": c["POLY_PASSPHRASE"],
        "POLY_TIMESTAMP": ts,
        "POLY_SIGNATURE": sig,
    }


def curl(url, headers=None, post=None, timeout="30"):
    args = ["curl", "-s", "-m", timeout]
    if PROXY:
        args += ["--socks5-hostname", PROXY.split("://", 1)[-1]]
    for k, v in (headers or {}).items():
        args += ["-H", f"{k}: {v}"]
    if post is not None:
        args += ["-X", "POST", "-H", "Content-Type: application/json", "-d", post]
    out = subprocess.run(args + [url], capture_output=True)
    return out.stdout.decode("utf-8", "replace")


def get_json(url, headers=None, post=None):
    body = curl(url, headers, post)
    if not body:
        raise RuntimeError(f"ures valasz: {url[:80]}")
    return json.loads(body)


def open_orders(c):
    path = "/data/orders"
    j = get_json(CLOB + path, headers=l2_headers(c, "GET", path))
    if isinstance(j, dict) and j.get("error"):
        raise RuntimeError(f"CLOB: {j['error']}")
    return j if isinstance(j, list) else j.get("data", [])


def reward_configs():
    j = get_json(f"{CLOB}/rewards/markets/current?limit=500")
    rows = j["data"] if isinstance(j, dict) else j
    return {r["condition_id"]: r for r in rows if (r.get("total_daily_rate") or 0) > 0}


def books_for(tokens):
    out = {}
    for i in range(0, len(tokens), 50):
        try:
            bs = get_json(f"{CLOB}/books",
                          post=json.dumps([{"token_id": t} for t in tokens[i:i + 50]]))
        except (RuntimeError, json.JSONDecodeError):
            continue
        for b in bs if isinstance(bs, list) else []:
            if b.get("asset_id"):
                out[b["asset_id"]] = b
    return out


def levels(book, key):
    return sorted(
        [(float(l["price"]), float(l["size"])) for l in book.get(key, []) if float(l["size"]) > 0],
        reverse=(key == "bids"),
    )


def analyse(c):
    orders = open_orders(c)
    if not orders:
        return {"at": int(time.time()), "orders": [], "markets": [], "totals": {
            "per_minute": 0.0, "per_hour": 0.0, "per_day": 0.0, "orders": 0, "capital": 0.0}}

    cfgs = reward_configs()
    tokens = sorted({o["asset_id"] for o in orders if o.get("asset_id")})
    books = books_for(tokens)

    # group by market so the two-sided rule can be applied per market, not per order
    by_market = {}
    for o in orders:
        tok = o.get("asset_id")
        cid = o.get("market") or o.get("condition_id")
        if not tok or not cid:
            continue
        by_market.setdefault((cid, tok), []).append(o)

    markets, rows = [], []
    total_min = 0.0
    total_capital = 0.0

    for (cid, tok), os_ in by_market.items():
        cfg = cfgs.get(cid)
        book = books.get(tok)
        if not book:
            continue
        bids, asks = levels(book, "bids"), levels(book, "asks")
        if not bids or not asks:
            continue
        mid = (bids[0][0] + asks[0][0]) / 2
        v = cfg["rewards_max_spread"] if cfg else 0.0
        rate = cfg["total_daily_rate"] if cfg else 0.0

        q_bid = q_ask = 0.0
        for o in os_:
            price = float(o.get("price", 0))
            size = float(o.get("original_size", 0)) - float(o.get("size_matched", 0))
            if size <= 0:
                continue
            total_capital += size * price
            d = abs(price - mid) * 100
            s = scoring.order_score(d, size, v) if v else 0.0
            if o.get("side", "").upper() == "BUY":
                q_bid += s
            else:
                q_ask += s
            rows.append({
                "market": cid, "token": tok, "side": o.get("side"),
                "price": round(price, 4), "size": round(size, 2),
                "distance_c": round(d, 2), "score": round(s, 2),
                "in_band": bool(v) and d <= v,
            })

        mine = scoring.combine_sides(q_bid, q_ask, mid) if v else 0.0
        # the book already contains these orders, so this is a share of the whole,
        # not an addition to it
        book_score = (scoring.book_score(bids, mid, v) + scoring.book_score(asks, mid, v)) if v else 0.0
        share = (mine / book_score) if book_score > 0 else 0.0
        share = min(share, 1.0)
        daily = rate * share
        if daily < scoring.MIN_PAYOUT_USD:
            daily = 0.0
        total_min += daily / 1440.0

        markets.append({
            "market": cid, "token": tok, "mid": round(mid, 4),
            "max_spread": v, "rate": rate,
            "q_bid": round(q_bid, 2), "q_ask": round(q_ask, 2),
            "my_score": round(mine, 2), "book_score": round(book_score, 2),
            "share": round(share, 4),
            "per_minute": daily / 1440.0, "per_hour": daily / 24.0, "per_day": daily,
            "two_sided_required": scoring.requires_two_sided(mid),
            "one_sided": (q_bid == 0) != (q_ask == 0),
            "orders": len(os_),
        })

    markets.sort(key=lambda m: -m["per_day"])
    return {
        "at": int(time.time()),
        "orders": rows,
        "markets": markets,
        "totals": {
            "per_minute": total_min,
            "per_hour": total_min * 60,
            "per_day": total_min * 1440,
            "orders": len(rows),
            "markets": len(markets),
            "capital": round(total_capital, 2),
            "paying": sum(1 for m in markets if m["per_day"] > 0),
        },
    }


def show(state):
    t = state["totals"]
    print(f"\n  {t['orders']} nyitott megbizas, {t['markets']} piacon "
          f"({t['paying']} fizet) | lekotve ${t['capital']:,.2f}")
    print(f"  {'$%.4f' % t['per_minute']:>12} / perc"
          f"   {'$%.2f' % t['per_hour']:>10} / ora"
          f"   {'$%.2f' % t['per_day']:>10} / nap")
    if not state["markets"]:
        return
    print(f"\n  {'$/ora':>8} {'$/perc':>9} {'reszesedes':>11} {'pontszam':>10} {'konyv':>9}  piac")
    for m in state["markets"][:15]:
        note = ""
        if m["one_sided"]:
            note = " (egy oldal)" if not m["two_sided_required"] else " (KET OLDAL KELL)"
        print(f"  {m['per_hour']:>8.3f} {m['per_minute']:>9.5f} {m['share']*100:>10.2f}% "
              f"{m['my_score']:>10,.0f} {m['book_score']:>9,.0f}  {m['market'][:18]}…{note}")


class Handler(BaseHTTPRequestHandler):
    state = {"at": 0, "orders": [], "markets": [], "totals": {}}

    def do_GET(self):
        if self.path.split("?")[0] != "/orders.json":
            self.send_error(404)
            return
        body = json.dumps(Handler.state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        # the dashboard is served from github.io and reads this from localhost
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="refresh and serve on localhost")
    args = ap.parse_args()
    c = creds()

    if not args.watch:
        try:
            show(analyse(c))
        except RuntimeError as e:
            sys.exit(explain(e))
        return

    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"  http://127.0.0.1:{PORT}/orders.json — a dashboard innen olvas")
    while True:
        try:
            Handler.state = analyse(c)
            show(Handler.state)
        except (RuntimeError, json.JSONDecodeError) as e:
            sys.stderr.write(f"  lekeres sikertelen: {e}\n")
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
