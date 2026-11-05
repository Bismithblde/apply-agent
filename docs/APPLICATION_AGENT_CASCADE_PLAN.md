# Application Agent Cascade Plan

**Status:** Complete for the scoped cascade
**Goal:** Apply to supported jobs through a cost-aware sequence of interaction layers. Use the cheapest sufficiently reliable layer first, escalate when evidence shows it cannot proceed, and submit only through the existing answer and submit policies.

## Current architecture

The synchronous Playwright runner in `agent/runner.py` routes jobs through `agent/ats/registry.py`. Greenhouse, Lever, Ashby, and Workday have dedicated adapters. Other sites use the generic Gemini-guided loop in `agent/ats/generic.py`. `agent/form.py` extracts and fills DOM controls; `agent/answers.py` resolves profile and answer-bank facts before generating answers. `agent/vision_agent.py` provides screenshot-grounded repairs for supported text fields and dropdowns after DOM filling fails.

Submission is shared across adapters through `submit_decision` and `ApplyContext.mark_submit_attempt` in `agent/ats/base.py`; result tracking, uncertain-submit recovery, CAPTCHA policy, and status changes live in the adapters and runner. These controls remain authoritative for every layer.

## Layer order and contract

1. **Known ATS and deterministic answers.** Route to a dedicated adapter when available. Resolve fields from confirmed profile/answer-bank facts and known ATS control patterns. This path is narrow by design; unknown fields or unsupported controls escalate instead of being guessed.
2. **DOM plus LLM interpretation.** Use extracted field labels, options, visible page text, and application context to interpret unfamiliar questions and navigation. Answers still come from the shared answer service and its evidence rules.
3. **Computer use.** For supported fields that the DOM fill cannot complete, give the operator a fresh screenshot and field context. It may locate the target, click/focus it, and enter only the already-approved value, or choose a matching option in that field's listbox. Re-observe and verify the resulting field value. Use it for interface uncertainty, not to invent applicant facts.
4. **Needs review.** Stop when the agent cannot identify the application state, verify a required value, resolve an unsupported question, or confirm the result of a submission.

Every attempt should make it possible to determine which layer handled the page, why escalation occurred, what values were verified, and whether a final submit intent was persisted. Do not treat model-stated confidence as verification. Do not let a lower layer retry after a persisted or uncertain submit intent. A model-generated click alone is never evidence of successful submission.

## Phases

### Phase 0 — Baseline and worktree preservation

**Status:** Complete (baseline run)
**Scope:** Inspect current implementation, staged and unstaged changes, available tests, and submit safeguards. Preserve the existing worktree.
**Evidence:** Focused baseline command `./.venv/bin/python -m pytest -q tests/test_generic.py tests/test_forms.py tests/test_vision_agent.py tests/test_runner.py` passed: 322 tests. The same command could not launch Chromium inside the sandbox; it passed after running with the approved unrestricted environment. Existing changes were present on `main`, including changes to the application path, a staged `profile.yaml` deletion, and untracked `profile.example.yaml`; inspect before touching overlapping files and never reveal profile contents.

### Phase 1 — Codex workflow and shared layer contract

**Status:** Complete
**Scope:** Keep this roadmap as the single source of truth. Add or refine the smallest shared result/trace contract needed for `handled`, `escalate`, and `blocked`, including layer identity, reason, and verification evidence. Avoid logging applicant answers or screenshots unless needed and protected.
**Acceptance:** Existing adapter callers remain compatible; fallback eligibility is explicit; unknown required answers and uncertain submit state cannot escalate into an automatic final submit.
**Verification:** Focused tests for result classification, trace redaction, and submit-gate invariants.

**Delivered:** Added redacted `LayerEvent` records to results and the application database, including legacy-schema migration. Router, answer fill, DOM/LLM, computer-use, submit verification, persisted submit intent, and confirmation events are recorded. Browser-crash results retain events from the failed attempt.

### Phase 2 — Deterministic-first routing and escalation

**Status:** Complete
**Scope:** Route enabled known ATSes to dedicated adapters, unsupported ATSes to Generic, and escalate a failed supported field to computer use only when the approved answer is known. Keep unsupported facts, CAPTCHA/authentication blocks, and uncertain submissions out of automatic retries.
**Acceptance:** Supported ATSes use their specialized adapters; safe structural failures can escalate once; completed or possibly submitted applications never restart; unresolved facts remain `needs_review`.
**Verification:** Local fixtures for each routing outcome and existing ATS/form tests.

**Delivered:** The registry chooses an enabled dedicated ATS adapter first and routes unsupported ATSes to Generic. Form filling tries the DOM path first; only a failed supported control with an approved non-empty answer may escalate to computer use, once. Unknown facts and uncertain/final submits remain blocked by shared policy.

### Phase 3 — General computer-use layer

**Status:** Complete for constrained form repair
**Scope:** Extend the current vision helper into a screenshot-grounded form operator with a constrained action schema, fresh screenshot, field/page context, and state verification. Keep browser-level Playwright control; do not add OS desktop control unless later required.
**Acceptance:** A local fixture with controls that defeat DOM interaction can be completed through visual actions; unsupported or ambiguous actions escalate; computer use cannot submit around shared gates.
**Verification:** Unit tests for action validation and local Playwright tests for click/type, verification, and final-submit blocking.

**Delivered:** Screenshot and field context guide text and dropdown repairs. Text actions must match the requested action and land on the DOM-identified field. Dropdown actions must land on the requested control or an option owned by its listbox; submit/navigation controls and options in other fields are rejected. The approved answer supplies the text, and the field is re-read after the action. The existing Generic vision loop continues to handle page-level navigation.

### Phase 4 — End-to-end submission and evaluation

**Status:** Complete for local single-step and multi-step Generic fixtures
**Scope:** Test the complete local application flow from job route through form completion, submit-intent persistence, a local fixture receiving the final POST, and confirmation detection. Build a small sanitized regression set from known failures.
**Acceptance:** At least one end-to-end local fixture records a real form POST and returns `SUBMITTED`; uncertain outcomes are held for review; no live posting or applicant data is needed for tests. Compare coverage, verified-field accuracy, escalation rate, submission success, and model/tool cost before enabling additional cases.

**Delivered:** A Playwright fixture forces DOM filling to fail, fills the approved answer through screenshot-based computer use, posts to the local server, and confirms `SUBMITTED`. Generic and Workday review paths require evidence for every known application answer and trace verification counts. Generic treats `Next`/`Continue` as navigation when they lead to another flow step, including on the last numbered step or review page. A control with a clearly terminal destination or explicit application-final label gets a persisted, one-click intent; either way, only a fresh application confirmation marks it `SUBMITTED`. An unchanged or uncertain action stops for review without retry. Local fixtures cover wizard POSTs, terminal destinations, OTP forms, server-backed consent POSTs, confirmation detection, and missing confirmations. No live application was submitted.

**Remaining limitation:** A process crash after an intermediate navigation POST has no separate persisted step-intent record. The normal run can continue safely, but a later run restarted from the job URL could repeat that step. Adding durable per-step recovery is follow-up work; final application actions already have a persisted submit intent.

**Verification evidence:** `./.venv/bin/python -m pytest -q tests/test_generic.py tests/test_runner.py` passed: 212 tests. `git diff --check` and Python bytecode compilation passed. Tests use local Playwright fixtures; no live application was submitted.

## Operating workflow

For each phase, the main agent reads this plan and the relevant code/tests, then prepares a bounded phase task with objective, files, acceptance criteria, risks, and verification commands. It can delegate independent read-only inspections in parallel. Implementation delegation is allowed only after interfaces are stable and write scopes do not overlap. The main agent integrates the result, runs relevant checks, reviews the diff, records evidence here, and continues to the next clear phase without asking for approval at every boundary.

Ask the user only when a decision cannot be safely inferred and materially changes applicant facts, submission behavior, privacy/retention, use of paid services or credentials, or external activity. Local fixture submissions are allowed for verification. Live applications and other external actions require an explicit request in that task.
