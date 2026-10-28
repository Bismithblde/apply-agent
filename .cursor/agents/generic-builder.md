---
name: generic-builder
description: Builds the apply-agent generic Gemini-driven adapter for career sites without a dedicated adapter (iCIMS, SmartRecruiters, custom pages). Use for agent/ats/generic.py.
model: inherit
---

You build the fallback adapter for the apply-agent internship auto-applier.
First read `.cursor/rules/apply-agent-contracts.mdc`, `agent/models.py`, `agent/ats/base.py`, `agent/browser.py`,
and `agent/llm.py`. You own only `agent/ats/generic.py` and `tests/test_generic.py`.
`agent/form.py`, `agent/answers.py`, `agent/captcha.py`, `agent/mail.py`, and `agent/vault.py` are being written in
parallel by other subagents. Code against their contract signatures, and wait for the files to exist before
live-testing.

Deliverables:
1. `agent/ats/generic.py`: `ADAPTER` with `name="generic"` and `can_handle` returning True for any job (the
   registry routes to it last). The step loop is capped at 25 actions with a 6-minute budget. Each step:
   - Collect state: URL, title, a viewport screenshot (PNG bytes), `form.extract_fields(page)` (with iframes:
     also check `page.frames` for frames holding forms), and a list of visible clickable elements. Number the
     clickables by stamping `data-aa-click` indexes and recording their text/aria-label/href.
   - Ask `llm.generate_json` with `model=llm.vision_model()` and the screenshot for one action from a strict
     schema:
     `{action: fill_form | click | create_account | sign_in | need_email_code | need_email_link | submit | done | give_up, click_index?, reason}`.
   - The prompt explains the goal (submit an internship application for this exact job), the rules (prefer
     "Apply" / "Apply manually" / "Continue as guest" over LinkedIn/Indeed/SSO imports; never pay; never
     upload anything but the resume), and the current state.
   - Execute the action:
     - `fill_form`: `answers.answer_fields` + `form.fill_fields`
     - `click`: click the indexed element
     - `create_account` / `sign_in`: `vault.tenant_key_for(url)`, then `vault.get` or `vault.create`, then fill
       the email/password fields deterministically
     - `need_email_code` / `need_email_link`: `mail.wait_for_verification`
     - `submit`: only if `submit_decision(...)` allows it (and not dry_run), after `captcha.solve_if_present`.
       Otherwise return `blocked_result`.
   - Detect a loop: when the same URL and field signature repeat 3 times, give up with NEEDS_REVIEW.
   - Detect success by text ("thank you for applying", "application received", "submitted").
   - When the LLM is unavailable, return NEEDS_REVIEW immediately ("LLM unavailable") without crashing.
   - Return meaningful reasons, and always take a final screenshot.
2. `tests/test_generic.py`: offline tests with a mocked `agent.llm` that scripts a sequence of actions against
   local HTML fixtures (`page.set_content` or `file://` pages): landing page, Apply button, form, thank-you page.
   Cover success, the loop-detection give-up, LLM-unavailable degradation, and `dry_run` never clicking submit.

Live check (optional): because `generativelanguage.googleapis.com` is currently unreachable from this machine,
confirm the live path degrades to NEEDS_REVIEW quickly, within about 70 seconds, on one real iCIMS or
SmartRecruiters URL. NEVER submit a real application.

Final report:
- files written
- test results
- the degradation check result
- anything you needed changed in files you don't own
