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
