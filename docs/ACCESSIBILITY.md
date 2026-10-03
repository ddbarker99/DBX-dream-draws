# Accessibility

Target: **WCAG 2.2 AA**. Automated checks catch roughly a third of problems; the rest needs people.

## Automated (every push, in CI)
- `python tests/qa_crawl.py` — every page as guest, player and admin: one `<h1>`, labelled form fields, image alt text, titles and descriptions.
- `python tests/a11y_keyboard.py` (with `AXE_JS` set, as CI does) — in real Chromium at **390px** and **1280px**:
  - axe-core WCAG 2.0/2.1/2.2 A and AA rules on 14 key pages (public and logged-in),
  - the first Tab reaches *Skip to content* and it works,
  - every Tab stop is visible on screen and has a visible focus ring, with "reduce motion" on,
  - no keyboard traps,
  - the mobile menu opens with Enter, closes with Escape and returns focus,
  - log in → answer the question → choose entries → add to basket, keyboard only,
  - no Content-Security-Policy violations.

Fixed by this walkthrough: date-of-birth fields showed no focus ring when tabbed into (Chrome focuses the day/month/year segments without `:focus-visible`).

## Manual keyboard walkthrough (each release that changes a journey)
Unplug the mouse. On desktop Chrome and Safari:
1. Home → a competition → answer → quantity → *Add to basket* → basket → checkout → Stripe test page → confirmation.
2. Account: every tab; change a limit; take-a-break form (don't submit); withdrawal review step.
3. Free entry, contact/support form (errors announced next to fields?), notifications (mark read).
4. Instant-win game: reveal with Enter/Space; results readable with animation off.
Check: focus always visible, order matches the visual order, nothing needs a hover, error messages are reachable and say how to fix the problem.

## Assistive technology testing (needs a person — at least before launch and yearly)
| Tool | Device | Journeys |
|---|---|---|
| VoiceOver | iPhone (Safari) | Sign up, enter a competition, check My tickets, read a result |
| TalkBack | Android (Chrome) | Same |
| NVDA | Windows (Firefox or Chrome) | Same + withdrawal + support request |
| Zoom 200% / 400% reflow | Desktop | Competition page and checkout don't need horizontal scrolling |
| Windows high-contrast mode | Desktop | Buttons, focus rings and status pills still visible |

Ideally include at least one regular screen-reader user in the usability test (`USABILITY-TEST.md`). Record results here:

| Date | Who / tool | Journey | Problems | Fixed in |
|---|---|---|---|---|
| | | | | |
