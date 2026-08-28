# LinkedIn Profile API

A hosted HTTPS API that accepts a LinkedIn profile URL and returns structured
JSON (name, headline, location, about, experience, education, skills,
certifications, languages, profile images) — built by directly reverse-
engineering LinkedIn's internal "Voyager" REST API via plain HTTP requests.
**No browser, no JavaScript execution** — every request is a direct call to
the same internal endpoints linkedin.com's own frontend calls, made with an
HTTP client that reproduces a real browser's TLS fingerprint rather than by
driving one. Also includes a second endpoint to find a profile URL from a
name plus company/email, mirroring PhantomBuster's separate "Profile
Scraper" and "Profile URL Finder" tools in one service.

> **Unofficial.** This talks to an undocumented, private API that LinkedIn
> can change or block at any time, and doing so is against LinkedIn's User
> Agreement. Use it against your own account/data, understand the risk to
> the LinkedIn account whose session you use (rate limiting, checkpoint
> challenges, suspension), and don't use this for bulk harvesting. See
> [Known limitations](#known-limitations).

## How it works (approach)

LinkedIn profile pages are rendered client-side from JSON the browser fetches
from `https://www.linkedin.com/voyager/api/identity/profiles/<public-id>/profileView`
once you're logged in. That single endpoint returns a normalized payload
covering the summary/headline/photo plus positions, education, skills,
certifications and languages in one shot, which is why this service only
needs one upstream call per profile.

**This went through three iterations, and each one was driven directly by
what live testing against a real account showed:**

1. Plain `httpx` calling `profileView` with `li_at` + `JSESSIONID` cookies
   and a matching `csrf-token` header — the "textbook" reverse-engineering
   approach, and how this endpoint URL/response shape was found in the
   first place. Live testing got a clean `403 CSRF check failed` on every
   attempt, even with fresh, valid cookies: a bare HTTP client's TLS/HTTP2
   handshake has a different fingerprint than a real browser's, and
   LinkedIn's edge filters on that before the CSRF token is meaningfully
   checked at all.
2. A headless-Chromium version (driving a real browser to make the same
   request) confirmed that theory — it got past the CSRF check. But a
   browser is explicitly outside this assignment's requirements (a pure
   reverse-engineered, direct-HTTP solution was asked for), so that path
   was abandoned rather than pursued further, despite having worked.
3. **Current approach**: [`curl_cffi`](https://github.com/lexiforest/curl_cffi),
   a library that reproduces a real Chrome build's TLS/JA3 fingerprint (and
   its matching header set — `Sec-Ch-Ua`, `Accept`, etc.) at the HTTP-client
   level, with no browser or JS involved. Verified live that this alone
   clears the CSRF check plain `httpx` couldn't. The remaining piece:
   LinkedIn only issues `bcookie`/`bscookie`/`lidc`/`JSESSIONID` to a
   session that has actually visited the site — presenting `li_at` cold,
   with none of that history, doesn't look like a real returning session.
   `LinkedInClient._ensure_session()` does one anonymous GET to
   linkedin.com first — exactly what a real browser's first request of a
   session looks like — to pick up those cookies *before* ever presenting
   `li_at`, then reuses that one session for every subsequent request,
   the same way a real browser only "warms up" once rather than before
   every page view.

   **Live end-to-end verification of this final version was inconclusive**:
   after extensive testing throughout development (the abandoned browser
   version included), the test account itself started getting a
   self-redirecting `302` loop back to the exact URL requested — observed
   even on a plain `/feed/` page load with nothing but `li_at`, no API call
   involved — which looks like LinkedIn challenging the account/session
   itself (almost certainly from the cumulative volume of automated
   requests made against it that day) rather than rejecting anything
   about this specific request or technique. See
   [Known limitations](#known-limitations) for the full, honest account of
   what was and wasn't confirmed live.

The flow:

1. **Warm-up** — on first use, `LinkedInClient` makes one anonymous request
   to linkedin.com to receive `bcookie`/`bscookie`/`lidc`/`JSESSIONID`
   exactly as a real browser's session would, then layers the configured
   `li_at` cookie on top. This one session is reused for every subsequent
   request, not rebuilt each time.
2. **Fetch** — `fetch_profile_view()` calls `profileView` directly via
   `curl_cffi`'s Chrome-impersonating client with the matching
   `csrf-token`/`x-restli-protocol-version`/`Referer` headers a real
   browser sends.
3. **Parse** — Voyager responses are "normalized": a `data` object plus a
   flat `included` array of entities (positions, education, skills, ...)
   tagged by `$type`. `app/parser.py` indexes `included` by type and builds
   our own clean `LinkedInProfile` schema from it (`app/schemas.py`).
4. **Serve** — a FastAPI app (`app/main.py`) exposes `GET /api/v1/profile`,
   gated by an API key, with a per-key rate limit and a TTL cache so repeat
   lookups of the same profile don't re-hit LinkedIn.
5. **Throttle** — outbound LinkedIn requests are spaced by a randomized
   interval (`MIN_REQUEST_INTERVAL_SECONDS`–`MAX_REQUEST_INTERVAL_SECONDS`,
   not a fixed delay — perfectly even spacing is itself a machine-like
   tell), enforced process-wide regardless of how many API callers are
   hitting this service concurrently. A separate `DAILY_REQUEST_LIMIT`
   caps this instance's total LinkedIn-bound requests per rolling UTC day,
   independent of LinkedIn's own rate limiting — a self-imposed ceiling on
   this account's total automated footprint (`app/linkedin_client.py`'s
   `_GlobalRateLimiter` and `_DailyRequestBudget`).
6. **Detect, don't just retry, a challenge response** — LinkedIn's observed
   challenge behavior is a `302` redirect back to the exact URL requested.
   Rather than following that in a loop (which is what a naive client
   configured with `allow_redirects=True` would do, burning requests
   against an already-challenged session), `_is_self_redirect()` detects
   this pattern from the very first response and raises a specific,
   named error for it.

## API

### `GET /api/v1/profile?url=<linkedin-profile-url>`

Headers: `X-API-Key: <your configured key>`

```bash
curl -H "X-API-Key: $API_KEY" \
  "https://your-deployment.example.com/api/v1/profile?url=https://www.linkedin.com/in/someone/"
```

Response `200`:

```json
{
  "public_identifier": "someone",
  "profile_url": "https://www.linkedin.com/in/someone/",
  "full_name": "Jane Doe",
  "first_name": "Jane",
  "last_name": "Doe",
  "headline": "Senior Engineer at Example Corp",
  "location": "San Francisco, California, United States",
  "about": "Building things...",
  "profile_images": [
    { "url": "https://media.licdn.com/dms/image/.../400x400.jpg", "width": 400, "height": 400 }
  ],
  "background_image_url": null,
  "experience": [
    {
      "title": "Senior Engineer",
      "company": "Example Corp",
      "company_linkedin_url": "https://www.linkedin.com/company/example-corp",
      "location": "San Francisco, CA",
      "start_date": "2022-03",
      "end_date": null,
      "is_current": true,
      "description": "...",
      "employment_type": "Full-time"
    }
  ],
  "education": [
    {
      "school": "State University",
      "school_linkedin_url": "https://www.linkedin.com/school/state-university",
      "degree": "B.S.",
      "field_of_study": "Computer Science",
      "start_date": "2014",
      "end_date": "2018",
      "description": null
    }
  ],
  "skills": [{ "name": "Python", "endorsement_count": 37 }],
  "certifications": [
    {
      "name": "AWS Certified Solutions Architect",
      "issuing_organization": "Amazon Web Services",
      "issue_date": "2021-06",
      "credential_id": "ABC123",
      "credential_url": null
    }
  ],
  "languages": [{ "name": "English", "proficiency": "Native or bilingual proficiency" }],
  "connections_count": 500,
  "follower_count": null,
  "fetched_at": "2026-08-27T10:15:00+00:00",
  "cache_hit": false
}
```

Error responses are `{"error": "<message>"}` with an appropriate status code.
When the failure is one of the known, documented limitations below (a
challenge-response redirect, a request timeout, an expired session) rather
than a genuine bug, the body also includes an `alert` field spelling that
out in plain language, e.g.:

```json
{
  "error": "LinkedIn blocked the automated request: ...",
  "alert": "Known limitation: LinkedIn responded with a self-redirect loop instead of serving the request — observed live even on a plain authenticated page load, consistent with an account/session-level challenge rather than a flaw in this request specifically. ..."
}
```

| Status | Meaning |
|---|---|
| 400 | URL didn't contain a `/in/<public-id>` segment |
| 401 | Missing/invalid `X-API-Key` |
| 404 | LinkedIn returned "not found" for that profile |
| 429 | Per-key rate limit exceeded, `DAILY_REQUEST_LIMIT` reached for this instance, or LinkedIn itself rate-limited us |
| 502 | LinkedIn session invalid/expired, a challenge-response redirect, a network-level timeout, or an unexpected upstream failure — check for an `alert` field first |

### `GET /api/v1/find-profile-url?name=<full name>&company=<company>|&email=<email>`

Headers: `X-API-Key: <your configured key>`

Finds a LinkedIn profile URL from a name plus a company or email — mirrors
PhantomBuster's "LinkedIn Profile URL Finder". A name alone matches too many
people to be useful, so `company` or `email` is required to disambiguate
(the same constraint that tool documents); omitting both returns a `400`.

```bash
curl -H "X-API-Key: $API_KEY" \
  "https://your-deployment.example.com/api/v1/find-profile-url?name=Jane+Doe&company=Example+Corp"
```

Response `200`:

```json
{
  "public_identifier": "jane-doe-1234",
  "profile_url": "https://www.linkedin.com/in/jane-doe-1234/",
  "matched_query": "Jane Doe · Example Corp",
  "fetched_at": "2026-08-28T10:15:00+00:00"
}
```

Implemented by calling LinkedIn's internal people-search API directly and
regex-scanning the raw JSON response text for the first `/in/<public-id>`
occurrence, rather than parsing a specific result-object schema — this
endpoint's exact query-parameter/response shape is the highest-uncertainty
part of this codebase (unlike `profileView`, it was not confirmed against a
live response during development — see
[Known limitations](#known-limitations)), so extraction is deliberately
tolerant of the surrounding structure drifting from what's assumed in
`app/linkedin_client.py`. `404` means no `/in/` reference was found
anywhere in the response; a `502` with an `alert` means the search hit the
same class of issue documented for `/api/v1/profile`.

### `GET /health`

No auth required. Returns `{"status": "ok", "linkedin_session_configured": true|false}` —
useful as a deployment platform's health check, and to confirm the cookie
was set correctly without spending a LinkedIn request.

Interactive docs (Swagger UI) are auto-served at `/docs`. A landing page at
`/` offers a live try-it-now form for both endpoints in the browser.

## Setup

### 1. Get your LinkedIn session cookie

1. Log into linkedin.com in a normal browser with the account you're willing
   to use for this (ideally not your only/primary account, given the ToS
   and challenge/ban risk — see [Known limitations](#known-limitations)).
2. Open DevTools → Application (Chrome) / Storage (Firefox) → Cookies →
   `https://www.linkedin.com`.
3. Copy the value of `li_at`.
4. Put it into `.env` (copy `.env.example` first) as `LI_AT_COOKIE`.

Only `li_at` is needed — do **not** also copy `JSESSIONID` from your
browser. It's issued fresh per HTTP session and this app obtains its own via
an anonymous warm-up request; copying a browser's static value in caused
exactly the account-challenge symptom described above during development
(see [Known limitations](#known-limitations)).

`li_at` expires (typically after ~1 year, sooner if you log out elsewhere
or LinkedIn flags the session) — if `/health` starts reporting auth errors,
repeat this step.

### 2. Configure

```bash
cp .env.example .env
```

Fill in `LI_AT_COOKIE`, and set `API_KEY` to a random string you generate
(e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`) —
this is what callers of *your* hosted API will need to send.

### 3. Run locally

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
uvicorn app.main:app --reload
```

Visit `http://127.0.0.1:8000/docs`. No browser binary to download — this is
a plain HTTP client, so setup and cold starts are fast.

### 4. Run the tests

```bash
pip install pytest
pytest tests/ -v
```

The test suite covers URL parsing, the Voyager-response parser against a
fixture payload, the daily-request-budget logic, and the self-redirect
challenge-detection logic (no live LinkedIn calls — nothing here talks to
LinkedIn during tests).

### 5. Run with Docker

```bash
docker build -t linkedin-profile-api .
docker run --rm -p 8000:8000 --env-file .env linkedin-profile-api
```

Plain `python:3.12-slim` — no browser runtime, so the image is small and
runs comfortably on any free tier.

## Deployment

Any container host works since this ships a `Dockerfile`. Quick options:

**Render / Railway / Fly.io** (all have a generous free/hobby tier and give
you HTTPS automatically):

1. Push this repo to GitHub.
2. Create a new Web Service from the repo (Render/Railway auto-detect the
   `Dockerfile`; for Fly.io run `fly launch` then `fly deploy`).
3. Set `LI_AT_COOKIE` and `API_KEY` as secret environment variables in the
   platform's dashboard — **never** in the repo. No particular RAM/CPU
   requirement — this is a lightweight HTTP client, not a browser, so the
   smallest free-tier instance size is fine.
4. Confirm `GET /health` on the public URL returns
   `"linkedin_session_configured": true`.

## Known limitations

- **Undocumented API, no SLA.** Voyager's response shape has changed before
  and will again. If fields come back empty, open a profile in a browser
  with DevTools open, find the `profileView` XHR response, and update the
  `$type` suffixes / key names in `app/parser.py` to match.
- **ToS risk.** Using this against LinkedIn is against their User
  Agreement. LinkedIn can rate-limit, challenge (CAPTCHA/email verification),
  or restrict the account behind `LI_AT_COOKIE`. This implementation
  throttles requests and caches results specifically to reduce that risk,
  but cannot eliminate it — deliberately does not attempt to defeat CAPTCHAs
  or automate around a checkpoint.
- **Only public-ish profile data.** Depth of data returned depends on the
  viewing account's connection degree to the profile owner and the profile
  owner's own privacy settings (e.g. a 3rd-degree connection profile may
  return less than a 1st-degree one). There's no way around this short of
  using an account connected to the target.
- **No automated login.** Session cookies must be captured manually from a
  browser and rotated when they expire; username/password login isn't
  implemented — it reliably trips LinkedIn's CAPTCHA/checkpoint flow when
  attempted from a server.
- **Single-process cache/rate-limit.** The in-memory cache and rate limiter
  in `app/cache.py` / `app/rate_limit.py` are per-process — fine for a
  single instance, but won't coordinate across multiple replicas. Swap in
  Redis if you scale horizontally.
- **Background image** isn't populated (`background_image_url` is always
  `null`) — the cover-photo field in Voyager's response wasn't reliably
  present across the profiles used to build this and was left as a
  documented gap rather than guessed at.
- **TLS-fingerprint impersonation defeated one specific check, live, but
  the account then hit a deeper challenge that's unresolved.** In order,
  what was actually verified during development: (1) plain `httpx` got a
  consistent `403 CSRF check failed` regardless of cookie validity —
  confirmed to be a TLS/HTTP2 fingerprint issue, not a cookie/header
  problem, by reproducing success with a headless browser (since abandoned
  per this assignment's no-browser requirement). (2) Switching to
  `curl_cffi`'s Chrome impersonation, plus sourcing `JSESSIONID` from the
  client's own anonymous warm-up rather than a copied browser value,
  cleared that specific CSRF check. (3) The account then started returning
  a `302` redirect back to the exact URL requested — reproduced even on a
  plain `/feed/` page load with nothing but `li_at`, no API call involved
  — which reads as LinkedIn challenging the account/session itself (very
  likely from the cumulative volume of automated requests made against it
  across every iteration of testing that day) rather than anything specific
  to this request. **A live, end-to-end successful `profileView` response
  was not obtained after this point** — this repo's confidence in the
  approach rests on (1) and (2) being independently verified live, and on
  the redirect-loop specifically matching a documented, known LinkedIn
  challenge pattern rather than a generic failure. `_is_self_redirect()` +
  `LinkedInBotDetected` in `app/linkedin_client.py` exist specifically to
  surface this distinctly (fast, via the very first response) instead of
  masking it as a generic timeout or exhausting redirects trying to follow
  it. If this recurs on a fresh account/session: confirm via `/health` that
  the cookie loaded, and expect that TLS impersonation alone is not a
  guaranteed, permanent bypass — this remains an active arms race, exactly
  like the browser-fingerprinting problem it was chosen to sidestep.
- **`curl_cffi`'s impersonation profiles need occasional upkeep.**
  `IMPERSONATE_BROWSER` (default `"chrome"`, curl_cffi's own rolling
  default) pins to whatever Chrome build that library currently ships a
  fingerprint for — if a `curl_cffi` upgrade drops an older profile or
  LinkedIn starts keying off a newer Chrome version's fingerprint
  specifically, bump this value (see `.env.example` for where to find the
  current list of supported identifiers).
- **The default `DAILY_REQUEST_LIMIT=300` (and the jittered pacing) are
  heuristics, not a verified-safe number.** There's no universal safe
  volume — it depends on account age, history, and how "normal" its usage
  otherwise looks, none of which this service can know. Treat these as a
  reasonable conservative starting point to tune down (or up) for your own
  account, not a guarantee against restriction.
- **`/api/v1/find-profile-url`'s search endpoint was not verified live**
  (see above — the account was already challenged by the time search was
  built), and its regex-based extraction is a deliberate hedge against
  that uncertainty rather than a precise parse: it returns the *first*
  `/in/<public-id>` reference anywhere in the response, which could in
  principle match an unrelated reference in the payload (e.g. a "People
  also viewed" entry) rather than the primary result, and carries no
  confidence score or disambiguation beyond what `company`/`email` narrowed
  the search to. Separately, LinkedIn caps people-search results for
  non-Premium accounts (its "commercial use limit"), after which it serves
  a blurred/limited results payload instead of real ones — indistinguishable
  from a genuine no-match (`404`) from the outside.
