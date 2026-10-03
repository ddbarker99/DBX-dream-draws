"""Pre-release compliance sign-off for each competition mechanic.

A format that works legally (a prize draw with a free postal route) doesn't mean every new format does. Before any
competition using a mechanic can go live, someone responsible for the business/legal side records how that mechanic
fits the intended legal structure. A mechanic added to the code later has no sign-off, so it can't be published until
one is recorded. See docs/NEW-MECHANIC-CHECKLIST.md."""
import json

from .db import iso, utcnow

MECHANICS = {
    "prize_draw": "Prize draw with an entry question and free postal route",
    "prize_draw_no_question": "Prize draw with no question (relies on the free postal route alone)",
    "instant_prizes": "Instant prizes on numbered tickets in a prize draw",
    "free_daily": "Daily free-to-play game (no payment)",
    "game_scratch": "Paid instant-win game: scratch card",
    "game_spin": "Paid instant-win game: spin the wheel",
    "game_box": "Paid instant-win game: mystery box",
}

CHECKLIST = [
    "How the mechanic fits our intended legal structure (prize competition / free draw) has been confirmed, with advice where needed",
    "There is a free entry route with an equal chance of winning, and it is explained on the competition page",
    "Paid and free entries are treated identically once accepted",
    "Any skill/knowledge element (if relied on) is genuine and not trivially easy",
    "Results are decided by a published, checkable method and can't be changed after sales open",
    "Terms and competition conditions describe the mechanic accurately",
    "Responsible-play protections (limits, breaks, age checks) apply to it",
    "The marketing for it won't mislead (odds, prize value, urgency)",
]


def mechanics_of(db, comp):
    if comp["free_daily"]:
        return ["free_daily"]
    if comp["game_type"]:
        return [f"game_{comp['game_type']}"]
    out = ["prize_draw_no_question" if comp["question_mode"] == "none" else "prize_draw"]
    if comp["id"] and db.execute("SELECT 1 FROM instant_prizes WHERE competition_id=? LIMIT 1", (comp["id"],)).fetchone():
        out.append("instant_prizes")
    return out


def signoff(db, mechanic):
    return db.execute("SELECT * FROM mechanic_signoffs WHERE mechanic=? ORDER BY id DESC LIMIT 1", (mechanic,)).fetchone()


def unsigned(db, comp):
    """Mechanics this competition uses that nobody has signed off."""
    return [m for m in mechanics_of(db, comp) if not signoff(db, m)]


def record(db, mechanic, staff, responsible, note, ticked):
    db.execute("INSERT INTO mechanic_signoffs (mechanic, signed_by, signed_at, responsible, note, checklist) VALUES (?,?,?,?,?,?)",
               (mechanic, staff["id"] if staff else None, iso(utcnow()), responsible[:120], (note or "")[:2000], json.dumps(ticked)))


def name(mechanic):
    return MECHANICS.get(mechanic, mechanic.replace("_", " ").capitalize() + " (new — not described yet)")
