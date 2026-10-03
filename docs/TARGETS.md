# Measurable targets

Shown live on Admin → **Targets** (green = on target). Review monthly; change a target only deliberately and note why here.

| Target | Goal | Measured by | Why it matters |
|---|---|---|---|
| Checkout completion (started checkout → paid) | ≥ 70% | Cookie-free journey counts, 30 days | Drop-off at payment is lost revenue and often a usability or payment problem |
| Mobile vs desktop completion gap | ≤ 10 points | Journey counts by device | Most customers are on phones |
| Payment problems (credit-card refusals, refunds needed) | ≤ 2% of card payments | Checkouts, 30 days | Rising numbers mean confusing payment wording or provider issues |
| "How do I / what happened?" support requests | ≤ 3 per 100 paid orders | Support cases (entry, account, payment, other), 30 days | A proxy for confusion — each one is a page that didn't explain itself |
| Server errors | ≤ 0.1% of requests | App's own request counts and error log, 7 days | Every 5xx is a customer who couldn't do something |
| Server time, 95th percentile | ≤ 500 ms | App's request timing, 7 days | The part of page speed we control on the server |
| Largest Contentful Paint (phone, homepage) | ≤ 2.5 s | PageSpeed Insights, recorded on the Targets page | Google's "good" threshold; perceived speed |
| Uptime | ≥ 99.9% (≈ 43 min down a month) | UptimeRobot on `/healthz`, recorded monthly | Especially around draws |
| Lighthouse accessibility (phone, competition page) | ≥ 95 | Lighthouse, recorded | Mobile usability and accessibility |
| Usability test tasks completed without help | ≥ 90% | Each usability round (`USABILITY-TEST.md`) | Real-user confirmation |

## Budgets enforced automatically (CI fails if exceeded)
From `tests/perf_budget.py` on seeded data: HTML ≤ 15 KB gzipped per page; page weight ≤ 250 KB excluding prize photos; ≤ 15 requests; inline script ≤ 12 KB; server p95 ≤ 150 ms locally; stylesheet ≤ 15 KB gzipped. Prize photos are served as ~640px thumbnails and lazy-loaded; keep uploads under 2 MB and landscape.

## Error tracking
Every server error is grouped on Admin → Targets → *Server errors* with a count and stack trace; System health turns red and emails `SUPPORT_EMAIL` when a new one appears (at most every 6 hours per check). Mark it fixed once the fix is live; it comes back if it recurs.
