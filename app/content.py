"""Editable, versioned content: FAQs, help text, the homepage intro and legal documents.

Staff write in a small, safe text format (no HTML allowed) and publish a new version; earlier versions are kept
forever (database triggers stop edits and deletes) and legal pages show which version applies and link to the
history. Pages with no published version keep using their built-in template.
"""
import html
import re

from markupsafe import Markup

from .db import get_db, iso, utcnow

# slug: (title, is legal document — versions listed publicly and a change note is required)
EDITABLE = {
    "faq": ("FAQs", False),
    "home-intro": ("Homepage introduction", False),
    "support-intro": ("Help & support introduction", False),
    "terms": ("Terms & conditions", True),
    "privacy": ("Privacy policy", True),
    "cookies": ("Cookie policy", True),
    "responsible-play": ("Responsible play", True),
    "free-entry": ("Free postal entry", True),
    "complaints": ("Complaints procedure", True),
}

_LINK = re.compile(r"\[([^\]]+)\]\(((?:https://|/)[^\s)]*)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text):
    t = html.escape(text)
    t = _BOLD.sub(r"<strong>\1</strong>", t)
    return _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', t)


def render(source):
    """Safe formatting: '# ' / '## ' / '### ' headings, '- ' bullet lists, '1. ' numbered lists, blank-line paragraphs,
    **bold** and [links](https://…) or [links](/page). Everything else is shown as plain text."""
    out, para, items, kind = [], [], [], None

    def flush():
        nonlocal para, items, kind
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
        if items:
            tag = "ol" if kind == "ol" else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{tag}>")
        para, items, kind = [], [], None

    for raw in (source or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        m = re.match(r"^(#{1,3}) (.+)", line)
        if m:
            flush()
            level = len(m.group(1)) + 1                     # the page itself has the h1
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif re.match(r"^[-*] ", line):
            if kind != "ul":
                flush()
            kind = "ul"
            items.append(line[2:])
        elif re.match(r"^\d+[.)] ", line):
            if kind != "ol":
                flush()
            kind = "ol"
            items.append(line.split(" ", 1)[1])
        elif not line.strip():
            flush()
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return Markup("\n".join(out))


def current(slug, db=None):
    db = db or get_db()
    return db.execute("SELECT * FROM content_versions WHERE slug=? ORDER BY version DESC LIMIT 1", (slug,)).fetchone()


def publish(slug, body, note, staff, db=None):
    db = db or get_db()
    title, legal = EDITABLE[slug]
    prev = current(slug, db)
    version = (prev["version"] + 1) if prev else 1
    cur = db.execute("INSERT INTO content_versions (slug, version, body, note, created_by, created_at) VALUES (?,?,?,?,?,?)",
                     (slug, version, body, (note or "").strip()[:300] or None, staff["id"], iso(utcnow())))
    return cur.lastrowid, version
