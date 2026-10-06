# AI Resume Agent

> **Built for one candidate, then generalized.** The pipeline is data-driven (everything it
> may claim comes from your own `Ankush_Master_Data.json`), but a few prompts still describe the
> maintainer's profile (visa status, stack, seniority) — see *Using your own data* below.
> Personal project; not affiliated with any job board or employer.

An autonomous job-search pipeline: score a job posting for fit before spending
any money on it, then tailor a resume and cover letter only for postings worth
applying to. Three entry points (web UI, CLI, scraper) share one core engine.

## Project structure

```
./
├── core/                   Provider-agnostic engine internals — the parts every
│   ├── llm.py                entry mode shares.
│   ├── red_flags.py         llm.py:        cheap-tier model resolution (Groq → Ollama →
│   ├── jd_cache.py                         Claude Haiku fallback) vs. quality-tier (Sonnet).
│   ├── should_apply.py      red_flags.py:  free, deterministic visa/clearance rejection regexes.
│   └── profile_cache.py     jd_cache.py:   permanent, content-hash-keyed cache — a posting
│                                           is never rescored twice, from any entry mode.
│                            should_apply.py: the 3-stage fit gate (red flags → cache → LLM
│                                              rubric) that everything else depends on.
│                            profile_cache.py: compact candidate-profile text for prompts.
│
├── engine/                 The LangGraph tailoring pipeline itself.
│   ├── graph.py              graph.py:      the 6-node graph (should_apply → triage →
│   ├── schemas.py                           strategist → writer → editor → finalizer)
│   ├── state.py                             plus the Python validators that check every
│   ├── pdf_generator.py                     generated claim against master data.
│   ├── generate_generic.py   pdf_generator.py: Jinja2 → LaTeX → PDF, plus the single
│   └── generate_role_family.py               sanitize_output_text() choke point (no em
│                                               dashes, no stray review tags — applies to
│                                               every PDF this repo produces).
│                            generate_generic.py:    deterministic, no-LLM, no-JD resume.
│                            generate_role_family.py: same, plus scoped overrides (summary,
│                                                       skills, email) for a family of similar
│                                                       postings without touching master data.
│
├── modes/                  The three ways to invoke the engine.
│   ├── manual.py              manual.py:   paste company + JD, run the full graph.
│   ├── scraper.py             scraper.py:  thin wrapper over scraper/ for one cycle or the daemon.
│   └── autofill.py            autofill.py: Playwright form-fill, stops before submit — never
│                                            answers visa/EEO/salary questions, never touches
│                                            a CAPTCHA, never clicks submit.
│
├── scraper/                 Multi-platform job scraping + filtering (LinkedIn/Indeed via
│   └── ...                   jobspy, Dice RSS, Google Jobs via SerpApi) → Google Sheets.
│
├── cover_letter/             Standalone cover-letter regeneration from an existing run
│   └── ...                   folder — for when you want a new CL without rerunning the
│                              whole (paid) pipeline.
│
├── frontend/                Static single-page web UI (no build step) — served by
│   ├── index.html             server.py, or deployable standalone to Cloudflare Pages.
│   ├── app.js
│   └── style.css
│
├── templates/                LaTeX templates (Jinja2) for resume / cover letter / the
│   └── *.tex                 no-JD generic resume.
│
├── tests/                   Automated pytest suite (deterministic logic only — no paid
│   ├── test_*.py              LLM calls) plus two manual template-check scripts.
│   └── manual_check_*.py
│
├── docs/                     Analysis notes and the free-tier deployment guide.
│
├── apply.py                  Unified CLI — see "Running it" below.
├── server.py                  FastAPI backend for the web UI.
├── run.py                     GitHub Actions entry point (env-var driven; do not remove,
│                               .github/workflows/*.yml call this directly).
├── Ankush_Master_Data.example.json   Fictional sample of the data format (copy it — see Setup).
│                                      Your real Ankush_Master_Data.json is gitignored.
├── Dockerfile / fly.toml      Free-tier deployment (Fly.io).
└── requirements.txt
```

**Why three top-level entry-point scripts?** They're not redundant — `apply.py`
is the interactive CLI, `server.py` is the web backend, `run.py` is what
GitHub Actions invokes with environment variables. Each is documented below.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in real values — see below
cp Ankush_Master_Data.example.json Ankush_Master_Data.json   # then replace with YOUR data
```

Required in `.env`: `ANTHROPIC_API_KEY`. Everything else has a sane default or
only matters for a specific mode (SerpApi/Sheets/Telegram for scraper mode,
`APP_PASSWORD` for the web UI).

**To actually use the full potential of the agent — set `GROQ_API_KEY`.**
Free at [console.groq.com](https://console.groq.com). Without it, every
"cheap tier" call (should-apply scoring, keyword extraction, strategist,
editor) silently falls back to paid Claude Haiku, which works but defeats the
entire cost-optimization design. This is the single highest-leverage thing to
configure before relying on this daily.

## Using your own data

`Ankush_Master_Data.example.json` is a **fictional** person ("Alex Rivera") in the exact
format the engine reads: contact info, summary, skills, employers with bullets, projects,
education. Copy it to `Ankush_Master_Data.json` (gitignored — it stays on your machine) and
replace every value with your own real, verifiable experience. The tailoring pipeline only
rearranges and rewords what is in that file; Python validators then check every generated
bullet against it (invented metrics, technologies you never listed and dropped numbers are
rejected or reverted), so a claim can't enter a resume unless you wrote it there first.

Things written for the maintainer that you should edit for yourself:
- `core/should_apply.py` — the `CANDIDATE CONTEXT` block in the scoring prompt (years of
  experience, visa status, target level, stack).
- `engine/graph.py` — the writer/editor prompts describe which technologies are "production" vs
  "project-only" for that profile.
- `scraper/` settings (Scraper tab) — keywords, location, daily cap and the minimum score.

### Optional: LinkedIn session cookie
LinkedIn scraping works without it but is more limited. If you set `LINKEDIN_COOKIE` (your own
`li_at` session), results and descriptions improve — **but automated access with a logged-in
session is against LinkedIn's terms of service and can get your account restricted.** It is
off unless you opt in. Indeed, Dice, Google Jobs (SerpApi) and company ATS boards
(Greenhouse / Lever / Ashby / Workday) need no login. Never commit your cookie; `.env` is
gitignored.

## Running it — three ways

### 1. Web UI (recommended day-to-day)

```bash
./venv/bin/uvicorn server:app --reload --port 8000
```

Open `http://localhost:8000`, sign in with `APP_PASSWORD`. Paste a company +
JD, click **Check fit (free)** — this runs only the should-apply gate, costs
nothing if it's a bad fit. If it passes, **Generate tailored resume** runs the
full pipeline and gives you download links. A **Scraper** tab triggers one
scrape cycle on demand; a **History** tab lists past runs.

### 2. CLI (`apply.py`) — scriptable, same engine

```bash
./venv/bin/python apply.py score --jd-file jd.txt                     # free verdict only
./venv/bin/python apply.py manual --company "Stripe" --title "SWE" --jd-file jd.txt --cover-letter
./venv/bin/python apply.py scrape [--daemon]                          # one cycle, or the scheduler
./venv/bin/python apply.py autofill --url <application url> --folder <run output folder>
./venv/bin/python apply.py cache-stats                                # how many JDs cached, pass/reject split
```

### 3. Deterministic, no-JD resumes — zero cost, zero LLM

For cold outreach, career fairs, or a "keep this constant" resume:

```bash
./venv/bin/python engine/generate_generic.py
```

For a resume tuned to a family of similar postings (e.g. "Microsoft SWE,
Redmond") without hand-editing a PDF — see `engine/generate_role_family.py`,
called from a small script with explicit overrides (summary, skill order,
email). Every override is checked against real master data; it can't
introduce a skill or fact that doesn't already exist there.

## Running the test suite

```bash
./venv/bin/pytest tests/ -v
```

Covers the deterministic logic only (red-flag regexes, JD cache, PDF text
sanitization, bullet-fidelity checks) — no API keys needed, no cost. `conftest.py` supplies a
placeholder key so the suite can never make a paid call, and skips the few tests that assert
facts about the maintainer's real data when the sample data is in use. These exist specifically because
two of them (`test_itar_flags_only_as_a_whole_word`,
`test_negated_clearance_requirement_is_not_a_red_flag`) are regression tests
for real false-positive bugs found during development; they'll fail loudly if
either regresses.

`tests/manual_check_resume_template.py` and `manual_check_cover_letter_template.py`
are separate, non-pytest dev scripts — they render the most recent real output
against the LaTeX templates with no API calls, for instantly checking a
template edit without burning tokens.

## Cost model, in one paragraph

Every job goes through `should_apply` first: free regex red-flags, then a
free permanent cache lookup, then (only if neither resolved it) one cheap-tier
LLM call. A bad fit costs $0. A good fit proceeds through keyword extraction,
bullet selection, and editing — all cheap-tier, with the editor skipped
entirely if deterministic validators find nothing wrong. The only paid,
quality-tier call in the whole pipeline is the final resume/cover-letter
writer (Claude Sonnet).

Measured on the maintainer's own runs (Aug 6 – Oct 5, 2026): about **$0.003 per posting scored**
(Claude Haiku with Anthropic prompt caching, ~87% of the prompt served from cache), and about
**$0.10 per tailored application** all-in — roughly $16.6 of API spend over 154 generated
applications, an upper bound because it also includes scraping, scoring thousands of postings
and development test runs. Median generation time was 83 seconds.

## Deployment

See `docs/DEPLOYMENT.md` — Fly.io (free tier, scales to zero) + optional
Cloudflare Pages, or a $0/month serverless variant using only GitHub Actions.
