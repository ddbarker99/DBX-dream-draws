"""The Back button shown at the top of every page (except the homepage and the admin Control Centre).

In the browser it returns to the page you came from. When there's no sensible previous page — you arrived from an email
or a bookmark, or the previous step was a form you submitted — it goes to the page's logical parent instead, given here."""
from flask import request, url_for

NO_BACK = {"public.home", "control.centre"}

# endpoint -> (label of the parent, function(view_args) -> url)
PARENTS = {
    # customers
    "public.competition": ("Competitions", lambda a: url_for("public.competitions")),
    "public.draw_verify": ("Competition", lambda a: url_for("public.competition", slug=a["slug"])),
    "public.entry_list": ("Competition", lambda a: url_for("public.competition", slug=a["slug"])),
    "public.comp_conditions": ("Competition", lambda a: url_for("public.competition", slug=a["slug"])),
    "public.play": ("Instant Wins", lambda a: url_for("public.instant_wins")),
    "public.results": ("Winners", lambda a: url_for("public.winners")),
    "public.basket": ("Competitions", lambda a: url_for("public.competitions")),
    "public.checkout_done": ("My tickets", lambda a: url_for("public.account", tab="entries")),
    "public.checkout_pay": ("Basket", lambda a: url_for("public.basket")),
    "public.demo_pay": ("Basket", lambda a: url_for("public.basket")),
    "public.deposit_done": ("Wallet", lambda a: url_for("public.account", tab="wallet")),
    "public.deposit_demo": ("Wallet", lambda a: url_for("public.account", tab="wallet")),
    "public.ticket_detail": ("My tickets", lambda a: url_for("public.account", tab="entries")),
    "public.order_detail": ("Transactions", lambda a: url_for("public.account", tab="transactions")),
    "public.withdrawal_detail": ("Wallet", lambda a: url_for("public.account", tab="wallet")),
    "public.prize_claim": ("Wins & prizes", lambda a: url_for("public.account", tab="wins")),
    "public.account_activity_page": ("My account", lambda a: url_for("public.account")),
    "public.notifications": ("My account", lambda a: url_for("public.account")),
    "public.case_view": ("My support requests", lambda a: url_for("public.my_cases")),
    "public.my_cases": ("Help & support", lambda a: url_for("public.support_centre")),
    "public.support_centre": ("Help Centre", lambda a: url_for("public.page", page="faq")),
    "public.legal_versions": ("Page", lambda a: url_for("public.page", page=a["slug"]) if a["slug"] in
                              ("terms", "privacy", "cookies", "responsible-play", "free-entry", "complaints") else url_for("public.home")),
    "public.legal_version": ("All versions", lambda a: url_for("public.legal_versions", slug=a["slug"])),
    "public.reset": ("Log in", lambda a: url_for("public.login")),
    "public.forgot": ("Log in", lambda a: url_for("public.login")),
    # staff
    "admin.entries": ("Competitions", lambda a: url_for("admin.dashboard")),
    "admin.edit_competition": ("Competition", lambda a: url_for("admin.entries", cid=a["cid"])),
    "admin.new_competition": ("Competitions", lambda a: url_for("admin.dashboard")),
    "admin.claim_detail": ("Winners", lambda a: url_for("admin.prizes")),
    "admin.user_detail": ("Customers", lambda a: url_for("admin.users")),
    "control.timeline": ("Customer", lambda a: url_for("admin.user_detail", uid=a["uid"])),
    "admin.order_detail": ("Payouts", lambda a: url_for("admin.payouts")),
    "admin.content_edit": ("Content", lambda a: url_for("admin.content_list")),
    "admin.announcements": ("Settings", lambda a: url_for("admin.settings")),
    "admin.features": ("Settings", lambda a: url_for("admin.settings")),
    "admin.reset": ("Settings", lambda a: url_for("admin.settings")),
    "control.backlog_item": ("Development board", lambda a: url_for("control.backlog")),
    "control.case_detail": ("Cases", lambda a: url_for("control.cases")),
    "control.support_insights": ("Cases", lambda a: url_for("control.cases")),
    "control.feedback_admin": ("Cases", lambda a: url_for("control.cases")),
}


def back_link():
    """(href, label) for this page's Back button, or None where it shouldn't show."""
    ep = request.endpoint or ""
    if ep in NO_BACK or not ep or ep == "static":
        return None
    if ep in PARENTS:
        label, build = PARENTS[ep]
        try:
            return build(request.view_args or {}), label
        except Exception:
            pass
    if ep.startswith(("admin.", "control.", "security.")):
        return url_for("control.centre"), "Control Centre"
    if ep.startswith("public.account") or ep in ("public.account",):
        return url_for("public.home"), "Home"
    return url_for("public.home"), "Home"
