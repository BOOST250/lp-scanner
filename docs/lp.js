/*
 * Mirror of scoring.py, kept small on purpose.
 *
 * The page recomputes rather than reading precomputed numbers, so changing the
 * size or the bankroll answers instantly instead of waiting for the next scan.
 * That only works if the two implementations agree, so this file holds the
 * formulas and nothing else - no fetching, no DOM. Any change here has a twin
 * in scoring.py.
 */

export const ONE_SIDED_DIVISOR = 3.0;
export const MIN_PAYOUT_USD = 1.0;
const TWO_SIDED_BELOW = 0.10, TWO_SIDED_ABOVE = 0.90;

/* S(v,s) = ((v-s)/v)^2 * size. The square is the whole point: on a 5.5c band an
   order 1c from mid scores 0.67 of full, at 4c it scores 0.074. */
export function orderScore(distance, size, maxSpread) {
  if (maxSpread <= 0 || distance > maxSpread || size <= 0) return 0;
  return Math.pow((maxSpread - distance) / maxSpread, 2) * size;
}

export const requiresTwoSided = mid => !(mid >= TWO_SIDED_BELOW && mid <= TWO_SIDED_ABOVE);

/* Q_min. Quoting both sides scores in full; one side scores a third in the
   middle band and nothing at the extremes. */
export function myScore(size, distance, mid, maxSpread, twoSided) {
  const one = orderScore(distance, size, maxSpread);
  if (requiresTwoSided(mid)) return twoSided ? one : 0;
  return twoSided ? one : one / ONE_SIDED_DIVISOR;
}

/* A two-sided quote ties up capital on both legs. */
export const capitalPerShare = row => row.mid * (row.two_sided ? 2 : 1);

export function rewardAt(row, size, distance) {
  if (size <= 0) return 0;
  const mine = myScore(size, distance, row.mid, row.max_spread, row.two_sided);
  if (mine <= 0) return 0;
  const pay = row.rate * mine / (row.existing_score + mine);
  return pay >= MIN_PAYOUT_USD ? pay : 0;
}

/* Everything the row shows, at the user's size. */
export function recompute(row, size, distance) {
  const daily = rewardAt(row, size, distance);
  const capital = size * capitalPerShare(row);
  const mine = myScore(size, distance, row.mid, row.max_spread, row.two_sided);
  const share = mine > 0 ? mine / (row.existing_score + mine) : 0;

  /* Capacity: dollars before the yield falls to 1%/day. Solving
     rate * mine/(e + mine) / capital = target for size. */
  const w = myScore(1, distance, row.mid, row.max_spread, row.two_sided);
  const per = capitalPerShare(row);
  let capacity = 0;
  if (w > 0 && per > 0) {
    const target = 0.01;
    const n = (row.rate * w - target * per * row.existing_score) / (target * per * w);
    capacity = Math.max(0, n * per);
  }
  return { ...row, daily_usd: daily, capital, my_share: share, capacity,
           daily_yield: capital ? daily / capital : 0,
           below_min_size: size < (row.min_size || 0) };
}

/* Smallest stake that still clears the $1 floor, and its yield.
   Yield strictly decreases with size, so this is the best price in the market. */
export function minStake(row, distance) {
  const v = row.max_spread, mid = row.mid;
  if (v <= 0 || mid <= 0 || distance > v || row.rate <= MIN_PAYOUT_USD) return null;
  const w = myScore(1, distance, mid, v, row.two_sided);
  if (w <= 0) return null;
  let n = row.existing_score / (w * (row.rate / MIN_PAYOUT_USD - 1));
  n = Math.max(n, row.min_size || 0);
  let daily = rewardAt(row, n, distance);
  if (daily <= 0) { n *= 1.02; daily = rewardAt(row, n, distance); if (daily <= 0) return null; }
  const capital = n * capitalPerShare(row);
  return { shares: n, capital, daily, yield: capital ? daily / capital : 0 };
}

/*
 * Two phases, because the $1 floor makes the payout discontinuous rather than
 * concave and a single marginal-greedy pass gets it badly wrong - on live data
 * it put $1,812 of a $2,000 bankroll into one market earning $9.88 a day while
 * a $6 stake elsewhere earned $13.
 *
 *   1. Entry: rank by yield at the minimum viable stake and buy down the list.
 *      That is the question the floor actually poses.
 *   2. Top-up: within the chosen set every payout is concave again, so marginal
 *      greedy is now correct. Stop when the next dollar earns less than the
 *      opportunity cost - entering at minimum stakes everywhere maximises yield
 *      but leaves most of the bankroll idle, which is not what "where do I put
 *      $2,000" means.
 */
export function allocate(rows, budget, distance, maxMarkets = 20, minMarginal = 0.002) {
  const empty = { capital: 0, daily: 0, yield: 0, markets: 0, orders: 0, unspent: Math.max(0, budget) };
  if (budget <= 0 || !rows.length) return { allocations: [], totals: empty };

  const ranked = [];
  for (const r of rows) {
    const st = minStake(r, distance);
    if (st) ranked.push({ row: r, ...st });
  }
  ranked.sort((a, b) => b.yield - a.yield);

  const chosen = [];
  let spent = 0;
  for (const c of ranked) {
    if (chosen.length >= maxMarkets || spent + c.capital > budget) continue;
    chosen.push({ row: c.row, shares: c.shares });
    spent += c.capital;
  }

  const STEP = 25;
  while (spent < budget && chosen.length) {
    let best = null, bestGain = minMarginal, bestCost = 0;
    for (const a of chosen) {
      const cost = STEP * capitalPerShare(a.row);
      if (cost <= 0 || spent + cost > budget) continue;
      const gain = (rewardAt(a.row, a.shares + STEP, distance) - rewardAt(a.row, a.shares, distance)) / cost;
      if (gain > bestGain) { best = a; bestGain = gain; bestCost = cost; }
    }
    if (!best) break;
    best.shares += STEP;
    spent += bestCost;
  }

  const allocations = chosen.map(a => ({
    row: a.row, shares: a.shares,
    capital: a.shares * capitalPerShare(a.row),
    daily: rewardAt(a.row, a.shares, distance),
  })).sort((x, y) => y.capital - x.capital);

  const capital = allocations.reduce((s, a) => s + a.capital, 0);
  const daily = allocations.reduce((s, a) => s + a.daily, 0);
  return { allocations, totals: {
    capital, daily, yield: capital ? daily / capital : 0,
    markets: allocations.length, orders: allocations.length * 2,
    unspent: Math.max(0, budget - capital),
  } };
}

/* Reasons not to quote here, in plain terms. An empty book is the loudest one:
   every other maker looked at this market and declined. */
export function flagsFor(r) {
  const f = [];
  if (r.existing_score < 50) f.push({ t: 'üres könyv', hard: true });
  if (r.hours_left != null && r.hours_left < 24) f.push({ t: `${Math.round(r.hours_left)}h múlva zár`, hard: false });
  if (r.vol_c != null && r.vol_c > 2) f.push({ t: `volatilis ${r.vol_c.toFixed(1)}c/h`, hard: true });
  if (r.two_sided) f.push({ t: 'két oldal kell', hard: false });
  if (r.below_min_size) f.push({ t: `méret < ${r.min_size}`, hard: false });
  if (r.daily_usd === 0) f.push({ t: '<$1, nem fizet', hard: false });
  return f;
}
