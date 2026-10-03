# Milestone: 100 real customers, no explanations needed

**Goal:** 100 real customers can use the whole platform — find a competition, understand it, enter, confirm the entry,
see the result and (if they win) receive the prize — without anyone explaining anything to them. What they struggle
with becomes the next development work. No new features are planned until this milestone is done.

## Before inviting anyone
- [ ] Release check READY, DR drill PASSED (already true on the live server)
- [ ] UptimeRobot watching `/healthz/deep`
- [ ] Solicitor has reviewed terms, privacy, free entry and responsible play, and the **existing mechanics** on the
      Compliance page have proper sign-offs
- [ ] Retention periods confirmed (docs/RETENTION.md)
- [ ] Email delivery working (Admin → More → Customer emails log shows "Sent")
- [ ] A second admin account exists, so four-eye approvals are active for large adjustments and redraws
- [ ] At least one real competition with a realistic prize and closing date

## Running it
1. Invite in waves of ~20 (friends of friends, a small social post) so problems are fixed before the next wave.
2. Don't explain the site. Give them the link only.
3. Watch, every day:
   - **Support insights** and **Cases** — what are people asking? Tag every case with a reason.
   - **Customer feedback** — one-tap ratings after purchase and after support.
   - **Checkout diagnostics** and the funnel — where do people drop out?
   - **Risk dashboard** and **System health** — anything unusual.
   - **Customer emails log** — failures.
4. Run 3–5 recorded usability sessions using `docs/USABILITY-TEST.md` (watch, don't help).
5. Every repeated problem goes on the **Development board** with its evidence and the four answers.

## Done when
- 100 customers have each completed at least one entry
- At least one competition has been drawn and its prize fulfilled end to end
- The top 5 friction items from real evidence are on the board with answers, and the critical ones are fixed
- Support contacts per 100 orders is known and trending down

## Then
The next phase is whatever those 100 customers struggled with — not a new feature list.
