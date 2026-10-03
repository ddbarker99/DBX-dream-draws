# Reporting definitions — for agreement with the accountant

Admin → **Reports** shows these figures for any date range (UK dates; a day runs midnight to midnight UK time), with the previous period alongside and a CSV export. Each figure has exactly one definition, below, written from the code (`business_report()` in `app/control.py`). **Please review with your accountant and note any changes needed in the last column before using the numbers for VAT, gaming duty, corporation tax or management accounts.** All money is in pence in the CSV.

| Figure | Definition | Counted on | Accountant's notes |
|---|---|---|---|
| Gross paid entries | List price × quantity of every entry in orders paid in the period, before any discount | Payment date | |
| Multi-buy discounts | Bulk-buy discounts given on those orders | Payment date | |
| Net entry value | Gross paid entries − multi-buy discounts | Payment date | |
| Promo-code discounts | Discount from promo codes on paid orders | Payment date | |
| Paid with site credit (promotional) | Spend-only credit used at checkout (from referrals, points, instant credit prizes, goodwill, refunds as credit) | Payment date | |
| Paid with customers' own money | Card at checkout + deposited funds used + cash winnings used. Net entry value = promo-code discounts + site credit + this figure | Payment date | |
| Refunds of entries | Money returned to wallets when a competition was cancelled (cash, credit or deposited funds, each refunded the way it was paid) | Refund date | |
| Refunds to cards | Card payments refunded because a credit card was used, plus unspent deposits refunded to the card | Refund date | |
| Promotional credit issued | New spend-only credit created: referral rewards, points redeemed, staff goodwill adjustments (not refunds, not prizes) | Issue date | |
| Draw prizes | The prize value entered on each competition drawn in the period (the cost if the cash alternative is chosen may differ — use the prize-claims records) | Draw date | |
| Instant cash / credit prizes | Wallet credits for instant wins | Win date | |
| Instant physical prizes | Value entered for non-cash instant prizes won | Win date | |
| Total prize costs | Draw prizes + all instant prizes | | |
| Card payments received | Card money for entries + card deposits | Payment date | |
| Estimated card fees | Card payments received × the percentage + the fixed fee per payment, as set on the Reports page. **An estimate** — Stripe's own reports have the exact fees | Payment date | |
| Estimated contribution | Customers' own money − entry refunds − prize costs − estimated card fees. Not profit: excludes VAT/duty, staff, hosting, marketing, prize delivery | | |
| Withdrawals paid out | Cash winnings paid to customers' banks/PayPal | Paid date | |
| Paid orders / paid entries | Checkouts paid in the period / tickets in them | Payment date | |
| Free postal entries | Accepted free entries (tickets issued) in the period | Entry date | |
| Unique paying customers | Different accounts with at least one paid checkout in the period | | |
| — new | Of those, accounts whose first ever paid checkout is in the period | | |
| — returning | Of those, accounts that had paid before the period started | | |
| Average order / per customer | Net entry value ÷ paid orders / ÷ unique paying customers | | |

## Questions to settle with the accountant
1. Is revenue recognised at **payment** (as above) or at the **draw** (when the obligation is fulfilled)? Reports can be switched if needed.
2. Treatment of **deposits** (customer money held, refundable) and **cash winnings held** in wallets — liability balances are on Admin → Liability.
3. Treatment of **promotional credit** and promo codes (discount vs marketing expense).
4. Whether prize-competition income is subject to VAT or any gaming duty given the free entry route (legal advice may be needed too).
5. Which date range and format they want each month (the CSV export, plus Stripe's payout report).

Agreed with: ______________________  Date: __________
