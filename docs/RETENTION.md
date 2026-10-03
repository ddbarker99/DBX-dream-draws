# Data retention

Nothing is kept forever by default. Every kind of record has a rule: either it is deleted automatically after a set
period, or it is kept on purpose for a stated legal or integrity reason. The rules live in **`app/retention.py`**
(single source of truth). The nightly `prune` job applies the automatic ones, and staff can see them all at
**Admin → More → Compliance & retention**.

> The periods below are sensible defaults for a UK prize-competition business. **Confirm them with your solicitor/accountant**
> and change `app/retention.py` if their advice differs.

## Deleted automatically (nightly)

| Data | Kept for | Why |
|---|---|---|
| Signed-in device records | 90 days after last use | Security only — needed to sign out old devices |
| Read notifications | 1 year | Convenience: a year of history in the account (failed-email records are kept for investigation) |
| Background job logs | 90 days | Troubleshooting |
| Fixed server errors | 90 days after fixed | Troubleshooting (personal data is scrubbed before storing) |
| Error references shown to customers (DBX-XXXXXX) | 180 days | Long enough to answer any complaint about an error |
| Expired password-reset links | 1 day after expiry | Useless once expired |
| Request speed statistics | 400 days | Year-on-year comparison; counts only |
| Funnel counts | 400 days | Product decisions; counts only |
| Feedback free-text comments | 2 years (score kept) | Product decisions |

## Kept on purpose

| Data | Rule | Reason |
|---|---|---|
| Orders, payments, refunds, wallet and points ledgers | At least 6 years after the end of the tax year | HMRC accounting records; ledgers are append-only and can't be edited |
| Tickets, entry snapshots, draws, draw checks | Permanently | Published results must stay verifiable; they contain ticket numbers, not personal data |
| Customer accounts, postal entries | While open; 6 years after closure, then anonymised by staff | Disputes, complaints, accounting |
| Withdrawal bank details | 6 years with the withdrawal | Payment records; masked everywhere they're shown, including the customer's data export |
| Support requests | 6 years after resolution, then deleted by staff | Complaints and disputes |
| Audit log, approvals | Permanently | Tamper-evident record of staff actions |
| Marketing consent history | While the account exists | Proof of consent (UK GDPR / PECR) |
| Self-exclusion and limit history | At least 6 years after it ends | Protects the customer from re-marketing and re-registration |

## Customer rights
- **Access:** customers download a complete copy of their data themselves (My account → Profile & security →
  *Download a complete copy of your data*, JSON) plus CSVs of orders, tickets and wallet transactions.
- **Erasure:** closing an account is handled by staff (contact form) because cash balances must be paid out first and
  the records above have legal retention periods. Anything not covered by a "kept on purpose" rule is deleted.

## Changing a rule
1. Edit `RULES` in `app/retention.py` (period, reason, and the SQL for automatic ones).
2. Update this document.
3. Note the change and the advice behind it on the Development board.
