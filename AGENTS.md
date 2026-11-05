# Repository guidance

This repository automates job discovery and applications with Python, Playwright, ATS adapters, and LLM assistance.

## Working in this repository

- Preserve all existing staged, unstaged, and untracked work. Never reset, clean, overwrite, or stash user changes to make a task easier. If changes overlap, inspect them and integrate carefully; ask only when intent cannot be established safely.
- For application-cascade work, use `docs/APPLICATION_AGENT_CASCADE_PLAN.md` as the roadmap and update its status and verification evidence as phases are completed.
- Continue through clear, local phases without requesting approval between phases. Ask only when an unresolved choice materially affects applicant facts, submission policy, privacy, paid services, credentials, or an external action and no conservative default fits.
- Keep applicant data and credentials private. Do not print or copy values from `profile.yaml`, `.env`, `secrets/`, browser profiles, or application databases into logs, tests, plans, or responses. Prefer synthetic fixtures.
- Tests and development runs must use local fixtures. Never submit a real application, create a real account, send or read real email, spend CAPTCHA credits, or otherwise act on an external service unless the user explicitly requests that action in the current task.
- Preserve the shared answer and submit safeguards. Unknown or unverified required answers must stop for review; computer-use actions must not bypass `submit_decision` or the persisted submit-intent gate.

## Delegation

- Use subagents for independent repository exploration, focused review, or implementation with a disjoint file scope. Give each task a concrete question, expected output, and boundaries.
- Keep shared interfaces, final integration, plan status, and final verification with the main agent. Do not delegate overlapping edits. Review delegated changes and evidence before integrating them.
- Use the parent session's sandbox and tool permissions as the security boundary; a subagent prompt alone does not restrict access.

## Verification

- Run relevant existing tests before changes when practical, then run focused tests for changed behavior. Add tests only for meaningful behavior, edge cases, or regressions.
- Verify critical browser flows with local fixtures when possible. Report commands and observed results; do not claim live-application success from a dry run or mocked test.
