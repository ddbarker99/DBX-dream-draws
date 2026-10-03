"""Branded winner share cards (1200×630, the size social networks use for link previews).

Only made when the winner has given permission (competitions.winner_consent_at). Shows the prize, the winner's
public name (first name + initial), ticket number and draw date — nothing else about them.
"""
import hashlib
import os

from PIL import Image, ImageDraw, ImageFont, ImageOps

W, H = 1200, 630
STATIC = os.path.join(os.path.dirname(__file__), "static")


def _font(name, size):
    try:
        return ImageFont.truetype(os.path.join(STATIC, "fonts", name), size)
    except OSError:                                   # FreeType without WOFF2 support: fall back to the built-in font
        return ImageFont.load_default(size=size)


def _wrap(draw, text, font, width, max_lines=3):
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,") + "…"
    return lines


def card_path(upload_dir, comp):
    key = hashlib.sha256(f"{comp['id']}|{comp['winner_consent_at']}|{comp['image']}|{comp['title']}|v1".encode()).hexdigest()[:12]
    return os.path.join(upload_dir, "cards", f"winner-{comp['id']}-{key}.png")


def winner_card(upload_dir, comp, name, number, drawn, site_name):
    """Return the path of the card PNG, creating it if needed."""
    path = card_path(upload_dir, comp)
    if os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = Image.new("RGB", (W, H), (18, 6, 8))
    d = ImageDraw.Draw(img)
    for y in range(H):                                 # dark red to black
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(60 * (1 - t) + 14 * t), int(8 * (1 - t) + 4 * t), int(16 * (1 - t) + 6 * t)))
    photo_box = (60, 75, 540, 555)
    src = os.path.join(upload_dir, comp["image"]) if comp["image"] else None
    if src and os.path.exists(src):
        try:
            p = ImageOps.fit(Image.open(src).convert("RGB"), (480, 480))
            mask = Image.new("L", (480, 480), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, 479, 479), radius=28, fill=255)
            img.paste(p, photo_box[:2], mask)
        except OSError:
            src = None
    if not src or not os.path.exists(src or ""):
        d.rounded_rectangle(photo_box, radius=28, fill=(40, 12, 18), outline=(255, 201, 61), width=4)
        d.text((300, 315), "WIN", font=_font("russo-one-latin-400-normal.woff2", 120), fill=(255, 201, 61), anchor="mm")
    x = 590
    d.text((x, 95), "WINNER", font=_font("russo-one-latin-400-normal.woff2", 64), fill=(255, 201, 61))
    y = 190
    for line in _wrap(d, comp["title"], _font("inter-latin-800-normal.woff2", 50), W - x - 60):
        d.text((x, y), line, font=_font("inter-latin-800-normal.woff2", 50), fill=(255, 255, 255))
        y += 62
    y += 18
    d.text((x, y), f"Congratulations {name}!", font=_font("inter-latin-600-normal.woff2", 36), fill=(255, 220, 225))
    d.text((x, y + 52), f"Ticket #{number} · drawn {drawn}", font=_font("inter-latin-400-normal.woff2", 30), fill=(220, 190, 195))
    try:
        logo = Image.open(os.path.join(STATIC, "brand", "logo-160.png")).convert("RGBA").resize((84, 84))
        img.paste(logo, (x, 500), logo)
    except OSError:
        pass
    d.text((x + 100, 542), site_name, font=_font("russo-one-latin-400-normal.woff2", 34), fill=(255, 255, 255), anchor="lm")
    img.save(path, "PNG", optimize=True)
    return path
