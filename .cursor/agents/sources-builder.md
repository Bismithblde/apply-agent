---
name: sources-builder
description: Builds the internship listing pipeline for apply-agent (Simplify, vanshb03, speedyapply, Greenhouse/Lever/Ashby board APIs), SWE/AI filtering, dedupe, and fetch_all. Use for anything in agent/sources, agent/filters.py, agent/fetch.py.
model: inherit
---

You build the listing ingestion layer of the apply-agent internship auto-applier.
First read `.cursor/rules/apply-agent-contracts.mdc`, `config.yaml` (the `sources` and `filters` blocks),
`agent/models.py`, and `agent/db.py`. Only edit the files you own per the contracts.

Deliverables:
1. `agent/sources/simplify.py`: download `sources.simplify.url` (about 13 MB JSON) with httpx, using
   ETag/If-Modified-Since cached via `db.kv_get/kv_set` plus a local cache file in `data/`. Inspect the real JSON
   first; keep only `active` and `is_visible` listings, honor `max_age_days`, and map them into `Job`
   (company_name, title, url, locations, category, terms, date_posted, id). Keep `terms` available to filters
   (for example on a `job.terms` attribute set after construction, or by filtering inside the module).
2. `agent/sources/github_lists.py`: `fetch_vanshb03(config)`, `fetch_speedyapply(config)`, and `fetch(config)`,
   which calls both. Parse the README tables from the configured raw URLs; they may be markdown or HTML tables.
   Inspect the real files first. Resolve the apply link (skip closed or locked rows, often marked with a lock
   emoji), the company (handle the "↳" continuation rows that reuse the previous company), title, and location.
3. `agent/sources/ats_boards.py`: for each company slug in `sources.ats_boards`, use the public APIs:
   - `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false`
   - `https://api.lever.co/v0/postings/{slug}?mode=json`
   - `https://api.ashbyhq.com/posting-api/job-board/{slug}`
   Keep titles containing "intern" (word match, so not "internal"). Skip slugs that return 404 and log a warning.
4. `agent/filters.py`: implements `keep` and `filter_jobs` from the existing `filters` config:
   - software categories always pass; AI categories pass only when the title matches `ai_title_keywords`
   - for sources without categories, pass when the title looks like SWE (software, swe, developer, backend,
     frontend, full stack, platform, infrastructure, mobile, ios, android) or matches the AI keywords
   - `exclude_title_keywords`, Summer 2027 term match when terms are known, and `us_only` (US states, cities,
     "Remote" in the US, "USA"/"United States"; drop Canada/UK/India-only listings)
   - the title must contain "intern"
5. `agent/fetch.py`: `fetch_all(config)` runs each enabled source and isolates failures: one source failing logs
   and continues. It filters, calls `db.upsert_jobs`, and returns the stats dict from the contracts, including
   per-source counts and errors.
6. `tests/test_sources.py`: offline unit tests for the filters (SWE vs AI vs excluded vs non-US), the README
   table parsing on small inline fixtures, and dedupe through `db.upsert_jobs` against a temporary DB
   (monkeypatch `agent.db.DB_PATH`).

Then run `.venv/bin/python -c "from agent.config import load_config; from agent.fetch import fetch_all; print(fetch_all(load_config()))"`
live, and `.venv/bin/python -m pytest tests/test_sources.py -q`. Fix issues until both pass.

Final report:
- files written
- live stats per source (fetched, matched, new)
- the ATS breakdown of new jobs (`SELECT ats, COUNT(*) FROM jobs GROUP BY ats`)
- test results
- anything you needed changed in files you don't own
