"""Studio — the campaign production engine (feature four).

a brief rides in, a structured campaign plan rides out, and the plan's days
are rendered into real PNG files by a stdlib graphics pipeline: no PIL, no
build step, no new bill. an LLM may speak the plan (env-configured, schema-
validated, the deterministic voice answers when it is silent or speaks out of
turn); an image model may paint one hero (opt-in, env-gated — the house does
not spend without a word). QC reads the bytes that shipped — dimensions come
from re-parsing the written PNG header, never from a claim — and the ZIP is
the campaign's receipt: every file the manifest lists is inside it.

the shape stays brand-agnostic: the brief names the project (the example
runs PAPERBOARD), the palette falls back to the PAPER defaults, and nothing
here needs the website to exist to work.
"""

import binascii
import io
import json
import os
import re
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

from .artboard import sniff
from .settings import SITE_DIR

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# the five ways a day may wear its words — the layouts differ in KIND, not
# in tint: five cards, five compositions, no five clones of one panel.
ASSET_TYPES = {
    "xpost":    dict(use="X post",         w=1600, h=900,  aspect="16:9"),
    "meme":     dict(use="Meme square",    w=1080, h=1080, aspect="1:1"),
    "promo":    dict(use="Promo card",     w=1080, h=1350, aspect="4:5"),
    "banner":   dict(use="Website banner", w=1920, h=640,  aspect="3:1"),
    "announce": dict(use="Community card", w=1200, h=750,  aspect="8:5"),
    "ai_hero":  dict(use="AI hero image",  w=0,    h=0,    aspect="as provided"),
}
DAY_KINDS = ("xpost", "meme", "promo", "banner", "announce")

# the week's law: each day carries a distinct purpose, every purpose carries
# a pool so two campaigns (or one regeneration) never print the same words
# twice in a row. PAPERBOARD-flavoured, and verifiable claims only.
WEEK = (
    dict(purpose="the hook", pool=(
        "a new week of paper opens: the board is blank and the ink is wet",
        "the fold that starts the run — PAPERBOARD opens its doors",
        "issue zero-one: post paper, earn points, climb the front page")),
    dict(purpose="the joke", pool=(
        "a mascot who has seen the chart and kept the receipt",
        "diamond hands, paper wallet",
        "posting through the pain and calling it strategy")),
    dict(purpose="the feature", pool=(
        "paste one post: the server reads it, gates it, scores it",
        "points that print on the front page — no vibes, a ledger",
        "no wallet ceremony: email in, points ride the account")),
    dict(purpose="the ask", pool=(
        "the wall is open: hang your paper on it",
        "one post, one seat on the board",
        "submit something worth framing this week")),
    dict(purpose="the game", pool=(
        "the week is the server's naming — a best score, not every score",
        "the boards stay empty until a hand fills them",
        "a lesser score simply defers; the week keeps its best")),
    dict(purpose="the receipt", pool=(
        "verified posts, counted likes, a frozen cut for the batch",
        "the ledger remembers, politely",
        "numbers the provider reported, not numbers we liked")),
    dict(purpose="the send-off", pool=(
        "fold the week, print the recap, keep the receipts",
        "the front page closes and the next issue already opens",
        "one week of paper: what the board says, it says")),
)
CTAS = ("Follow @Papersol6", "Post PAPER. Earn points.", "Join PAPERBOARD.",
        "Check the leaderboard.", "Submit your post.")
MEME_PUNCH = ("no rug, only paper", "the byline holds", "receipts, not feelings",
              "one post, one point", "the board does not blink")

# who wears which pose — the pose files are the real cut-outs on disk
# (server/tools/pose_alpha.py made their alpha); never paste one pose for
# two jobs, and never invent one the shelf does not have.
POSE_BY_PURPOSE = {
    "the hook": ("celebrate", "victory", "front"),
    "the joke": ("silly", "cool", "celebrate"),
    "the feature": ("cool", "front", "running"),
    "the ask": ("running", "front", "silly"),
    "the game": ("victory", "cool", "celebrate"),
    "the receipt": ("front", "celebrate", "silly"),
    "the send-off": ("celebrate", "front", "victory"),
}
_POSE_CACHE = {}

# the brand's printed defaults — the fallback voice when no reference spoke
PAPER_BRAND = {"paper": (242, 239, 228), "press": (12, 13, 9),
               "accent": (62, 201, 95), "gold": (217, 181, 84)}


class StudioError(Exception):
    """one error key for the whole door, like every other door in the house."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# —————————————————————————— images, without a library ——————————————————————————
def png_dims(raw):
    """(w, h) straight from the IHDR, or None. QC's authority is the file."""
    if not raw.startswith(PNG_MAGIC):
        return None
    try:
        ln = struct.unpack(">I", raw[8:12])[0]
        if raw[12:16] != b"IHDR" or ln < 13:
            return None
        return tuple(struct.unpack(">II", raw[16:24]))
    except (IndexError, struct.error):
        return None


def png_read(raw):
    """(w, h, rgba) for 8-bit non-interlaced PNGs (grey, grey+a, rgb, rgba);
    anything deeper than the shelf can read raises StudioError."""
    if not raw.startswith(PNG_MAGIC):
        raise StudioError("bad_image_type")
    off, idat, ihdr = 8, b"", None
    n = len(raw)
    while off + 8 <= n:
        ln = struct.unpack(">I", raw[off:off + 4])[0]
        typ = raw[off + 4:off + 8]
        data = raw[off + 8:off + 8 + ln]
        if typ == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", data[:13])
        elif typ == b"IDAT":
            idat += data
        elif typ == b"IEND":
            break
        off += 12 + ln
    if ihdr is None:
        raise StudioError("bad_image_data")
    w, h, depth, ctype, _comp, _filt, interlace = ihdr
    if depth != 8 or interlace != 0 or ctype not in (0, 2, 4, 6):
        raise StudioError("unsupported_png")
    nch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    scan = zlib.decompress(idat)
    stride = w * nch
    px = bytearray(w * h * 4)
    prev = bytearray(stride)
    p = 0
    for _y in range(h):
        f = scan[p]
        p += 1
        row = bytearray(scan[p:p + stride])
        p += stride
        if f == 1:                                   # Sub
            for x in range(nch, stride):
                row[x] = (row[x] + row[x - nch]) & 0xff
        elif f == 2:                                 # Up
            for x in range(stride):
                row[x] = (row[x] + prev[x]) & 0xff
        elif f == 3:                                 # Average
            for x in range(stride):
                a = row[x - nch] if x >= nch else 0
                row[x] = (row[x] + ((a + prev[x]) >> 1)) & 0xff
        elif f == 4:                                 # Paeth (spec order)
            for x in range(stride):
                a = row[x - nch] if x >= nch else 0
                b0 = prev[x]
                c0 = prev[x - nch] if x >= nch else 0
                pa, pb, pc = abs(b0 - c0), abs(a - c0), abs(a + b0 - 2 * c0)
                pred = a if (pa <= pb and pa <= pc) else (b0 if pb <= pc else c0)
                row[x] = (row[x] + pred) & 0xff
        o = _y * stride * 4 // nch * 4 if False else _y * w * 4
        if ctype == 6:
            px[o:o + stride] = bytes(row)
        else:
            for x in range(w):
                s, d = x * nch, o + x * 4
                if ctype == 0:
                    px[d:d + 4] = bytes((row[s], row[s], row[s], 255))
                elif ctype == 4:
                    px[d:d + 4] = bytes((row[s], row[s], row[s], row[s + 1]))
                else:
                    px[d:d + 4] = bytes((row[s], row[s + 1], row[s + 2], 255))
        prev = row
    return w, h, bytes(px)


def _chunk(typ, payload):
    return (struct.pack(">I", len(payload)) + typ + payload
            + struct.pack(">I", binascii.crc32(typ + payload) & 0xffffffff))


def png_write(w, h, rgba):
    """8-bit RGBA out: IHDR, one IDAT, IEND. The writer the whole shelf leans on."""
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        s = y * stride
        raw += rgba[s:s + stride]
    return (PNG_MAGIC
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


def img_dims(raw):
    """best-effort (w, h) for the shipped formats; None when the file keeps
    its size to itself (an exotic webp) — the ledger speaks null, never a 0."""
    kind = sniff(raw)
    if kind == "png":
        return png_dims(raw)
    if kind == "jpg":
        i, n = 2, len(raw)
        while i + 4 < n:
            if raw[i] != 0xff:
                i += 1
                continue
            marker = raw[i + 1]
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                          0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return w, h
            if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7 or marker == 0x01:
                i += 2
                continue
            i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
        return None
    if kind == "gif":
        return tuple(struct.unpack("<HH", raw[6:10]))
    if kind == "webp":
        fourcc = raw[12:16]
        try:
            if fourcc == b"VP8 ":
                return (struct.unpack("<H", raw[26:28])[0] & 0x3fff,
                        struct.unpack("<H", raw[28:30])[0] & 0x3fff)
            if fourcc == b"VP8L":
                bits = struct.unpack("<I", raw[20:24])[0]
                return ((bits >> 18) & 0x3fff) + 1, ((bits >> 4) & 0x3fff) + 1
        except (IndexError, struct.error):
            return None
    return None


# ————————————————————————————————— the press font ———————————————————————————————
# 5x7, one bit per pixel, leftmost pixel is the leftmost bit. Uppercase for
# the voice of a headline — posters get their caps the way walls do.
_G = {
    " ": ("00000",) * 7,
    "!": ("00100", "00100", "00100", "00100", "00000", "00100", "00000"),
    "\"": ("01010", "01010", "00000", "00000", "00000", "00000", "00000"),
    "#": ("01010", "11111", "01010", "01010", "11111", "01010", "00000"),
    "$": ("00100", "01111", "10100", "01110", "00101", "11110", "00100"),
    "%": ("11001", "11010", "00010", "00100", "01011", "10011", "00000"),
    "'": ("00100", "00100", "00000", "00000", "00000", "00000", "00000"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "*": ("00000", "00100", "10101", "01110", "10101", "00100", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    ",": ("00000", "00000", "00000", "00000", "00100", "01100", "00000"),
    "-": ("00000", "00000", "00000", "01110", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00100", "00000"),
    ".dot": None,
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    ":": ("00000", "00100", "00000", "00000", "00100", "00000", "00000"),
    ";": ("00000", "00100", "00000", "00000", "00100", "01100", "00000"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    ">": ("01000", "00100", "00010", "00001", "00010", "00100", "01000"),
    "?": ("01110", "10001", "00001", "00110", "00100", "00000", "00100"),
    "@": ("01110", "10001", "10111", "10101", "10111", "10000", "01110"),
    "0": ("01110", "10011", "10101", "10101", "10101", "11001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "[": ("01110", "01000", "01000", "01000", "01000", "01000", "01110"),
    "]": ("01110", "00010", "00010", "00010", "00010", "00010", "01110"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "11001", "10101", "10011", "10011", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
}
_G.pop(".dot")
_FOLD = {"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-",
         " ": " ", "✦": "*", "✻": "*", "⋆": "+", "·": ".", "…": "."}
_GW, _GH, _GSTEP = 5, 7, 6                 # width, height, advance (5 + 1 air)


def _glyph_rows(ch):
    return _G.get(ch) or _G.get(_FOLD.get(ch, ""), "") or None


def _fold(s):
    """caps, and the type-scale voice of a headline: anything the shelf
    cannot draw rides as a space — the width is kept, the lie is not."""
    out = []
    for ch in str(s).upper():
        out.append(ch if _glyph_rows(ch) is not None else " ")
    return "".join(out)


def text_measure(s, scale):
    return max(0, _GSTEP * scale * len(s) - scale)


def _wrap(s, bound_px, scale):
    maxch = max(1, bound_px // (_GSTEP * scale))
    lines, cur = [], ""
    for word in str(s).split():
        while len(word) > maxch:                     # a giant word may break hard
            if cur:
                lines.append(cur)
                cur = ""
            take = maxch
            lines.append(word[:take])
            word = word[take:]
        if cur and len(cur) + 1 + len(word) > maxch:
            lines.append(cur)
            cur = word
        else:
            cur = (cur + " " + word) if cur else word
    if cur:
        lines.append(cur)
    return lines or [""]


class Canvas:
    """one RGBA sheet of pixels: fills, hairlines, alpha paste, press text.
    everything in this file that ships a byte walks across one of these."""

    def __init__(self, w, h, rgb=(12, 13, 9)):
        self.w, self.h = w, h
        self.ground = tuple(rgb)
        px = bytes(rgb) + b"\xff"
        self.buf = bytearray(px * (w * h))

    def rect(self, x, y, w, h, rgb, alpha=255):
        x = max(0, min(self.w, int(x)))
        y = max(0, min(self.h, int(y)))
        w = max(0, min(self.w - x, int(w)))
        h = max(0, min(self.h - y, int(h)))
        if not (w and h):
            return
        if alpha >= 255:
            px = bytes(rgb) + b"\xff"
            for row in range(y, y + h):
                o = (row * self.w + x) * 4
                self.buf[o:o + w * 4] = px * w
            return
        for row in range(y, y + h):
            o = (row * self.w + x) * 4
            span = self.buf[o:o + w * 4]
            out = bytearray(len(span))
            for i in range(0, len(span), 4):
                for k in range(3):
                    out[i + k] = ((int(span[i + k]) * (255 - alpha))
                                  + (int(rgb[k]) * alpha) + 127) // 255
                out[i + 3] = 255
            self.buf[o:o + w * 4] = out

    def paste(self, img, x, y, tw=None, th=None):
        """nearest-neighbour fit with straight alpha over — the cut-out
        rides on, never a white box, and no mascot is ever stretched."""
        iw, ih, src = img
        if tw is None and th is None:
            tw, th = iw, ih
        elif tw is None:
            tw = max(1, int(iw * th / ih))
        elif th is None:
            th = max(1, int(ih * tw / iw))
        x, y = int(x), int(y)
        for dy in range(th):
            if not (0 <= y + dy < self.h):
                continue
            iy = min(ih - 1, dy * ih // th)
            srow = iy * iw * 4
            o = ((y + dy) * self.w + x) * 4
            for dx in range(tw):
                if not (0 <= x + dx < self.w):
                    continue
                sx = srow + min(iw - 1, dx * iw // tw) * 4
                a = src[sx + 3]
                if a == 0:
                    continue
                d = o + dx * 4
                if a >= 250:
                    self.buf[d:d + 3] = src[sx:sx + 3]
                    self.buf[d + 3:d + 4] = b"\xff"
                    continue
                for k in range(3):
                    self.buf[d + k] = ((int(src[sx + k]) * a
                                        + int(self.buf[d + k]) * (255 - a)) + 127) // 255
                self.buf[d + 3] = 255

    def _draw_line(self, s, x, y, rgb, scale):
        cx = int(x)
        for ch in s:
            rows = _glyph_rows(ch)
            if rows:
                for gy in range(_GH):
                    bits = rows[gy]
                    if len(bits) == 1:
                        bits = ("0000" + bits)[-5:]
                    if "1" not in bits:
                        continue
                    for gx in range(_GW):
                        if bits[gx] == "1":
                            self.rect(cx + gx * scale, int(y) + gy * scale,
                                      scale, scale, rgb)
            cx += _GSTEP * scale

    def text(self, x, y, s, rgb=(242, 239, 228), scale=2, align="left",
             bound_w=None, max_lines=3, dim=False):
        """press text: caps, wrapped, aligned, bounded. returns the bottom y."""
        lines = _wrap(str(s), bound_w, scale) if bound_w else [str(s)]
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1] + "…"
        ink = rgb
        if dim:
            ink = tuple((int(c) + 3 * int(g)) // 4
                        for c, g in zip(rgb, self.ground))
        lh = _GH * scale + max(2, scale)
        top = int(y)
        for ln, line in enumerate(lines):
            wpx = text_measure(_fold(line), scale) or 1
            lx = x
            if align == "center":
                lx = x + (int(bound_w or wpx) - wpx) // 2
            elif align == "right":
                lx = x + int(bound_w or wpx) - wpx
            self._draw_line(_fold(line), lx, top + ln * lh, ink, scale)
        return top + len(lines) * lh

    def to_png(self):
        return png_write(self.w, self.h, bytes(self.buf))


# ————————————————————————————————— the brand ———————————————————————————————————
def analyze_reference(raw):
    """palette roles from the bytes that arrived; suggestions stay labelled.
    one image does not contain a whole brand, and the note says so."""
    if raw is None:
        return {"source": "suggested", "roles": dict(PAPER_BRAND),
                "palette": [], "notes": ["no reference image — the PAPER "
                                         "defaults speak"]}
    kind = sniff(raw)
    if kind is None:
        raise StudioError("bad_image_type")
    if kind != "png":
        return {"source": "kept, not decoded", "roles": dict(PAPER_BRAND),
                "palette": [],
                "notes": ["reference kept (%s: the stdlib shelf reads PNG "
                          "colour; this one rides along unread)" % kind]}
    try:
        w, h, px = png_read(raw)
    except StudioError:
        return {"source": "kept, not decoded", "roles": dict(PAPER_BRAND),
                "palette": [], "notes": ["reference is a PNG this shelf "
                                         "cannot open (palette suggested)"]}
    step = max(1, int(((w * h) / 40000.0) ** 0.5))
    buckets = {}
    for i in range(0, len(px), step * 4):
        a = px[i + 3]
        if a < 40:
            continue
        key = (px[i] >> 4, px[i + 1] >> 4, px[i + 2] >> 4)
        buckets[key] = buckets.get(key, 0) + 1
    top = sorted(buckets.items(), key=lambda kv: -kv[1])[:5]
    if not top:
        return {"source": "faded", "roles": dict(PAPER_BRAND), "palette": [],
                "notes": ["reference carried no opaque colour worth naming"]}
    cols = [(k[0] * 16 + 8, k[1] * 16 + 8, k[2] * 16 + 8, n) for k, n in top]

    def lum(c):
        return (c[0] * 3 + c[1] * 4 + c[2]) // 10

    def sat(c):
        return max(c[0], c[1], c[2]) - min(c[0], c[1], c[2])
    paper = max(cols, key=lum)[:3]
    press = min(cols, key=lum)[:3]
    vivid = [c for c in cols if sat(c) > 60] or cols
    accent = max(vivid, key=sat)[:3]
    if accent in (paper, press):
        accent = PAPER_BRAND["accent"]
    return {"source": "extracted",
            "roles": {"paper": paper, "press": press, "accent": accent,
                      "gold": PAPER_BRAND["gold"]},
            "palette": [list(c) for c in cols],
            "notes": ["palette extracted from the reference (buckets, not a "
                      "guess); voice and rules remain a human's call"]}


# ————————————————————————————————— the plan ———————————————————————————————————
PLAN_SCHEMA = "paper-studio-plan/1"


def _clean_txt(v, cap):
    if isinstance(v, bool) or not isinstance(v, str):
        return None
    s = " ".join(v.split())
    if not s or len(s) > cap:
        return None
    return s


def _validate_plan(plan, days_n):
    """the schema gate for model words — every field named, bounded, alive.
    None means invalid: the deterministic voice takes the shift."""
    if not isinstance(plan, dict):
        return None
    fields = {}
    for key, cap in (("objective", 140), ("audience", 120), ("core_message", 160)):
        val = _clean_txt(plan.get(key), cap)
        if val is None:
            return None
        fields[key] = val
    days = plan.get("days")
    if not isinstance(days, list) or len(days) != days_n:
        return None
    out = []
    for n, d in enumerate(days, 1):
        if not isinstance(d, dict) or isinstance(d.get("day"), bool) \
                or not isinstance(d.get("day"), int) or d["day"] != n:
            return None
        row = {"day": n, "platform": "X"}
        for key, cap in (("purpose", 40), ("concept", 160), ("caption", 280),
                         ("cta", 80), ("visual", 160)):
            val = _clean_txt(d.get(key), cap)
            if val is None:
                return None
            row[key] = val
        out.append(row)
    concepts = [d["concept"] for d in out]
    if len(set(concepts)) != len(concepts):
        return None                      # no seven variations of one joke
    fields["days"] = out
    return fields


class LlmPlanner:
    """one chat call, one JSON answer, and the schema does the talking.
    a miss is not an error — the deterministic voice answers and says so.
    keys stay on the server; the browser never learns them."""

    def __init__(self, cfg):
        self.base = (cfg.get("baseUrl") or "").rstrip("/")
        self.key = cfg.get("apiKey") or ""
        self.model = cfg.get("model") or "qwen-plus"
        self.timeout = float(cfg.get("timeoutSeconds", 40))

    @property
    def configured(self):
        return bool(self.base and self.key)

    def plan(self, brief, days_n):
        if not self.configured:
            return None
        system = ("Reply with JSON only, no prose, shaped exactly like: "
                  "{objective, audience, core_message, days:[{day, purpose, "
                  "concept, caption, cta, visual}]}. %d days, day numbers "
                  "1..%d, one distinct purpose per day, no concept repeated."
                  % (days_n, days_n))
        user = json.dumps(dict(brief, days=days_n), ensure_ascii=False)
        body = json.dumps({"model": self.model, "temperature": 0.4,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(
            self.base + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            content = payload["choices"][0]["message"]["content"]
            spoken = json.loads(_strip_fences(content))
        except (urllib.error.URLError, OSError, ValueError, KeyError,
                IndexError, TypeError):
            return "unreachable"
        return _validate_plan(spoken, days_n) or "rejected"


def _strip_fences(content):
    s = (content or "").strip()
    if s.startswith("```"):
        s = s.partition("\n")[2].rpartition("```")[0]
    return s.strip()


def deterministic_plan(name, goals, days_n):
    """the reliable voice: a distinct purpose each day, a distinct concept
    per day, and copy that claims nothing the board cannot verify."""
    week_len = len(WEEK)
    days = []
    for n in range(1, days_n + 1):
        lane = WEEK[(n - 1) % week_len]
        lap = (n - 1) // week_len
        concept = lane["pool"][(n - 1 + lap * 2) % len(lane["pool"])]
        days.append({"day": n, "platform": "X",
                     "purpose": (lane["purpose"] if lap == 0
                                 else "%s, take %d" % (lane["purpose"], lap + 1)),
                     "concept": concept,
                     "caption": "Day %d — %s" % (n, concept),
                     "cta": CTAS[(n - 1) % len(CTAS)],
                     "visual": "%s wears the day (the pose follows the purpose)"
                               % lane["purpose"]})
    objective = _clean_txt(goals, 140) if goals else None
    return {"objective": objective or ("%s needs the timeline posting, "
                                        "playing, and coming back" % name)[:140],
            "audience": "the X timeline and the PAPERBOARD board",
            "core_message": "%s — post paper, earn points, climb the front page" % name,
            "days": days}


def _asset_specs(days_n, types, max_assets):
    """the calendar's shape: everything asked for while the cap allows, then
    a rotating lane so a wide campaign never prints one card twice a day."""
    want = [t for t in DAY_KINDS if t in types]
    if days_n * len(want) <= max_assets:
        return {d: list(want) for d in range(1, days_n + 1)}
    return {d: [want[(d - 1) % len(want)]] for d in range(1, days_n + 1)}


# ————————————————————————————————— the desk ———————————————————————————————————
_NAME_FILE = re.compile(r"^s[0-9]{1,9}-a[0-9]{1,9}-[a-z]{2,10}-v[0-9]{1,3}"
                        r"-[0-9a-f]{8}$")
_NAME_REF = re.compile(r"^s[0-9]{1,9}-r-[0-9a-f]{8}$")
_B64_SHAPE = re.compile(r"[A-Za-z0-9+/]+={0,2}\Z")


def _overlay(cfg, pairs):
    """the house's env doctrine: config carries the defaults, the
    environment carries the live word (and every secret). an unset name
    keeps the shipped default; PAPER_STUDIO_IMG=1 opts the hero in."""
    out = dict(cfg)
    for key, name in pairs:
        val = (os.environ.get(name) or "").strip()
        if val:
            out[key] = val
    if (os.environ.get("PAPER_STUDIO_IMG") or "").strip() in ("1", "true", "yes"):
        out["enabled"] = True
    return out


class Studio:
    """the store and the whole door: brief in, plan + rendered files out.
    the bytes ride the data dir (PAPER_STUDIO_DIR, else <db dir>/studio),
    the row is their ledger, and /studio/assets/<name> is the single door
    between them — the name regex is the wall, traversal can not walk in."""

    def __init__(self, db, settings):
        self.db = db
        cfg = settings.pb.get("studio", {})
        self.days_default = int(cfg.get("daysDefault", 7))
        self.days_max = int(cfg.get("daysMax", 14))
        self.max_assets = int(cfg.get("maxAssets", 36))
        self.max_ref_bytes = int(cfg.get("maxRefBytes", 2500000))
        self.list_max = int(cfg.get("listMax", 12))
        env = (os.environ.get("PAPER_STUDIO_DIR") or "").strip()
        self.root = env or os.path.join(os.path.dirname(settings.db_path), "studio")
        os.makedirs(self.root, exist_ok=True)
        self.llm = LlmPlanner(_overlay(cfg.get("llm", {}), (
            ("baseUrl", "PAPER_STUDIO_LLM_BASE_URL"),
            ("apiKey", "PAPER_STUDIO_LLM_API_KEY"),
            ("model", "PAPER_STUDIO_LLM_MODEL"))))
        img = _overlay(cfg.get("image", {}), (
            ("baseUrl", "PAPER_STUDIO_IMG_BASE_URL"),
            ("apiKey", "PAPER_STUDIO_IMG_API_KEY"),
            ("model", "PAPER_STUDIO_IMG_MODEL")))
        self.image_model = img.get("model") or ""
        self.image_base = (img.get("baseUrl") or "").rstrip("/")
        self.image_key = img.get("apiKey") or ""
        self.image_on = bool(img.get("enabled"))
        self.image_timeout = float(img.get("timeoutSeconds", 90))

    # ------------------------------------------------------------ capabilities
    def capabilities(self):
        """what genuinely answers here — no capability is claimed that a
        route does not actually keep."""
        img = ("off" if not (self.image_base and self.image_key)
               else ("on" if self.image_on else "ready, opted out"))
        return {"ok": True, "version": 1, "schema": PLAN_SCHEMA,
                 "planner": (self.llm.model if self.llm.configured
                             else "deterministic"),
                 "llmConfigured": self.llm.configured,
                 "imageGen": img, "imageModel": self.image_model or None,
                 "types": [{"id": k, "use": v["use"], "aspect": v["aspect"],
                            "px": ("%dx%d" % (v["w"], v["h"])) if v["w"] else None}
                           for k, v in ASSET_TYPES.items() if k != "ai_hero"],
                 "limits": {"daysDefault": self.days_default,
                            "daysMax": self.days_max,
                            "maxAssets": self.max_assets,
                            "maxRefBytes": self.max_ref_bytes},
                 "brand": {k: list(v) for k, v in PAPER_BRAND.items()},
                 "note": ("zero keys needed: the deterministic planner and "
                          "the stdlib renderer answer by default; set "
                          "PAPER_STUDIO_LLM_* to let a model speak the plan "
                          "and PAPER_STUDIO_IMG_* (plus the on-flag) for one "
                          "AI hero")}

    # ------------------------------------------------------------------ words
    @staticmethod
    def _slug(name):
        s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
        return (s or "campaign")[:24]

    def _land(self, stem, raw):
        """the artboard's own landing ritual: a part-file, a chmod, a rename."""
        tmp = os.path.join(self.root, stem + ".part")
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.chmod(tmp, 0o644)
            os.replace(tmp, os.path.join(self.root, stem))
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise StudioError("studio_store_failed")

    # ------------------------------------------------------------------- create
    def create(self, body):
        """(view|None, reason|None). one POST per campaign: a model speaks
        the plan only when configured, and a model that speaks out of turn
        falls to the deterministic voice — which says so in the engine."""
        body = body or {}
        name = _clean_txt(body.get("project"), 60)
        if name is None:
            return None, "bad_project"
        brief = _clean_txt(body.get("brief"), 1200)
        if brief is None:
            return None, "bad_brief"
        goals = _clean_txt(body.get("goals"), 400) or ""
        directives = _clean_txt(body.get("directives"), 600) or ""
        days = body.get("days", self.days_default)
        if isinstance(days, bool) or not isinstance(days, int) \
                or not 1 <= days <= self.days_max:
            return None, "bad_days"
        types = body.get("types")
        if not isinstance(types, list) or not types \
                or not all(isinstance(t, str) and t in DAY_KINDS for t in types):
            return None, "bad_types"
        ref_raw = ref_kind = None
        ref_b64 = body.get("ref_b64")
        if isinstance(ref_b64, str) and ref_b64:
            payload = ref_b64.partition(",")[2] or ref_b64
            if _B64_SHAPE.match(payload) is None:
                return None, "bad_image_data"
            try:
                ref_raw = binascii.a2b_base64(payload)
            except binascii.Error:
                return None, "bad_image_data"
            if not ref_raw:
                return None, "bad_image_data"
            if len(ref_raw) > self.max_ref_bytes:
                return None, "too_large"
            ref_kind = sniff(ref_raw)
            if ref_kind is None:
                return None, "bad_image_type"
        try:
            brand = analyze_reference(ref_raw)
        except StudioError as e:
            return None, e.reason
        engine, plan = "deterministic", None
        if self.llm.configured:
            spoken = self.llm.plan({"project": name, "brief": brief,
                                    "goals": goals, "directives": directives},
                                   days)
            if isinstance(spoken, dict):
                plan, engine = spoken, self.llm.model
            elif spoken == "rejected":
                engine = "deterministic (model rejected)"
            elif spoken == "unreachable":
                engine = "deterministic (model unreachable)"
        if plan is None:
            plan = deterministic_plan(name, goals, days)
        t = time.time()
        cur = self.db.conn.execute(
            "INSERT INTO studio_campaigns(name,slug,brief_json,brand_json,"
            "plan_json,qc_json,engine,ref_file,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (name, self._slug(name),
             json.dumps(dict(brief=brief, goals=goals, directives=directives,
                             days=days, types=types,
                             reference=("%s reference kept" % ref_kind)
                             if ref_raw is not None else None)),
             json.dumps(brand), json.dumps(plan), "{}", engine, None, t))
        cid = cur.lastrowid
        self.db.conn.commit()
        if ref_raw is not None:
            stem = "s%d-r-%s.%s" % (cid, secrets.token_hex(4), ref_kind)
            try:
                self._land(stem, ref_raw)
                self.db.conn.execute(
                    "UPDATE studio_campaigns SET ref_file=? WHERE id=?",
                    (stem, cid))
                self.db.conn.commit()
            except StudioError:
                pass                       # a reference that cannot land is
                                           # not a lost campaign
        specs = _asset_specs(days, types, self.max_assets)
        for n in range(1, days + 1):      # the plan grows its own schedule
            plan["days"][n - 1]["aspect"] = \
                ASSET_TYPES[specs[n][0]]["aspect"] if specs.get(n) else "16:9"
        plan["sources"] = (["reference image"] if ref_raw is not None else []) \
                          + ["brief", "brand defaults"]
        plan["schedule_note"] = ("every asked kind each day"
                                 if all(len(v) == len(types) for v in specs.values())
                                 else "cap reached: kinds rotate across days")
        self.db.conn.execute("UPDATE studio_campaigns SET plan_json=? WHERE id=?",
                             (json.dumps(plan), cid))
        self.db.conn.commit()
        self._render_campaign(cid, name, plan, brand, specs, t)
        note = self._maybe_hero(cid, name, plan, t)
        qc = self._qc(cid, plan, note)
        self.db.conn.execute("UPDATE studio_campaigns SET qc_json=? WHERE id=?",
                             (json.dumps(qc), cid))
        self.db.conn.commit()
        return self.view(cid), None

    # ------------------------------------------------------------------ the pen
    def _pose(self, name):
        if name not in _POSE_CACHE:
            path = os.path.join(SITE_DIR, "public", "assets",
                                "pose-%s.png" % name)
            try:
                with open(path, "rb") as fh:
                    _POSE_CACHE[name] = png_read(fh.read())
            except (OSError, StudioError):
                _POSE_CACHE[name] = None
        return _POSE_CACHE[name]

    def _render(self, name, day, kind, drow, brand, variant):
        """one card, drawn. returns (png|None, reason|None, texts)."""
        roles = brand["roles"]
        paper = tuple(roles["paper"])
        press = tuple(roles["press"])
        accent = tuple(roles["accent"])
        gold = tuple(roles.get("gold") or PAPER_BRAND["gold"])
        head, cap, cta = drow["concept"], drow["caption"], drow["cta"]
        purpose = drow["purpose"].split(",")[0]
        punch = MEME_PUNCH[(day - 1 + variant) % len(MEME_PUNCH)]
        poses = POSE_BY_PURPOSE.get(purpose, POSE_BY_PURPOSE["the ask"])
        pose = self._pose(poses[variant % len(poses)])
        texts = dict(head=head, cap=cap, cta=cta)
        spec = ASSET_TYPES[kind]
        W, H = spec["w"], spec["h"]
        m = max(40, int(W * 0.06))
        v = variant % 2
        cv = Canvas(W, H, press)
        if kind == "xpost":
            cv.rect(0, 48, W, 6, accent)
            kick = name.upper()[:36]
            if v == 0:
                cv.text(m, 92, kick, accent, 3)
                cv.text(m, 210, head.upper(), paper, 7, "left", W - 2 * m, 3)
                cv.text(m, H - 230, cap.upper(), paper, 3, "left",
                        int(W * 0.54), 2, dim=True)
                cv.text(m, H - 158, cta.upper(), accent, 3)
                cv.text(m, H - 96, "$PAPER · SOLANA", paper, 2, "left", dim=True)
            else:
                cv.text(W - m - text_measure(_fold(kick), 3), 92, kick, accent,
                        3, "right")
                cv.text(m, 210, head.upper(), paper, 7, "left", W - 2 * m, 3)
                cv.text(m, H - 230, cap.upper(), paper, 3, "left",
                        int(W * 0.48), 2, dim=True)
                cv.text(m, H - 122, ("DAY %02d" % day).upper(), paper, 3)
                cv.text(W - m - text_measure(_fold(cta.upper()), 3), H - 122,
                        cta.upper(), accent, 3, "right", W - m)
            if pose:
                ph = 430 if v == 0 else 400
                pw = int(pose[0] * ph / pose[1])
                px = (W - m - pw) if v == 0 else m
                cv.paste(pose, px, H - 76 - ph, th=ph)
        elif kind == "meme":
            cv = Canvas(W, H, paper)
            cv.text(0, 46, ("PAPERBOARD DAY %d" % day).upper(), accent, 2,
                    "center", W, 1)
            cv.text(0, 96, cap.upper(), press, 4, "center", W - 2 * m, 2)
            if pose:
                ph = 500 if v == 0 else 460
                pw = int(pose[0] * ph / pose[1])
                cv.paste(pose, (W - pw) // 2, (190 if v == 0 else 170), th=ph)
            cv.text(0, H - (150 if v == 0 else 180), punch.upper(), press, 4,
                    "center", W - 2 * m, 1)
        elif kind == "promo":
            cv = Canvas(W, H, press)
            cv.rect(W - 240, 90, 210, 5, gold)
            cv.text(W - 240, 116, ("DAY %d" % day).upper(), gold, 3)
            cv.text(m, 90, name.upper()[:36], accent, 3)
            if pose:
                cv.paste(pose, m, 240, th=500)
            cv.text(m, 810, head.upper(), paper, 5, "left", W - 2 * m, 3)
            cv.text(m, 1070, cap.upper(), paper, 3, "left", W - 2 * m, 3, dim=True)
            for k in range(3):
                cv.rect(m, H - 190 + k * 36, W - 2 * m, 5, accent)
        elif kind == "banner":
            plate_w = int(W * 0.42)
            plate_x = (W - plate_w if v == 0 else 0)
            cv = Canvas(W, H, press)
            cv.rect(plate_x, 0, plate_w, H, paper)
            if pose:
                ph = 430
                pw = int(pose[0] * ph / pose[1])
                cv.paste(pose, plate_x + (plate_w - pw) // 2, H - 60 - ph, th=ph)
            tx = m if v == 0 else (W - plate_w + m)
            twid = (W - plate_w - 2 * m)
            cv.text(tx, 84, name.upper()[:36], accent, 3)
            cv.text(tx, 150, head.upper(), paper, 6, "left", twid, 3)
            cv.text(tx, 330, cap.upper(), paper, 3, "left", twid, 2, dim=True)
            cv.text(tx, H - 112, cta.upper(), accent, 3)
        elif kind == "announce":
            cv = Canvas(W, H, paper)
            cv.rect(0, 0, W, 96, accent)
            cv.text(0, 30, "PAPERBOARD · THE WEEK", press, 3, "center", W, 1)
            cv.text(0, 180, head.upper(), press, 5, "center", W - 2 * m, 3)
            cv.text(0, 350, cap.upper(), press, 3, "center", W - 2 * m, 3, dim=True)
            for k in range(3):
                cv.rect(W - m - 320, 480 + k * 62, 320, 6, press)
            cv.text(m, H - 130, "$PAPER · SOLANA", press, 3)
        else:
            return None, "bad_types", texts
        return cv.to_png(), None, texts

    def _render_campaign(self, cid, name, plan, brand, specs, t):
        """all the cards, in day order. one broken card is that card's note,
        never a lost campaign: the rows that did render ride on."""
        for n in range(1, plan_days_len(plan) + 1):
            drow = plan["days"][n - 1]
            for kind in specs.get(n, []):
                spec = ASSET_TYPES[kind]
                png, why, texts = self._render(name, n, kind, drow, brand, 0)
                if png is None:
                    self._insert_asset(cid, n, kind, spec, None, 0, 0, 0, texts,
                                       "failed", why, 0, t)
                    continue
                stem = "s%d-a%d-%s-v0-%s.png" % (cid, n, kind,
                                                 secrets.token_hex(4))
                try:
                    self._land(stem, png)
                except StudioError as e:
                    self._insert_asset(cid, n, kind, spec, None, spec["w"],
                                       spec["h"], 0, texts, "failed", e.reason,
                                       0, t)
                    continue
                dims = png_dims(png) or (spec["w"], spec["h"])
                self._insert_asset(cid, n, kind, spec, stem, dims[0], dims[1],
                                   len(png), texts, "ready", "", 0, t)

    def _insert_asset(self, cid, day, kind, spec, stem, w, h, nbytes, texts,
                      status, note, variant, t):
        self.db.conn.execute(
            "INSERT INTO studio_assets(campaign_id,day,kind,use,width,height,"
            "file,bytes,text_json,status,note,variant,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, day, kind, spec["use"], w, h, stem, nbytes, json.dumps(texts),
             status, note, variant, t))
        self.db.conn.commit()

    def _maybe_hero(self, cid, name, plan, t):
        """the optional image-model hero: the base, the key, and the explicit
        on-switch must all agree before a call is made. its failure is one
        asset's note, and the deterministic cards already stand."""
        if not (self.image_on and self.image_base and self.image_key):
            return None
        body = json.dumps({"model": self.image_model, "size": "1024x1024",
                           "messages": [{"role": "user",
                                         "content": "%s: %s" % (name, plan["days"][0]["concept"])}]}).encode()
        req = urllib.request.Request(
            self.image_base + "/images/generations", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.key_or_llm()})
        raw = None
        try:
            with urllib.request.urlopen(req, timeout=self.image_timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            item = payload["data"][0]
            if item.get("b64_json"):
                raw = binascii.a2b_base64(item["b64_json"])
            elif item.get("url"):
                with urllib.request.urlopen(item["url"],
                                            timeout=self.image_timeout) as r2:
                    raw = r2.read()
        except (urllib.error.URLError, OSError, ValueError, KeyError,
                IndexError, TypeError, binascii.Error):
            raw = None
        if raw is None:
            return "image hero: the model did not answer"
        kind = sniff(raw)
        if kind is None:
            return "image hero: the model sent bytes that are not an image"
        dims = img_dims(raw) or (0, 0)
        stem = "s%d-a0-aihero-v0-%s.%s" % (cid, secrets.token_hex(4), kind)
        try:
            self._land(stem, raw)
        except StudioError:
            return "image hero: the bytes would not land"
        ext = kind
        self.db.conn.execute(
            "INSERT INTO studio_assets(campaign_id,day,kind,use,width,height,"
            "file,bytes,text_json,status,note,variant,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, 1, "ai_hero", ASSET_TYPES["ai_hero"]["use"], dims[0], dims[1],
             stem, len(raw), json.dumps({"head": plan["days"][0]["concept"]}),
             "ready", "model-painted", 0, t))
        self.db.conn.commit()
        return "image hero: model-painted (%s)" % ext

    def key_or_llm(self):
        return self.image_key or self.llm.key

    # ---------------------------------------------------------------------- qc
    def _qc(self, cid, plan, hero_note):
        """the machines that read what shipped: dimensions come from the
        written header (png) or the file's own report (hero), the concepts
        must be as many as the days, and a file counts only while it is on
        the disk under its real name."""
        rows = self.db.conn.execute(
            "SELECT day, kind, width, height, file, bytes, status FROM"
            " studio_assets WHERE campaign_id=?", (cid,)).fetchall()
        total = ready = dim_ok = failed = 0
        for r in rows:
            total += 1
            on_disk = bool(r["file"]) and os.path.isfile(os.path.join(self.root,
                                                                      r["file"]))
            if r["status"] == "ready" and on_disk:
                ready += 1
                if r["kind"] == "ai_hero":
                    dim_ok += 1 if r["width"] > 0 else 0
                else:
                    spec = ASSET_TYPES.get(r["kind"], {})
                    if spec and (r["width"], r["height"]) == (spec["w"], spec["h"]):
                        dim_ok += 1
            else:
                failed += 1
        concepts = [d["concept"] for d in plan["days"]]
        text_ok = all(_clean_txt(d.get("caption"), 400) and
                      _clean_txt(d.get("cta"), 100) for d in plan["days"])
        return {"assets_total": total, "assets_ready": ready,
                "assets_failed": failed, "sizes_verified": dim_ok,
                "days": len(plan["days"]),
                "concepts_distinct": len(set(concepts)),
                "text_ok": bool(text_ok),
                "notes": ([hero_note] if hero_note else [])
                         + ([] if text_ok else
                            ["a caption or a call to action came back empty"])}

    # -------------------------------------------------------------------- read
    def _assets(self, cid):
        out = []
        for r in self.db.conn.execute(
                "SELECT id, day, kind, use, width, height, file, bytes,"
                " text_json, status, note, variant, created_at FROM studio_assets"
                " WHERE campaign_id=? ORDER BY day, id", (cid,)):
            d = dict(r)
            d["text"] = json.loads(d.pop("text_json") or "{}")
            d["href"] = ("/studio/assets/" + d["file"]) if d["file"] else None
            out.append(d)
        return out

    def view(self, cid):
        r = self.db.conn.execute(
            "SELECT id, name, slug, brief_json, brand_json, plan_json,"
            " qc_json, engine, ref_file, created_at FROM studio_campaigns"
            " WHERE id=?", (cid,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["brief"] = json.loads(d.pop("brief_json") or "{}")
        brand = json.loads(d.pop("brand_json") or "{}")
        d["brand"] = brand
        d["plan"] = json.loads(d.pop("plan_json") or "{}")
        d["qc"] = json.loads(d.pop("qc_json") or "{}")
        d["assets"] = self._assets(cid)
        if d["ref_file"]:
            d["ref_href"] = "/studio/assets/" + d["ref_file"]
        return d

    def list(self):
        out = []
        for r in self.db.conn.execute(
                "SELECT id, name, engine, created_at FROM studio_campaigns"
                " ORDER BY id DESC LIMIT ?", (self.list_max,)):
            n = self.db.conn.execute(
                "SELECT COUNT(*) FROM studio_assets WHERE campaign_id=?",
                (r["id"],)).fetchone()[0]
            out.append(dict(id=r["id"], name=r["name"], engine=r["engine"],
                            created_at=r["created_at"], assets=n))
        return out

    # -------------------------------------------------------------- regenerate
    def regenerate(self, campaign_id, asset_id):
        """one card answers for itself: the lane it rides gives it a fresh
        take (a rotated concept, a rotated pose, a flipped layout), and every
        other card keeps its bytes, its time, and its word. the campaign view
        rides back so the shelf can repaint from one answer."""
        a = self.db.conn.execute(
            "SELECT id, campaign_id, day, kind, variant, status FROM studio_assets"
            " WHERE id=?", (asset_id,)).fetchone()
        if a is None or a["campaign_id"] != campaign_id:
            return None, None, "asset_absent"
        c = self.db.conn.execute(
            "SELECT id, name, plan_json, brand_json FROM studio_campaigns"
            " WHERE id=?", (campaign_id,)).fetchone()
        if c is None:
            return None, None, "campaign_absent"
        plan = json.loads(c["plan_json"])
        brand = json.loads(c["brand_json"])
        drow = dict(plan["days"][a["day"] - 1])
        lane = WEEK[(a["day"] - 1) % len(WEEK)]
        new_var = a["variant"] + 1
        drow["concept"] = lane["pool"][(a["day"] - 1 + 3 * new_var)
                                       % len(lane["pool"])]
        drow["caption"] = "Day %d — %s" % (a["day"], drow["concept"])
        spec = ASSET_TYPES[a["kind"]]
        png, why, texts = self._render(c["name"], a["day"], a["kind"], drow,
                                       brand, new_var)
        t = time.time()
        if png is None:
            self.db.conn.execute(
                "UPDATE studio_assets SET status='failed', note=?, variant=?,"
                " created_at=? WHERE id=?", (why, new_var, t, asset_id))
        else:
            stem = "s%d-a%d-%s-v%d-%s.png" % (campaign_id, a["day"], a["kind"],
                                              new_var, secrets.token_hex(4))
            try:
                self._land(stem, png)
            except StudioError as e:
                self.db.conn.execute(
                    "UPDATE studio_assets SET status='failed', note=?, variant=?,"
                    " created_at=? WHERE id=?", (e.reason, new_var, t, asset_id))
                self.db.conn.commit()
                return self.view(campaign_id), new_var, None
            dims = png_dims(png) or (spec["w"], spec["h"])
            self.db.conn.execute(
                "UPDATE studio_assets SET file=?, bytes=?, status='ready', note='',"
                " variant=?, created_at=?, text_json=?, width=?, height=?"
                " WHERE id=?",
                (stem, len(png), new_var, t, json.dumps(texts), dims[0], dims[1],
                 asset_id))
        self.db.conn.commit()
        qc = self._qc(campaign_id, plan, None)
        self.db.conn.execute("UPDATE studio_campaigns SET qc_json=? WHERE id=?",
                             (json.dumps(qc), campaign_id))
        self.db.conn.commit()
        return self.view(campaign_id), new_var, None

    # ------------------------------------------------------------------ export
    def export(self, cid):
        """the ZIP is the campaign's receipt. the manifest is written from
        the names actually added, so it can not list a file the archive
        never met; the calendar rides the plan's own days."""
        c = self.db.conn.execute(
            "SELECT id, name, slug, plan_json, brand_json FROM studio_campaigns"
            " WHERE id=?", (cid,)).fetchone()
        if c is None:
            return None, None, None
        plan = json.loads(c["plan_json"])
        rows = [dict(r) for r in self.db.conn.execute(
            "SELECT day, kind, width, height, file, bytes, status FROM"
            " studio_assets WHERE campaign_id=? ORDER BY day, id", (cid,))]
        root = "%s-%d" % (c["slug"], cid)
        buf = io.BytesIO()
        manifest = []
        zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
        try:
            def put(name, payload):
                zf.writestr(zipfile.ZipInfo("%s/%s" % (root, name)), payload)
                manifest.append(name)
            put("campaign-plan.json",
                json.dumps(plan, indent=1).encode())
            put("brand-profile.json",
                json.dumps(json.loads(c["brand_json"]), indent=1,
                           ensure_ascii=False).encode())
            lines = ["day,purpose,concept,caption,cta,platform,aspect,asset_files"]
            for d in plan["days"]:
                files = ";".join(sorted({r["file"] for r in rows
                                         if r["day"] == d["day"] and r["file"]}))
                lines.append("%d,%s,%s,%s,%s,%s,%s,%s" % (
                    d["day"], _csv(d["purpose"]), _csv(d["concept"]),
                    _csv(d["caption"]), _csv(d["cta"]), d.get("platform") or "X",
                    _csv(d.get("aspect") or "16:9"), _csv(files)))
            put("campaign-calendar.csv", ("\r\n".join(lines) + "\r\n").encode())
            for d in plan["days"]:
                put("copy/day-%02d.md" % d["day"],
                    ("# day %d — %s\n\n%s\n\n%s\n"
                     % (d["day"], d["purpose"], d["caption"], d["cta"])).encode())
            for r in rows:
                if not r["file"] or r["status"] != "ready":
                    continue
                try:
                    with open(os.path.join(self.root, r["file"]), "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue           # a file that vanished is not in, not listed
                zf.writestr(zipfile.ZipInfo("%s/assets/%s" % (root, r["file"])),
                            raw)
                manifest.append("assets/" + r["file"])
            put("README.md", ("# %s — a paper studio campaign\n\n%d days, %d"
                              " named files in the archive, one plan behind"
                              " them.\n"
                              % (c["name"], len(plan["days"]),
                                 len(manifest))).encode())
            put("manifest.json", json.dumps(manifest, indent=1).encode())
        finally:
            zf.close()
        check = zipfile.ZipFile(io.BytesIO(buf.getvalue()))   # it must extract
        names = check.namelist()
        check.close()
        return buf.getvalue(), "studio-%d-%s.zip" % (cid, c["slug"]), names

    # -------------------------------------------------------------- file door
    def served(self, name):
        """(bytes, mime) for GET /studio/assets/<name>, walls first: one
        dot, an allowed image extension, and a name the ledger itself
        signed. a stranger's path shape never reaches the disk."""
        if not name or "/" in name or "\\" in name or ".." in name \
                or name.count(".") != 1:
            return None
        base, _, ext = name.rpartition(".")
        ext = ext.lower()
        if ext not in ("png", "jpg", "gif", "webp"):
            return None
        if _NAME_FILE.match(base) is None and _NAME_REF.match(base) is None:
            return None
        full = os.path.abspath(os.path.join(self.root, name))
        if not full.startswith(os.path.abspath(self.root) + os.sep):
            return None
        try:
            with open(full, "rb") as fh:
                return fh.read(), {"png": "image/png", "jpg": "image/jpeg",
                                   "gif": "image/gif",
                                   "webp": "image/webp"}[ext]
        except OSError:
            return None

    def count(self):
        return self.db.conn.execute(
            "SELECT COUNT(*) FROM studio_campaigns").fetchone()[0]


def plan_days_len(plan):
    return len(plan.get("days") or [])


def _csv(s):
    s = (s or "").replace('"', "'")
    return '"%s"' % s if "," in s else s
