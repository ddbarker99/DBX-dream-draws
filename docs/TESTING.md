# Testing

| Command | What it covers | When |
|---|---|---|
| `python -m pytest -q tests/test_app.py` | 130+ tests: sign-up, login, MFA, roles, ticket allocation, limits, closing and snapshots, postal entries, checkout and payment idempotency, wallets, refunds, withdrawals, instant wins, draws and redraws, notifications, pre-launch and draw-readiness checks, audit chain, ledgers, reconciliation, feature flags, support desk, reports, error tracking, disaster-recovery drill, security attacks. After **every** test the data must pass `integrity_problems()`. | Every change (CI) |
| `python tests/qa_crawl.py` | Every page as guest, player and admin: no errors or broken links; one h1; labels; alt text; titles/descriptions/canonicals; data integrity afterwards | Every change (CI) |
| `python tests/perf_budget.py` | Page weight, requests, inline JS, server time against budgets | Every change (CI) |
| `AXE_JS=… python tests/a11y_keyboard.py` | Real browser, 390px and 1280px: keyboard-only journeys, focus visibility, traps, menu, axe-core WCAG AA, CSP violations | Every change (CI) |
| `python tests/load_test.py https://staging…` | Many simultaneous visitors and buyers on staging; contested numbers; p95 | Before big launches |
| `flask dr-drill` | Restores the latest backup into a clean copy and uses it | Quarterly (shown on System health) |
| `docs/QA-CHECKLIST.md` | Manual checks: real phones, real emails, Stripe test cards | Each release (relevant sections), full before going live |
| `docs/USABILITY-TEST.md` | Real people on their own phones | Before launch, then after big changes |

Security testing: `SecurityTests` covers IDOR, privilege escalation, price/quantity tampering, coupon races, concurrent withdrawals, CSRF, injection, open redirects, rate-limit bypass, webhook forgery and headers — **only ever against local/test instances** (`docs/SECURITY-REVIEW.md`).
