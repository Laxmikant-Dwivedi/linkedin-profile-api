# LinkedIn Profile API

A hosted HTTPS API that accepts a LinkedIn profile URL and returns structured
JSON (name, headline, location, about, experience, education, skills,
certifications, languages, profile images) — built by reverse-engineering
LinkedIn's internal "Voyager" REST API, the same one linkedin.com's own
frontend calls. Also includes a second endpoint to find a profile URL from a
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

**This went through two iterations, and the second one is why the code looks
the way it does:**

1. First pass: call `profileView` directly with `httpx`, using `li_at` +
   `JSESSIONID` cookies and a matching `csrf-token` header (the "textbook"
   reverse-engineering approach, and how the endpoint URL/response shape
   here was discovered in the first place). Live testing against a real
   account got a clean `403 CSRF check failed` on every attempt, even with
   fresh, valid cookies — a bare HTTP client has a different TLS/HTTP2
   fingerprint than a real browser, and LinkedIn's edge filters that before
   the CSRF token is meaningfully checked.
2. Current approach: drive a real headless Chromium via Playwright
   (`app/linkedin_client.py`), inject only the `li_at` cookie, navigate to
   the profile page, and intercept the network response the *browser's own
   JS* sends to that same `profileView` endpoint. The JSON payload is
   identical either way, so the parser (`app/parser.py`) didn't need to
   change. This is materially harder for LinkedIn to distinguish from a real
   visit, though as documented under
   [Known limitations](#known-limitations), it isn't a guaranteed bypass —
   headless browsers have their own detectable tells, and this repo
   includes the standard mitigations for those (masking
   `navigator.webdriver`, disabling the automation-controlled flag) without
   attempting to defeat any CAPTCHA/JS-challenge outright.

The flow:

1. **Auth** — a session cookie (`li_at`) captured from a real browser login
   is injected into a fresh Playwright browser context per request; no
   login automation, since that reliably trips LinkedIn's CAPTCHA/checkpoint
   flow when done from a server.
2. **Fetch** — `LinkedInClient.fetch_profile_view()` launches (or reuses) a
   headless Chromium instance, opens `linkedin.com/in/<public-id>/`, and
   listens for the `profileView` XHR the page itself fires, capturing its
   JSON body.
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
When the failure is one of the known, documented limitations below (bot
detection, a capture timeout, an expired session) rather than a genuine bug,
the body also includes an `alert` field spelling that out in plain language,
e.g.:

```json
{
  "error": "LinkedIn blocked the automated request: ...",
  "alert": "Known limitation: LinkedIn's automation detection can bounce a headless-browser request into a redirect loop instead of serving the page. This is not a bug in this service — see README 'Known limitations' ..."
}
```

| Status | Meaning |
|---|---|
| 400 | URL didn't contain a `/in/<public-id>` segment |
| 401 | Missing/invalid `X-API-Key` |
| 404 | LinkedIn returned "not found" for that profile |
| 429 | Per-key rate limit exceeded, `DAILY_REQUEST_LIMIT` reached for this instance, or LinkedIn itself rate-limited us |
| 502 | LinkedIn session invalid/expired, bot detection triggered, a capture timeout, or an unexpected upstream failure — check for an `alert` field first |

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

Implemented by navigating LinkedIn's people-search results page and reading
`href="...linkedin.com/in/..."` links straight out of the rendered DOM,
rather than intercepting LinkedIn's internal search API response — the
`/in/` URL pattern in a result link is far more stable across LinkedIn
frontend changes than that endpoint's internal JSON schema (the tradeoff
`app/parser.py`'s docstring describes for `profileView`, resolved the other
way here since we only need a URL out of it, not full structured data).
`404` means no matching link was found on the results page; a `502` with an
`alert` means the search hit the same bot-detection/timeout class of issue
documented for `/api/v1/profile` — see
[Known limitations](#known-limitations).

### `GET /health`

No auth required. Returns `{"status": "ok", "linkedin_session_configured": true|false}` —
useful as a deployment platform's health check, and to confirm cookies were
set correctly without spending a LinkedIn request.

Interactive docs (Swagger UI) are auto-served at `/docs`. A landing page at
`/` offers a live try-it-now form for `/api/v1/profile` in the browser.

## Setup

### 1. Get your LinkedIn session cookie

1. Log into linkedin.com in a normal browser with the account you're willing
   to use for this (ideally not your only/primary account, given the ToS
   and ban risk — see [Known limitations](#known-limitations)).
2. Open DevTools → Application (Chrome) / Storage (Firefox) → Cookies →
   `https://www.linkedin.com`.
3. Copy the value of `li_at`.
4. Put it into `.env` (copy `.env.example` first) as `LI_AT_COOKIE`.

This cookie expires (typically after ~1 year, sooner if you log out
elsewhere or LinkedIn flags the session) — if `/health` starts reporting
auth errors, repeat this step.

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
playwright install chromium   # one-time browser binary download (~140 MB)
uvicorn app.main:app --reload
```

Visit `http://127.0.0.1:8000/docs`.

### 4. Run the tests

```bash
pip install pytest
pytest tests/ -v
```

The test suite covers URL parsing, the Voyager-response parser against a
fixture payload, and the daily-request-budget logic (no live LinkedIn
calls, no browser needed — nothing here talks to LinkedIn during tests).

### 5. Run with Docker

```bash
docker build -t linkedin-profile-api .
docker run --rm -p 8000:8000 --env-file .env linkedin-profile-api
```

The image is built on Playwright's own base image (`mcr.microsoft.com/playwright/python`),
which bundles a matching Chromium build and all its system dependencies —
several hundred MB larger than a plain Python slim image, but far less
fragile than installing headless-browser dependencies by hand.

## Deployment

Any container host works since this ships a `Dockerfile`. Quick options:

**Render / Railway / Fly.io** (all have a generous free/hobby tier and give
you HTTPS automatically):

1. Push this repo to GitHub.
2. Create a new Web Service from the repo (Render/Railway auto-detect the
   `Dockerfile`; for Fly.io run `fly launch` then `fly deploy`).
3. Set `LI_AT_COOKIE` and `API_KEY` as secret environment variables in the
   platform's dashboard — **never** in the repo. Give the service at least
   ~1 GB RAM — a headless Chromium instance under load needs meaningfully
   more than a typical free-tier API service; the smallest free tiers on
   some platforms may be too small or too slow to cold-start it.
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
  implemented (see above).
- **Single-process cache/rate-limit.** The in-memory cache and rate limiter
  in `app/cache.py` / `app/rate_limit.py` are per-process — fine for a
  single instance, but won't coordinate across multiple replicas. Swap in
  Redis if you scale horizontally.
- **Background image** isn't populated (`background_image_url` is always
  `null`) — the cover-photo field in Voyager's response wasn't reliably
  present across the profiles used to build this and was left as a
  documented gap rather than guessed at.
- **Bot detection is an active adversary, not a solved problem.** This was
  tested live against a real account during development. The first
  (raw-HTTP) implementation got a consistent `403 CSRF check failed` from
  LinkedIn's edge regardless of cookie validity — a bare HTTP client's
  TLS/HTTP2 fingerprint alone was enough to get filtered. Switching to a
  real headless Chromium (the current approach) got further, but a plain
  headless launch hit `ERR_TOO_MANY_REDIRECTS` — LinkedIn's automation
  detection (`navigator.webdriver` and related tells) bounced the
  navigation into a security-challenge loop instead of serving the page.
  The mitigations for that (masking `navigator.webdriver`, a realistic
  viewport/locale, `--disable-blink-features=AutomationControlled`) are in
  `app/linkedin_client.py`, but this repo does **not** claim they're a
  guaranteed bypass — LinkedIn's detection evolves, and getting reliably
  past it in production is exactly the hard, ongoing part of this problem
  that commercial scrapers (PhantomBuster included) invest heavily in
  (residential proxy pools, real browser fingerprint farms, session
  warm-up). If a deployed instance still hits a redirect loop or repeated
  403s: confirm with `/health` that the cookie loaded, try a longer
  `REQUEST_TIMEOUT_SECONDS`, and expect that getting this fully reliable
  is genuinely open-ended work, not a one-line fix.
- **The default `DAILY_REQUEST_LIMIT=300` (and the jittered pacing) are
  heuristics, not a verified-safe number.** There's no universal safe
  volume — it depends on account age, history, and how "normal" its usage
  otherwise looks, none of which this service can know. Treat these as a
  reasonable conservative starting point to tune down (or up) for your own
  account, not a guarantee against restriction.
- **Free-tier hosting is CPU-starved for a headless browser, and this
  changes the failure mode.** Deployed to Render's free tier (0.1 CPU,
  512 MB) and tested live: no `ERR_TOO_MANY_REDIRECTS` this time — the
  navigation succeeded — but the `profileView` network response never
  arrived before our own request timeout, then before Render's own
  platform-level proxy timeout (~30s, independent of anything configurable
  in this app) kicked in and returned an empty `502` from Render's edge
  infrastructure rather than from the app. Rendering a JS-heavy SPA like a
  LinkedIn profile page is measurably slower on 0.1 vCPU than on a normal
  machine. `REQUEST_TIMEOUT_SECONDS` is deliberately kept a little below
  the platform's own timeout (see `.env.example`) so the app returns a
  clean JSON error instead of an opaque empty response when this happens —
  but the real fix, if this needs to work reliably in production, is more
  CPU (a paid instance tier), not a longer timeout.
- **`/api/v1/find-profile-url` inherits every limitation above** (it uses
  the same browser/session machinery) plus two of its own: LinkedIn caps
  people-search results for non-Premium accounts (its "commercial use
  limit"), after which it serves a blurred/limited results page instead of
  real ones — this looks identical to a genuine no-match (`404`) from the
  outside, there's no reliable way to tell them apart from the response
  alone. It also just returns the *first* DOM match for the query, with no
  confidence score or disambiguation beyond what `company`/`email` narrowed
  the search to — for a common name at a large company, that first result
  isn't guaranteed to be the right person.
