# Usability test plan

**Stop adding features until this has been run.** Five people find most of the big problems; run a round of **5–10 people who have never used the site**, on **their own phones**, fix what you learn, then run another small round.

## Who
- 5–10 adults (18+) who enter online competitions or would consider it, and have **never seen DBX Dream Draws**.
- Mix of iPhone and Android; at least two aged 45+; at least one who rarely shops online; ideally one assistive-technology user (screen reader or zoom).
- Not friends who'll be polite, not staff. Offer a thank-you (e.g. £10 voucher — not site credit).

## Set-up
- Use **staging** with Stripe test mode, real-looking competitions and photos. Give each person a test card number on paper.
- Video call with screen share, or in person watching over their shoulder (with permission to record the screen, not their face).
- One facilitator (only reads tasks and asks "what are you thinking?"), one note-taker. **Never help or explain** — if they're stuck for 2 minutes, note it and move on.
- 30 minutes each.

## Script
Intro (2 min): "We're testing the website, not you. Please think out loud. There are no wrong answers."

| # | Task (read exactly) | Success looks like | Measure |
|---|---|---|---|
| 1 | "You've been sent this link. In your own words, what is this site and how does it work?" (home page, 1 minute) | Mentions prizes, buying entries, a draw at a set time, free entry | Correct? |
| 2 | "Find a competition for something you'd like to win and tell me when it will be drawn and your chance of winning if you bought 5 entries." | Finds draw date and max entries | Time, success |
| 3 | "Enter it with 3 entries and pay." (test card) | Sign-up → answer → basket → payment → confirmation | Time, errors, success |
| 4 | "Show me your ticket numbers and when you'll find out if you've won." | My tickets / ticket page | Success |
| 5 | "Is there a way to enter without paying? How?" | Finds free postal route | Success |
| 6 | "You'd like to spend no more than £20 a week here. Set that up." | Responsible play → limit saved | Success |
| 7 | "Say you'd won £15 cash. How would you get it into your bank?" | Wallet → withdrawal review | Success |
| 8 | "Something went wrong with an order. Get help." | Help & support with the order selected | Success |
| 9 | "Stop us sending you marketing emails." | Communication preferences | Success |

After (5 min): "What was confusing? What would stop you using this? Did anything feel untrustworthy? Rate 1–5: easy to use / trustworthy."

## Recording results
For each person × task: completed without help (Y/N), time, where they hesitated, what they said. Then:
1. List every problem, how many people hit it, and severity (blocks task / slows / cosmetic).
2. Fix the "blocks task" items seen by 2+ people first.
3. Record the task completion rate on Admin → Targets ("Usability test: tasks completed without help").
4. Re-test the fixed journeys with 3–5 new people.

| Round | Date | People | Tasks completed without help | Top 3 problems | Fixed in |
|---|---|---|---|---|---|
| 1 | | | | | |
