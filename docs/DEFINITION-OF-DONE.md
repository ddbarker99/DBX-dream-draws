# Definition of Done

A change is **done** only when every applicable line is true. "It works on my machine" is not done. Copy the checklist into the pull request.

## Every change
- [ ] Does what was asked, and the person who asked has seen it on staging.
- [ ] Automated tests added or updated for the new behaviour **and** for what must not happen (wrong user, wrong role, bad input, double-click/race where relevant).
- [ ] `pytest`, `qa_crawl.py`, `perf_budget.py` and the keyboard/axe walkthrough all pass (CI green).
- [ ] Works at 390px and 1280px wide; no horizontal scrolling; tap targets ≥ 44px.
- [ ] Keyboard-only use works; focus visible; form errors appear next to the field and in a summary; labels on every field.
- [ ] Empty, loading, error and "not allowed" states designed — no blank pages or raw errors.
- [ ] Wording is plain English, says what happens next, and matches the terms and the actual rules.
- [ ] No secrets in code or logs; `.env.example` updated for any new setting (with a safe default).
- [ ] Docs updated: README (what changed), QA checklist rows, runbook if it adds a failure mode, privacy audit if it stores new personal data.

## Money, entries, draws, wallets or permissions (additionally)
- [ ] Server decides every price, limit and eligibility; nothing trusted from the browser.
- [ ] Runs inside one write transaction; idempotent if it can be retried (webhooks, double-clicks).
- [ ] Ledger changes are new lines, never edits; an audit-log entry records who did what.
- [ ] Integrity checks still pass (tests assert this automatically after every test).
- [ ] Reviewed by a second person, reading the diff, with `docs/MECHANICS.md` open.
- [ ] Rollback note added to `docs/DEPLOY.md` if the schema changed.

## Customer-facing features (additionally)
- [ ] Can be switched off without a deploy (feature flag) if it's risky or new.
- [ ] Measured: we know which target in `docs/TARGETS.md` it should move.
- [ ] Optional emails/SMS only to people who opted in; essential messages still sent.

## Released
- [ ] Verified backup taken; deployed; smoke test passed; System health green for 15 minutes; no new errors on the Targets page.
