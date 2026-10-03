"""Email sender with a branded HTML design (black, red & chrome — matches the site).

Every email is sent as HTML + a plain-text version, with proper Date and Message-ID
headers (missing ones push mail into spam). If SMTP isn't configured, emails are
written to the log instead, so nothing breaks while you set it up."""
import html as _html
import re
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from flask import current_app

RED, DARK, PANEL, LINE, TEXT, MUTED = "#ff2436", "#060406", "#140b0e", "#3a1820", "#f3f1f2", "#b4a7aa"
FONT = "'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
DISPLAY = "'Russo One','Arial Black','Segoe UI Black',Impact,Arial,sans-serif"
_URL = re.compile(r"(https?://[^\s<]+)")


def _para(text, skip_url=None):
    """Plain text -> safe HTML paragraphs, with links made clickable."""
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in block.split("\n") if ln.strip() and ln.strip() != skip_url]
        if not lines:
            continue
        safe = "<br>".join(_html.escape(ln) for ln in lines)
        safe = _URL.sub(lambda m: f'<a href="{m.group(1)}" style="color:{RED};word-break:break-all">{m.group(1)}</a>', safe)
        out.append(f'<p style="margin:0 0 16px;font:16px/1.6 {FONT};color:{TEXT}">{safe}</p>')
    return "".join(out)


def _button(label, url):
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:8px auto 20px">'
            f'<tr><td bgcolor="{RED}" style="border-radius:6px;background:linear-gradient(180deg,#ff3a4a,#c4001a);'
            f'box-shadow:0 0 24px rgba(255,36,54,.45)">'
            f'<a href="{url}" style="display:inline-block;padding:16px 34px;font:bold 16px/1 {FONT};color:#ffffff;'
            f'text-decoration:none;text-transform:uppercase;letter-spacing:2px">{_html.escape(label)}</a></td></tr></table>')


def _highlight(items):
    """A list -> red ticket chips. A string -> one big glowing line (e.g. a prize)."""
    if isinstance(items, str):
        return (f'<div style="margin:6px 0 22px;padding:20px;text-align:center;background:#1f070c;border:1px solid {RED};'
                f'border-radius:10px;font:28px/1.25 {DISPLAY};color:#ffffff;text-shadow:0 0 18px rgba(255,36,54,.8)">'
                f'{_html.escape(items)}</div>')
    chips = "".join(
        f'<span style="display:inline-block;margin:0 6px 8px 0;padding:6px 12px;border-radius:6px;background:#2a0d14;'
        f'border:1px solid {LINE};font:bold 15px/1 {FONT};color:#ffffff">{_html.escape(str(i))}</span>' for i in items)
    return f'<div style="margin:0 0 20px">{chips}</div>'


def render(subject, body, button=None, heading=None, highlight=None, preheader=None):
    cfg = current_app.config
    site, base = cfg["SITE_NAME"], cfg["SITE_URL"]
    first, _, rest = site.partition(" ")
    banner = f"{base}/static/brand/banner-900.jpg"
    logo = f"{base}/static/brand/logo-160.png"
    pre = _html.escape(preheader or body.strip().split("\n")[0])[:140]
    btn = _button(*button) if button else ""
    fallback = (f'<p style="margin:0 0 6px;font:12px/1.5 {FONT};color:{MUTED};text-align:center">Button not working? '
                f'Copy this link:<br><a href="{button[1]}" style="color:{MUTED};word-break:break-all">{button[1]}</a></p>'
                if button else "")
    head = (f'<h1 style="margin:0 0 18px;font:26px/1.2 {DISPLAY};color:#ffffff;text-transform:uppercase;letter-spacing:1px">'
            f'{_html.escape(heading)}</h1>') if heading else ""
    hl = _highlight(highlight) if highlight else ""
    social = " &nbsp;·&nbsp; ".join(f'<a href="{u}" style="color:{MUTED};text-decoration:none">{k.capitalize()}</a>'
                                    for k, u in cfg.get("SOCIAL", {}).items() if u)
    return f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark"><title>{_html.escape(subject)}</title></head>
<body style="margin:0;padding:0;background:{DARK}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{pre}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{DARK}" style="background:{DARK}">
<tr><td align="center" style="padding:24px 12px">
 <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background:{PANEL};border:1px solid {LINE};border-radius:12px;overflow:hidden">
  <tr><td style="background:{DARK};line-height:0"><a href="{base}"><img src="{banner}" width="600" alt="{_html.escape(site)} — New Era"
     style="display:block;width:100%;max-width:600px;height:auto;border:0;color:#ffffff;font:bold 24px {DISPLAY};background:#1a0509"></a></td></tr>
  <tr><td style="height:3px;line-height:3px;font-size:0;background:{RED}">&nbsp;</td></tr>
  <tr><td style="padding:32px 32px 12px">{head}{_para(body, skip_url=button[1] if button else None)}{hl}{btn}{fallback}</td></tr>
  <tr><td style="padding:0 32px"><div style="height:1px;background:{LINE}"></div></td></tr>
  <tr><td align="center" style="padding:22px 32px 26px">
    <a href="{base}"><img src="{logo}" width="56" height="56" alt="" style="display:block;margin:0 auto 10px;border:0"></a>
    <div style="font:18px/1 {DISPLAY};text-transform:uppercase"><span style="color:#e6e8ec">{_html.escape(first)}</span> <span style="color:{RED}">{_html.escape(rest)}</span></div>
    <p style="margin:12px 0 0;font:13px/1.6 {FONT};color:{MUTED}">
      <a href="{base}/instant-wins" style="color:{MUTED}">Instant Wins</a> &nbsp;·&nbsp; <a href="{base}/winners" style="color:{MUTED}">Winners</a>
      &nbsp;·&nbsp; <a href="{base}/account" style="color:{MUTED}">My account</a>{('<br>' + social) if social else ''}</p>
    <p style="margin:14px 0 0;font:11px/1.6 {FONT};color:#7d6d71">18+ UK residents only. Play responsibly — help is available from
      <a href="https://www.gamcare.org.uk" style="color:#7d6d71">GamCare</a> and <a href="https://www.gambleaware.org" style="color:#7d6d71">GambleAware</a>.
      You can set spend limits or take a break any time in your account.{('<br>' + _html.escape(cfg['COMPANY_DETAILS'])) if cfg.get('COMPANY_DETAILS') else ''}</p>
  </td></tr>
 </table>
</td></tr></table></body></html>"""


def send(to, subject, body, button=None, heading=None, highlight=None, preheader=None):
    """button=(label, url); highlight=str (big prize line) or list (ticket chips)."""
    cfg = current_app.config
    text = body
    if highlight:
        text += "\n\n" + (highlight if isinstance(highlight, str) else ", ".join(map(str, highlight)))
    if button and button[1] not in body:
        text += f"\n\n{button[0]}: {button[1]}"
    text += f"\n\n— {cfg['SITE_NAME']}\n{cfg['SITE_URL']}"
    if not cfg.get("SMTP_HOST"):
        current_app.logger.warning("EMAIL NOT SENT (SMTP not set in .env) to=%s subject=%s\n%s", to, subject, text)
        return
    msg = EmailMessage()
    name, addr = parseaddr(cfg["MAIL_FROM"])
    msg["From"] = formataddr((name or cfg["SITE_NAME"], addr or cfg["MAIL_FROM"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=(addr or "dreamdraws.local").split("@")[-1])
    if cfg.get("SUPPORT_EMAIL"):
        msg["Reply-To"] = cfg["SUPPORT_EMAIL"]
    msg.set_content(text)
    msg.add_alternative(render(subject, body, button, heading, highlight, preheader), subtype="html")
    settings = (cfg["SMTP_HOST"], int(cfg["SMTP_PORT"]), cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
    logger = current_app.logger

    def worker():
        host, port, user, pw = settings
        try:
            if port == 465:
                s = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=20)
            else:
                s = smtplib.SMTP(host, port, timeout=20)
                s.starttls(context=ssl.create_default_context())
            if user:
                s.login(user, pw)
            s.send_message(msg)
            s.quit()
            logger.warning("Email sent to %s: %s", to, subject)
        except Exception:
            logger.exception("Email to %s failed", to)

    threading.Thread(target=worker, daemon=True).start()
