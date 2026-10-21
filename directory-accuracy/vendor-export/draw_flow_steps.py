"""Draws vendor-export-flow-steps.png: the six steps and the side paths. Usage: python3 draw_flow_steps.py vendor-export-flow-steps.png"""
import sys
from PIL import Image, ImageDraw, ImageFont

OUT = sys.argv[1]
W = 2378
BG = (14, 17, 20)
MUTED = (127, 138, 149)
TEXT = (236, 239, 242)
BODY = (181, 190, 199)
LINE = (38, 44, 52)
ACCENT = (255, 122, 82)
SANS = "/System/Library/Fonts/SFNS.ttf"
MONO = "/System/Library/Fonts/SFNSMono.ttf"


def font(path, size, weight=None):
    f = ImageFont.truetype(path, size)
    if weight:
        try:
            f.set_variation_by_name(weight)
        except (OSError, ValueError):
            pass
    return f


CAPTION = font(SANS, 25, "Semibold")
TITLE = font(SANS, 33, "Semibold")
BODYF = font(SANS, 27, "Regular")
NUM = font(SANS, 23, "Semibold")
MONOF = font(MONO, 22, "Regular")
MONOB = font(MONO, 22, "Semibold")


def wrap(draw, text, fnt, width):
    lines, line = [], ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if draw.textlength(trial, font=fnt) <= width:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def column(draw, x, y, width, title, body, foot_label, foot_value, body_top=None):
    lines = wrap(draw, title, TITLE, width)
    for line in lines:
        draw.text((x, y), line, font=TITLE, fill=TEXT)
        y += 45
    y = max(y + 12, body_top or 0)
    for line in wrap(draw, body, BODYF, width):
        draw.text((x, y), line, font=BODYF, fill=BODY)
        y += 39
    return y


def foot(draw, x, y, width, label, value):
    draw.line([(x, y), (x + width, y)], fill=LINE, width=2)
    draw.text((x, y + 22), label, font=MONOF, fill=MUTED)
    lx = x + draw.textlength(label, font=MONOF)
    draw.text((lx, y + 22), " · ", font=MONOF, fill=MUTED)
    lx += draw.textlength(" · ", font=MONOF)
    draw.text((lx, y + 22), value, font=MONOB, fill=TEXT)


STEPS = [
    ("1", "solid", "Create the schedule",
     "An operator sets cadence, timezone, and a saved filter id for a tenant; the egress template is provisioned.",
     "schedule row", "enabled"),
    ("2", "solid", "Daily tick", "Every morning a due schedule becomes one batch for the period.",
     "batch", "SCHEDULED"),
    ("3", "solid", "Request egress",
     "Send the saved filter id (a retry: the earlier attempt, to reuse its list) and the destination filename to the egress service.",
     "batch", "EGRESS_REQUESTED"),
    ("4", "dashed", "Egress selects practitioners",
     "It fetches the filter, freezes the matching practitioner list once, and writes the list-used file.",
     "egress", "list frozen"),
    ("5", "dashed", "Egress builds and places the file",
     "It writes the CSV, copies it to the vendor bucket, and publishes a completion event with its selection counts.",
     "batch", "EGRESS_COMPLETED"),
    ("6", "accent", "Confirm delivery",
     "Check the file in the GCS bucket, compare its count with the practitioners egress selected, mark done.",
     "batch", "DELIVERED"),
]

SIDE = [
    ("The filter matches nobody",
     "Egress reports no matching practitioners. Nothing is sent for the period, and an alert fires.",
     "batch", "EMPTY"),
    ("Egress fails or refuses",
     "The batch fails with its cause. An operator retries; the retry reuses the earlier practitioner list, or takes the schedule's current filter id when that list cannot be reused.",
     "batch", "FAILED → SCHEDULED"),
    ("Egress lost the reused list",
     "Egress accepted a retry's reuse, then found the list gone. The next attempt starts by itself and evaluates the filter again.",
     "batch", "SCHEDULED, next attempt"),
    ("Egress is busy",
     "Too many large exports are running for the tenant. The request is sent again after five minutes, up to twelve times.",
     "batch", "SCHEDULED"),
]

img = Image.new("RGB", (W, 1200), BG)
d = ImageDraw.Draw(img)
left, col_w, gap = 40, 345, 38
pitch = col_w + gap

d.text((left, 42), "THE FLOW IN SIX STEPS", font=CAPTION, fill=MUTED)
cy = 143
d.line([(left + 27, cy), (left + 5 * pitch + col_w, cy)], fill=LINE, width=2)
bottom = 0
for i, (num, style, title, body, fl, fv) in enumerate(STEPS):
    x = left + i * pitch
    box = [x, cy - 27, x + 54, cy + 27]
    if style == "accent":
        d.ellipse(box, fill=ACCENT)
        d.text((x + 27, cy), num, font=NUM, fill=BG, anchor="mm")
    else:
        d.ellipse(box, fill=BG)
        if style == "solid":
            d.ellipse(box, outline=TEXT, width=3)
        else:
            for k in range(0, 360, 20):
                d.arc(box, k, k + 11, fill=TEXT, width=3)
        d.text((x + 27, cy), num, font=NUM, fill=TEXT, anchor="mm")
    bottom = max(bottom, column(d, x, 198, col_w, title, body, fl, fv, body_top=None))
foot_y = bottom + 30
for i, (_, _, _, _, fl, fv) in enumerate(STEPS):
    foot(d, left + i * pitch, foot_y, col_w, fl, fv)

top = foot_y + 120
d.text((left, top), "WHEN IT DOES NOT GO STRAIGHT THROUGH", font=CAPTION, fill=MUTED)
side_w = (W - 2 * left - 3 * gap) // 4
bottom = 0
for i, (title, body, fl, fv) in enumerate(SIDE):
    x = left + i * (side_w + gap)
    d.rectangle([x, top + 52, x + 6, top + 52 + 40], fill=ACCENT if i == 2 else MUTED)
    bottom = max(bottom, column(d, x + 24, top + 50, side_w - 24, title, body, fl, fv))
foot_y2 = bottom + 30
for i, (_, _, fl, fv) in enumerate(SIDE):
    foot(d, left + i * (side_w + gap) + 24, foot_y2, side_w - 24, fl, fv)

img = img.crop((0, 0, W, foot_y2 + 90))
img.save(OUT, optimize=True)
print(img.size)
