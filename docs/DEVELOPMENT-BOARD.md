# How we decide what to build

The platform is mature. Asking "what else can we add?" now mostly creates bloat. From Phase 7 on, work comes from
**evidence**, through one board with four columns (Admin → More → Development board):

| Column | What goes here |
|---|---|
| **Critical bugs** | Money, entries, draws or security wrong; anything that stops customers entering or getting paid |
| **Customer friction** | Customers struggle, get confused or contact support — proven by cases, feedback, funnel drops |
| **Business / operations** | Staff time, risk or cost — manual work, missing controls, reporting |
| **Future ideas** | Everything else. Parked until evidence moves it to another column |

## The four questions
Nothing is built just because it sounds good. **An item can't be moved to "Planned" until it answers:**
1. **What problem does this solve?**
2. **Who has this problem?** (how many, which customers or staff)
3. **How will we know the change improved it?** (the number we'll watch: support cases, funnel step, error count, time)
4. **What new risks does it introduce?** (money, legal, security, complexity, support load)

When it's done, record **"Did it work?"** against the measure. If it didn't, that's evidence too.

## Where evidence comes from
- Support cases → "Add to the development backlog" on any case (the case is attached as evidence)
- Customer feedback (after purchase and after support) → Admin → More → Customer feedback
- Server errors → Admin → Reports → Targets & errors → "To backlog"
- Funnel and checkout diagnostics, Support insights, Risk dashboard

## Order of work
Critical bugs first, always. Then the friction item with the most evidence. Operations items when they remove real staff
time or risk. Future ideas only when evidence promotes them.

## New competition mechanics
Also need a compliance sign-off before launch — see `docs/NEW-MECHANIC-CHECKLIST.md`.
