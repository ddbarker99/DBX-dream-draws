"""Ready-made prize tables for instant win games (and instant prizes on prize draws), sized to the game.

Every table is built from three things: the price per play, the number of plays, and a style. The style sets how much
of the takings goes back as prizes if the game sells out (payout), how often a play wins (odds), and how big the top
prize is. Prize values are always "nice" amounts (£2, £5, £10, £25 …), every win is at least double the stake, and the
numbers below are the targets each table is checked against in the tests.

Payout and odds are business decisions — these are sensible starting points, shown to staff before anything is saved.
"""
import random

NICE = [10, 20, 25, 50, 100, 150, 200, 250, 300, 500, 750, 1000, 1500, 2000, 2500, 5000, 7500, 10000, 15000, 20000, 25000,
        50000, 75000, 100000, 150000, 200000, 250000, 500000]          # pence

# key: (label, payout share of takings, one win every N plays, top prize share of the pool, share of small wins as site credit,
#       short description for staff)
STYLES = {
    "balanced": ("Balanced", 0.50, 6, 0.20, 0.0, "Half the takings back as prizes, a win about every 6 plays, a headline top prize."),
    "winners": ("Lots of winners", 0.55, 4.5, 0.10, 0.0, "A win about every 4–5 plays — mostly small cash wins. Feels busy and fun."),
    "jackpot": ("Big jackpot", 0.45, 10, 0.40, 0.0, "A much bigger top prize to headline the game, fewer small wins."),
    "credit": ("Cash + site credit", 0.55, 4.5, 0.15, 0.6, "Small wins paid as site credit (spent on entries), bigger prizes in cash."),
    "draw": ("Instant prizes — standard", 0.12, 40, 0.25, 0.3, "Instant prizes on top of the main prize: about 12% of takings, a win every ~40 tickets."),
    "draw_big": ("Instant prizes — generous", 0.20, 25, 0.25, 0.3, "More instant wins on top of the main prize: about 20% of takings, a win every ~25 tickets."),
    "free": ("Free daily game", 0.25, 20, 0.15, 0.7, "Mostly small site-credit wins with a few cash prizes — a sign-up and retention budget, not takings."),
}
FREE_NOTIONAL_PRICE = 10      # pence: a free play is valued at 10p when sizing the free game's prize budget
STYLES_FOR = {"game": ["balanced", "winners", "jackpot", "credit"], "draw": ["draw", "draw_big"], "free": ["free"]}
DEFAULT_STYLE = "balanced"

# Middle tiers: (share of the pool left after the top prize, share of all wins, value range as multiples of the stake)
TIERS = [(0.20, 0.012, 40, 200), (0.24, 0.06, 10, 39), (0.22, 0.20, 3, 9)]


def _floor(p):
    return max([v for v in NICE if v <= p] or [NICE[0]])


def _ceil(p):
    return min([v for v in NICE if v >= p] or [NICE[-1]])


def label(pence, credit=False):
    amount = f"{pence}p" if pence < 100 else (f"£{pence // 100:,}" if pence % 100 == 0 else f"£{pence / 100:.2f}")
    return f"{amount} {'Site Credit' if credit else 'Cash'}"


def build(price, plays, style=DEFAULT_STYLE, jitter=False):
    """[(name, value_pence, quantity, kind)] — biggest first. kind is 'cash' or 'credit'.
    jitter=True varies payout/odds slightly (used for random games, so they don't all look the same).

    The small "double your stake" wins are funded first (so the advertised odds hold), then the top prize, then the
    middle tiers share what's left; any rounding left over becomes extra small winners rather than extra margin."""
    price, plays = max(1, int(price)), max(1, int(plays))
    _, payout, odds, top_share, credit_share, _ = STYLES.get(style, STYLES[DEFAULT_STYLE])
    if jitter:
        payout *= random.uniform(0.94, 1.06)
        odds *= random.uniform(0.9, 1.15)
    pool = price * plays * payout
    floor_win = _ceil(max(2 * price, 10))                           # every win at least doubles the stake
    wins = max(2, min(int(plays * 0.5), round(plays / odds), int(pool // floor_win)))
    counts = [max(1, round(wins * w)) for _, w, _, _ in TIERS]
    n_low = max(0, wins - 1 - sum(counts))
    rest = pool - n_low * floor_win                                 # money for the top prize and the middle tiers
    while rest < pool * 0.25 and n_low > 0:                          # keep enough for prizes worth talking about
        n_low = int(n_low * 0.9)
        rest = pool - n_low * floor_win
    top = max(_floor(min(pool * top_share, rest * 0.5)), _ceil(10 * price))
    table, spent, prev = [(top, 1)], top, top
    budget = rest - top
    weights = sum(s for s, _, _, _ in TIERS)
    for (share, _, lo, hi), n in zip(TIERS, counts):
        b = budget * share / weights
        v = min(max(_floor(b / n), _ceil(lo * price)), _floor(hi * price), _floor(prev * 0.6))
        if v <= floor_win or v >= prev or b < v:
            continue
        n = int(b // v)
        table.append((v, n))
        spent, prev = spent + v * n, v
    n_low = int((pool - spent) // floor_win)                        # rounding left over → a few more small winners,
    already = sum(n for _, n in table)                              # without drifting far from the advertised odds
    n_low = min(n_low, int(plays * 0.5) - already, max(0, round(wins * 1.15) - already))
    if n_low > 0 and floor_win < prev:
        table.append((floor_win, n_low))
    left = pool - sum(v * n for v, n in table)                       # still some budget? one or two more middle prizes
    for i, (v, n) in enumerate(table[1:], 1):
        extra = min(2, int(left // v))
        if extra:
            table[i] = (v, n + extra)
            left -= v * extra
    merged = {}
    for v, n in table:
        merged[v] = merged.get(v, 0) + n
    rows = sorted(merged.items(), reverse=True)
    out = []
    n_credit_tiers = round(len(rows) * credit_share)                  # the smallest tiers become site credit
    for i, (v, n) in enumerate(rows):
        credit = i >= len(rows) - n_credit_tiers and i > 0
        out.append((label(v, credit), v, n, "credit" if credit else "cash"))
    return out


def summary(price, plays, table):
    """(prizes, total value pence, payout share, one win every N plays) for showing staff before saving."""
    n = sum(r[2] for r in table)
    value = sum(r[1] * r[2] for r in table)
    takings = max(1, price * plays)
    return n, value, value / takings, (plays / n if n else 0)
