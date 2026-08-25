"""
Polymarket liquidity-rewards scoring.

This module is the one place the reward mechanics live, because getting them
wrong silently produces a plausible-looking ranking that sends capital to the
wrong markets.

THE FORMULA (from Polymarket's liquidity rewards documentation)

    S(v, s) = ((v - s) / v)^2 * size

    v     maximum qualifying spread for the market, in cents
    s     the order's distance from the midpoint, in cents
    size  order size in shares

  The square is the part that matters and the part that is easy to miss. On a
  market with v = 5.5c:

    1c from mid -> ((5.5-1)/5.5)^2 = 0.67 of full score
    4c from mid -> ((5.5-4)/5.5)^2 = 0.074

  So a book that looks deep can be scoring almost nothing if its depth sits near
  the edge of the qualifying band. Measuring competition by notional depth
  overstates it - on one market observed while building this, $12,660 of
  qualifying depth carried only a sixth of the score that flat weighting implied.

ONE-SIDED VERSUS TWO-SIDED

    midpoint in [0.10, 0.90]:  Q_min = max(min(Q_one, Q_two), max(Q_one/c, Q_two/c))
    midpoint outside that:     Q_min = min(Q_one, Q_two)

  with c = 3.0. In the middle of the range a single side still scores, at a third
  of its value. Near the extremes a single side scores nothing at all - and those
  are exactly the markets where the far side is a 2c lottery ticket that gaps to
  zero, so the two-sided requirement is a real capital and risk cost, not a
  formality.

SAMPLING

  Orders are sampled once a minute, aggregated over a one-week epoch of 10,080
  samples, and the daily pool is split by normalised score share. Two consequences
  the dashboard depends on:

    - Resting continuously is the whole game. A quote that is up half the time
      earns half the score, so a scanner refresh faster than ~30s buys nothing.
    - Minimum payout is $1. A share of the pool worth less than that pays zero,
      which quietly makes small positions in big-competition markets worthless.
"""

# Single-sided liquidity is divided by this before scoring.
ONE_SIDED_DIVISOR = 3.0

# Below/above this midpoint, only two-sided quoting scores at all.
TWO_SIDED_BELOW = 0.10
TWO_SIDED_ABOVE = 0.90

# Rewards below this are not paid out.
MIN_PAYOUT_USD = 1.0


def order_score(distance_cents: float, size: float, max_spread_cents: float) -> float:
    """Score for one resting order. Zero once it is outside the qualifying band."""
    if max_spread_cents <= 0 or distance_cents > max_spread_cents or size <= 0:
        return 0.0
    return ((max_spread_cents - distance_cents) / max_spread_cents) ** 2 * size


def book_score(levels, mid: float, max_spread_cents: float) -> float:
    """
    Total score resting on one side of a book.

    `levels` is [(price, size), ...] as returned by the CLOB. Distance is taken
    from the plain midpoint; Polymarket uses a size-adjusted midpoint, which
    moves it slightly on thin books. That approximation is noted rather than
    hidden: it biases scores for the top-of-book orders on lopsided books.
    """
    total = 0.0
    for price, size in levels:
        total += order_score(abs(price - mid) * 100, size, max_spread_cents)
    return total


def requires_two_sided(mid: float) -> bool:
    return not (TWO_SIDED_BELOW <= mid <= TWO_SIDED_ABOVE)


def combine_sides(q_one: float, q_two: float, mid: float) -> float:
    """Apply Polymarket's Q_min rule to a maker's two side scores."""
    if requires_two_sided(mid):
        return min(q_one, q_two)
    return max(min(q_one, q_two), max(q_one / ONE_SIDED_DIVISOR, q_two / ONE_SIDED_DIVISOR))


def my_score(size: float, distance_cents: float, mid: float, max_spread_cents: float,
             two_sided: bool) -> float:
    """
    Score I would earn posting `size` shares at `distance_cents` from mid.

    Two-sided means the same order on both sides, so both side scores are equal
    and Q_min collapses to that value. One-sided in the middle band takes the
    /3 penalty.
    """
    one = order_score(distance_cents, size, max_spread_cents)
    return combine_sides(one, one if two_sided else 0.0, mid)


def capital_required(size: float, mid: float, two_sided: bool) -> float:
    """
    Dollars tied up by the quote.

    A bid for `size` shares at roughly `mid` costs size*mid. The ask side needs
    `size` shares to sell, which cost size*mid to acquire - so a two-sided quote
    ties up both. This is the denominator that turns a reward into a yield, and
    it is why the two-sided markets near 0 and 1 are worse than they look.
    """
    return size * mid * (2 if two_sided else 1)


def daily_reward(size: float, distance_cents: float, mid: float, max_spread_cents: float,
                 existing_score: float, daily_rate: float, two_sided: bool):
    """
    Expected daily payout and yield for adding a quote to an existing book.

    Returns (reward_usd, yield_fraction, my_share).

    The dilution term is the honest part: your own size enters the denominator,
    so the eye-catching yields on empty books collapse the moment you post real
    money into them. A market paying $106/day against $42 of existing score looks
    like 165% a day until you notice you are the one setting the denominator.
    """
    mine = my_score(size, distance_cents, mid, max_spread_cents, two_sided)
    if mine <= 0:
        return 0.0, 0.0, 0.0
    share = mine / (existing_score + mine)
    reward = daily_rate * share
    if reward < MIN_PAYOUT_USD:
        reward = 0.0
    capital = capital_required(size, mid, two_sided)
    return reward, (reward / capital if capital else 0.0), share


def reward_at(row: dict, size: float, distance_cents: float) -> float:
    """
    Daily payout for `size` shares in one scanned market, zero below the floor.

    Takes a scanner row rather than loose arguments because the allocator calls
    it thousands of times across hundreds of markets, and every call needs the
    same five fields travelling together.
    """
    if size <= 0:
        return 0.0
    mine = my_score(size, distance_cents, row["mid"], row["max_spread"], row["two_sided"])
    if mine <= 0:
        return 0.0
    payout = row["rate"] * mine / (row["existing_score"] + mine)
    return payout if payout >= MIN_PAYOUT_USD else 0.0


def min_stake(row: dict, distance_cents: float):
    """
    Smallest position that still clears the $1/day floor, and its yield.

    This is the only size worth holding in a market, and it falls out of the
    algebra rather than being a heuristic. Yield is

        rate * w*n / (e + w*n) / (n * per_share)  =  rate * w / ((e + w*n) * per_share)

    which is strictly decreasing in n. So every extra share past the floor buys
    a worse rate than the one before it, and the yield-maximising stake is the
    smallest one that gets paid at all. Solving reward = $1 for n:

        n = e / (w * (rate - 1))

    A market paying $1/day or less can never clear the floor at any size, which
    is why `rate > MIN_PAYOUT_USD` is a hard gate rather than a filter applied
    afterwards.

    Returns (shares, capital, daily, yield) or None if the market cannot pay.
    """
    v, mid = row["max_spread"], row["mid"]
    rate, e = row["rate"], row["existing_score"]
    if v <= 0 or mid <= 0 or distance_cents > v or rate <= MIN_PAYOUT_USD:
        return None
    two = row["two_sided"]
    # score per share, including the one-sided penalty where it applies
    w = my_score(1.0, distance_cents, mid, v, two)
    if w <= 0:
        return None
    n = e / (w * (rate / MIN_PAYOUT_USD - 1.0))
    n = max(n, row.get("min_size") or 0.0)          # exchange minimum order size
    per = capital_required(1.0, mid, two)
    daily = reward_at(row, n, distance_cents)
    if daily <= 0:
        # rounding at the boundary: nudge up until it pays, or give up
        n *= 1.02
        daily = reward_at(row, n, distance_cents)
        if daily <= 0:
            return None
    capital = n * per
    return n, capital, daily, (daily / capital if capital else 0.0)


def allocate(rows, budget: float, distance_cents: float = 1.0, max_markets: int = 20,
             min_marginal_yield: float = 0.002):
    """
    Spread a bankroll across reward markets, best yield first.

    WHY NOT MARGINAL GREEDY

      Greedy on marginal yield is the obvious approach and it is wrong here,
      because the $1/day floor makes each market's payout discontinuous rather
      than concave: it jumps from nothing to a dollar, so a market needing a
      large stake to cross looks worthless at every step until it suddenly
      isn't. Run on live data, marginal greedy put $1,812 of a $2,000 bankroll
      into one market earning $9.88 a day while a $6 stake elsewhere earned $13.

      Since yield strictly decreases with size, each market has exactly one
      sensible stake - `min_stake` above - and the problem collapses to
      ranking markets by the yield they offer at it, then buying down the list.

    THE FLOOR IS NOT A POST-FILTER

      Applied afterwards it produces a confident, wrong answer: a $2,000
      bankroll spread without it lands on 224 markets promising 68% a day,
      crediting each sliver with a payout that would never arrive.

    `max_markets` is an operating constraint, not a financial one: every market
    is a bid and an ask that have to be re-quoted as the mid moves.

    Returns (allocations, totals) with allocations sorted by capital.
    """
    empty = {"capital": 0.0, "daily": 0.0, "yield": 0.0, "markets": 0, "orders": 0,
             "unspent": max(0.0, budget)}
    if budget <= 0 or not rows:
        return [], empty

    # Phase 1 - which markets to enter.
    #
    # Ranking by yield at the minimum viable stake is what handles the floor's
    # discontinuity: it asks "if I take the cheapest position that gets paid
    # here, how good is it", which is exactly the entry decision.
    ranked = []
    for r in rows:
        st = min_stake(r, distance_cents)
        if st:
            n, capital, daily, yld = st
            ranked.append((yld, n, capital, r))
    ranked.sort(key=lambda x: -x[0])

    chosen, spent = [], 0.0
    for yld, n, capital, r in ranked:
        if len(chosen) >= max_markets or spent + capital > budget:
            continue
        chosen.append({"row": r, "shares": n})
        spent += capital

    # Phase 2 - how much to put in each.
    #
    # Entering at the minimum stake maximises yield and deploys almost nothing:
    # on live data it left $1,760 of a $2,000 bankroll idle. Yield is the wrong
    # objective for someone asking where to put a bankroll; total daily payout
    # subject to the budget is the right one.
    #
    # Above the floor every chosen market's payout is concave again, so marginal
    # greedy is now correct - the discontinuity that broke it is behind us.
    # Topping up stops when the next dollar earns less than `min_marginal_yield`,
    # because past that point idle capital is the better holding.
    def per_share(r):
        return capital_required(1.0, r["mid"], r["two_sided"])

    step = 25.0
    while spent < budget and chosen:
        best, best_gain, best_cost = None, min_marginal_yield, 0.0
        for a in chosen:
            r = a["row"]
            cost = step * per_share(r)
            if cost <= 0 or spent + cost > budget:
                continue
            gain = (reward_at(r, a["shares"] + step, distance_cents)
                    - reward_at(r, a["shares"], distance_cents)) / cost
            if gain > best_gain:
                best, best_gain, best_cost = a, gain, cost
        if best is None:
            break
        best["shares"] += step
        spent += best_cost

    allocations = []
    for a in chosen:
        r = a["row"]
        allocations.append({
            "row": r,
            "shares": a["shares"],
            "capital": a["shares"] * per_share(r),
            "daily": reward_at(r, a["shares"], distance_cents),
        })
    allocations.sort(key=lambda a: -a["capital"])
    daily = sum(a["daily"] for a in allocations)
    capital = sum(a["capital"] for a in allocations)
    return allocations, {
        "capital": capital,
        "daily": daily,
        "yield": daily / capital if capital else 0.0,
        "markets": len(allocations),
        # every market is a bid and an ask, and both have to be maintained
        "orders": len(allocations) * 2,
        # Left over on purpose. Past the best stake in every market worth
        # quoting, more capital buys a worse rate than the capital already
        # working, so the honest answer is that the bankroll does not all fit.
        "unspent": max(0.0, budget - capital),
    }
