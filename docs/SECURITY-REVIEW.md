# Security review

Tested **only against our own local/test instance** (`tests/test_app.py::SecurityTests` plus the existing integrity, concurrency and permission tests). Never point these techniques at the live site without a written test plan, a staging copy, and the owner's sign-off.

Last reviewed: 3 October 2026. Re-run on every release (CI does it automatically) and repeat this review when payments, wallets, draws, roles or login change.

## What was tried, and the result

| Area | Attack tried | Result | Test |
|---|---|---|---|
| **Insecure direct object references** | Second customer opens another's order, checkout pay/cancel/done, ticket, withdrawal, support case, notification; reveals their instant-win play; marks their notification read; pays their checkout | All 404 / no effect | `test_idor_other_customers_records` |
| **Privilege escalation** | Customer opens every admin page; posts to admin actions (make admin, credit wallet); extra form fields on profile (`is_admin`, `admin_role`, `points`, `email_verified`); Support-role staff open payouts / features / promos and credit a wallet | All refused; extra fields ignored | `test_privilege_escalation`, `test_roles_limit_what_staff_can_do` |
| **Price / quantity manipulation** | Negative, zero, huge, NaN, non-numeric quantities; ticket numbers outside the range; posting a `price` field; withdrawals of `inf`, `nan`, `1e308`, negative, fractions of a penny; redeeming negative/infinite points | Price always computed on the server; limits hold; invalid amounts refused | `test_price_and_quantity_tampering` |
| **Coupon abuse** | Same one-use code in 5 simultaneous checkouts; reuse after success; SQL-looking code | Used at most once | `test_promo_cannot_be_reused_or_raced` |
| **Race conditions** | 40 people buying the same number at once; 30 buyers for 25 tickets; 6 simultaneous withdrawals of more than half the balance; double-reveal of an instant win; repeated webhooks | One winner; no oversell; one withdrawal; paid once | `test_forty_people_racing_for_one_number`, `test_stampede_never_oversells`, `test_concurrent_withdrawals_cannot_overdraw`, integrity tests |
| **CSRF** | Admin action with no token; with another session's token; customer profile change with no token | 400, nothing changed | `test_csrf_required` |
| **Injection** | SQL fragments in search, login, promo codes, audit filters; script tags in names and support messages viewed by staff | Parameterised queries everywhere; output escaped | `test_injection_and_escaping` |
| **Open redirect** | `next=https://evil.com`, `//evil.com`, `/\evil.com`, `javascript:`, CRLF | Only same-site paths followed | `test_open_redirects_blocked` |
| **Rate-limit bypass** | Password guessing while changing `X-Forwarded-For` each time | Account-level lockout still applies | `test_login_lockout_survives_spoofed_ip` |
| **Admin access** | Guest and customer to `/admin/*`; admin without MFA; wrong MFA code; recovery code reuse | 404 / MFA required / logged | `test_privilege_escalation`, `test_mfa_required_for_admins` |
| **Webhook forgery** | Fake Stripe event without or with a bad signature | 400, ignored | `test_webhook_needs_valid_signature` |
| **Tampering after the fact** | Editing/deleting audit rows, ledger rows, draws, frozen entry lists directly in the database | Blocked by triggers; audit hash chain detects edits made by bypassing them | `OperationsTests` |

## Found and fixed in this review
1. **Open redirect** — `/login?next=/\evil.com` was accepted (browsers treat `\` as `/`). Fixed in `safe_next()`; notification links now use it too.
2. **Server error on `amount=inf`** in withdrawals (and the same parsing in deposits, limits and admin money fields). All money input now goes through `to_pence()`, which rejects non-finite and absurd values.
3. **Server error on an invalid date** in the audit-log filter. Now ignored.
4. **Missing browser protections.** Added `Content-Security-Policy` (same-site only; Stripe allowed as a form target), `Permissions-Policy`, `Cross-Origin-Opener-Policy`, and `Strict-Transport-Security` on HTTPS. Session cookies are now `Secure` by default when `SITE_URL` is https.

## Added in Phase 4
- Admin step-up confirmation (password or MFA within 10 minutes) for money, results and access changes; 30-minute admin idle timeout; large wallet adjustments limited to Administrators; reasons required.
- Database-level rules against overselling, double payouts and editing final records.
- nginx rate limiting of authentication endpoints across all workers; `X-Forwarded-For` overwritten by nginx.
- Error reports scrubbed of personal data and secrets.
- Chargebacks / Stripe-side refunds flagged immediately.
- Scope for the independent test: `docs/PENTEST-SCOPE.md`.

## Known limitations / follow-ups
- The CSP still allows inline scripts (`'unsafe-inline'`) because templates use small inline handlers. Moving them into `static/app.js` would allow a strict policy.
- `GET /checkout/<id>/cancel` changes state (Stripe's cancel link must be a GET). Impact is limited to the owner's own pending checkout, and it's owner-only.
- App rate limits are per web worker, in memory; nginx now adds a shared per-IP limit on login, sign-up, reset and MFA (install the updated `deploy/nginx-prizecomp.conf`).
- The app trusts one proxy hop for the client IP (`ProxyFix x_for=1`); nginx must stay in front and the app port must stay bound to 127.0.0.1 (as in `docker-compose.yml`).
- Recommended before launch: an independent penetration test of staging by a CREST-accredited tester; keep this file updated with their findings.
