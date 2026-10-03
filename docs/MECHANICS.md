# Competition mechanics: legal review

> This is an engineering summary to help you take advice — **not legal advice**. Have a solicitor who knows the Gambling Act 2005 review each mechanic before it launches, and again whenever you add a new game type or change how entry works.

## The legal framework in one paragraph

Under the Gambling Act 2005, an arrangement where people **pay** to take part, for a **prize**, allocated wholly by **chance**, is a **lottery** (s.14), which needs a licence most promoters can't get. There are two common ways a paid prize promotion avoids being a lottery:

1. **Free draw** (Schedule 2, para 8): a genuinely free entry route exists, is *as prominent as* the paid route, and free entrants have *exactly the same chance* of winning every prize. The Gambling Commission's guidance stresses that the prize system must not distinguish between paid and free entries, and that the free route must be properly publicised and administered.
2. **Prize competition** (s.339): success depends to a *sufficient degree* on skill, knowledge or judgement that deters a significant proportion of people from entering, or prevents a significant proportion of entrants from winning. The Commission has said simple multiple-choice questions (like "What is the capital of Scotland?") **rarely meet that threshold**.

The Commission published research on this sector in January 2026 and continues to emphasise the distinction. Expect scrutiny of free-entry routes in particular.

## How DBX is built (and why)

**DBX relies on the free-draw route for every mechanic.** It does **not** rely on the entry question being a skill test. That's why:

- Every competition and instant-win game has a postal free entry route, linked from the main nav, every competition page (next to the paid route) and the footer.
- Postal entries go into the **same pool** as paid ones: the same frozen entry list, the same draw, the same instant-win numbers, and the same per-person limits. Free entrants who match an account get their tickets in their account and instant prizes paid automatically.
- Every envelope is recorded from arrival to decision with an audit trail. A competition **cannot close or draw while envelopes are waiting**, so a valid free entry can't be forgotten.
- The public entry list doesn't say which entries were paid or free.

Because the question isn't what keeps DBX legal, the entry question is now **configurable** per competition (and as a site default in Admin → Settings → Competition mechanics):

| Setting | What happens | When to use it |
|---|---|---|
| **Multiple-choice question** (default) | Entrants must answer correctly; postal entries must include the right answer | If your solicitor wants a question kept (e.g. as an eligibility filter or extra protection) |
| **No question** | No question online or by post; it's a straightforward free draw | If your solicitor advises the question adds nothing and only creates friction |

Don't switch to calling anything a "skill competition" without advice. A genuine prize competition would need a properly difficult skill element and would be a different product.

## Mechanic-by-mechanic review

| Mechanic | Prize allocated by | Paid route | Free route | Points for your solicitor |
|---|---|---|---|---|
| **Main prize draw** | One draw at the advertised closing time from the frozen entry list (HMAC-SHA256 of a pre-committed seed) | Tickets, lucky dip or chosen numbers | Postal entry, same pool, same limits | Free route prominence; postal deadline (must *arrive* before close); per-person limit counting both routes; cash alternatives; publicity consent |
| **Instant prizes on a draw** | Winning ticket numbers fixed and sealed (hash published) before sales | Same tickets | Postal entries get a random number from the same pool and win on the same basis | Confirm instant prizes are within the free-draw exemption on the same terms as the main prize |
| **Instant-win games** (scratch card, wheel, mystery box) | Fixed number of plays; winning plays chosen at random and sealed before launch; the animation only reveals a result already stored | Paid plays | Postal entry gives a play from the same pool | The highest-risk mechanic. Ask specifically whether a game with a sealed finite prize table plus a postal route is a free draw rather than a lottery or gaming, and how prominent the free route must be **on the game page**. The result is fixed server-side before any reveal and can't change on refresh or another device |
| **Daily free game** | Same as games; one free play per verified member per UK day | None (free) | — | No payment, so not a lottery. Check it isn't treated as an inducement for paid play in a way that matters |
| **Referral credit / DBX Points / promo codes** | Not prizes; site credit (not withdrawable) | — | — | Make sure credit can't be converted into cash, and that promotional credit spent on entries doesn't change the free-route analysis |

## Before launching any new mechanic

Answer these in writing, get sign-off, and keep it with the release notes:

1. What do people pay, and what can they win?
2. How is the winner chosen, and when is the result fixed? (It must be fixed server-side before anything is shown.)
3. What is the free route, and does it give exactly the same chance of every prize?
4. Is the free route as prominent as paying, on every page where people can pay?
5. Do paid and free entries share the same limits, pool and allocation? (They must.)
6. Does the software enforce what the terms say? Add a test for each rule.
7. Has a solicitor reviewed it?
