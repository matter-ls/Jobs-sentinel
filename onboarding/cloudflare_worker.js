/**
 * Jobs Sentinel — Cloudflare Worker Webhook Relay (Optional Alternative to Google Apps Script)
 *
 * Environment Secrets to configure in Cloudflare Worker Settings -> Variables and Secrets:
 *   - GITHUB_REPO    : e.g. "your-org/Jobs-sentinel"
 *   - GITHUB_PAT     : Fine-grained GitHub PAT with "Contents: Read and write" on GITHUB_REPO
 *   - INVITE_CODE    : (Optional) shared passcode to restrict public submissions
 *   - ALLOWED_ORIGIN : (Optional) e.g. "https://your-org.github.io" (defaults to "*")
 */

function constantTimeEqual(a, b) {
  const sa = String(a || "");
  const sb = String(b || "");
  let diff = sa.length ^ sb.length;
  const maxLen = Math.max(sa.length, sb.length);
  for (let i = 0; i < maxLen; i++) {
    diff |= (i < sa.length ? sa.charCodeAt(i) : 0) ^ (i < sb.length ? sb.charCodeAt(i) : 0);
  }
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const corsHeaders = {
      "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Content-Type": "application/json; charset=utf-8",
    };
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders });
    }
    if (request.method !== "POST") {
      return new Response(JSON.stringify({ ok: false, error: "Method not allowed" }), { status: 405, headers: corsHeaders });
    }
    try {
      const raw = await request.text();
      if (raw.length > 520000) {
        return new Response(JSON.stringify({ ok: false, error: "Payload too large" }), { status: 413, headers: corsHeaders });
      }
      const body = JSON.parse(raw);
      if (env.INVITE_CODE && !constantTimeEqual(String(body.invite_code || "").trim(), String(env.INVITE_CODE).trim())) {
        return new Response(JSON.stringify({ ok: false, error: "Invalid Team Invite Code." }), { status: 403, headers: corsHeaders });
      }
      const email = String(body.email || "").trim().toLowerCase();
      const name = String(body.name || "").trim().slice(0, 80);
      const cvText = String(body.cv_text || "").trim().slice(0, 45000);
      if (!email.includes("@") || cvText.length < 60) {
        return new Response(JSON.stringify({ ok: false, error: "Valid email and plain-text CV required." }), { status: 400, headers: corsHeaders });
      }
      const ghRes = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
        method: "POST",
        headers: {
          "Accept": "application/vnd.github+json",
          "Authorization": `Bearer ${env.GITHUB_PAT}`,
          "User-Agent": "Jobs-Sentinel-Onboarding-Worker",
          "X-GitHub-Api-Version": "2022-11-28",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          event_type: "onboard_subscriber",
          client_payload: {
            name,
            email,
            cv_text: cvText,
            levels: Array.isArray(body.levels) ? body.levels.slice(0, 8) : [],
            domains: Array.isArray(body.domains) ? body.domains.slice(0, 10) : [],
            locations: Array.isArray(body.locations) ? body.locations.slice(0, 5) : [],
            language: String(body.language || "English").slice(0, 24),
            free_text: String(body.free_text || "").slice(0, 4000),
          },
        }),
      });
      if (ghRes.status !== 204 && ghRes.status !== 200) {
        return new Response(JSON.stringify({ ok: false, error: `GitHub dispatch HTTP ${ghRes.status}` }), { status: 502, headers: corsHeaders });
      }
      return new Response(
        JSON.stringify({
          ok: true,
          message: `Welcome aboard, ${name || email}! Lucy is adding your profile to the codebase now — check ${email} in ~60 seconds for your confirmation email.`,
        }),
        { status: 200, headers: corsHeaders }
      );
    } catch (err) {
      return new Response(JSON.stringify({ ok: false, error: String(err.message || err) }), { status: 400, headers: corsHeaders });
    }
  },
};
