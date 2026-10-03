"""Real-browser audit (Chromium via Playwright) of a running DBX Dream Draws site.

    python tests/qa_crawl.py --serve 5091 &            # seeded site
    python tests/browser_audit.py --base http://127.0.0.1:5091 --out /path/to/browser_audit.json

For guest / player / admin it:
  1. BFS-crawls every same-origin page reachable by links (cap per persona), skipping logout, start-fresh,
     downloads and destructive-looking query strings.
  2. Per page: console errors/warnings, page errors, failed requests (>=400 or requestfailed), broken images,
     http:// (mixed) resources, href="#"/javascript: links, links to localhost/staging/odd hosts, links/buttons
     without an accessible name, duplicate ids, missing <title>/<h1>, multiple <h1>.
  3. Responsive: horizontal overflow at 13 widths + iPhone 13 / Pixel 7 / iPad Mini emulation, naming the culprit.
  4. Storage inventory: cookies, localStorage/sessionStorage keys, third-party request hosts.
  5. Mobile purchase journey (iPhone 13) as the player, with tap-target checks for primary buttons.
"""
import argparse
import json
import os
import re
import sys
import time
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.sync_api import sync_playwright

PASSWORD = "supersecret123"
USERS = {"player": "player@dbx.test", "admin": "admin@dbx.test"}
SKIP_PATH = re.compile(r"/logout|/admin/start-fresh|/stripe/webhook|/sw\.js$|/manifest\.webmanifest$|/uploads/")
SKIP_EXT = re.compile(r"\.(csv|json|txt|png|jpe?g|gif|webp|svg|pdf|zip|xml|ico)(\?|$)", re.I)
DESTRUCTIVE = re.compile(r"(delete|remove|destroy|purge|wipe|reset|revoke|cancel|void|refund|close|"
                         r"unsubscribe|deactivate|suspend|ban|drop|clear|action=|confirm=|do=|format=|export|download)", re.I)
WIDTHS = [320, 360, 375, 390, 414, 480, 600, 768, 820, 1024, 1280, 1440, 1920]
DEVICES = ["iPhone 13", "Pixel 7", "iPad Mini"]
SUSPECT_HOST = re.compile(r"localhost|127\.0\.0\.1|0\.0\.0\.0|staging|stage\.|dev\.|test\.|example\.(com|org)|\.local\b", re.I)

PAGE_JS = r"""() => {
  const out = {};
  out.title = (document.title || '').trim();
  out.h1 = [...document.querySelectorAll('h1')].map(h => h.innerText.trim().slice(0, 80));
  const sel = el => {
    if (el.id) return el.tagName.toLowerCase() + '#' + el.id;
    let s = el.tagName.toLowerCase();
    if (el.className && typeof el.className === 'string') s += '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.');
    const p = el.parentElement;
    if (p && p !== document.body) { const pi = p.id ? '#' + p.id : (p.className && typeof p.className === 'string' ? '.' + p.className.trim().split(/\s+/)[0] : ''); s = p.tagName.toLowerCase() + pi + ' > ' + s; }
    return s;
  };
  out.brokenImages = [...document.images].filter(i => i.complete && i.naturalWidth === 0 && (i.currentSrc || i.src))
      .map(i => ({src: i.currentSrc || i.src, sel: sel(i)}));
  // only things the browser actually loads (not canonical/alternate <link>s, not <a>)
  out.httpResources = [...document.querySelectorAll('img[src],script[src],iframe[src],source[src],video[src],audio[src],' +
      'link[rel~=stylesheet][href],link[rel~=icon][href],link[rel~=preload][href],link[rel~=modulepreload][href],link[rel=manifest][href]')]
      .map(e => e.getAttribute('src') || e.getAttribute('href')).filter(u => u && /^http:\/\//i.test(u));
  out.titleRaw = document.querySelector('title') ? document.querySelector('title').textContent : null;
  out.headIntruders = [...document.body.querySelectorAll('meta,title,link:not([rel=stylesheet])')].map(e => e.outerHTML.slice(0, 120));
  out.badHrefs = [], out.links = [], out.allHrefs = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.getAttribute('href').trim();
    out.allHrefs.push({href: a.href, raw: h, text: (a.innerText || '').trim().slice(0, 40), sel: sel(a),
                       inFooter: !!a.closest('footer'), inNav: !!a.closest('nav,header')});
    if (h === '#' || /^javascript:/i.test(h)) out.badHrefs.push({raw: h, text: (a.innerText || '').trim().slice(0, 40), sel: sel(a)});
  }
  const accName = el => {
    const t = (el.getAttribute('aria-label') || '').trim() || (el.textContent || '').trim() || (el.getAttribute('title') || '').trim();
    if (t) return t;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const n = lb.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(x => x.textContent.trim()).join(' '); if (n) return n; }
    const img = el.querySelector('img[alt]:not([alt=""]),svg[aria-label],[role=img][aria-label]');
    if (img) return img.getAttribute('alt') || img.getAttribute('aria-label');
    if (el.tagName === 'INPUT') return el.value || '';
    return '';
  };
  out.unnamed = [...document.querySelectorAll('a[href],button,input[type=submit],input[type=button],[role=button]')]
      .filter(el => !accName(el)).map(el => ({sel: sel(el), html: el.outerHTML.slice(0, 160)}));
  const ids = {};
  document.querySelectorAll('[id]').forEach(e => { ids[e.id] = (ids[e.id] || 0) + 1; });
  out.dupIds = Object.entries(ids).filter(([k, v]) => v > 1).map(([k, v]) => k + ' x' + v);
  return out;
}"""

OVERFLOW_JS = r"""() => {
  const vw = window.innerWidth, sw = document.documentElement.scrollWidth;
  const res = {vw, sw, overflow: sw > vw + 1, culprits: []};
  if (!res.overflow) return res;
  const clipped = el => { for (let p = el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX; if (o !== 'visible') return true; } return false; };
  const sel = el => { let s = el.tagName.toLowerCase(); if (el.id) s += '#' + el.id;
      else if (el.className && typeof el.className === 'string' && el.className.trim()) s += '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.');
      return s; };
  const path = el => { const a = []; for (let e = el; e && e !== document.body && a.length < 4; e = e.parentElement) a.unshift(sel(e)); return a.join(' > '); };
  const offenders = [];
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed' && cs.visibility === 'hidden') continue;
    if (r.right > vw + 1 && !clipped(el)) offenders.push(el);
  }
  // keep the deepest offenders (whose children don't overflow) — that's what actually sticks out
  const set = new Set(offenders);
  const leaves = offenders.filter(el => ![...el.children].some(c => set.has(c)));
  res.culprits = leaves.slice(0, 6).map(el => { const r = el.getBoundingClientRect();
      return {path: path(el), right: Math.round(r.right), width: Math.round(r.width), text: (el.innerText || '').trim().slice(0, 50),
              html: el.outerHTML.slice(0, 140)}; });
  return res;
}"""


def norm(url, base):
    u, _ = urldefrag(url)
    p = urlparse(u)
    b = urlparse(base)
    if p.scheme not in ("http", "https") or p.netloc != b.netloc:
        return None
    path = p.path or "/"
    return path + (("?" + p.query) if p.query else "")


def skip(path):
    return bool(SKIP_PATH.search(path) or SKIP_EXT.search(path) or ("?" in path and DESTRUCTIVE.search(path.split("?", 1)[1])))


def login(page, who):
    page.goto("/login")
    page.fill("#email", USERS[who])
    page.fill("#pw", PASSWORD)
    with page.expect_navigation():
        page.click("form button:has-text('Log in')")
    return "/login" not in page.url


class Recorder:
    """Collects console/page/network events for the current page."""

    def __init__(self, page, base):
        self.base_host = urlparse(base).netloc
        self.reset()
        self.third_party = {}
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.pageerrors.append(str(e)[:300]))
        page.on("response", self._response)
        page.on("requestfailed", self._failed)
        page.on("request", self._request)

    def reset(self):
        self.console, self.pageerrors, self.failed = [], [], []

    def _console(self, m):
        if m.type in ("error", "warning"):
            loc = m.location or {}
            self.console.append({"type": m.type, "text": m.text[:400], "url": loc.get("url", "")})

    def _response(self, r):
        if r.status >= 400:
            self.failed.append({"url": r.url, "status": r.status, "type": r.request.resource_type})

    def _failed(self, r):
        err = r.failure or ""
        if "ERR_ABORTED" in err and r.resource_type == "document":
            return
        self.failed.append({"url": r.url, "status": "failed", "error": err, "type": r.resource_type})

    def _request(self, r):
        h = urlparse(r.url).netloc
        if h and h != self.base_host and urlparse(r.url).scheme in ("http", "https"):
            self.third_party.setdefault(h, set()).add(r.url[:120])


def audit_page(page, rec, url, who, shotdir, findings):
    rec.reset()
    try:
        resp = page.goto(url, wait_until="load", timeout=20000)
        page.wait_for_timeout(150)
    except Exception as e:  # noqa: BLE001
        findings.append({"persona": who, "url": url, "check": "navigation", "detail": str(e)[:200]})
        return None, None
    status = resp.status if resp else None
    final = page.url
    if not (resp and "text/html" in (resp.headers.get("content-type") or "")):
        return status, None
    info = page.evaluate(PAGE_JS)
    issues = []
    if status and status >= 400:
        issues.append(("http_status", f"{status}"))
    for c in rec.console:
        issues.append((f"console_{c['type']}", c["text"] + (f" @ {c['url']}" if c["url"] else "")))
    for e in rec.pageerrors:
        issues.append(("pageerror", e))
    for f in rec.failed:
        if f["type"] == "document" and f["status"] == status:
            continue  # already reported as http_status
        issues.append(("failed_request", f"{f['status']} {f['type']} {f['url']} {f.get('error', '')}".strip()))
    for b in info["brokenImages"]:
        issues.append(("broken_image", f"{b['src']} ({b['sel']})"))
    for h in info["httpResources"]:
        issues.append(("mixed_content", h))
    for b in info["badHrefs"]:
        issues.append(("bad_href", f"href={b['raw']!r} text={b['text']!r} ({b['sel']})"))
    base_host = urlparse(page.url).netloc
    for a in info["allHrefs"]:
        p = urlparse(a["href"])
        if p.scheme in ("http", "https") and p.netloc != base_host and SUSPECT_HOST.search(p.netloc):
            issues.append(("suspect_link_host", f"{a['href']} text={a['text']!r} ({a['sel']})"))
        if p.scheme == "http" and p.netloc != base_host:
            issues.append(("insecure_external_link", f"{a['href']} text={a['text']!r}"))
    for u in info["unnamed"]:
        issues.append(("no_accessible_name", f"{u['sel']} :: {u['html']}"))
    for d in info["dupIds"]:
        issues.append(("duplicate_id", d))
    if not info["title"]:
        issues.append(("missing_title", ""))
    elif re.search(r"<[a-z/]", info["title"], re.I):
        issues.append(("markup_in_title", info["title"][:160]))
    for h in info["headIntruders"]:
        issues.append(("head_element_in_body", h))
    if len(info["h1"]) == 0:
        issues.append(("missing_h1", ""))
    elif len(info["h1"]) > 1:
        issues.append(("multiple_h1", " | ".join(info["h1"])))
    shot = None
    serious = {"http_status", "console_error", "markup_in_title", "pageerror", "failed_request", "broken_image", "mixed_content"}
    if any(k in serious for k, _ in issues):
        shot = os.path.join(shotdir, f"defect_{who}_{re.sub(r'[^A-Za-z0-9]+', '_', url)[:80]}.png")
        try:
            page.screenshot(path=shot, full_page=True)
        except Exception:  # noqa: BLE001
            shot = None
    for k, d in issues:
        findings.append({"persona": who, "url": url, "final_url": final, "check": k, "detail": d, "screenshot": shot})
    return status, info


def crawl(ctx, base, who, start, cap, shotdir, findings):
    page = ctx.new_page()
    rec = Recorder(page, base)
    seen, q, visited, nav_footer = set(), deque(start), [], set()
    while q and len(visited) < cap:
        url = q.popleft()
        if url in seen or skip(url):
            continue
        seen.add(url)
        status, info = audit_page(page, rec, url, who, shotdir, findings)
        landed = norm(page.url, base)
        if landed and landed != url:
            seen.add(landed)
        visited.append({"url": url, "status": status, "landed": landed})
        if not info:
            continue
        for a in info["allHrefs"]:
            n = norm(a["href"], base)
            if a["inFooter"] or a["inNav"]:
                nav_footer.add(n or a["href"])
            if n and n not in seen and not skip(n):
                q.append(n)
    page.close()
    return visited, rec.third_party, nav_footer


def overflow_check(ctx_factory, base, urls, label_sizes, shotdir, findings, who):
    results = []
    for label, opts in label_sizes:
        ctx = ctx_factory(opts)
        page = ctx.new_page()
        for url in urls:
            try:
                page.goto(url, wait_until="load", timeout=20000)
                page.wait_for_timeout(100)
                r = page.evaluate(OVERFLOW_JS)
            except Exception as e:  # noqa: BLE001
                results.append({"url": url, "size": label, "error": str(e)[:150]})
                continue
            row = {"url": url, "size": label, "vw": r["vw"], "scrollWidth": r["sw"], "overflow": r["overflow"]}
            if r["overflow"]:
                row["culprits"] = r["culprits"]
                shot = os.path.join(shotdir, f"overflow_{who}_{label.replace(' ', '')}_{re.sub(r'[^A-Za-z0-9]+', '_', url)[:60]}.png")
                try:
                    page.screenshot(path=shot)
                except Exception:  # noqa: BLE001
                    shot = None
                row["screenshot"] = shot
                findings.append({"persona": who, "url": url, "check": "horizontal_overflow", "size": label,
                                 "detail": f"scrollWidth {r['sw']} > innerWidth {r['vw']}; culprits: "
                                           + "; ".join(f"{c['path']} (right={c['right']}, w={c['width']})" for c in r["culprits"][:3]),
                                 "screenshot": shot})
            results.append(row)
        ctx.close()
    return results


def storage(ctx, page):
    cookies = [{"name": c["name"], "domain": c["domain"], "httpOnly": c["httpOnly"], "secure": c["secure"],
                "sameSite": c.get("sameSite"), "expires": c.get("expires")} for c in ctx.cookies()]
    ls = page.evaluate("() => { try { return Object.keys(localStorage) } catch (e) { return ['<error>'] } }")
    ss = page.evaluate("() => { try { return Object.keys(sessionStorage) } catch (e) { return ['<error>'] } }")
    sw = page.evaluate("async () => { try { return (await navigator.serviceWorker.getRegistrations()).map(r => r.scope) } catch (e) { return [] } }")
    caches = page.evaluate("async () => { try { return await caches.keys() } catch (e) { return [] } }")
    return {"cookies": cookies, "localStorage": ls, "sessionStorage": ss, "serviceWorkers": sw, "cacheStorage": caches}


def journey(browser, p, base, shotdir, findings):
    dev = dict(p.devices["iPhone 13"])
    ctx = browser.new_context(base_url=base, **dev)
    page = ctx.new_page()
    rec = Recorder(page, base)
    steps, small = [], []

    def tap_check(locator, name):
        try:
            bb = locator.bounding_box()
        except Exception:  # noqa: BLE001
            bb = None
        if bb and (bb["width"] < 24 or bb["height"] < 24):
            small.append({"button": name, "w": round(bb["width"], 1), "h": round(bb["height"], 1), "url": page.url})
        return bb

    def step(name, fn):
        rec.reset()
        try:
            note = fn()
            ok = True
        except Exception as e:  # noqa: BLE001
            note, ok = str(e)[:300], False
        rec_errs = [c["text"] for c in rec.console if c["type"] == "error"] + rec.pageerrors
        rec_fail = [f"{f['status']} {f['url']}" for f in rec.failed]
        shot = os.path.join(shotdir, f"journey_{len(steps):02d}_{re.sub(r'[^a-z0-9]+', '_', name.lower())}.png")
        try:
            page.screenshot(path=shot)
        except Exception:  # noqa: BLE001
            shot = None
        steps.append({"step": name, "ok": ok, "url": page.url, "note": note, "console_errors": rec_errs,
                      "failed_requests": rec_fail, "screenshot": shot})
        if not ok or rec_errs or rec_fail:
            findings.append({"persona": "player", "url": page.url, "check": "journey_step", "size": "iPhone 13",
                             "detail": f"{name}: ok={ok} {note} errors={rec_errs} failed={rec_fail}", "screenshot": shot})
        return ok

    def do_login():
        page.goto("/login")
        page.fill("#email", USERS["player"])
        page.fill("#pw", PASSWORD)
        b = page.locator("form button:has-text('Log in')")
        tap_check(b, "Log in")
        with page.expect_navigation():
            b.tap()
        assert "/login" not in page.url, "still on login page"

    state = {}

    def open_comp():
        page.goto("/competitions")
        link = page.locator("a[href^='/c/']").first
        href = link.get_attribute("href")
        page.goto("/c/ps5-pro-bundle")
        assert page.locator("form#enter").count() == 1, f"no entry form (first listing link was {href})"
        state["badge_before"] = page.evaluate("(() => { const b = document.querySelector('[aria-label^=\"Basket\"]'); return b && b.getAttribute('aria-label') })()")
        return f"first listing link {href}; basket badge {state['badge_before']!r}"

    def answer():
        lab = page.locator("form#enter label:has(input[name=answer][value=b])")
        tap_check(lab, "answer option label")
        lab.tap()
        assert page.locator("input[name=answer][value=b]").is_checked(), "answer radio not checked after tap"

    def add_tickets():
        plus = page.locator("button[aria-label='One more']")
        tap_check(plus, "+ (one more)")
        minus = page.locator("button[aria-label='One fewer']")
        tap_check(minus, "- (one fewer)")
        plus.tap()
        plus.tap()
        q5 = page.locator(".quick button").first
        if q5.count():
            tap_check(q5, "quick qty " + q5.inner_text())
        v = page.input_value("#qty")
        assert v == "3", f"qty after two + taps is {v!r}, expected '3'"
        btn = page.locator("button[name=go][value=basket]")
        tap_check(btn, "Add to basket and keep browsing")
        tap_check(page.locator("button[name=go][value=checkout]"), "Enter now")
        with page.expect_navigation():
            btn.tap()
        after = page.evaluate("(() => { const b = document.querySelector('[aria-label^=\"Basket\"]'); return b && b.getAttribute('aria-label') })()")
        assert after != state["badge_before"], f"basket badge unchanged ({after!r})"
        return f"landed {page.url}; badge {after!r}"

    def basket():
        page.goto("/basket")
        assert page.locator("text=PS5 Pro Bundle").count() > 0, "PS5 line not in basket"
        pay = page.locator("form[action$='/basket/checkout'] button, form[data-once] button.btn.lg").first
        tap_check(pay, "Pay / Confirm (basket)")
        state["pay_label"] = pay.inner_text()
        return state["pay_label"]

    def checkout():
        pay = page.locator("form[data-once] button.btn.lg").first
        with page.expect_navigation():
            pay.tap()
        note = [page.url]
        if "demo-pay" in page.url:
            b = page.locator("main form button, form button.btn.block").last
            tap_check(b, "Pay (test) on demo-pay")
            with page.expect_navigation():
                b.tap()
            note.append(page.url)
        page.wait_for_timeout(1500)
        for _ in range(10):                          # checkout_wait page may poll
            if "/done" in page.url or "confirm" in page.content().lower():
                break
            page.wait_for_timeout(1000)
        note.append(page.url)
        return " -> ".join(note)

    def confirmation():
        txt = page.inner_text("main") if page.locator("main").count() else page.inner_text("body")
        h1 = page.locator("h1").first.inner_text() if page.locator("h1").count() else ""
        assert re.search(r"confirm|thank|you're in|good luck|entered|tickets", txt, re.I), f"no confirmation wording; h1={h1!r}"
        link = page.locator("a:has-text('ticket')").first
        if link.count():
            tap_check(link, "My tickets link on confirmation")
        return f"h1={h1!r}"

    def my_tickets():
        link = page.locator("a:has-text('ticket')").first
        if link.count():
            with page.expect_navigation():
                link.tap()
        else:
            page.goto("/account?tab=entries")
        assert page.locator("text=PS5 Pro Bundle").count() > 0, "PS5 entry not listed in My tickets"
        return page.url

    for name, fn in (("log in", do_login), ("open live competition", open_comp), ("answer question", answer),
                     ("add tickets", add_tickets), ("basket", basket), ("checkout (demo payment)", checkout),
                     ("confirmation", confirmation), ("open My tickets", my_tickets)):
        if not step(name, fn):
            break
    # mobile nav: the menu button itself is a primary control
    try:
        page.goto("/")
        if page.locator("#menubtn").count():
            tap_check(page.locator("#menubtn"), "mobile menu button")
        tap_check(page.locator("[aria-label^='Basket']").first, "header basket icon")
    except Exception:  # noqa: BLE001
        pass
    for s in small:
        findings.append({"persona": "player", "url": s["url"], "check": "small_tap_target", "size": "iPhone 13",
                         "detail": f"{s['button']}: {s['w']}x{s['h']}px (< 24px)"})
    ctx.close()
    return {"steps": steps, "small_tap_targets": small}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5091")
    ap.add_argument("--out", default="browser_audit.json")
    ap.add_argument("--cap", type=int, default=250)
    a = ap.parse_args()
    base = a.base.rstrip("/")
    shotdir = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(shotdir, exist_ok=True)
    findings, report = [], {"base": base, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    t0 = time.time()
    with sync_playwright() as p:
        exe = "/opt/pw-browsers/chromium"
        browser = p.chromium.launch(executable_path=exe if os.path.exists(exe) else None)
        personas = {}
        storage_inv = {}
        admin_comp = None
        account_tabs = []
        for who in ("guest", "player", "admin"):
            ctx = browser.new_context(base_url=base, viewport={"width": 1280, "height": 900})
            lp = ctx.new_page()
            if who != "guest":
                ok = login(lp, who)
                if not ok:
                    findings.append({"persona": who, "url": "/login", "check": "login", "detail": "login failed"})
            start = ["/"] + (["/account"] if who != "guest" else []) + (["/admin/"] if who == "admin" else [])
            visited, third, navfoot = crawl(ctx, base, who, start, a.cap, shotdir, findings)
            lp.goto("/")
            storage_inv[who] = storage(ctx, lp)
            storage_inv[who]["thirdPartyHosts"] = {h: sorted(v)[:5] for h, v in third.items()}
            personas[who] = {"visited": visited, "count": len(visited), "nav_footer_links": sorted(navfoot)}
            urls = [v["url"] for v in visited]
            if who == "admin":
                admin_comp = next((u for u in urls if re.fullmatch(r"/admin/competitions/\d+", u)), None)
            if who == "player":
                account_tabs = sorted({u for u in urls if re.fullmatch(r"/account(\?tab=[a-z_-]+)?", u)})
            # keep the logged-in context for responsive checks
            personas[who]["_state"] = ctx.storage_state()
            ctx.close()

        # responsive
        live = "/c/ps5-pro-bundle"
        customer = ["/", "/competitions", live, "/basket", "/faq", "/results", "/c/apple-watch-ultra/draw"]
        player_pages = ["/basket"] + (account_tabs or ["/account"]) + ["/account/activity"]
        admin_pages = ["/admin/", "/admin/competitions/new?kind=draw"] + ([admin_comp] if admin_comp else [])
        sizes = [(f"{w}px", {"viewport": {"width": w, "height": 900}}) for w in WIDTHS]
        devs = [(d, dict(p.devices[d])) for d in DEVICES]

        def factory(state):
            return lambda opts: browser.new_context(base_url=base, storage_state=state, **opts)

        resp = []
        resp += overflow_check(factory(None), base, customer, sizes + devs, shotdir, findings, "guest")
        resp += overflow_check(factory(personas["player"]["_state"]), base, player_pages, sizes + devs, shotdir, findings, "player")
        resp += overflow_check(factory(personas["admin"]["_state"]), base, admin_pages, sizes, shotdir, findings, "admin")
        for w in personas.values():
            w.pop("_state", None)

        jr = journey(browser, p, base, shotdir, findings)
        browser.close()

    checks_per_page = 14
    report.update({
        "personas": personas,
        "responsive": resp,
        "storage": storage_inv,
        "journey": jr,
        "findings": findings,
        "summary": {
            "pages_visited": {k: v["count"] for k, v in personas.items()},
            "page_checks": sum(v["count"] for v in personas.values()) * checks_per_page,
            "responsive_checks": len(resp),
            "console_errors": sum(1 for f in findings if f["check"] == "console_error"),
            "console_warnings": sum(1 for f in findings if f["check"] == "console_warning"),
            "page_errors": sum(1 for f in findings if f["check"] == "pageerror"),
            "failed_requests": sum(1 for f in findings if f["check"] in ("failed_request", "http_status")),
            "overflow_cases": sum(1 for r in resp if r.get("overflow")),
            "findings_by_check": {},
            "seconds": round(time.time() - t0, 1),
        },
    })
    for f in findings:
        report["summary"]["findings_by_check"][f["check"]] = report["summary"]["findings_by_check"].get(f["check"], 0) + 1
    with open(a.out, "w") as fh:
        json.dump(report, fh, indent=1, default=list)
    print(json.dumps(report["summary"], indent=1))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
