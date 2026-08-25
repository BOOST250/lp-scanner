/**
 * Read-only relay for Polymarket's public data endpoints.
 *
 * WHY THIS EXISTS
 *   data-api.polymarket.com already sends Access-Control-Allow-Origin: *, so
 *   CORS is not the obstacle. The obstacle is that Hungarian ISPs reset the TLS
 *   handshake on any *.polymarket.com SNI - measured from this network, the
 *   endpoint returns HTTP 000 while github.io returns 200. So the published
 *   dashboard would load fine for everyone except its owner. This function runs
 *   on Supabase's infrastructure, outside that network.
 *
 * WHAT IT WILL RELAY
 *   Only the paths below, only GET, only public data. Everything reachable here
 *   is already world-readable without authentication - a wallet address is a
 *   public identifier, not a credential - so the risk being managed is the
 *   function being used as a general-purpose proxy, not disclosure.
 *
 *   GET ?path=positions&user=0x...
 *   GET ?path=trades&user=0x...&limit=500
 *   GET ?path=activity&user=0x...&limit=500
 *   GET ?path=prices-history&market=<token>&startTs=<unix>&fidelity=<mins>
 *
 * NO CREDENTIALS ARE ACCEPTED OR FORWARDED. Open orders and actual reward
 * payouts need signed L2 authentication, and this deliberately cannot reach
 * them: an unauthenticated relay cannot leak keys it never receives.
 *
 * Deploy: supabase functions deploy pm --no-verify-jwt
 */

const DATA = "https://data-api.polymarket.com";
const CLOB = "https://clob.polymarket.com";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
};

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { ...cors, "Content-Type": "application/json", "Cache-Control": "no-store" },
  });

const ADDRESS = /^0x[a-fA-F0-9]{40}$/;
const TOKEN = /^[0-9]{1,120}$/;
const DIGITS = /^[0-9]{1,12}$/;

/** Build the upstream URL, or return null if anything about the request is off. */
function upstreamFor(p: URLSearchParams): string | null {
  const path = p.get("path") ?? "";

  if (path === "positions" || path === "trades" || path === "activity") {
    const user = p.get("user") ?? "";
    if (!ADDRESS.test(user)) return null;
    const limit = p.get("limit") ?? "500";
    if (!DIGITS.test(limit)) return null;
    const capped = Math.min(parseInt(limit, 10), 1000);
    const offset = p.get("offset") ?? "0";
    if (!DIGITS.test(offset)) return null;
    return `${DATA}/${path}?user=${user}&limit=${capped}&offset=${offset}`;
  }

  if (path === "prices-history") {
    const market = p.get("market") ?? "";
    if (!TOKEN.test(market)) return null;
    const startTs = p.get("startTs") ?? "";
    if (!DIGITS.test(startTs)) return null;
    const fidelity = p.get("fidelity") ?? "1";
    if (!DIGITS.test(fidelity)) return null;
    return `${CLOB}/prices-history?market=${market}&startTs=${startTs}&fidelity=${fidelity}`;
  }

  return null;
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "GET") return json({ error: "GET only" }, 405);

  const url = upstreamFor(new URL(req.url).searchParams);
  if (!url) return json({ error: "unsupported path or malformed parameters" }, 400);

  try {
    const upstream = await fetch(url, {
      headers: { "User-Agent": "lp-scanner/1.0" },
      signal: AbortSignal.timeout(20_000),
    });
    if (!upstream.ok) return json({ error: `upstream ${upstream.status}` }, 502);
    return json(await upstream.json());
  } catch (e) {
    return json({ error: e instanceof Error ? e.message : "relay failed" }, 502);
  }
});
