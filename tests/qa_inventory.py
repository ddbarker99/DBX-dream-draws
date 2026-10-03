"""Generate docs/qa/INVENTORY.md — every route, form, template, job, notification/email, setting and integration,
read from the code itself so nothing is left out because someone assumed it works.

    python tests/qa_inventory.py
"""
import inspect
import os
import re
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from app import create_app  # noqa: E402

app = create_app({"TESTING": True})
out = ["# DBX Dream Draws — platform inventory", "",
       "Generated from the code by `tests/qa_inventory.py`. Regenerate after any change.", ""]


def guard(view):
    """What protects a view: the permission in @require(...), login_required, or nothing."""
    src = ""
    try:
        src = inspect.getsource(inspect.unwrap(view))
    except (OSError, TypeError):
        pass
    perm = None
    cl = getattr(view, "__closure__", None) or ()
    for c in cl:
        try:
            v = c.cell_contents
        except ValueError:
            continue
        if isinstance(v, str) and (v in __import__("app.perms", fromlist=["PERMISSIONS"]).PERMISSIONS):
            perm = v
    mod = view.__module__
    try:
        whole = inspect.getsource(sys.modules[mod])
        m = re.search(r"((?:@[^\n]+\n)+)def " + re.escape(view.__name__) + r"\(", whole)
        decos = m.group(1) if m else ""
    except (OSError, TypeError):
        decos = ""
    if perm or "@require(" in decos:
        p = perm or re.search(r'@require\("([^"]+)"\)', decos).group(1)
        return f"staff: `{p}`"
    if 'not g.user["is_admin"]' in src:
        return "staff (checked inside the view)"
    if "@login_required" in decos:
        return "logged in"
    if "require_feature" in decos:
        return "public (feature flag)"
    _ = src
    return "public"


rules = sorted(app.url_map.iter_rules(), key=lambda r: (r.endpoint.split(".")[0], r.rule))
groups = {}
for r in rules:
    if r.endpoint == "static":
        continue
    bp = r.endpoint.split(".")[0]
    groups.setdefault(bp, []).append(r)
names = {"public": "Customer-facing (public + account)", "admin": "Admin", "control": "Admin — Control Centre & reports",
         "security": "Admin security (MFA, step-up)"}
total = 0
for bp, rs in groups.items():
    out += [f"## Routes — {names.get(bp, bp)} ({len(rs)})", "", "| URL | Methods | Endpoint | Protected by |", "|---|---|---|---|"]
    for r in rs:
        methods = ",".join(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS")))
        out.append(f"| `{r.rule}` | {methods} | `{r.endpoint}` | {guard(app.view_functions[r.endpoint])} |")
        total += 1
    out.append("")

# Forms in templates
tdir = os.path.join(ROOT, "app", "templates")
form_rows = []
for dirpath, _, files in os.walk(tdir):
    for fn in sorted(files):
        if not fn.endswith(".html"):
            continue
        path = os.path.join(dirpath, fn)
        rel = os.path.relpath(path, tdir)
        html = open(path, encoding="utf-8").read()
        for m in re.finditer(r"<form\b([^>]*)>(.*?)</form>", html, re.S):
            attrs, body = m.group(1), m.group(2)
            method = (re.search(r'method="(\w+)"', attrs) or [None, "get"])[1].upper()
            action = re.search(r'action="([^"]+)"', attrs)
            action = re.sub(r"\{\{\s*url_for\('([^']+)'.*?\}\}", r"\1", action.group(1)) if action else "(same page)"
            fields = sorted(set(re.findall(r'name="([a-z_0-9]+)"', body)) - {"csrf"})
            form_rows.append((rel, method, action, ", ".join(fields[:14]) + (" …" if len(fields) > 14 else "")))
out += [f"## Forms ({len(form_rows)})", "", "| Template | Method | Posts to | Fields |", "|---|---|---|---|"]
out += [f"| `{t}` | {m} | `{a}` | {f} |" for t, m, a, f in form_rows]
out.append("")

# Templates
tmpls = sorted(os.path.relpath(os.path.join(d, f), tdir) for d, _, fs in os.walk(tdir) for f in fs if f.endswith(".html"))
out += [f"## Templates ({len(tmpls)})", "", ", ".join(f"`{t}`" for t in tmpls), ""]

# Jobs
from app.jobs import JOBS  # noqa: E402
out += [f"## Background jobs ({len(JOBS)})", "", "| Job | Every | What it does |", "|---|---|---|"]
out += [f"| `{k}` | {v[0]} s | {v[1]} |" for k, v in JOBS.items()]
out.append("")

# Notifications / emails
src = ""
for fn in os.listdir(os.path.join(ROOT, "app")):
    if fn.endswith(".py"):
        src += open(os.path.join(ROOT, "app", fn), encoding="utf-8").read() + "\n"
kinds = sorted(set(re.findall(r'kind="([a-z_]+)"', src)))
subjects = sorted(set(s for s in re.findall(r'(?:tell|mailer\.send)\([^,]+,\s*f?"([^"]{4,90})"', src)))
out += [f"## Notification kinds ({len(kinds)})", "", ", ".join(f"`{k}`" for k in kinds), "",
        f"## Email subjects found in code ({len(subjects)})", ""] + [f"- {s}" for s in subjects] + [""]

# CLI, settings, flags, integrations
cli = sorted(app.cli.commands)
out += [f"## CLI commands ({len(cli)})", "", ", ".join(f"`flask {c}`" for c in cli), ""]
from app.flags import FEATURES as FLAGS  # noqa: E402
out += [f"## Feature flags ({len(FLAGS)})", ""] + [f"- `{k}` — {v[0]}" for k, v in FLAGS.items()] + [""]
envs = sorted(set(re.findall(r'_env\("([A-Z_]+)"', open(os.path.join(ROOT, "app", "__init__.py")).read())))
out += [f"## Configuration (.env) ({len(envs)})", "", ", ".join(f"`{e}`" for e in envs), ""]
out += ["## Third-party integrations", "",
        "| Service | Used for | Code | Failure behaviour |", "|---|---|---|---|",
        "| Stripe Checkout + webhooks | Card payments, deposits, refunds, reconciliation | app/payments.py, /stripe/webhook | 3 errors in 10 min auto-pause payments; webhook retried by Stripe; reconcile job |",
        "| SMTP (any provider) | All customer/staff email | app/mailer.py, app/notify.py | Outbox retries; failures in Customer emails log + health check; in-account copy always kept |",
        "| Discord webhook (optional) | Draw/live announcements | DISCORD_WEBHOOK_URL | Best-effort; never blocks a draw |",
        "| UptimeRobot / GitHub Actions (external) | Uptime checks of /healthz/deep | .github/workflows/uptime.yml | External |",
        "| None for analytics | Cookie-free counts stored locally | app/analytics.py | n/a |", ""]
out += ["## Summary", "", f"- Routes: {total}", f"- Forms: {len(form_rows)}", f"- Templates: {len(tmpls)}",
        f"- Background jobs: {len(JOBS)}", f"- Notification kinds: {len(kinds)}", f"- CLI commands: {len(cli)}",
        f"- Feature flags: {len(FLAGS)}", ""]
os.makedirs(os.path.join(ROOT, "docs", "qa"), exist_ok=True)
open(os.path.join(ROOT, "docs", "qa", "INVENTORY.md"), "w").write("\n".join(out))
print(f"routes={total} forms={len(form_rows)} templates={len(tmpls)} jobs={len(JOBS)} kinds={len(kinds)} cli={len(cli)}")
unprotected = [l for l in out if l.startswith("| `/admin") and l.rstrip().endswith("| public |")]
print("ADMIN ROUTES WITHOUT A PERMISSION DECORATOR:", len(unprotected))
for l in unprotected:
    print("  ", l)
