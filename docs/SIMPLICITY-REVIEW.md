# Simplicity review (after Phase 6)

The rule: every feature must make the main journey easier — **find a competition → understand it → enter → see the entry is confirmed → see the result → receive the prize** — or make running the business safer. If it doesn't, it should be switched off, merged or removed. From now on, changes come from evidence (Support insights, the funnel, Targets, feedback), not from feature lists.

## The main journey, checked step by step

| Step | What the customer sees | Kept simple by |
|---|---|---|
| Find | Homepage (returning customers see their own draws first), Competitions with categories that only appear when something is enterable, typo-tolerant search, draw calendar | One main menu everywhere; no pop-ups or banners competing with it |
| Understand | Competition page: prize, price, tickets left, draw time, per-person limit, free entry, competition conditions | Conditions are versioned; legal pages show their version |
| Enter | Answer, choose numbers, basket, one payment button | Price and limits decided on the server; no upsells in checkout |
| Confirm | Confirmation page and email with ticket numbers; My tickets | One-tap feedback, optional |
| Result | Notification, results page marks "You entered / You won", My draws timeline | Results never disappear (archive) |
| Prize | Prize page: what happens next, prize or cash, delivery details, progress | Winner told at every step; no need to email back and forth |

## Every feature, and why it stays

**Customer-facing**
| Feature | Justification | Verdict |
|---|---|---|
| Personal homepage panel | Returning customers find their draws without scrolling past promotions | Keep |
| My tickets + timeline (Today/Tomorrow/This week/Later) | Answers "when is my draw?" — a top support question | Keep |
| Results "You entered" + filter | Answers "did I win?" without searching | Keep |
| Winner page | Replaces back-and-forth emails; reduces prize support cases | Keep |
| Draw calendar | One place for "when are the draws?" | Keep — watch usage; merge into Competitions if unused after 3 months |
| Saved competitions + opt-in reminders | Customer asked for it; opt-in only | Keep (feature flag `watchlist`) |
| Notification centre | One place for confirmations, results, account messages | Keep |
| Points dashboard + milestones | Points are understandable without maths; milestones never reward spending | Keep — milestones are display-only and can be removed with no data change |
| Referrals | Growth with anti-abuse rules | Keep (flag `referrals`) |
| Search | Typo-tolerant; covers competitions, results, winners | Keep (flag `search`) |
| Help centre, feedback | Structured support; one-tap feedback after purchase/case | Keep |
| Security activity, CSV downloads | Self-service instead of support requests | Keep |
| Share buttons, winner cards | Correct previews; consent-only winner cards | Keep |

**Staff-facing**
| Feature | Justification | Verdict |
|---|---|---|
| Control Centre work queues | The main daily screen: everything waiting, with links | Keep — the hub |
| Universal search | Replaces hunting through several pages | Keep |
| Order page + single-order refunds | Correct refunds without database edits | Keep |
| Goodwill credit vs wallet correction | Two different purposes with different limits | Keep both, clearly labelled |
| Promotions rules engine | One place for all promo rules; scheduling | Keep |
| Announcements | Replaced the old single "announcement" setting (merged — see below) | Keep |
| Content editor | Help/legal changes without a deploy; legal versions kept | Keep |
| Reports, Targets, Liability, Performance, Diagnostics, Segments, Experiments | Each answers a different management question | Keep, but grouped under one **Reports** menu |
| Support insights, Feedback, Backlog | The evidence loop for deciding what to improve | Keep, grouped under **More** |
| Health, Releases, Audit log | Operations and accountability | Keep, grouped under **More** |

## Merged or removed in this review
- **Admin menu**: 22 top-level links → 8 everyday links + two menus (Reports, More). Nothing lost; less to scan.
- **Announcements**: the old single "announcement bar" setting is gone; any existing text was moved automatically into scheduled announcements. One way to do it.
- **Homepage**: the duplicate "instant win plays to reveal" line was removed from the personal panel (it's already in the attention list).
- Earlier phases: dead function and unused 250 KB image removed; old URLs redirect to their single replacement (`/games`, `/live`, `/?tab=`).

## Manual work removed (item 39)
| Used to be manual | Now |
|---|---|
| Checking every morning what needs doing | Daily summary email at 7am + Control Centre queues |
| Comparing Stripe with the site | Daily automatic reconciliation; chargebacks flagged instantly |
| Switching promotions/announcements on and off | Scheduled start and end |
| Telling winners about prize progress | Automatic notification on each status change |
| Closing competitions and running draws | Automatic, with readiness checks |
| Paying instant-win prizes | Automatic on payment/reveal |
| Spotting errors, slowdowns, failed payments | Health checks and alerts |
| Weekly figures for the business | Weekly report email |

**Still manual (on purpose):** paying withdrawals (money leaving the business should be a person's decision), verifying winners, physical prize delivery, postal envelope processing, and anything a customer complaint needs judgement on. Candidates to automate later, if volume justifies it: bank payouts via a payments API, postal entry data capture.

## How to keep it simple from here
1. Before building anything, write the problem and the evidence (Support insights count, funnel drop, feedback, error) in the **Backlog**.
2. Prefer changing an existing page to adding a new one.
3. New customer features start behind a **feature flag** at "Staff only".
4. Test big UX changes with an **experiment** judged by completed purchases.
5. Every quarter, look at each feature's use (funnel, flags, feedback). Switch off what nobody uses, and remove it a release later.

## Second review (after Phase 7)
Phase 7 added trust features. The test for each was: does it answer a question customers would otherwise have to ask
us ("what stage is it at?", "was it fair?", "where's my receipt?", "is the site broken?")? If yes, it stays — and it
replaces a support conversation rather than adding a screen.

**Kept out of the customer's way**
- No new items in the main menu. The draw record, status page and activity history are reached from where the question
  arises (a finished competition, the footer, the account) rather than from navigation.
- The FAQ page *became* the Help Centre (same address) instead of adding a second help page.
- "Download all my data" replaced "contact us for a copy".
- Receipts are the existing order pages, not a new section.

**Staff side**
- New tools live in the existing **More** and **Reports** menus; the Control Centre shows them only when they need
  action (an approval waiting, the emergency lock on, an after-draw check failed).
- Four-eye approval is *automatic*: a one-person team isn't blocked; it switches on when a second administrator exists.

**Candidates to merge later (evidence first)**
- Footer "Help" has eight links. If analytics show low use, merge *Transparency centre*, *Fair draws* and *How it works*
  into one "How it works & fairness" page.
- *Draw calendar* overlaps the Competitions list sorted by closing time; merge if it's rarely used after three months.

**From now on**: changes go through the Development board's four questions (`docs/DEVELOPMENT-BOARD.md`), and the next
milestone is 100 real customers using everything without help (`docs/FIRST-100-USERS.md`).
