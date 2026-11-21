/**
 * Jobs Sentinel — Zero-Cost Google Apps Script Webhook Relay
 *
 * Safely connects your static `docs/onboard.html` form (hosted on GitHub Pages or anywhere)
 * to your private GitHub repository's `.github/workflows/onboard.yml` (`repository_dispatch`)
 * WITHOUT ever exposing your GitHub Personal Access Token in browser JavaScript.
 *
 * 2-MINUTE SETUP:
 * 1. Open https://script.google.com -> click "New project".
 * 2. Paste this file into `Code.gs` and click Save.
 * 3. Go to Project Settings (gear icon) -> "Script Properties" -> Add 3 properties:
 *      - GITHUB_REPO   : your_github_username_or_org/Jobs-sentinel
 *      - GITHUB_PAT    : Fine-grained GitHub Personal Access Token scoped ONLY to this repo
 *                        with "Contents: Read and write" permission.
 *      - INVITE_CODE   : (Optional) shared team passcode, e.g. "sentinel2026" (leave empty to allow open link access)
 * 4. Click "Deploy" -> "New deployment" -> Type: "Web app":
 *      - Execute as: "Me"
 *      - Who has access: "Anyone"
 * 5. Copy the Web App `.../exec` URL and set it in your GitHub repo:
 *      Settings -> Secrets and variables -> Actions -> Variables -> `ONBOARD_WEBHOOK_URL`
 *      (or set `"onboard_webhook_url"` in `config.json`).
 */

function constantTimeEquals_(a, b) {
  var sa = String(a || "");
  var sb = String(b || "");
  var diff = sa.length ^ sb.length;
  var maxLen = Math.max(sa.length, sb.length);
  for (var i = 0; i < maxLen; i++) {
    var ca = i < sa.length ? sa.charCodeAt(i) : 0;
    var cb = i < sb.length ? sb.charCodeAt(i) : 0;
    diff |= (ca ^ cb);
  }
  return diff === 0;
}

function jsonResponse_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return jsonResponse_({ ok: false, error: "Empty request body." });
    }
    var props = PropertiesService.getScriptProperties();
    var repo = (props.getProperty("GITHUB_REPO") || "").trim();
    var pat = (props.getProperty("GITHUB_PAT") || "").trim();
    var requiredInvite = (props.getProperty("INVITE_CODE") || "").trim();

    if (!repo || !pat) {
      return jsonResponse_({ ok: false, error: "Server relay missing GITHUB_REPO or GITHUB_PAT Script Properties." });
    }

    var body = JSON.parse(e.postData.contents);
    if (requiredInvite && !constantTimeEquals_(String(body.invite_code || "").trim(), requiredInvite)) {
      return jsonResponse_({ ok: false, error: "Invalid Team Invite Code." });
    }

    var email = String(body.email || "").trim().toLowerCase();
    var name = String(body.name || "").trim().slice(0, 80);
    var cvText = String(body.cv_text || "").trim().slice(0, 45000);
    if (!email || email.indexOf("@") < 1 || cvText.length < 60) {
      return jsonResponse_({ ok: false, error: "Please provide a valid email and plain-text CV (at least 60 characters)." });
    }

    // 60-second anti-double-click throttle per email
    var cache = CacheService.getScriptCache();
    var rateKey = "onboard_" + email.replace(/[^a-z0-9]/g, "_").slice(0, 60);
    if (cache.get(rateKey)) {
      return jsonResponse_({ ok: true, message: "Your onboarding request is already running — check your inbox in ~60 seconds!" });
    }
    cache.put(rateKey, "1", 60);

    // GitHub repository_dispatch allows max 10 top-level keys and <= 65,535 bytes total
    var dispatchPayload = {
      event_type: "onboard_subscriber",
      client_payload: {
        name: name,
        email: email,
        cv_text: cvText,
        levels: Array.isArray(body.levels) ? body.levels.slice(0, 8) : [],
        domains: Array.isArray(body.domains) ? body.domains.slice(0, 10) : [],
        locations: Array.isArray(body.locations) ? body.locations.slice(0, 5) : [],
        language: String(body.language || "English").slice(0, 24),
        free_text: String(body.free_text || "").slice(0, 4000)
      }
    };

    var resp = UrlFetchApp.fetch("https://api.github.com/repos/" + repo + "/dispatches", {
      method: "post",
      contentType: "application/json",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + pat,
        "X-GitHub-Api-Version": "2022-11-28"
      },
      payload: JSON.stringify(dispatchPayload),
      muteHttpExceptions: true
    });

    var code = resp.getResponseCode();
    if (code !== 204 && code !== 200) {
      return jsonResponse_({ ok: false, error: "GitHub dispatch returned HTTP " + code + "." });
    }

    return jsonResponse_({
      ok: true,
      message: "Welcome aboard, " + (name || email) + "! Lucy is calibrating your CV & adding you to the codebase right now — check " + email + " in ~60 seconds for your confirmation email."
    });
  } catch (err) {
    return jsonResponse_({ ok: false, error: "Relay error: " + String(err && err.message ? err.message : err) });
  }
}
