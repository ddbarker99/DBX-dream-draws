"""Retention rules: how long each kind of record is kept, and why. The nightly prune job applies the rows that have an
automatic action; the rest are kept because the law or the integrity of the draws requires it. Shown to staff on the
Privacy & retention page and documented in docs/RETENTION.md. Change periods here, not in the job.

Periods are business decisions to confirm with legal advice; they're written down so nothing is kept forever by default."""
from datetime import timedelta

from .db import iso, utcnow

# (data, period in days or None = keep, reason, SQL run by the prune job with ? = cutoff, or None)
RULES = [
    ("Signed-in device records", 90, "Security: only needed to sign out old devices",
     "DELETE FROM user_sessions WHERE last_seen<?"),
    ("Read notifications", 365, "Convenience: a year of history in the account",
     "DELETE FROM notifications WHERE read_at IS NOT NULL AND created_at<? AND COALESCE(email_status,'') != 'failed'"),
    ("Background job logs", 90, "Operations: troubleshooting only", "DELETE FROM job_runs WHERE started_at<?"),
    ("Fixed server errors", 90, "Operations: troubleshooting only (no personal data — scrubbed)",
     "DELETE FROM error_log WHERE resolved_at IS NOT NULL AND resolved_at<?"),
    ("Error references shown to customers", 180, "Support: long enough to answer any complaint about an error",
     "DELETE FROM error_refs WHERE at<?"),
    ("Expired password-reset links", 1, "Security: useless once expired", "DELETE FROM password_resets WHERE expires_at<?"),
    ("Request speed statistics", 400, "Operations: year-on-year comparison (counts only, no personal data)",
     "DELETE FROM request_stats WHERE day<substr(?,1,10)"),
    ("Funnel counts", 400, "Product decisions (counts only, no personal data)",
     "DELETE FROM funnel_counts WHERE day<substr(?,1,10)"),
    ("Feedback comments", 730, "Product decisions; the score is kept, the free-text comment is removed",
     "UPDATE feedback SET comment=NULL WHERE comment IS NOT NULL AND created_at<?"),
    ("Orders, payments, refunds, wallet and points ledgers", None,
     "Kept at least 6 years after the end of the tax year (HMRC accounting records); the ledgers are append-only by design", None),
    ("Tickets, entry snapshots, draws and draw checks", None,
     "Kept permanently: published results must stay verifiable, and they hold no personal data beyond ticket numbers", None),
    ("Customer accounts and postal entries", None,
     "Kept while the account is open; after closure, kept 6 years for disputes and accounting, then anonymised by staff", None),
    ("Withdrawal bank details", None, "Kept with the withdrawal for 6 years (payment records); shown masked everywhere", None),
    ("Support requests", None, "Kept 6 years after resolution (complaints and disputes), then deleted by staff", None),
    ("Audit log and approvals", None, "Kept permanently: tamper-evident record of staff actions (hash-chained)", None),
    ("Marketing consent history", None, "Kept while the account exists: proof of consent (PECR/UK GDPR)", None),
    ("Self-exclusion and spending-limit history", None,
     "Kept at least 6 years after it ends: protects the customer from being re-marketed to", None),
]


def apply(db):
    """Run every automatic rule. Returns [(data, rows affected)] for rules that changed something."""
    out = []
    for data, days, _, sql in RULES:
        if days and sql:
            n = db.execute(sql, (iso(utcnow() - timedelta(days=days)),)).rowcount
            if n:
                out.append((data, n))
    return out
