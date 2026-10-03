# DBX Dream Draws — Release QA report

**Release tested:** commit `a3e3caf` on branch `claude/loving-johnson-ovk1if` (asset version 30)
**Date of testing:** 3 October 2026 (UK)
**Environment:** isolated test copies of the application (Flask test client, a real gunicorn server with the production configuration, and headless Chromium). No production systems were tested and no destructive or load testing touched production.
**Register:** `docs/qa/QA-REGISTER.xlsx` / `.csv` (one row per test: ID, area, feature, test, expected, actual, device, pass/fail, severity, evidence, bug ID, retest). **Bug log:** `docs/qa/bugs.json` (also the *Bugs* sheet). **Inventory:** `docs/qa/INVENTORY.md`.

This report says what was actually run, what it found and what was **not** run. It is not a claim that "everything works".

---

## 1–3. Totals
| | Count |
|---|---|
| Register rows (tests and checks) | **299** |
| Automated tests executed (final run) | **252** (business logic, money journeys, draws, roles, security sweeps, regressions) |
| Scripted checks (crawl, browser sweep, race scenarios ×3 rounds, load, restore, static analysis) | **38** rows covering 6,104 browser page checks, 23 race scenarios × 3 rounds, 433 crawled pages |
| **Passed** | **290** |
| **Failed (final)** | **0** |
| Not run here (need people, devices, or the live server) | **9** — see §21 |

## 4–7. Bugs discovered (14)
| Severity | Found | IDs |
|---|---|---|
| **P0 — critical** | **5** | BUG-004 approval applied several times · BUG-005 deposit refund sent to Stripe per click · BUG-006 deposit refund racing a purchase · BUG-007 credit-card auto-refund sent per duplicate webhook · BUG-008 double-submitted redraw ran several redraws |
| **P1 — high** | **2** | BUG-001 responsible-play break could be shortened · BUG-009 admin wallet/goodwill form could apply twice on double-click |
| **P2 — medium** | **3** | BUG-010 stray HTML in two admin page titles · BUG-011 cookie policy missing the Back button's session storage · BUG-012 wallet page sideways scroll at 320 px |
| **P3 — low** | **4** | BUG-002 sitemap dead code · BUG-003 unused code · BUG-013 admin pages sideways scroll on phones · BUG-014 `/favicon.ico` 404 |

All five P0s are **race conditions** — they only happen when the same request arrives twice at the same instant (double-click, two tabs, two staff, Stripe retrying a webhook). Ordinary single-request tests had passed them; the multi-process race suite found them.

## 8–10. Fixes and retests
| | |
|---|---|
| Bugs fixed | **14 of 14** |
| Bugs awaiting fixes | **0** |
| Retested successfully | **14 of 14** — each has a regression test or a re-run scenario (see register *Retest Result*), and the full suite was re-run after every fix (final 252/252). |

Process followed for each: reproduce → fix → developer test → retest the failing scenario → full regression → close. One of my own fixes (deposit refunds) was itself caught by its new regression test before release — the first version could not return money if Stripe failed because a database safeguard (correctly) blocks lowering a deposit's refunded amount; it was redesigned to reserve in the wallet ledger instead.

## 11. Browser / device matrix
| Browser / device | Method | Result |
|---|---|---|
| Chromium (desktop 1280–1920) | Real headless browser, 436 pages × 3 personas | Pass after fixes |
| Widths 320, 360, 375, 390, 414, 480, 600, 768, 820, 1024, 1280, 1440, 1920 | Real browser at each width | Pass after fixes (no sideways scrolling) |
| iPhone 13, Pixel 7, iPad Mini | Chromium **device emulation** (not physical devices) | Pass; full purchase journey on iPhone 13 emulation passed, no tap targets under 24 px |
| Safari, Firefox, Edge | **Not run** — only Chromium is available here | Needs manual testing |
| Physical phones/tablets | **Not run** | Needs manual testing |

## 12. Performance
Page-weight budgets: **all within budget** (homepage 5.1 KB HTML, competition page 8.7 KB, CSS 13.4 KB gzipped). Keyboard walkthrough and axe accessibility scan: **no problems**.

## 13. Load test (gunicorn, 3 processes × 4 threads, 4-core machine, load generator on the same machine)
| Non-stop users | Requests/s | p50 | p95 | p99 | Errors | Contested ticket |
|---|---|---|---|---|---|---|
| 50 | 365 | 8 ms | 29 ms | 395 ms | 0 | sold once |
| 150 | 573 | 124 ms | 294 ms | 502 ms | 0 | sold once |
| 300 | 565 | 364 ms | 946 ms | 1.11 s | 0 | sold once |

Safe operating capacity matches `docs/CAPACITY.md`: ≈ 550–600 pages/s on 4 cores before pages slow; no errors or data risk past that.

## 14. Security testing summary
Automated sweeps over **every** route (159 routes, 123 forms):
- **Admin access:** every admin URL refuses visitors and customers (GET and POST); every staff role can open only pages its permissions allow; no admin view lacks a permission check; support staff can't trigger refunds, draws, redraws, settings or the emergency lock by direct POST.
- **CSRF:** every POST endpoint rejects requests without the token (webhook requires a valid Stripe signature instead).
- **Input:** wrong methods → 405; junk/huge/negative/script/SQL-like ids and query strings on every page → no server errors, no reflected script, no tracebacks.
- **Customer isolation:** existing IDOR tests (orders, tickets, prizes, withdrawals, cases, notifications) pass.
- **Sessions:** cookie HttpOnly + SameSite=Lax (Secure on https); private pages `no-store` and `noindex`; login and reset don't reveal whether an account exists; lockout and rate limits tested.
- **Secrets:** none in the code or the full git history.
- **Not done here:** an independent penetration test (`docs/PENTEST-SCOPE.md`) and a dependency vulnerability scan (declined at the permission prompt).

## 15. Payment reconciliation
Existing tests cover order = Stripe = ledger for successful, failed, expired, refunded, wrong-amount, credit-card-refused and chargeback cases, and deliberate mismatches are detected by the reconciliation job. Race tests: duplicate confirmations and duplicate webhooks give **one** payment, order, ticket set and ledger set (S6a, S6b); duplicate credit-card webhooks now give **one** refund (S6c, fixed). Real Stripe test-mode on staging: **not run** here.

## 16. Competitions and draws
1-entry, multi-entry, paid + free, refunded entries excluded, sold-out and not-sold-out draws; draw re-run never makes a second winner; a crash mid-draw leaves no partial result and the retry picks the same single winner; a notification failure after the draw keeps the result; concurrent draw execution → exactly one draw (S7a); double-submitted redraw → one redraw (S7b, fixed); after-draw checks recorded and shown publicly; UK clock changes handled.

## 17. Instant wins
Repeated reveals return the same result and pay once; concurrent reveals of one play pay once (S9a); settlement racing a reveal pays once (S9b); unrevealed wins settle exactly once even when the job runs repeatedly.

## 18. Wallet and withdrawals
Amount edges (below £5, zero, negative, junk, over balance; exactly £5 and full balance allowed); site credit and deposits can't be withdrawn; rejected withdrawals return money once; concurrent withdrawals and withdrawal + purchase never overdraw (S3a/S3b); same wallet in two tabs never goes negative (S2); balances can't go negative even at database level; deposit refunds can't double-refund or refund spent money (S12a/S12b, fixed).

## 19. Free entry
Wrong answers never entered; approving the same envelope twice → one ticket; late envelopes refused; free + paid share the per-person limit; accepted postal tickets appear in the frozen draw pool on equal terms (draw-record tests recompute the winner from the published list).

## 20. Backup restore
`flask backup` then `flask dr-drill` against seeded data: **PASSED** — restored copy starts, pages load, 2 users / 5 competitions / 55 tickets / 1 draw present, the stored draw still recomputes to the same winner, integrity and audit chain clean. Restore-and-verify time **0.4 s** (excluding server rebuild); RPO 0 h at the time of the drill.

## 21. Known limitations — not run here
| ID | What | Why / who |
|---|---|---|
| NR-01 | Safari, Firefox, Edge | Only Chromium installed — you or a tester |
| NR-02 | Physical iPhone / Android / tablet | No devices here — you or a tester |
| NR-03 | Email in Gmail / Outlook / Apple Mail | Needs real mailboxes — send yourself each email from Admin → Email previews flows |
| NR-04 | Real Stripe test-mode purchase and refund | Run on staging with test keys |
| NR-05 | Production smoke test | After you deploy — checklist in `docs/QA-CHECKLIST.md` §14 |
| NR-06 | Independent penetration test | External tester under `docs/PENTEST-SCOPE.md` |
| NR-07 | Dependency vulnerability scan | Declined at the permission prompt; run `pip-audit -r requirements.txt` when convenient |
| NR-08 | Exploratory testing by fresh testers | People who didn't build it — use `docs/USABILITY-TEST.md` |
| NR-09 | Legal sign-off of terms and mechanics | Solicitor — record on Admin → Compliance |

Other limitations: Stripe and email are simulated in tests (their real failure modes are mocked — outage, decline, duplicate, late delivery); race tests run on one machine; device testing is emulation.

## 22. Evidence
- Register with per-test evidence paths: `docs/qa/QA-REGISTER.xlsx`
- Bug log with reproduction, fix and retest: `docs/qa/bugs.json`
- Test code: `tests/test_qa_audit.py`, `tests/test_security_audit.py`, `tests/race_audit.py`, `tests/browser_audit.py`, `tests/test_app.py`
- Run logs and screenshots (session scratchpad): race runs before/after fixes, browser sweep JSON, mobile journey screenshots, load-test output.

## 23. Release tested
`a3e3caf` (fixes) — this report and register are committed immediately after it.

## 24. Final production smoke test
**Not yet performed** — it happens after you deploy this zip. Use `docs/QA-CHECKLIST.md` §14 and record the date/time here.

## 25. QA sign-off
Automated and scripted QA performed by Claude (AI developer). **Final sign-off must be given by a named person** (the owner or a tester) after items NR-01 to NR-09 and the production smoke test — name: ____________ date: ____________.

---

### Definition of pass — status
| Criterion | Status |
|---|---|
| Critical journeys work | ✓ (automated + emulated mobile) |
| Financial calculations reconcile | ✓ |
| Payments can't be replayed into duplicate orders | ✓ (incl. race tests) |
| Tickets can't be duplicated | ✓ (incl. multi-process races) |
| Entry limits can't be bypassed | ✓ |
| Competition states transition correctly | ✓ |
| Draws operate correctly | ✓ |
| Instant-win results consistent | ✓ |
| Wallet balances reconcile with the ledger | ✓ (+ database guard) |
| Withdrawals behave correctly | ✓ |
| Free entry follows the rules | ✓ |
| Customer accounts isolated | ✓ |
| Admin permissions work | ✓ (route-wide sweep) |
| Critical actions audited | ✓ |
| Mobile journeys work | ✓ emulated — physical devices outstanding |
| Supported browsers work | Chromium ✓ — Safari/Firefox/Edge outstanding |
| Accessibility testing completed | ✓ automated + keyboard — screen-reader manual check outstanding |
| Backups restored successfully | ✓ |
| Critical background jobs survive retries | ✓ |
| No P0 bugs remain | ✓ |
| No unresolved P1 bugs | ✓ |
| P0/P1 fixes regression-tested | ✓ |
| Final production smoke test passes | **Pending deployment** |
