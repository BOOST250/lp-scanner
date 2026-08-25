"""
Live scan of Polymarket's liquidity-reward markets: where is resting capital
actually paid, and what does it cost you in risk to rest there.

WHAT THIS ANSWERS
  Not "which market pays the most" - that question has a useless answer. The
  markets with the highest headline yield are empty, and they are empty for a
  reason. It answers "where is the reward large relative to the competition I
  would actually be scored against, on a market I can quote without being run
  over".

WHY THE HEADLINE YIELD LIES, TWICE
  1. Dilution. Rewards split by score share, so your own size sits in the
     denominator. $106/day against $42 of existing score reads as 165% a day
     until you are the one setting the denominator.
  2. Adverse selection. An hourly crypto market pays a high rate because every
     tick runs over a resting quote. The reward is compensation for a real cost,
     not a free lunch, and this scanner shows the cost next to the payment
     rather than only the payment.

TRANSPORT
  curl through the SOCKS tunnel: Hungarian ISPs reset the TLS handshake on any
  *.polymarket.com SNI. Set POLYMARKET_PROXY_URL empty on an unfiltered network.

USAGE
  python scanner.py                 # one scan, printed
  python scanner.py --watch         # refresh forever, writes state.json
  python scanner.py --size 200      # size the quote in shares (default 100)
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import statistics
import subprocess
import sys
import time

import scoring

CLOB = "https://clob.polymarket.com"
PROXY = os.environ.get("POLYMARKET_PROXY_URL", "socks5://127.0.0.1:40000")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "markets_cache.json")
STATE = os.path.join(HERE, "docs", "data", "state.json")

# Reward scores are sampled once a minute, so refreshing faster buys nothing.
REFRESH_SECONDS = 30
# Market metadata (tokens, question, end date) barely moves.
CACHE_TTL_HOURS = 6
# The CLOB accepts a batch of book requests; this is a polite chunk size.
BOOK_BATCH = 50
# Volatility costs one history call per market, so only the top candidates get it.
VOL_SAMPLE = 60
# Below this existing score, you would effectively own the reward pool alone.
CONTESTED_SCORE = 50


def curl(url, post=None, timeout="30"):
    args = ["curl", "-s", "-m", timeout]
    if PROXY:
        args += ["--socks5-hostname", PROXY.split("://", 1)[-1]]
    args += ["-H", "User-Agent: lp-scanner/1.0"]
    if post is not None:
        args += ["-X", "POST", "-H", "Content-Type: application/json", "-d", post]
    out = subprocess.run(args + [url], capture_output=True)
    return out.stdout.decode("utf-8", "replace")


def get_json(url, post=None, timeout="30"):
    body = curl(url, post, timeout)
    if not body:
        raise RuntimeError(f"empty response: {url[:90]}")
    return json.loads(body)


def reward_markets():
    """Markets currently paying liquidity rewards, with their scoring parameters."""
    j = get_json(f"{CLOB}/rewards/markets/current?limit=500")
    rows = j["data"] if isinstance(j, dict) else j
    return [r for r in rows if (r.get("total_daily_rate") or 0) > 0]


def load_cache():
    if not os.path.exists(CACHE):
        return {}
    age = (time.time() - os.path.getmtime(CACHE)) / 3600
    if age > CACHE_TTL_HOURS:
        return {}
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def market_meta(condition_ids, cache):
    """
    Tokens, question and end date per market.

    Gamma rejects a comma-joined list of condition_ids, so this goes one at a
    time against the CLOB and caches the result - the metadata is static enough
    that paying for it once every few hours is right.
    """
    missing = [c for c in condition_ids if c not in cache]
    if missing:
        sys.stderr.write(f"  {len(missing)} piac metaadata lekerese…\n")

        def one(cid):
            try:
                m = get_json(f"{CLOB}/markets/{cid}")
            except (RuntimeError, json.JSONDecodeError):
                return cid, None
            if not m or "tokens" not in m:
                return cid, None
            return cid, {
                "question": m.get("question", ""),
                "tokens": [t["token_id"] for t in m["tokens"] if t.get("token_id")],
                "end": m.get("end_date_iso") or m.get("game_start_time") or "",
                "tick": m.get("minimum_tick_size"),
                "min_order": m.get("minimum_order_size"),
            }

        with cf.ThreadPoolExecutor(8) as ex:
            for cid, meta in ex.map(one, missing):
                if meta:
                    cache[cid] = meta
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    return cache


def fetch_books(tokens):
    """Books for many tokens, keyed by asset_id - never by position."""
    out = {}
    for i in range(0, len(tokens), BOOK_BATCH):
        chunk = tokens[i : i + BOOK_BATCH]
        try:
            books = get_json(
                f"{CLOB}/books",
                post=json.dumps([{"token_id": t} for t in chunk]),
                timeout="40",
            )
        except (RuntimeError, json.JSONDecodeError):
            continue
        for b in books if isinstance(books, list) else []:
            if b.get("asset_id"):
                out[b["asset_id"]] = b
    return out


def levels(book, key):
    return sorted(
        [(float(l["price"]), float(l["size"])) for l in book.get(key, []) if float(l["size"]) > 0],
        reverse=(key == "bids"),
    )


def hours_left(end_iso):
    if not end_iso:
        return None
    try:
        end = dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (end - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600


def analyse(cfg, meta, books, size, distance):
    """One market: competition, what my quote would earn, and what it risks."""
    tokens = meta["tokens"]
    if not tokens:
        return None
    book = books.get(tokens[0])
    if not book:
        return None
    bids, asks = levels(book, "bids"), levels(book, "asks")
    if not bids or not asks:
        return None

    mid = (bids[0][0] + asks[0][0]) / 2
    v = cfg["rewards_max_spread"]
    spread_c = (asks[0][0] - bids[0][0]) * 100

    existing = scoring.book_score(bids, mid, v) + scoring.book_score(asks, mid, v)
    two_sided = scoring.requires_two_sided(mid)
    reward, yld, share = scoring.daily_reward(
        size, distance, mid, v, existing, cfg["total_daily_rate"], two_sided
    )

    hrs = hours_left(meta.get("end", ""))
    return {
        "token": tokens[0],
        "question": meta["question"],
        "condition_id": cfg["condition_id"],
        "rate": cfg["total_daily_rate"],
        "max_spread": v,
        "min_size": cfg["rewards_min_size"],
        "mid": round(mid, 4),
        "spread_c": round(spread_c, 2),
        "existing_score": round(existing, 1),
        "my_share": round(share, 4),
        "daily_usd": round(reward, 2),
        "daily_yield": round(yld, 5),
        "capital": round(scoring.capital_required(size, mid, two_sided), 2),
        "two_sided": two_sided,
        "hours_left": None if hrs is None else round(hrs, 1),
        "below_min_size": size < (cfg["rewards_min_size"] or 0),
    }


def realised_vol(token, hours=24):
    """
    Standard deviation of hourly mid moves, in cents - the adverse-selection proxy.

    A resting quote is a free option written to whoever knows more than you. The
    premium for that option is the reward; the cost is how far the price travels
    while you are resting. This does not measure adverse selection directly, but a
    market whose mid moves 4c an hour will pick off a quote posted 1c from mid
    many times a day, and one that moves 0.2c will not.
    """
    start = int(time.time()) - hours * 3600
    try:
        j = get_json(f"{CLOB}/prices-history?market={token}&startTs={start}&fidelity=60")
    except (RuntimeError, json.JSONDecodeError):
        return None
    pts = j.get("history") or []
    if len(pts) < 6:
        return None
    moves = [
        (float(b["p"]) - float(a["p"])) * 100 for a, b in zip(pts, pts[1:])
    ]
    return statistics.pstdev(moves) if len(moves) > 1 else None


def capacity(row, distance, target_daily_yield=0.01):
    """
    Dollars you could rest here before the yield falls to `target` a day.

    This is the number the headline yield hides. Reward share is
    mine / (existing + mine), so every dollar you add cuts the rate on the
    dollars already there - a market showing 250% a day on $25 of capital may
    not clear 1% a day on $2,000. Solving

        rate * mine/(existing + mine) / capital = target

    for size gives the point where this stops being interesting, which is the
    real measure of whether an opportunity is worth the effort of quoting.
    """
    v, mid = row["max_spread"], row["mid"]
    if v <= 0 or mid <= 0 or distance > v:
        return 0.0
    w = ((v - distance) / v) ** 2                    # score per share
    two = row["two_sided"]
    per_share_capital = mid * (2 if two else 1)
    e = row["existing_score"]
    rate = row["rate"]
    # rate * (w*n)/(e + w*n) = target * per_share_capital * n
    #   -> target*cap*w*n^2 + target*cap*e*n - rate*w*n = 0
    #   -> n = (rate*w - target*cap*e) / (target*cap*w)
    denom = target_daily_yield * per_share_capital * w
    if denom <= 0:
        return 0.0
    n = (rate * w - target_daily_yield * per_share_capital * e) / denom
    return max(0.0, n * per_share_capital)


def risk_flags(row):
    """
    Reasons not to quote here, in plain terms.

    An empty book is the loudest signal on the board: every other maker looked at
    this market and declined. The yield is high *because* of that, not despite it.
    Whether that verdict is correct is exactly what the fills half of this project
    would settle - until then it is an open question, not a free lunch.

    Note what is deliberately *not* a flag: a book spread wider than the reward
    band. That is not a disqualification, it is the mechanism behind an empty
    score - the existing orders all sit outside the qualifying band - and flagging
    it separately just says "empty book" twice.
    """
    f = []
    if row["existing_score"] < 50:
        f.append("ures konyv")
    if row["hours_left"] is not None and row["hours_left"] < 24:
        f.append(f"{row['hours_left']:.0f}h mulva zar")
    if row["vol_c"] is not None and row["vol_c"] > 2.0:
        f.append(f"volatilis ({row['vol_c']:.1f}c/h)")
    if row["two_sided"]:
        f.append("ket oldal kell")
    if row["below_min_size"]:
        f.append("meret < minimum")
    if row["daily_usd"] == 0:
        f.append("<$1, nem fizet")
    return f


def scan(size=100.0, distance=1.0):
    cfgs = reward_markets()
    cache = market_meta([c["condition_id"] for c in cfgs], load_cache())
    cfgs = [c for c in cfgs if c["condition_id"] in cache]

    tokens = []
    for c in cfgs:
        tokens.extend(cache[c["condition_id"]]["tokens"][:1])
    books = fetch_books(tokens)

    rows = []
    for c in cfgs:
        r = analyse(c, cache[c["condition_id"]], books, size, distance)
        # A market past its end date still has a book and a reward rate, but it
        # is not somewhere to put capital.
        if r and (r["hours_left"] is None or r["hours_left"] > 0):
            rows.append(r)

    # Capacity before dilution is the number that decides how much of this is
    # worth your time, so it is computed for everything.
    for r in rows:
        r["capacity"] = capacity(r, distance)

    # Volatility costs one history call each. Measure the top yields plus every
    # contested market - the contested ones rank low on yield but are the rows a
    # decision actually gets made on, and leaving them blank made the first
    # version of this table useless where it mattered most.
    rows.sort(key=lambda r: -r["daily_yield"])
    head = list({id(r): r for r in rows[:VOL_SAMPLE]
                 + [x for x in rows if x["existing_score"] >= CONTESTED_SCORE]}.values())
    with cf.ThreadPoolExecutor(8) as ex:
        for r, v in zip(head, ex.map(lambda r: realised_vol(r["token"]), head)):
            r["vol_c"] = None if v is None else round(v, 2)
    for r in rows:
        r.setdefault("vol_c", None)
        r["flags"] = risk_flags(r)
    rows.sort(key=lambda r: -r["daily_yield"])
    return rows


def _section(title, rows, limit):
    print(f"\n{title}")
    print(f"  {'napi $':>7} {'hozam/nap':>10} {'kapacitas':>10} {'verseny':>8} {'vol':>7} {'zar':>7}  piac")
    print("  " + "-" * 96)
    for r in rows[:limit]:
        hrs = "—" if r["hours_left"] is None else f"{r['hours_left']:.0f}h"
        vol = "—" if r["vol_c"] is None else f"{r['vol_c']:.1f}c"
        print(
            f"  {r['daily_usd']:>7.2f} {r['daily_yield']*100:>9.2f}% "
            f"${r['capacity']:>9,.0f} {r['existing_score']:>8,.0f} {vol:>7} {hrs:>7}  {r['question'][:42]}"
        )
        if r["flags"]:
            print(f"  {'':>43}{', '.join(r['flags'])}")
    if not rows:
        print("  (egy sem)")


def print_table(rows, size, limit=12):
    """
    Two rankings, because they are two different decisions.

    Contested markets are a competition: your share is what your score buys
    against everyone else's, and the yield is modest and probably real.
    Uncontested markets are a question: nobody else is quoting, and the reward
    per dollar looks absurd. Mixing them into one list buries the first under
    the second, which is what the first version of this table did.
    """
    contested = [r for r in rows if r["existing_score"] >= CONTESTED_SCORE and r["daily_usd"] > 0]
    empty = [r for r in rows if r["existing_score"] < CONTESTED_SCORE and r["daily_usd"] > 0]
    clean = [r for r in contested if not r["flags"]]

    print(f"\n{len(rows)} elo jutalmazott piac | {len(contested)} versengo | {len(empty)} ures")
    _section(f"VERSENGO PIACOK ({len(clean)} jelzes nelkul) - itt a hozam elhihetobb", contested, limit)
    _section("URES KONYVU PIACOK - a hozam magas, mert rajtad kivul senki nincs ott", empty, 8)
    print(f"\n  {size:.0f} reszveny 1c-re a kozeparhoz | vol = orankenti kozepar-szoras 24h-n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=float, default=100.0, help="quote size in shares")
    ap.add_argument("--distance", type=float, default=1.0, help="cents from midpoint")
    ap.add_argument("--watch", action="store_true", help="refresh forever")
    args = ap.parse_args()

    while True:
        t0 = time.time()
        try:
            rows = scan(args.size, args.distance)
        except (RuntimeError, json.JSONDecodeError) as e:
            sys.stderr.write(f"scan sikertelen: {e}\n")
            if not args.watch:
                sys.exit(1)
            time.sleep(REFRESH_SECONDS)
            continue

        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(
                {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "size": args.size, "distance": args.distance, "rows": rows},
                f, separators=(",", ":"),
            )
        print_table(rows, args.size)
        print(f"  {time.time()-t0:.1f}s | state.json frissitve")
        if not args.watch:
            return
        time.sleep(max(0, REFRESH_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    main()
