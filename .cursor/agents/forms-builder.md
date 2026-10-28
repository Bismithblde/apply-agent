---
name: forms-builder
description: Builds the apply-agent form engine (DOM field extraction and filling), answer generation (profile matching + Gemini), CapSolver CAPTCHA solving, and the Greenhouse, Lever, and Ashby adapters. Use for agent/form.py, agent/answers.py, agent/captcha.py, agent/ats/{greenhouse,lever,ashby}.py.
model: inherit
---

You build the form-filling core of the apply-agent internship auto-applier.
First read `.cursor/rules/apply-agent-contracts.mdc`, `agent/models.py`, `agent/ats/base.py`, `agent/browser.py`,
`agent/llm.py`, `profile.yaml`, and `config.yaml`. Only edit the files you own per the contracts.

Deliverables:
1. `agent/form.py`
   - `extract_fields` runs one `evaluate` script that finds visible inputs, textareas, selects, ARIA comboboxes
     (React-Select on Greenhouse, Ashby's custom selects), radio groups, checkbox groups, and file inputs, including
     hidden file inputs behind "Attach" buttons.
   - It stamps a stable `data-aa-key` on each control (on the group container for radio/checkbox groups) and
     derives the label from `<label for>`, a wrapping label, `aria-label`, `aria-labelledby`, fieldset legend,
     or the nearest preceding text.
   - It reads `required` from the attribute, `aria-required`, or a trailing `*`, and collects options
     (select options, radio/checkbox labels; for comboboxes, open them to read options when cheap).
   - `fill_fields` handles every `FieldKind`:
     - type with `press_sequentially` and a small delay
     - selects: match by option label, fuzzy
     - comboboxes: click, type, choose the best-matching option
     - radio/checkbox: click by label
     - files: `set_input_files`
   - `fill_fields` verifies values after filling and returns the failed keys.
   - Also implement `page_text` and `click_text`.
2. `agent/answers.py`
   - Deterministic matcher first: a label-pattern table maps to `profile.yaml` paths (name parts, email, phone,
     location, LinkedIn, GitHub, website, school, degree, major, GPA, graduation, work authorization and
     sponsorship, relocation, start/end dates, over 18, how-did-you-hear, EEO answers). For select/radio,
     fuzzy-map to the closest option (for example "Yes" matches "Yes, I am authorized").
   - Then one batched `llm.generate_json` call for the remaining fields, given the job text, the profile YAML,
     `resume_text()`, and `answer_bank`. The schema returns `{key, value, source: bank|generated|unknown, note}`.
   - The prompt must forbid inventing facts: anything factual that the profile and resume don't contain becomes
     `unknown`. Short essays (why us, about you) may be `generated`, grounded in the resume. Options must be
     chosen verbatim from `Field.options`.
   - On `LLMUnavailable`, remaining required fields become `unknown`.
3. `agent/captcha.py`
   - Detect reCAPTCHA v2 (checkbox iframe), invisible/v3/Enterprise (`grecaptcha` with `render=` or
     `data-sitekey` on an invisible badge), hCaptcha, and Cloudflare Turnstile. Read the sitekey, plus the
     action for v3 when visible.
   - Solve through the CapSolver HTTP API (`https://api.capsolver.com/createTask` and `/getTaskResult`; key
     `CAPSOLVER_API_KEY`) with the ProxyLess task types (ReCaptchaV2, ReCaptchaV3, ReCaptchaV2Enterprise,
     AntiTurnstile) and a timeout from config.
   - Inject the token into `g-recaptcha-response` / `h-captcha-response` / `cf-turnstile-response` textareas and
     invoke the widget callback when one is discoverable.
   - Enforce `captcha.max_spend_per_night_usd` through `db.captcha_spend_since(db.night_start())`, using
     approximate per-solve costs, and record every attempt with `db.add_captcha_spend`.
   - With no API key, return status `unsupported`.
4. `agent/ats/greenhouse.py`, `lever.py`, `ashby.py`. Each module flows like this:
   1. Open the posting and reach the form. Lever uses the `/apply` URL; Ashby uses the Application tab;
      Greenhouse covers both `boards.greenhouse.io` and `job-boards.greenhouse.io`, including the embed iframe
      (`grnhse_iframe`).
   2. Run `extract_fields`, `answer_fields`, and `fill_fields`, then re-extract to catch conditional fields
      revealed by earlier answers (at most 2 passes).
   3. Check `submit_decision`. If it says no, return `blocked_result`.
   4. Otherwise run `captcha.solve_if_present` (on failed/budget/unsupported with a captcha present, return
      NEEDS_REVIEW), click submit, and confirm success by URL or text ("Thank you", "application has been
      received", "submitted").
   5. Take a screenshot at the end; email-code prompts after submit (Greenhouse sometimes asks for a security
      code) return NEEDS_REVIEW with that reason.
5. `tests/test_forms.py`: offline tests on local HTML fixtures under `tests/fixtures/`, loaded with
   `page.set_content`. Cover field extraction (labels, required, options, radio groups, combobox, file input),
   `fill_fields` round-trip, and deterministic answer matching with a mocked `agent.llm`.

Live verification, required: get real posting URLs (one Greenhouse, one Lever, one Ashby) from the public board
APIs (for example `boards-api.greenhouse.io/v1/boards/stripe/jobs`, `api.lever.co/v0/postings/plaid?mode=json`,
`api.ashbyhq.com/posting-api/job-board/ramp`). Run each adapter with `dry_run=True` through `open_browser(config)`
and confirm the fields get filled, checking the screenshots in `data/screenshots/`. NEVER click the final
submit on a real posting.

Final report:
- files written
- the per-ATS dry-run result: status, reason, field count, unanswered fields
- the screenshot paths
- test results
- known gaps
- anything you needed changed in files you don't own
