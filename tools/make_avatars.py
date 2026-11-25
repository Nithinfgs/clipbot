"""Original Vice-City-vibe profile pictures (no Rockstar/GTA logos or fonts — trademark-safe).
Run: uv run python tools/make_avatars.py  -> branding/avatar_<handle>.png (1080x1080, circle-safe)."""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "branding"
OUT.mkdir(exist_ok=True)
S = 1080
IMPACT = "/System/Library/Fonts/Supplemental/Impact.ttf"
DIN = "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf"

ACCOUNTS = [
    ("vicecity.laughs", "LAUGHS", "😂", ((255, 170, 60), (255, 60, 150), (90, 30, 140))),
    ("wast.eddaily", "WASTED", "💀", ((255, 110, 70), (200, 30, 60), (40, 10, 50))),
    ("vice_reacts", "REACTS", "😱", ((255, 220, 90), (255, 90, 120), (60, 40, 160))),
    ("fivestarchaos4", "CHAOS", "★★★★★", ((80, 220, 255), (255, 60, 200), (30, 10, 80))),
    ("vicecityclips969", "CLIPS", "🎬", ((120, 255, 200), (60, 160, 255), (40, 20, 110))),
]


def gradient(top, mid, bot):
    img = Image.new("RGB", (S, S))
    px = img.load()
    for y in range(S):
        t = y / (S - 1)
        a, b, u = (top, mid, t / 0.55) if t < 0.55 else (mid, bot, (t - 0.55) / 0.45)
        c = tuple(int(a[i] + (b[i] - a[i]) * u) for i in range(3))
        for x in range(S):
            px[x, y] = c
    return img


def retro_sun(draw, cx, cy, r, col_top, col_bot):
    for y in range(cy - r, cy + r):
        dy = y - cy
        half = int(math.sqrt(max(r * r - dy * dy, 0)))
        t = (y - (cy - r)) / (2 * r)
        band = y > cy and int((y - cy) / 22) % 2 == 1 and (y - cy) % 22 < 6 + (y - cy) // 18
        if band:
            continue
        c = tuple(int(col_top[i] + (col_bot[i] - col_top[i]) * t) for i in range(3))
        draw.line([(cx - half, y), (cx + half, y)], fill=c)


def palm(draw, base_x, base_y, h, lean, col):
    # trunk: tapered curved polygon
    pts_l, pts_r = [], []
    for i in range(21):
        t = i / 20
        x = base_x + lean * (t ** 2) * h
        y = base_y - t * h
        w = 22 * (1 - 0.6 * t)
        pts_l.append((x - w, y))
        pts_r.append((x + w, y))
    draw.polygon(pts_l + pts_r[::-1], fill=col)
    top = pts_l[-1][0] + 20, pts_l[-1][1]
    for ang, ln in [(-160, 260), (-130, 230), (-100, 170), (-60, 220), (-25, 260), (10, 230), (-195, 220)]:
        a = math.radians(ang)
        pts = []
        for i in range(13):
            t = i / 12
            x = top[0] + math.cos(a) * ln * t
            y = top[1] + math.sin(a) * ln * t + (t ** 2) * ln * 0.45
            pts.append((x, y))
        widths = [max(2, int(34 * math.sin(math.pi * (i / 12)))) for i in range(13)]
        upper = [(x, y - w / 2) for (x, y), w in zip(pts, widths)]
        lower = [(x, y + w / 2) for (x, y), w in zip(pts, widths)]
        draw.polygon(upper + lower[::-1], fill=col)


def text_center(draw, y, txt, font, fill, stroke=0, stroke_fill=None):
    w = draw.textlength(txt, font=font)
    draw.text(((S - w) / 2, y), txt, font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)


def make(handle, word, badge, cols):
    top, mid, bot = cols
    img = gradient(top, mid, bot)
    d = ImageDraw.Draw(img)
    retro_sun(d, S // 2, 470, 250, (255, 240, 120), (255, 80, 140))
    # horizon / water
    d.rectangle([0, 640, S, S], fill=tuple(int(c * 0.55) for c in bot))
    for i, y in enumerate(range(660, S, 34)):
        d.line([(0, y), (S, y)], fill=tuple(min(255, int(c * 0.9) + 20) for c in mid), width=2 + i // 3)
    sil = (20, 8, 35)
    palm(d, 210, 700, 430, 0.35, sil)
    palm(d, 880, 700, 360, -0.4, sil)
    glow = img.filter(ImageFilter.GaussianBlur(2))
    img = Image.blend(img, glow, 0.25)
    d = ImageDraw.Draw(img)
    f_small = ImageFont.truetype(DIN, 92)
    f_big = ImageFont.truetype(IMPACT, 230 if len(word) <= 6 else 200)
    text_center(d, 150, "VICE CITY", f_small, (255, 255, 255), stroke=5, stroke_fill=(60, 10, 80))
    text_center(d, 680, word, f_big, (255, 255, 255), stroke=12, stroke_fill=(40, 5, 60))
    if badge.startswith("★"):
        f_star = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 70) \
            if Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf").exists() else f_small
        text_center(d, 930, badge, f_star, (255, 230, 80), stroke=4, stroke_fill=(40, 5, 60))
    p = OUT / f"avatar_{handle}.png"
    img.save(p, optimize=True)
    return p


if __name__ == "__main__":
    for a in ACCOUNTS:
        print(make(*a))
