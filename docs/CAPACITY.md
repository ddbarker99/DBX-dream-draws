# Capacity and load testing

How much traffic DBX can take, where it will slow down first, and how to re-measure.

## Measured (3 October 2026)
Production configuration (gunicorn, 3 processes × 4 threads, SQLite WAL) on a 4-core machine, with the load generator **on the same machine** (so real capacity is higher). Each virtual user clicks non-stop with no thinking time — browse home → competition → number picker → basket — while 20–30 buyers race to check out the same ticket number.

| Virtual users (non-stop) | Requests / second | Median | 95% under | 99% under | Errors | Contested ticket |
|---|---|---|---|---|---|---|
| 50 | 369 | 8 ms | 25 ms | 296 ms | 0 | sold once |
| 150 | 603 | 113 ms | 267 ms | 496 ms | 0 | sold once |
| 300 | 575 | 388 ms | 700 ms | 989 ms | 0 | sold once |
| 600 | 541 | 934 ms | 1.35 s | 1.89 s | 0 | sold once |

Concurrency (separate processes, `tests/stress_concurrency.py`): 24–40 buyers in 24–40 processes released at the same instant — no number owned twice, never oversold, no double charges, integrity checks clean. Runs in CI.

## What that means
- The ceiling is **about 550–600 pages per second**, limited by CPU building pages. Past it, pages get slower; nothing breaks and no data is put at risk (purchases are serialised safely by the database).
- A real visitor views roughly one page every 10–20 seconds, so ~600 pages/s ≈ **6,000+ people browsing at the same moment** with pages still under a second, or about **3,000** with 95% of pages under 0.7 s. In practice even a big launch rarely has more than a few hundred people on the site at the same second.
- **Purchases** are the one thing that can't be spread across processes: SQLite allows one write at a time. Each checkout's write takes a few milliseconds, so this allows hundreds of checkouts per second — far beyond expected demand.

## Bottlenecks, in the order you'd hit them
1. **CPU on the VPS** (page rendering). Fix: more gunicorn processes (`-w` in the Dockerfile = number of cores + 1), a bigger VPS, nginx caching (static files and photos are already cached by nginx — install the updated `deploy/nginx-prizecomp.conf`).
2. **Write lock** at extreme checkout rates (busy timeout is 30 s, so requests wait rather than fail). Fix: move to PostgreSQL — the SQL is standard; `db.py` is the only place connections are made.
3. **Disk** (database growth ~1 KB per ticket). Health → Disk space warns early.

## Before a big launch
1. Run on **staging** with production-like hardware: `python tests/load_test.py https://staging.YOURDOMAIN --users 300 --seconds 120 --slug <comp> --buyers 50 --answer <a|b|c>` → must print PASS (no errors, p95 under 2 s, contested number sold once). Set `SIGNUP_RATE_LIMIT=100000` on staging only.
2. Check Admin → Targets → "Requests today / 95% faster than" during the launch.
3. If pages pass 1 s at the 95th percentile, Health → *Site speed* turns red and emails you; add workers or pause promotion.
