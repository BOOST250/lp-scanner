/*
 * Your own fills, decomposed into the parts that mean different things.
 *
 * WHY A DECOMPOSITION AND NOT JUST "PnL"
 *
 *   A market maker who is up $100 has learned nothing from that number. The
 *   money could have come from capturing spread, which is the job, or from
 *   happening to be long something that went up, which is a directional bet
 *   wearing a market-making costume. Those have opposite implications for
 *   whether to keep going, so they are reported separately here:
 *
 *     spread capture   realised on round trips - bought passive, sold passive
 *     inventory        open position marked at the current price
 *     fees             entryFeesUsdc, only available for positions still open
 *     markout          where the mid went after each of your fills
 *
 *   Markout is the one that answers the question the scanner cannot. A resting
 *   quote is a free option written to whoever knows more than you; markout
 *   measures what that option cost. Persistently negative markout means you are
 *   being picked off, and no reward rate compensates for it.
 *
 * WHAT IS MISSING, AND WHY
 *
 *   Reward payouts and open orders need signed L2 authentication, which the
 *   relay deliberately cannot do. So this shows what your trading did, not what
 *   the rewards programme paid you - the two have to be added by hand for now.
 *   Fees on closed positions are also unavailable: /positions only carries them
 *   for positions you still hold.
 */

const RELAY = 'https://sfrstdphwmedknmdkvbt.supabase.co/functions/v1/pm';

/* Windows for the markout, in minutes. Five is roughly "was this fill stale the
   moment it happened"; thirty is "did the market move against it after". */
export const MARKOUTS = [5, 30];

const q = params => RELAY + '?' + new URLSearchParams(params);

export async function fetchAccount(address, limit = 500) {
  const [positions, trades] = await Promise.all([
    fetch(q({ path: 'positions', user: address, limit: 500 })).then(r => r.json()),
    fetch(q({ path: 'trades', user: address, limit })).then(r => r.json()),
  ]);
  if (!Array.isArray(positions) || !Array.isArray(trades)) throw new Error('váratlan válasz');
  return { positions, trades };
}

/*
 * Walk one asset's fills in order, average-cost style.
 *
 * Selling more than you hold is not an error to reject: on Polymarket the
 * opposite outcome token is economically a short, and the feed does not always
 * show both legs. Rather than silently mis-attributing, inventory is allowed to
 * go negative and the average cost follows the same rule mirrored.
 */
function walk(fills) {
  let inv = 0, avg = 0, realised = 0, bought = 0, sold = 0;
  for (const f of fills) {
    const size = f.size, price = f.price;
    const dir = f.side === 'BUY' ? 1 : -1;
    if (dir > 0) bought += size * price; else sold += size * price;

    if (inv === 0 || Math.sign(inv) === dir) {
      // opening or adding: blend the cost
      const total = Math.abs(inv) + size;
      avg = total ? (Math.abs(inv) * avg + size * price) / total : price;
      inv += dir * size;
    } else {
      // reducing: realise against the average, then flip if it overshoots
      const closing = Math.min(size, Math.abs(inv));
      realised += closing * (price - avg) * Math.sign(inv);
      inv += dir * size;
      if (Math.sign(inv) === dir && inv !== 0) avg = price;   // flipped through zero
    }
  }
  return { inv, avg, realised, bought, sold };
}

/** Fills for one asset, oldest first. The feed arrives newest first. */
function byAsset(trades) {
  const m = new Map();
  for (const t of trades) {
    if (!m.has(t.asset)) m.set(t.asset, []);
    m.get(t.asset).push({ side: t.side, size: +t.size, price: +t.price, t: +t.timestamp,
                          title: t.title, outcome: t.outcome, slug: t.slug });
  }
  for (const arr of m.values()) arr.sort((a, b) => a.t - b.t);
  return m;
}

/*
 * Price series for the assets you traded, so fills can be marked out.
 *
 * One call per asset covering its whole fill window, rather than one per fill -
 * a busy day is hundreds of fills across a handful of assets. Capped because
 * this runs in a page and each call is a relay round trip.
 */
async function fetchSeries(fills, cap = 40) {
  const out = new Map();
  // Busiest assets first: the markout is a weighted verdict, so the assets you
  // traded most are the ones it must cover. Taking them in map order instead
  // would let a single stray fill displace a hundred.
  const list = [...fills.entries()]
    .sort((a, b) => b[1].length - a[1].length)
    .slice(0, cap)
    .map(([asset]) => asset);
  await Promise.all(list.map(async asset => {
    const arr = fills.get(asset);
    const start = Math.floor(arr[0].t) - 3600;
    try {
      const j = await fetch(q({ path: 'prices-history', market: asset, startTs: start, fidelity: 1 }))
        .then(r => r.json());
      const pts = (j.history || []).map(p => ({ t: +p.t, p: +p.p }));
      if (pts.length) out.set(asset, pts);
    } catch { /* a missing series just means no markout for that asset */ }
  }));
  return out;
}

/** Price at or just before `t`; null when the series does not reach that far. */
function priceAt(pts, t) {
  if (!pts || !pts.length || pts[0].t > t) return null;
  let lo = 0, hi = pts.length - 1, best = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].t <= t) { best = pts[mid].p; lo = mid + 1; } else hi = mid - 1;
  }
  return best;
}

/**
 * Everything, decomposed.
 *
 * Markout sign convention: positive means the price moved your way after the
 * fill. A buy followed by a rise is good; a sell followed by a rise is not.
 */
export async function analyse(address, { limit = 500 } = {}) {
  const { positions, trades } = await fetchAccount(address, limit);
  const fills = byAsset(trades);
  const series = await fetchSeries(fills);
  const posByAsset = new Map(positions.map(p => [p.asset, p]));

  const markets = [];
  let spread = 0, inventory = 0, fees = 0;
  const markout = Object.fromEntries(MARKOUTS.map(m => [m, { sum: 0, n: 0 }]));
  const perFill = [];

  for (const [asset, arr] of fills) {
    const w = walk(arr);
    const pos = posByAsset.get(asset);
    const cur = pos ? +pos.curPrice : null;
    const open = pos ? +pos.size : Math.abs(w.inv) < 1e-6 ? 0 : w.inv;
    const unreal = pos && cur != null ? (cur - +pos.avgPrice) * +pos.size : 0;
    const fee = pos ? +(pos.entryFeesUsdc || 0) : 0;

    spread += w.realised;
    inventory += unreal;
    fees += fee;

    const pts = series.get(asset);
    const mk = Object.fromEntries(MARKOUTS.map(m => [m, pts ? 0 : null]));
    for (const f of arr) {
      const dir = f.side === 'BUY' ? 1 : -1;
      const row = { ...f, asset, markout: {} };
      for (const m of MARKOUTS) {
        const after = priceAt(pts, f.t + m * 60);
        if (after == null) continue;
        const v = (after - f.price) * dir * f.size;
        mk[m] += v;
        markout[m].sum += v;
        markout[m].n += 1;
        row.markout[m] = v;
      }
      perFill.push(row);
    }

    markets.push({
      asset, title: arr[0].title, outcome: arr[0].outcome, slug: arr[0].slug,
      fills: arr.length, spread: w.realised, unrealised: unreal, fees: fee,
      open, avg: pos ? +pos.avgPrice : w.avg, cur, markout: mk,
      volume: w.bought + w.sold,
      first: arr[0].t, last: arr[arr.length - 1].t,
    });
  }

  markets.sort((a, b) => (b.spread + b.unrealised) - (a.spread + a.unrealised));
  perFill.sort((a, b) => b.t - a.t);

  return {
    markets, perFill,
    totals: {
      spread, inventory, fees, net: spread + inventory - fees,
      fills: perFill.length,
      volume: markets.reduce((s, m) => s + m.volume, 0),
      markout: Object.fromEntries(MARKOUTS.map(m => [m, markout[m]])),
      // Deliberate cap, not a fetch failure: only the busiest assets are marked
      // out, because each one costs a relay round trip from inside a page.
      marketsTotal: fills.size,
      marketsMarked: series.size,
    },
  };
}

/**
 * Fills bucketed into periods, for the per-minute/hour/day question.
 *
 * Spread capture is not attributed per period - a round trip spans two fills
 * and possibly two buckets, so splitting it would invent precision. Volume,
 * fill count and markout are honest per bucket, and markout is the one that
 * actually varies by time of day.
 */
export function byPeriod(perFill, seconds) {
  const buckets = new Map();
  for (const f of perFill) {
    const k = Math.floor(f.t / seconds) * seconds;
    if (!buckets.has(k)) buckets.set(k, { t: k, fills: 0, volume: 0, markout: {} });
    const b = buckets.get(k);
    b.fills += 1;
    b.volume += f.size * f.price;
    for (const m of MARKOUTS) if (f.markout[m] != null) b.markout[m] = (b.markout[m] || 0) + f.markout[m];
  }
  return [...buckets.values()].sort((a, b) => b.t - a.t);
}
