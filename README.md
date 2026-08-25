# LP Scanner

Live ranking of Polymarket's liquidity-reward markets: where resting capital is
actually paid, how much it can absorb, and what it costs in risk to rest there.

**→ [boost250.github.io/lp-scanner](https://boost250.github.io/lp-scanner/)**

A GitHub Action re-scans every ten minutes and commits the result; the page
reloads it every minute. That cadence is not a compromise — reward scores are
sampled once a minute, and the decision this feeds is *where should capital rest
for the next several hours*, so a few minutes of age costs nothing.

Two views:

- **Jelzések** — one ranked list, ordered by *daily dollars at your size*, not by
  yield percentage. Yield ranking puts trivially small opportunities on top; a
  250%/day row on $25 of capital is not a business. Expanding a row shows where
  the competing score sits across the band, the quadratic score curve with your
  distance marked, and how the competition has moved over the past week.
- **Allokáció** — give it a bankroll and a risk tolerance and it says *what goes
  where*, with the daily total, the blended yield, and how many resting orders
  that means. Twenty markets is forty orders, re-quoted as the mids move.

**Change the size or the bankroll and every number recomputes in the browser.**
`docs/lp.js` mirrors `scoring.py`, so the page answers instantly instead of
waiting for the next scan. The two are cross-checked: on the same input both
produce $356 deployed across 20 markets for $206.49/day.

```bash
python scanner.py                 # one scan
python scanner.py --watch         # refresh every 30s, writes docs/data/state.json
python scanner.py --size 500      # size the quote in shares
```

Needs the SOCKS tunnel (`POLYMARKET_PROXY_URL`, default
`socks5://127.0.0.1:40000`) because Hungarian ISPs reset the TLS handshake on any
`*.polymarket.com` SNI. Set it empty on an unfiltered network. stdlib plus curl,
no dependencies.

**No API credentials.** Everything here is public. Credentials would only add two
things — your open orders and your actual reward payouts — and both belong to the
retrospective half of the project, which this is not.

---

## The three numbers, and why the obvious one is a trap

Sorting reward markets by yield produces a list of traps. Every market at the top
is empty, and it is empty for a reason. Three columns are needed before the
ranking means anything.

### Yield, after dilution

Rewards split by score share, so your own size is in the denominator:

```
share = my_score / (existing_score + my_score)
```

A market paying $106/day against $42 of existing score reads as 165% a day right
up until you are the one setting the denominator. The scanner always quotes the
yield at *your* size, never the yield on the book as it stands.

### Capacity

The number the headline yield hides: dollars you could rest before the yield
falls to 1% a day. A row showing 250% a day on $25 of capital may not clear 1% a
day on $2,000 — and if it cannot, the opportunity is not worth the effort of
quoting it. Solving `rate * mine/(existing + mine) / capital = target` for size
gives the point where it stops being interesting.

### Volatility

A resting quote is a free option written to whoever knows more than you. The
reward is the premium; the price travelling while you rest is the cost. Hourly
standard deviation of the mid, in cents, is a crude proxy and an effective one.

Measured on the first real scan, **every** empty-book market with a spectacular
yield was volatile — 10.3, 8.7, 6.3, 4.6 cents an hour — against 0.3–0.8 for the
contested markets worth quoting. Resting a cent from mid in a market that moves
four cents an hour means being run over several times a day. That is the whole
answer to "why is nobody else here".

---

## Allocating a bankroll

Two wrong answers came before the right one, and both are worth knowing because
each looks obviously correct until you check the output.

**Marginal greedy is wrong here.** Each market's payout looks concave —
`rate * w·n / (e + w·n)` — which invites greedy allocation by marginal yield. But
the $1/day floor makes it *discontinuous*: a market needing a large stake to
cross the floor gains nothing at every step until it suddenly gains a dollar. Run
on live data, marginal greedy put **$1,812 of a $2,000 bankroll into one market
earning $9.88 a day** while a $6 stake elsewhere earned $13.

**Maximising yield is the wrong objective.** Since yield strictly decreases with
size, every market's yield-maximising stake is the smallest one that clears the
floor. Ranking by that and buying down the list is correct — and it deployed
**$240 of $2,000**, leaving the rest idle at a headline 30%/day. Nobody asking
"where do I put $2,000" wants that answer.

So allocation runs in two phases:

1. **Entry** ranks by yield at the minimum viable stake, which is exactly the
   question the floor poses: *if I take the cheapest position that gets paid
   here, how good is it?*
2. **Top-up** redistributes the rest by marginal yield. Within the chosen set
   every payout is concave again — the discontinuity is behind us — so greedy is
   now correct. It stops when the next dollar earns less than the opportunity
   cost, which is why the planner sometimes reports capital left over.

**Leftover capital is a finding, not a failure.** Markets with an empty book pay
their whole pool to the first qualifying order, so they saturate at a few dollars
and more capital earns nothing there. Measured live: $2,000 across 20 markets
deploys $356; across 150 markets it deploys the lot. Restricted to *contested*
markets it deploys the full bankroll at 3.21%/day — a believable number, and the
one to trust.

## Scoring

`scoring.py` is the only place the reward mechanics live, because getting them
wrong produces a plausible ranking that sends capital to the wrong markets.

```
S(v, s) = ((v - s) / v)^2 * size
```

`v` is the market's maximum qualifying spread in cents, `s` the order's distance
from the midpoint. **The square is the part that is easy to miss.** On a market
with `v = 5.5c`, an order 1c from mid scores 0.67 of full; at 4c it scores 0.074.

So measuring competition by notional depth badly overstates it. On one market
during development, $12,660 of qualifying depth carried a sixth of the score that
flat weighting implied — the depth was all sitting near the edge of the band.

A book spread wider than the reward band is therefore **not** a disqualification.
It is the mechanism behind an empty score: the existing orders are all outside
the qualifying band, and the pool is unclaimed.

### One side or two

```
midpoint in [0.10, 0.90]:  Q_min = max(min(Q_one, Q_two), max(Q_one/c, Q_two/c))
midpoint outside:          Q_min = min(Q_one, Q_two)
```

with `c = 3.0`. In the middle band a single side still scores, at a third. Near
the extremes it scores nothing — and those are exactly the markets where the far
side is a 2c lottery ticket that can gap to zero, so two-sided is a real capital
and risk cost, not paperwork.

### Sampling

Orders are sampled once a minute, aggregated over a 10,080-sample weekly epoch,
and the pool splits by normalised score share. Two consequences:

- **Resting continuously is the game.** A quote up half the time earns half the
  score. This is why `--watch` refreshes at 30s: faster buys nothing, because the
  scorer is not looking more often than once a minute.
- **Minimum payout is $1.** A share worth less pays zero, which quietly makes
  small positions in crowded markets worthless. Rows below it are flagged.

---

## Known approximations

- **Midpoint.** Polymarket scores against a size-adjusted midpoint; this uses the
  plain `(best_bid + best_ask) / 2`. On thin or lopsided books the two differ,
  which biases the score of top-of-book orders.
- **In-game multiplier.** The `b` term in the documented formula is not modelled.
- **Competition is a snapshot.** Existing score is read now; other makers move.
- **Expired markets are dropped.** A market past its end date still carries a
  book and a reward rate, and is not somewhere to put capital.

## What this deliberately does not answer

Whether the empty markets are genuinely mispriced or correctly shunned. The
volatility column is circumstantial evidence, not proof. Settling it needs the
other half: your own fills, the mid a few minutes after each one, and the
resulting split of PnL into spread capture, inventory drift, rewards and fees.
Until that exists, an empty book with a high rate is an open question — not a
free lunch.

## Layout

```
scoring.py               reward mechanics: score, Q_min, dilution, allocation
scanner.py               fetch, rank, band profile, history, terminal table
docs/index.html          the dashboard - no build step, no dependencies
docs/lp.js               mirror of scoring.py, so the page recomputes live
docs/data/state.json     latest scan, committed by CI
docs/data/history.json   hourly competition and rate per market, one week
markets_cache.json       market metadata, committed so CI never starts cold
.github/workflows/       ten-minute scan and commit
```

`markets_cache.json` is tracked deliberately. Metadata needs one CLOB call per
market, and a cold cache means 500 sequential lookups: about half of them fail
under rate limiting, and the board silently shrinks. A market missing from the
board does not read as missing — it reads as *no competition here*, which is the
most expensive thing this tool could get wrong.
