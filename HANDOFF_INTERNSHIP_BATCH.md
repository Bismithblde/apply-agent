# Internship application batch handoff

Updated: 2026-10-27. The application goal is **paused**. Resume only when the user asks. The final forms, generic and runner agents were stopped while working, so their latest edits and tests need review before use.

## Objective and operating constraints

Submit the 50 selected internships listed in `logs/batch_50_ids.txt`, or document a concrete blocker for each. The user authorized final submissions for these IDs only. Never run `python -m agent apply` without `--job`: the database contains over 1,500 jobs. Use `.venv/bin/python`, inspect `.cursor/rules/apply-agent-contracts.mdc`, and do not print secrets from `.env` or Gmail codes.

Use the user's resume and `profile.yaml` as evidence for factual answers. The user said to infer experience from the resume, and specifically reported no agricultural experience. Do not invent experience, qualifications, commitments or consent. They want this thread to orchestrate subagents and requested Luna Max; that model was not offered by the available spawn tool, so the last builders used `gpt-6.1-sol` at `max` reasoning. Respect current model availability and user preferences.

The user suggested using an LLM to identify forms and buttons and fill fields dynamically. This is a promising direction: inspect page and frame state, ask the model to identify visible controls and their intended actions, use resume/profile facts to determine answers, then fill and **verify the selected value in the live DOM**. Handle dynamically rendered dropdowns, popups, iframes, required validation, and page transitions. The existing generic adapter already uses LLM-assisted actions, so extend and debug it rather than starting over. Do not treat a proposed selector, an attempted click, or saved `answers_json` as proof the form was actually filled or submitted. Preserve the submit-intent safeguards, avoid duplicate applications, and verify the employer's actual confirmation page or screenshot after final submission.

For a cleared job with fields, the runner proposed this workflow: run the exact ID with `--mode full --force --dry-run`, inspect its screenshot, actual displayed school, dates, other required selections, and unknowns, then perform the real run only if those values are correct. A dry-run screenshot does not itself guarantee the subsequent run makes identical selections; recheck during the real run where possible.

## Verified batch status

Four internship applications have confirmation screenshots/URLs:

| Company | Job ID | Confirmation |
|---|---|---|
| Awetomaton | `vanshb03:819220061f6a93e5` | `https://job-boards.greenhouse.io/awetomaton/jobs/5394046008/confirmation` |
| Coinbase | `vanshb03:87d534f655f22360` | `https://job-boards.greenhouse.io/embed/job_app/confirmation?for=coinbase&token=8175462` |
| Compeer Financial | `vanshb03:c3ba2f10eda63ed9` | `https://job-boards.greenhouse.io/compeerfinancial/jobs/5422577008/confirmation` |
| Ginkgo Bioworks | `vanshb03:74980d2d490cb41d` | `https://job-boards.greenhouse.io/ginkgobioworks/jobs/5033167007/confirmation` |

Hudson River Trading `vanshb03:c49a8c046a838981` has database status `submitted` and a confirmation, but it is for the HRT talent community/expression of interest, not the listed internship. Do not count it as a confirmed internship submission or submit it again without checking the actual posting. The other 45 jobs are not confirmed as internship submissions. Inspect current database state before retrying; status counts cover the entire database unless explicitly scoped.

Five jobs currently have unresolved submit intents according to `db.has_uncertain_submit_intent`: ADT `vanshb03:46fef8510e2db818`; Aerospace `vanshb03:8431ea1ffd374180` and `vanshb03:43b4f8bbeff22679`; Americo Software Development `vanshb03:c45493443e357025`; ETC `vanshb03:b6bc234b32d3b892`. Screenshots for ADT and Aerospace show empty email-code fields before verification; Americo shows page 1 of 5 with resume upload open; ETC shows the initial contact/SMS page. They do not show success, but review authoritative employer/mail/account state and the CLI's resolution requirements before declaring an outcome and retrying. Raw `submit_intents` rows also include already-resolved and successfully submitted attempts: use `db.has_uncertain_submit_intent`, not the raw table alone.

The runner reported that NVIDIA attempts 64/65/66 and Keysight attempts 73–77 have explicit `not_submitted` resolutions, even though some logs say a submit was clicked. Verify current state before relying on that report. NVIDIA `vanshb03:704790bcee171907` run 128 tried Apply Now twice but stayed on the job page, recorded no new submit intent and did not submit. The screenshot is `data/screenshots/20261027-130236_NVIDIA_vanshb03_704790bcee171907_generic_needs_review.png`.

## Confirmed email verification root cause

The user showed ADT and Aerospace verification emails in Gmail. A mail builder confirmed the prior agent searched Oracle-hosted forms using `from:oracle`, but ADT and Honeywell used employer sender domains. Fresh ADT codes arrived for both runs 101 and 119 within seconds, yet the sender-filtered search returned none. The builder changed `agent/mail.py`, `agent/vault.py`, `tests/test_mail_rest.py` and `tests/test_vault.py` to use recipient-scoped fallback with safeguards, pagination and improved date/alias handling. Its report: 127 baseline tests, 55 focused tests, 11 local browser checks and a 171-test mail/accounts run passed. Review changes in the actual worktree; never reuse an old code. The generic owner was asked to use the actual OTP request/resend timestamp instead of a broad start-time window.

## Important form correctness defect

Saved screenshots for Neuralink run 122, GuidePoint run 117 and Mach run 81 displayed **Aston University**, although the intended/saved answer was **Stony Brook University**. GuidePoint also displayed Spring 2026 for graduation and January 2027 for start, whereas the profile says May 2028 and May 2027 respectively. NISC run 110 displayed Bachelor's Degree as the most recently completed education despite the applicant being a current student. These were not confirmed submissions. Fix the generalized dropdown/autocomplete logic to verify the selected DOM value matches the intended value and stop safely on mismatch; validate year and month separately. Do not simply select the first returned option. Reinspect affected forms before any real submission. Neuralink screenshot: `data/screenshots/20261027-120112_Neuralink_vanshb03_42a050b8f3fef06f_review.png`.

## Current code and agent state

Previous completed Workday builder reported changes to `agent/ats/workday.py` and `tests/test_accounts.py`, with 155 account tests passing: referral leaf selection, Yes/No controls, resume conflict checks and device types. It reported no live-ready candidate among CoStar, Itron, HP and Activision: some referral choices do not match the actual source, HP education fields remain blocked, Activision's relationship answer is not established, and phone device type is absent from the profile. Check these findings rather than assuming them away.

Generic builder reported `agent/ats/generic.py` and `tests/test_generic.py` changes for iframe navigation, selected choices, resume upload, SMS declines and OTP timing. It reported 86 generic tests plus seven focused tests passing, but an adjacent forms test failed on full-time availability. Its cleared NVIDIA candidate still failed live as described above. The generic agent was resumed to investigate, then stopped on pause; inspect its most recent work and test state.

Forms builder was modifying `agent/form.py`, `agent/answers.py` and relevant tests to generalize dropdown checks and resume-grounded answers. It had not finished or reported final validation when stopped. The Workday, forms, mail and generic agents all shared the worktree; do not overwrite their uncommitted work. `git status` may reflect a larger parent repo where this project is untracked; inspect file timestamps and diffs carefully.

At handoff, one pre-existing pytest process (`tests/test_generic.py tests/test_accounts.py tests/test_review.py`) was still visible; check whether it has finished before editing or launching overlapping tests. No application process was active. The systemd user sleep inhibitor `hyprcaffeine-overnight.service` and HyprCaffeine infinite mode were active at last check. The service is transient; verify after restart or logout.

## Other known blockers and next steps

- DocuSign `vanshb03:d83aa3f40b0e107b` and `vanshb03:f71773d3c26c8c8a`: iCIMS embedded forms; generic builder said one destination reached a server error.
- Disney `vanshb03:683939f6729c9230`: repeated same-state navigation at application page.
- Five Keysight IDs: hCaptcha; configured CapSolver returned unsupported service, so do not repeat that unsupported task as if it worked.
- Barkback and LAXIR: SmartRecruiters one-click application endpoints remained temporarily restricted on the latest read-only check.
- Apple: Apple ID / 2FA required. John Deere: prior review found year-round local commuting and documentation requirements.
- GuidePoint, NISC, Neuralink and Mach have unanswered questions that must be grounded in the resume or an explicit user preference. Do not invent salary, travel, scores, clearance, or onsite commitments.

When resumed: 1. Inspect active processes, agent state, files and `data/apply.db`; review interrupted changes and relevant existing test results. 2. Finish and test actual selected-value verification and email request timing. 3. Resolve uncertain submission outcomes only on sufficient evidence and in accordance with the CLI. 4. Run a single exact-ID dry run, review real form state, then a real exact-ID application where appropriate. 5. Verify confirmation and iterate serially, delegating independent generalized fixes. Do not report 50 complete without evidence for each posting.
