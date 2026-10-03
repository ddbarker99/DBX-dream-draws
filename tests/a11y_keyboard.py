"""Keyboard-only walkthrough in a real browser (Chromium via Playwright), plus axe-core where available.

    python tests/a11y_keyboard.py            # AXE_JS=/path/to/axe.min.js to include the axe-core scan

What a keyboard user needs, checked on the main pages at phone (390px) and desktop (1280px) widths:
  - the first Tab lands on "Skip to content", and it moves focus to the main content
  - every element Tab reaches is visible on screen and shows a focus indicator
  - Tab never gets stuck (no keyboard trap) and nothing focusable is hidden off-screen
  - the mobile menu opens with Enter and closes with Escape, returning focus to the button
  - the whole journey works without a mouse: log in, answer the question, choose entries, add to basket
  - no Content-Security-Policy violations in the console
This does not replace testing with real assistive technology (VoiceOver, TalkBack, NVDA) — see docs/ACCESSIBILITY.md.
"""
import os
import socket
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = os.path.join(os.path.dirname(__file__), "..")
PAGES = ["/", "/competitions", "/c/ps5-pro-bundle", "/instant-wins", "/results", "/free-entry", "/login", "/signup",
         "/how-it-works"]
PLAYER_PAGES = ["/account", "/account?tab=entries", "/account?tab=wallet", "/basket", "/support"]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


FOCUS_JS = """async () => {
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));   // let styles settle
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
  const indicator = (cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0) || cs.boxShadow !== 'none'
                    || el.matches(':focus-visible') && (cs.textDecorationLine.includes('underline'));
  return {tag: el.tagName, text: (el.innerText || el.value || el.getAttribute('aria-label') || el.name || '').trim().slice(0, 40),
          id: el.id, w: r.width, h: r.height, top: r.top, left: r.left, vw: innerWidth, vh: innerHeight,
          indicator, key: el.outerHTML.slice(0, 120)};
}"""


def walk(page, url, problems, max_tabs=120):
    page.goto(url)
    page.keyboard.press("Tab")
    first = page.evaluate(FOCUS_JS)
    if not first or "skip" not in (first["key"] or "").lower():
        problems.append(f"{url}: first Tab doesn't reach 'Skip to content' (got {first and first['text']!r})")
    else:
        page.keyboard.press("Enter")
        if page.evaluate("location.hash") != "#main":
            problems.append(f"{url}: skip link doesn't move to #main")
        page.goto(url)
    seen, last, repeats = [], None, 0
    for _ in range(max_tabs):
        page.keyboard.press("Tab")
        f = page.evaluate(FOCUS_JS)
        if f is None:
            break                                       # wrapped round to the browser chrome: no trap
        k = f["key"]
        if k == last:
            repeats += 1
            if repeats >= 4:                        # date inputs have three stops (day, month, year)
                problems.append(f"{url}: focus stuck on {f['text']!r} (keyboard trap)")
                break
        else:
            repeats = 0
        last = k
        if k in seen and k == seen[0]:
            break                                       # full cycle
        seen.append(k)
        if f["w"] < 1 or f["h"] < 1:
            problems.append(f"{url}: focused element has no size: {f['key']}")
        elif f["top"] < -1 or f["top"] > f["vh"] or f["left"] < -1 or f["left"] > f["vw"]:
            problems.append(f"{url}: focused element off screen: {f['text']!r} {f['key'][:60]}")
        if not f["indicator"]:
            problems.append(f"{url}: no visible focus indicator on {f['text']!r} ({f['tag']})")
    return len(seen)


def main():
    port = free_port()
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "qa_crawl.py"), "--serve", str(port)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=ROOT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        for line in srv.stdout:
            if line.startswith("serving on"):
                break
        time.sleep(1.5)
        problems, axe_js = [], os.environ.get("AXE_JS")
        with sync_playwright() as p:
            exe = "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None
            try:
                browser = p.chromium.launch(executable_path=exe) if exe and os.path.isfile(exe) else p.chromium.launch()
            except Exception:
                browser = p.chromium.launch()
            for width in (390, 1280):
                ctx = browser.new_context(base_url=base, viewport={"width": width, "height": 844 if width == 390 else 900},
                                          reduced_motion="reduce")   # no smooth scrolling, so focus position is final
                page = ctx.new_page()
                csp = []
                page.on("console", lambda m: csp.append(m.text) if "Content Security Policy" in m.text else None)
                for url in PAGES:
                    n = walk(page, url, problems)
                    print(f"[{width}px] {url}: {n} focusable stops")
                # log in with the keyboard only
                page.goto("/login")
                page.focus("#email")
                page.keyboard.type("player@dbx.test")
                page.keyboard.press("Tab")
                page.keyboard.type("supersecret123")
                with page.expect_navigation():
                    page.keyboard.press("Enter")
                if "/login" in page.url:
                    problems.append(f"[{width}px] keyboard log-in failed")
                for url in PLAYER_PAGES:
                    n = walk(page, url, problems)
                    print(f"[{width}px] {url}: {n} focusable stops")
                # answer, choose, add to basket — keyboard only
                page.goto("/c/ps5-pro-bundle")
                before = page.evaluate("document.querySelector('[aria-label^=\"Basket\"]').getAttribute('aria-label')")
                radio = page.locator("input[name=answer]").nth(1)
                radio.focus()
                page.keyboard.press("Space")
                page.focus("#qty")
                page.keyboard.press("Control+A")
                page.keyboard.type("2")
                page.focus("button[name=go][value=basket]")
                with page.expect_navigation():
                    page.keyboard.press("Enter")
                after = page.evaluate("document.querySelector('[aria-label^=\"Basket\"]').getAttribute('aria-label')")
                if after == before:
                    problems.append(f"[{width}px] keyboard-only add to basket didn't work ({before} → {after})")
                if width == 390:                        # mobile menu
                    page.goto("/")
                    page.focus("#menubtn")
                    page.keyboard.press("Enter")
                    if page.get_attribute("#menubtn", "aria-expanded") != "true":
                        problems.append("menu doesn't open with Enter")
                    page.keyboard.press("Escape")
                    if page.get_attribute("#menubtn", "aria-expanded") != "false" or page.evaluate("document.activeElement.id") != "menubtn":
                        problems.append("Escape doesn't close the menu and return focus to the button")
                if axe_js and os.path.exists(axe_js):
                    src = open(axe_js).read()
                    for url in PAGES + PLAYER_PAGES:
                        page.goto(url)
                        page.add_script_tag(content=src)
                        res = page.evaluate("""async () => (await axe.run(document, {runOnly: {type: 'tag',
                            values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']}})).violations
                            .map(v => v.id + ' (' + v.impact + '): ' + v.nodes.length + ' × ' + v.nodes[0].target.join(' '))""")
                        problems += [f"[{width}px axe] {url}: {v}" for v in res]
                problems += [f"[{width}px] CSP: {m}" for m in csp]
                ctx.close()
            browser.close()
        print("\n".join(sorted(set(problems))) if problems else "KEYBOARD WALKTHROUGH: NO PROBLEMS FOUND"
              + ("" if axe_js else " (axe-core scan skipped — set AXE_JS)"))
        return 1 if problems else 0
    finally:
        srv.terminate()


if __name__ == "__main__":
    sys.exit(main())
