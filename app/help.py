"""Help Centre articles. Plain answers, grouped by topic, searchable. Edit the wording here (or publish an extra note
from Admin → Content → FAQs, which appears at the top of the Help Centre)."""

TOPICS = [
    ("entering", "How to enter", [
        ("How do I enter a competition?",
         "Open a competition, answer the question, choose a lucky dip or pick your own numbers, then pay. Your ticket numbers "
         "appear straight away on the confirmation page, by email and in My account → My tickets."),
        ("How long are my numbers held at checkout?",
         "30 minutes. If you don't finish paying in that time the numbers go back on sale and you aren't charged."),
        ("Is there a limit on how many tickets I can buy?",
         "Yes — each competition shows its per-person limit, and the page tells you how many you already hold."),
        ("Who can enter?", "UK residents aged 18 or over. One account per person."),
    ]),
    ("free-entry", "Free entry", [
        ("Can I enter for free?",
         "Yes. Every competition can be entered free by post, with exactly the same chance of winning as a paid entry. "
         "The Free entry page explains what to send and where."),
        ("Do free entries really have the same chance?",
         "Yes. Once a postal entry is accepted it gets a ticket number like any other, and the draw only looks at ticket "
         "numbers — it doesn't know who owns a ticket or how it was entered."),
        ("How do I know my postal entry was received?",
         "If your postal entry uses the email address on your account, its ticket appears in My tickets once we've processed "
         "it, and you get a notification. Envelopes are processed before the entry list is frozen for the draw."),
    ]),
    ("tickets", "Ticket numbers & receipts", [
        ("Where are my ticket numbers?",
         "My account → My tickets. Each ticket has its own page with the competition, how you entered, the time, the terms "
         "that applied and the result."),
        ("Where's my receipt?",
         "Every order has a permanent receipt in your account (My account → Transactions → the order). It shows the reference "
         "(DBX-000123), what you entered, how you paid and the exact time. You can print it or save it as a PDF."),
        ("Can I check my ticket is really in the draw?",
         "Yes — every competition has a public entry list (numbers and first names only). Search it for your number."),
    ]),
    ("draws", "Draws & results", [
        ("When is the draw?",
         "The draw date and time are on every competition page and in the draw calendar. The draw goes ahead at that time "
         "even if not every ticket sells."),
        ("How do I know if I've won?",
         "We tell you — by notification and email — as soon as the result is final. The results page and My results mark "
         "every draw you entered with \"You won\" or the winning ticket, so you never have to compare numbers yourself."),
        ("How do I know a draw was fair?",
         "Before any tickets sell we publish a fingerprint of a secret number for the draw. After the draw we reveal the secret "
         "and the frozen entry list, so anyone can recompute the winner. Every finished competition has a draw record page "
         "that walks through it step by step."),
        ("What do the competition stages mean?",
         "Live → Closing soon (less than 24 hours left) → Closed (entries being finalised) → Draw pending (the entry list is "
         "frozen) → Draw complete → Prize delivered. The current stage is shown at the top of every competition."),
        ("Do old results disappear?", "No. Every completed draw stays on the results page permanently."),
    ]),
    ("instant", "Instant wins", [
        ("How do instant prizes work?",
         "Some ticket numbers have instant prizes attached. If you get one you win straight away and still have your chance "
         "in the main draw."),
        ("How do Instant Win games work?",
         "Each game has a fixed number of plays and every winning play is chosen and sealed before the game goes live. "
         "Scratching, spinning or opening reveals a result that was already decided."),
        ("What if I don't reveal a play?", "Any prize you haven't revealed is paid to you automatically after 24 hours."),
    ]),
    ("payments", "Payments", [
        ("How can I pay?",
         "By UK debit card, or from your wallet (cash winnings, site credit or deposited funds). You see the full total "
         "before you pay."),
        ("My payment failed — was I charged?",
         "No. If a payment doesn't complete, no tickets are issued and nothing is taken. If your bank shows a pending amount it "
         "drops off by itself. If money was taken but tickets weren't issued, it's refunded automatically."),
        ("I pressed pay twice — will I be charged twice?", "No. A repeated press carries on with the same checkout."),
        ("Payments say they're paused — what does that mean?",
         "Occasionally we pause entries for a short while (for example if our payment provider has a problem). Nothing is "
         "charged while paused; check the Service status page for updates instead of retrying."),
    ]),
    ("wallet", "Wallet & balances", [
        ("What's the difference between cash, site credit and deposited funds?",
         "Cash is real winnings you can withdraw. Site credit comes from promotions, referrals and DBX Points and can be spent "
         "but not withdrawn. Deposited funds are money you've added by card; they're spent on entries and any unspent amount "
         "can be refunded to your card."),
        ("Which balance is used first?", "Site credit, then deposited funds, then cash winnings, then your card."),
        ("Can I add money?", "Yes — My account → Wallet → Add funds, from £5 to £250 at a time. Deposits count towards your spending limits."),
    ]),
    ("withdrawals", "Withdrawals", [
        ("How do I withdraw my winnings?",
         "My account → Wallet. Enter the amount (from £5) and your UK bank or PayPal details. You need to have confirmed your "
         "email address first."),
        ("How long does a withdrawal take?",
         "We aim to pay within 24 hours. Each withdrawal has its own page showing Requested → Processing → Paid, with a "
         "reference (DBX123) you can quote."),
        ("My withdrawal was returned — why?",
         "If we can't complete a withdrawal (for example the bank details don't match), the money goes straight back to your "
         "cash balance and the withdrawal page explains why."),
    ]),
    ("points", "DBX Points", [
        ("How do I earn DBX Points?",
         "You earn points on every £1 paid by card, and more as you move from Bronze to Silver, Gold and Diamond."),
        ("How do I use them?", "Redeem 100 points for £1 site credit from My account → DBX Points."),
    ]),
    ("prizes", "Prizes", [
        ("I've won — what happens now?",
         "Your prize page (in My account → Wins & prizes) shows each step: Winner confirmed → Verification → Prize arranged → "
         "Dispatched or paid → Completed. We tell you at every step. We will never ask you to pay to claim a prize."),
        ("Can I take cash instead?", "Where a competition lists a cash alternative, you choose prize or cash on your prize page."),
        ("Why do you need to verify me?", "To confirm you're 18+, live in the UK and entered within the rules. It usually takes 1–2 working days."),
    ]),
    ("account", "Account problems", [
        ("I've forgotten my password", "Use \"Forgot password\" on the log in page. The link lasts one hour. Resetting signs out every device."),
        ("I haven't received the confirmation email",
         "Check your spam folder, then use \"Resend link\" on the banner at the top of the site."),
        ("I don't recognise a sign-in",
         "Change your password in My account → Profile & security — that signs out every other device straight away — then "
         "tell us. You can also sign out individual devices there."),
        ("How do I change my name or email?", "Contact us — we check it's really you before changing either."),
        ("Can I get a copy of my data?",
         "Yes — My account → Profile & security → Download a complete copy of your data. You can also download your orders, "
         "tickets and wallet transactions as spreadsheets."),
        ("I saw an error with a reference like DBX-7KQ2MX", "Contact us and quote the reference — it lets us find exactly what went wrong."),
    ]),
    ("responsible", "Responsible participation", [
        ("Can I set spending limits?",
         "Yes — daily, weekly and monthly limits in My account → Responsible play. Lowering a limit is instant; raising one takes 72 hours."),
        ("Can I take a break?", "Yes — from 24 hours to a year, or self-exclude, from My account → Responsible play. It can't be undone early."),
        ("Where can I get help?",
         "Free, confidential support is available from GamCare (0808 8020 133) and GambleAware. If entering stops being fun, "
         "take a break."),
    ]),
]


def search(query):
    """Articles matching the query (typo-tolerant), as (topic title, question, answer)."""
    from .routes import fuzzy_match
    return [(title, q, a) for _, title, items in TOPICS for q, a in items if fuzzy_match(query, q, a, title)]
