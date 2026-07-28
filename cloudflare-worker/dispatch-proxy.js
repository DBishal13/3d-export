// Holds the repo-scoped GitHub token server-side so visitors to the docs UI
// never need their own PAT. Only proxies the workflow_dispatch POST (the one
// GitHub Actions call that genuinely requires write auth) — run-status
// polling and artifact listing stay direct, unauthenticated client calls,
// since GitHub's read endpoints already allow anonymous access on public
// repos (rate-limited per caller IP, so that traffic can't affect this
// project's own GitHub Actions quota).
const REPO = "DBishal13/3d-export";
const WORKFLOW_FILE = "generate-map.yml";
const REF = "main";
const ALLOWED_ORIGIN = "https://dbishal13.github.io";

const ALLOWED_INPUT_KEYS = ["country_code", "aoi_geojson", "output_format", "mode", "aggregate", "email", "title"];
const PER_IP_LIMIT_PER_HOUR = 5;
const GLOBAL_LIMIT_PER_DAY = 50;
// email triggers a real side effect against a third party (an inbox that
// isn't necessarily the caller's own), so it gets its own, stricter budget
// independent of the general dispatch limits above.
const EMAIL_PER_IP_LIMIT_PER_HOUR = 2;
const EMAIL_GLOBAL_LIMIT_PER_DAY = 10;

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return corsPreflightResponse();
    if (request.method !== "POST") return jsonResponse({ error: "Method not allowed" }, 405);

    // CORS headers only stop a *browser* from reading a cross-origin response;
    // they don't stop a direct request (curl, a script) from reaching this
    // Worker at all. This check rejects anything not claiming to come from
    // the docs site -- it stops casual/browser-based misuse, but a scripted
    // caller can still set its own Origin header to match, same as any
    // Origin check on a public, unauthenticated endpoint. Real bot
    // resistance would need something like Cloudflare Turnstile; not worth
    // the added complexity for a low-traffic hobby project right now.
    const origin = request.headers.get("Origin");
    if (origin !== ALLOWED_ORIGIN) {
      return jsonResponse({ error: "Forbidden origin." }, 403);
    }

    const ip = request.headers.get("CF-Connecting-IP") || "unknown";

    const globalOk = await consumeQuota(env, "global:" + dayKey(), GLOBAL_LIMIT_PER_DAY, 60 * 60 * 24);
    if (!globalOk) {
      return jsonResponse({ error: "This demo has hit its daily run limit. Try again tomorrow." }, 429);
    }

    const ipOk = await consumeQuota(env, "ip:" + ip, PER_IP_LIMIT_PER_HOUR, 60 * 60);
    if (!ipOk) {
      return jsonResponse({ error: "Too many runs from this IP. Wait a bit and try again." }, 429);
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResponse({ error: "Invalid JSON body." }, 400);
    }

    const inputs = {};
    for (const key of ALLOWED_INPUT_KEYS) {
      inputs[key] = typeof body[key] === "string" ? body[key] : "";
    }

    if (inputs.email) {
      const emailGlobalOk = await consumeQuota(env, "email-global:" + dayKey(), EMAIL_GLOBAL_LIMIT_PER_DAY, 60 * 60 * 24);
      if (!emailGlobalOk) {
        return jsonResponse({ error: "Email delivery has hit its daily limit. Try again tomorrow, or leave the email field blank." }, 429);
      }
      const emailIpOk = await consumeQuota(env, "email-ip:" + ip, EMAIL_PER_IP_LIMIT_PER_HOUR, 60 * 60);
      if (!emailIpOk) {
        return jsonResponse({ error: "Too many emailed runs from this IP. Wait a bit, or leave the email field blank." }, 429);
      }
    }

    const ghResponse = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "Content-Type": "application/json",
          "User-Agent": "3d-export-dispatch-proxy",
        },
        body: JSON.stringify({ ref: REF, inputs }),
      }
    );

    if (ghResponse.status === 204) {
      return jsonResponse({ ok: true, dispatchedAt: new Date().toISOString() }, 200);
    }

    const text = await ghResponse.text();
    return jsonResponse({ ok: false, githubStatus: ghResponse.status, message: text }, 502);
  },
};

// Fixed-window counter in KV. Good enough for a soft abuse guard on a hobby
// project's Actions quota — not meant to be exact under concurrent bursts.
async function consumeQuota(env, key, limit, ttlSeconds) {
  const current = parseInt((await env.RATE_LIMIT_KV.get(key)) || "0", 10);
  if (current >= limit) return false;
  await env.RATE_LIMIT_KV.put(key, String(current + 1), { expirationTtl: ttlSeconds });
  return true;
}

function dayKey() {
  return new Date().toISOString().slice(0, 10);
}

function jsonResponse(data, status) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
      Vary: "Origin",
    },
  });
}

function corsPreflightResponse() {
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Max-Age": "86400",
      Vary: "Origin",
    },
  });
}
