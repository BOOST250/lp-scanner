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
HISTORY = os.path.join(HERE, "docs", "data", "history.json")

# Reward scores are sampled once a minute, so refreshing faster buys nothing.
REFRESH_SECONDS = 30
# The CLOB accepts a batch of book requests; this is a polite chunk size.
BOOK_BATCH = 50
# Volatility costs one history call per market, so only the top candidates get it.
VOL_SAMPLE = 60
# Below this existing score, you would effectively own the reward pool alone.
CONTESTED_SCORE = 50
# Only markets paying at least this much a day are scanned.
#
# There are ~16,500 reward markets paying ~$180,000/day in total, and fetching a
# book and metadata for every one takes minutes and produces a ~9 MB state file
# committed every ten minutes. The cut is at $10/day because that keeps 72% of
# the whole pool in 2,436 markets - and because a market paying less can only
# ever clear the $1 minimum payout for one or two makers, so a third arrival
# makes it pay nobody.
#
# orders.py deliberately does NOT apply this: your own orders must be found
# wherever they are.
MIN_DAILY_RATE = 10
# History lands in hourly slots and keeps a week, so a 10-minute scan cadence
# does not inflate the file.
HISTORY_STEP = 3600
HISTORY_POINTS = 24 * 7


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
    """
    Every market currently paying liquidity rewards, with its scoring parameters.

    PAGE WITH THE CURSOR, NOT WITH offset
      `limit=500` is the page size, and the endpoint silently ignores `offset` -
      requesting offset=0 and offset=500 returns the *same* 500 rows, with 100%
      overlap. Only `next_cursor` advances.

      This was not a theoretical bug. Reading one page looked complete and gave
      500 markets; paging properly gives about 6,000. The scanner was seeing a
      twelfth of the board, which meant real reward-paying markets were reported
      as not being in the programme at all - the most misleading failure this
      tool can have, because it looks like a finding rather than a gap.

    The cursor is a base64-encoded offset; "LTE=" is base64 "-1" and marks the
    end. A repeated cursor is also treated as the end, so a server-side change
    cannot turn this into an endless loop.
    """
    rows, cursor, seen = [], "", set()
    while True:
        url = f"{CLOB}/rewards/markets/current?limit=500"
        if cursor:
            url += f"&next_cursor={cursor}"
        j = get_json(url)
        page = j.get("data", []) if isinstance(j, dict) else j
        if not page:
            break
        rows.extend(page)
        cursor = j.get("next_cursor") if isinstance(j, dict) else None
        if not cursor or cursor == "LTE=" or cursor in seen:
            break
        seen.add(cursor)
    # the same market can appear twice across pages; last one wins
    uniq = {r["condition_id"]: r for r in rows
            if (r.get("total_daily_rate") or 0) >= MIN_DAILY_RATE}
    return list(uniq.values())


def load_cache():
    """
    Market metadata, kept indefinitely rather than expired wholesale.

    An earlier version dropped the whole cache after six hours, which on CI - where
    it starts empty anyway - meant refetching 500 markets one at a time on every
    run. Roughly half those calls failed under rate limiting and the published
    board silently lost half its markets, which is worse than stale metadata:
    a missing market reads as "no competition here" to anyone looking at it.

    Tokens and questions do not change for a given condition_id, so entries never
    go wrong, only unused. Markets leaving the rewards programme simply stop being
    looked up.
    """
    if not os.path.exists(CACHE):
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
    time against the CLOB. The cache is committed to the repository precisely so
    that CI only ever pays for markets it has not seen before - a cold run means
    500 sequential lookups and a rate-limited, half-empty board.
    """
    missing = [c for c in condition_ids if c not in cache]
    if missing:
        sys.stderr.write(f"  {len(missing)} piac metaadata lekerese…\n")

        def one(cid):
            # One call per market and 500 of them on a cold cache, so transient
            # rate limiting is normal rather than exceptional. Without the retry
            # a cold run lost about half the board.
            m = None
            for attempt in range(3):
                try:
                    m = get_json(f"{CLOB}/markets/{cid}")
                    break
                except (RuntimeError, json.JSONDecodeError):
                    if attempt < 2:
                        time.sleep(0.6 * (attempt + 1))
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


def band_profile(bids, asks, mid, max_spread, bucket=0.5):
    """
    Where the competing score actually sits, in half-cent buckets from the mid.

    The detail view's real question is "should I quote at 1c or at 3c", and that
    is answered by the shape of the competition, not by the raw book. Storing
    levels would carry price/size pairs the page never draws; storing the score
    per bucket is about a dozen numbers per market and is exactly what the
    decision needs.

    Bucket i covers [i*bucket, (i+1)*bucket) cents from the midpoint.
    """
    n = int(math.ceil(max_spread / bucket))
    out = [0.0] * n
    for price, size in bids + asks:
        d = abs(price - mid) * 100
        if d > max_spread:
            continue
        i = min(int(d / bucket), n - 1)
        out[i] += scoring.order_score(d, size, max_spread)
    return [round(x, 1) for x in out]


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
        "band": band_profile(bids, asks, mid, v),
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


def update_history(rows, now=None):
    """
    Append this scan to a rolling per-market series of competition and rate.

    Competition is the number that decides whether a market stays worth quoting,
    and it is invisible in a snapshot: a market with $50 of score today may have
    had $5,000 last week, or be filling up as other makers notice it. The scan
    already runs every ten minutes, so the series costs nothing but the writing.

    Stored on a regular grid as {t0, step, score[], rate[]} rather than as
    timestamped points - the same compression used for the election globe's
    price history, where keeping {t, p} objects cost 1479 KB for what fits in
    190 KB on a grid. Samples land in hourly slots, last write wins, so a
    ten-minute cadence does not inflate the file.
    """
    now = int(now or time.time())
    slot = now - (now % HISTORY_STEP)
    hist = {}
    if os.path.exists(HISTORY):
        try:
            with open(HISTORY, encoding="utf-8") as f:
                hist = json.load(f)
        except (json.JSONDecodeError, OSError):
            hist = {}

    keep = HISTORY_POINTS
    for r in rows:
        cid = r["condition_id"]
        h = hist.get(cid)
        if not h or "t0" not in h:
            h = {"t0": slot, "step": HISTORY_STEP, "score": [], "rate": []}
        # index this sample lands in, relative to the series start
        i = (slot - h["t0"]) // HISTORY_STEP
        if i < 0:                       # clock skew; restart rather than corrupt
            h = {"t0": slot, "step": HISTORY_STEP, "score": [], "rate": []}
            i = 0
        # gaps become nulls so the x axis stays honest about missing scans
        while len(h["score"]) < i:
            h["score"].append(None)
            h["rate"].append(None)
        if len(h["score"]) == i:
            h["score"].append(round(r["existing_score"], 1))
            h["rate"].append(r["rate"])
        else:
            h["score"][i] = round(r["existing_score"], 1)
            h["rate"][i] = r["rate"]
        if len(h["score"]) > keep:
            drop = len(h["score"]) - keep
            h["t0"] += drop * HISTORY_STEP
            h["score"] = h["score"][drop:]
            h["rate"] = h["rate"][drop:]
        hist[cid] = h

    # markets that left the rewards programme stop being written and age out
    live = {r["condition_id"] for r in rows}
    hist = {k: v for k, v in hist.items() if k in live or any(x is not None for x in v["score"][-6:])}

    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(hist, f, separators=(",", ":"))
    return hist


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

        hist = update_history(rows)
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(
                {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "size": args.size, "distance": args.distance, "rows": rows},
                f, separators=(",", ":"),
            )
        print_table(rows, args.size)
        sz = os.path.getsize(STATE) / 1024
        hz = os.path.getsize(HISTORY) / 1024
        print(f"  {time.time()-t0:.1f}s | state {sz:.0f} KB | history {hz:.0f} KB, {len(hist)} sorozat")
        if not args.watch:
            return
        time.sleep(max(0, REFRESH_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    main()
