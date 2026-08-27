# LinkedIn Profile API

A hosted HTTPS API that accepts a LinkedIn profile URL and returns structured
JSON (name, headline, location, about, experience, education, skills,
certifications, languages, profile images) — built by reverse-engineering
LinkedIn's internal "Voyager" REST API, the same one linkedin.com's own
frontend calls.

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

The flow:

1. **Auth** — instead of automating a LinkedIn login (which reliably trips
   LinkedIn's CAPTCHA/checkpoint flow when done from a server), this uses a
   session captured from a real browser login: the `li_at` and `JSESSIONID`
   cookies, sent as `Cookie` headers plus a matching `csrf-token` header on
   every request (`app/linkedin_client.py`).
2. **Fetch** — `LinkedInClient.fetch_profile_view()` calls the `profileView`
   endpoint with a browser-like `User-Agent` and the Voyager-specific
   `Accept`/`x-restli-protocol-version` headers LinkedIn's frontend sends.
3. **Parse** — Voyager responses are "normalized": a `data` object plus a
   flat `included` array of entities (positions, education, skills, ...)
   tagged by `$type`. `app/parser.py` indexes `included` by type and builds
   our own clean `LinkedInProfile` schema from it (`app/schemas.py`).
4. **Serve** — a FastAPI app (`app/main.py`) exposes `GET /api/v1/profile`,
   gated by an API key, with a per-key rate limit and a TTL cache so repeat
   lookups of the same profile don't re-hit LinkedIn.
5. **Throttle** — a global minimum interval between outbound LinkedIn
   requests (`MIN_REQUEST_INTERVAL_SECONDS`) is enforced process-wide,
   regardless of how many API callers are hitting this service concurrently,
   to keep the LinkedIn account's behavior looking non-scripted.

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

Error responses are `{"error": "<message>"}` with an appropriate status code:

| Status | Meaning |
|---|---|
| 400 | URL didn't contain a `/in/<public-id>` segment |
| 401 | Missing/invalid `X-API-Key` |
| 404 | LinkedIn returned "not found" for that profile |
| 429 | Per-key rate limit exceeded, or LinkedIn itself rate-limited us |
| 502 | LinkedIn session invalid/expired, or an unexpected upstream failure |

### `GET /health`

No auth required. Returns `{"status": "ok", "linkedin_session_configured": true|false}` —
useful as a deployment platform's health check, and to confirm cookies were
set correctly without spending a LinkedIn request.

Interactive docs (Swagger UI) are auto-served at `/docs` once running.

## Setup

### 1. Get LinkedIn session cookies

1. Log into linkedin.com in a normal browser with the account you're willing
   to use for this (ideally not your only/primary account, given the ToS
   and ban risk — see [Known limitations](#known-limitations)).
2. Open DevTools → Application (Chrome) / Storage (Firefox) → Cookies →
   `https://www.linkedin.com`.
3. Copy the value of `li_at`.
4. Copy the value of `JSESSIONID` — it looks like `"ajax:1234567890123456789"`,
   quotes included; keep it exactly as shown.
5. Put both into `.env` (copy `.env.example` first) as `LI_AT_COOKIE` and
   `JSESSIONID_COOKIE`.

These cookies expire (typically after ~1 year for `li_at`, sooner if you log
out elsewhere or LinkedIn flags the session) — if `/health` starts reporting
auth errors, repeat this step.

### 2. Configure

```bash
cp .env.example .env
```

Fill in `LI_AT_COOKIE`, `JSESSIONID_COOKIE`, and set `API_KEY` to a random
string you generate (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
— this is what callers of *your* hosted API will need to send.

### 3. Run locally

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
uvicorn app.main:app --reload
```

Visit `http://127.0.0.1:8000/docs`.

### 4. Run the tests

```bash
pip install pytest
pytest tests/ -v
```

The test suite covers URL parsing and the Voyager-response parser against a
fixture payload (no live LinkedIn calls — nothing here talks to LinkedIn
during tests).

### 5. Run with Docker

```bash
docker build -t linkedin-profile-api .
docker run --rm -p 8000:8000 --env-file .env linkedin-profile-api
```

## Deployment

Any container host works since this ships a `Dockerfile`. Quick options:

**Render / Railway / Fly.io** (all have a generous free/hobby tier and give
you HTTPS automatically):

1. Push this repo to GitHub.
2. Create a new Web Service from the repo (Render/Railway auto-detect the
   `Dockerfile`; for Fly.io run `fly launch` then `fly deploy`).
3. Set `LI_AT_COOKIE`, `JSESSIONID_COOKIE`, and `API_KEY` as secret
   environment variables in the platform's dashboard — **never** in the repo.
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
- **Not tested against a live LinkedIn account** in this repo — the parser
  is verified with unit tests against a realistic fixture payload
  (`tests/test_parser.py`), but the exact Voyager response shape should be
  reconfirmed against a real profile after you configure your own cookies,
  since LinkedIn serves slightly different payload shapes over time and by
  account region/experiment.
