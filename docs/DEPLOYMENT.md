# Deployment Options — run the pipeline from any machine, free tier

**Status: the web app exists now** (`server.py` + `frontend/`) — this is no longer
just a plan. Locally: `./venv/bin/uvicorn server:app --reload --port 8000`
serves both the API and the frontend from one process at `http://localhost:8000`.
Password-gated via `APP_PASSWORD` in `.env` (required — the server refuses to
start protected routes without it).

What exists:
- `server.py` — FastAPI wrapper over `core.should_apply.evaluate`,
  `modes.manual.run`, and `modes.scraper.run_once`. Long pipeline runs go to a
  background thread, polled via `GET /api/runs/{id}`.
- `frontend/` — a static single-page frontend (no build step), styled to match the
  portfolio's design system. Manual-apply flow (score → generate → download),
  a scraper trigger, and a run history tab.
- `Dockerfile` + `fly.toml` — ready to deploy as-is.

## Recommended: Fly.io backend + Cloudflare Pages frontend

Why this pair over the alternatives:
- **Railway** free trial expires (then ~$5/mo minimum). **Fly.io** free
  allowance (3 shared-cpu-1x 256MB VMs) is enough for this app, and
  `auto_stop_machines = true` (already set in `fly.toml`) scales it to zero
  between requests — genuinely $0/month at low usage.
- **Vercel** is fine for the frontend too, but Cloudflare Pages has no
  bandwidth cap on the free tier and pairs with a Cloudflare-managed domain.

### Backend (Fly.io, free)

1. `fly launch` from the repo root (picks up `fly.toml` and `Dockerfile` already
   in place — decline auto-detected Postgres/Redis prompts, this app needs neither).
2. Set secrets before first deploy:
   ```
   fly secrets set ANTHROPIC_API_KEY=... APP_PASSWORD=... GROQ_API_KEY=... SERPAPI_KEY=...
   ```
   `APP_PASSWORD` is the shared secret the frontend's login screen checks —
   pick something you wouldn't mind putting in a password manager, since this
   gates paid Anthropic API calls.
3. `fly deploy` — the volume in `fly.toml` (`/data`, mapped to `OUTPUT_BASE_PATH`)
   persists the JD cache, seen-jobs store, and generated PDFs across deploys/restarts.
4. Scheduled scraping WITHOUT keeping a VM alive: run `apply.py scrape` from a
   **GitHub Actions cron** (free for public/private repos, 2,000 min/mo) —
   you already have workflows; point them at the CLI. The Fly VM then only
   wakes for interactive UI requests, or trigger a cycle manually from the
   Scraper tab in the web UI.

### Frontend (Cloudflare Pages, free)

The `frontend/` folder is already what you need — deploy it as-is (static files,
zero build step) to Cloudflare Pages, then set `window.API_BASE` in a small
`<script>` tag before `app.js` loads, pointing at your Fly.io URL
(e.g. `window.API_BASE = "https://resume-agent.fly.dev"`). CORS is already
handled server-side via the `CORS_ORIGINS` env var — set it to your Pages
domain instead of `*` once deployed, so only your frontend can call the API
cross-origin. Simplest option: skip the separate frontend host entirely and
let `server.py` serve `frontend/` directly (already wired via `StaticFiles` mount)
— one Fly.io app, one URL, no CORS configuration needed at all.

### Custom domain

1. Buy the domain at Cloudflare Registrar (at-cost, ~$10/yr, e.g. `ankushc.dev`).
2. `portfolio` → apex/`www` on Cloudflare Pages (or keep GitHub Pages: add a
   CNAME record `www → ankush1699.github.io`, set custom domain in repo settings).
3. `agent.ankushc.dev` → CNAME to the Pages front end.
4. `api.ankushc.dev` → `fly certs add api.ankushc.dev` + CNAME to your Fly app.
   TLS is automatic on both.

### Cheapest possible variant ($0/mo, no servers)

Skip the web app entirely:
- GitHub Actions cron runs `apply.py scrape` every 2h (SerpApi free tier: 100
  searches/mo — schedule accordingly, e.g. 3 runs/day × 1 query).
- Telegram notifications (already built) link high-match jobs.
- You trigger tailoring from any machine via `gh workflow run generate_resume.yml
  -f company=... -f jd=...` (the existing workflow, now pointing at `apply.py`);
  PDFs come back as workflow artifacts.
- JD cache persists as a committed file or an Actions cache key.
This costs $0 in infrastructure; the only spend is Sonnet writer tokens.

### Cost ceiling summary

| Piece | Provider | Cost |
|---|---|---|
| Cheap-tier LLM calls | Groq free tier (or local Ollama) | $0 |
| Writer/CL prose | Claude Sonnet | ~$0.05–0.08 per application |
| Backend | Fly.io free allowance (scale-to-zero) | $0 |
| Frontend | Cloudflare Pages / Vercel hobby | $0 |
| Scheduler | GitHub Actions cron | $0 |
| Scraping | SerpApi free 100/mo + jobspy/Dice RSS | $0 |
| Domain | Cloudflare Registrar | ~$10/yr |
