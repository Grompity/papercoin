#!/usr/bin/env python3
# pose_alpha.py IN.png OUT.png — turns the white ground of a pose render into
# real transparency: flood-fills border-connected near-white pixels (interior
# whites like shoe stripes survive), softens the rim one pass, writes RGBA PNG.
# Pure stdlib — the machine has no PIL and the CLT's Swift bindings are a maze.
import struct, sys, zlib
from collections import deque

def png_read(path):
    b = open(path, "rb").read()
    assert b[:8] == b"\x89PNG\r\n\x1a\n", "not a png"
    i, idat, ihdr = 8, b"", None
    while i < len(b):
        (ln,) = struct.unpack(">I", b[i:i+4]); typ = b[i+4:i+8]; data = b[i+8:i+8+ln]
        if typ == b"IHDR": ihdr = struct.unpack(">IIBBBBB", data)
        elif typ == b"IDAT": idat += data
        elif typ == b"IEND": break
        i += 12 + ln
    w, h, depth, ct, comp, filt, inter = ihdr
    assert depth == 8 and inter == 0 and ct in (0, 2, 4, 6), f"unsupported png ({ct}/{depth}/{inter})"
    nch = {0: 1, 2: 3, 4: 2, 6: 4}[ct]
    raw = zlib.decompress(idat)
    stride = w * nch
    px = bytearray(w * h * 4)                      # always RGBA out
    out_rows, prev = bytearray(stride), bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]; p += 1
        row = bytearray(raw[p:p+stride]); p += stride
        if f:                                       # unfilter the scanline
            pp = prev
            if f == 1:                              # Sub
                for x in range(nch, stride): row[x] = (row[x] + row[x-nch]) & 0xff
            elif f == 2:                            # Up
                for x in range(stride): row[x] = (row[x] + pp[x]) & 0xff
            elif f == 3:                            # Average
                for x in range(stride):
                    a = row[x-nch] if x >= nch else 0
                    row[x] = (row[x] + ((a + pp[x]) >> 1)) & 0xff
            elif f == 4:                            # Paeth (spec order, no shortcuts)
                for x in range(stride):
                    a = row[x-nch] if x >= nch else 0
                    c = pp[x-nch] if x >= nch else 0
                    b_ = pp[x]
                    pr0 = a + b_ - c
                    pa, pb, pc = abs(pr0 - a), abs(pr0 - b_), abs(pr0 - c)
                    pr = a if (pa <= pb and pa <= pc) else (b_ if pb <= pc else c)
                    row[x] = (row[x] + pr) & 0xff
            else: raise SystemExit("bad filter")
        o = y * w * 4
        for x in range(w):
            s = x * nch
            if nch == 1: r = g = bl = row[s]; a = 255
            elif nch == 2: r = g = bl = row[s]; a = row[s+1]
            elif nch == 3: r, g, bl, a = row[s], row[s+1], row[s+2], 255
            else: r, g, bl, a = row[s], row[s+1], row[s+2], row[s+3]
            px[o+x*4:o+x*4+4] = bytes((r, g, bl, a))
        prev = row
    return w, h, px

def encode_png(path_out, w, h, px):
    rows = b"".join(b"\0" + bytes(px[y*w*4:(y+1)*w*4]) for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    open(path_out, "wb").write(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))

def cutout(path_in, path_out):
    w, h, px = png_read(path_in)
    bg = bytearray(w * h)
    q = deque()
    THR = 228
    def near(i):
        o = i * 4
        return px[o] >= THR and px[o+1] >= THR and px[o+2] >= THR
    def push(x, y):
        i = y * w + x
        if not bg[i] and near(i): bg[i] = 1; q.append(i)
    for x in range(w): push(x, 0); push(x, h-1)
    for y in range(1, h-1): push(0, y); push(w-1, y)
    while q:
        i = q.popleft(); x, y = i % w, i // w
        if x > 0     and not bg[i-1] and near(i-1): bg[i-1] = 1; q.append(i-1)
        if x < w-1   and not bg[i+1] and near(i+1): bg[i+1] = 1; q.append(i+1)
        if y > 0     and not bg[i-w] and near(i-w): bg[i-w] = 1; q.append(i-w)
        if y < h-1   and not bg[i+w] and near(i+w): bg[i+w] = 1; q.append(i+w)
    # soften the rim: opaque counts 512, background counts 85, over the 5-cell
    for i in range(w*h):
        if bg[i]: px[i*4+3] = 0; continue
        x, y = i % w, i // w
        if (not x or x == w-1 or not y or y == h-1 or bg[i-1] + bg[i+1] + bg[i-w] + bg[i+w] == 0):
            px[i*4+3] = 255; continue
        acc = 512; n = 1
        for j in (i-1, i+1, i-w, i+w): acc += 85 if bg[j] else 512; n += 1
        px[i*4+3] = min(255, acc // (n * 2))
    # encode (straight alpha, one pass, filter 0)
    encode_png(path_out, w, h, px)
    print(f"cut {w}x{h} -> {path_out}")

NAMES = [("front", 0, 0), ("celebrate", 0, 1), ("running", 0, 2),
         ("cool", 1, 0), ("victory", 1, 1), ("silly", 1, 2)]

def slice_sheet(path_in, outdir, pad=16):
    """The 2026-10 founder sheet: three poses wide, two deep, transparent, and the
    rows interlock (a jumping foot sits beside a hat brim), so the slicer must
    work per connected component, not per grid cell: label every opaque blob,
    let the two biggest blobs in each column be the two characters, then hand
    every small island (bills, smoke, brims, shoes) to the character whose
    bounding box is nearest. Nothing ever touches a slice edge."""
    w, h, px = png_read(path_in)
    cols = [(120, 464), (512, 1008), (1056, 1488)]          # measured gutter bounds
    col_of = [0]*(w*h)
    for ci, (x0, x1) in enumerate(cols):
        for x in range(x0, x1):
            for y in range(h):
                col_of[y*w + x] = ci + 1
    # ---- label 8-connected opaque blobs ----
    alpha = px[3::4]
    seen = bytearray(w*h)
    lab = bytearray(w*h)                                # component id per pixel
    labels = []                                            # (col, minx,maxx,miny,maxy,count)
    q = deque()
    for i0 in range(w*h):
        if seen[i0] or alpha[i0] <= 40 or col_of[i0] == 0: continue
        ci = col_of[i0]
        lid = len(labels) + 1
        seen[i0] = 1; lab[i0] = lid
        q.clear(); q.append(i0)
        minx = maxx = i0 % w; miny = maxy = i0 // w; n = 0
        while q:
            i = q.popleft(); n += 1
            lab[i] = lid
            x, y = i % w, i // w
            if x < minx: minx = x
            if x > maxx: maxx = x
            if y < miny: miny = y
            if y > maxy: maxy = y
            for dy in (-1, 0, 1):
                ny = y + dy
                if ny < 0 or ny >= h: continue
                base = ny*w
                for dx in (-1, 0, 1):
                    nx = x + dx
                    if nx < 0 or nx >= w or col_of[base+nx] != ci: continue
                    j = base + nx
                    if not seen[j] and alpha[j] > 40:
                        seen[j] = 1; q.append(j)
        labels.append((ci, minx, maxx, miny, maxy, n))
    # ---- two biggest per column = the characters ----
    owners = {}
    for ci in (1, 2, 3):
        big = sorted((l for l in labels if l[0] == ci), key=lambda l: -l[5])
        b0 = big[0]
        c0 = (b0[3] + b0[4]) / 2
        far = [b for b in big[1:] if abs((b[3]+b[4])/2 - c0) > 250]
        b1 = far[0] if far else (big[1] if len(big) > 1 else b0)
        pair = sorted([b0, b1], key=lambda l: (l[3]+l[4])/2)   # top = row0
        owners[(ci-1, 0)] = (pair[0][1], pair[0][2], pair[0][3], pair[0][4])
        owners[(ci-1, 1)] = (pair[1][1], pair[1][2], pair[1][3], pair[1][4])
    # ---- assign every blob to the nearest character bbox center ----
    union = {k: [1 << 30, -1, 1 << 30, -1] for k in owners} # minx maxx miny maxy
    own = bytearray(len(labels) + 1)                       # label id -> owner code
    for li, (ci, mnx, mxx, mny, mxy, n) in enumerate(labels):
        cx, cy = (mnx+mxx)/2, (mny+mxy)/2
        r0, r1 = owners[(ci-1, 0)], owners[(ci-1, 1)]
        d0 = abs(cx - (r0[0]+r0[1])/2) + abs(cy - (r0[2]+r0[3])/2)
        d1 = abs(cx - (r1[0]+r1[1])/2) + abs(cy - (r1[2]+r1[3])/2)
        row = 0 if d0 <= d1 else 1
        key = (ci-1, row)
        own[li + 1] = (ci-1)*2 + row + 1
        u = union[key]
        u[0] = min(u[0], mnx); u[1] = max(u[1], mxx)
        u[2] = min(u[2], mny); u[3] = max(u[3], mxy)
    # ---- crop each union, painting only that character's components ----
    for name, r, c in NAMES:
        u = union[(c, r)]
        me = c*2 + r + 1
        bx0 = max(cols[c][0] - 40, u[0] - pad); bx1 = min(cols[c][1] + 40, u[1] + 1 + pad)
        by0 = max(0, u[2] - pad); by1 = min(h, u[3] + 1 + pad)
        cw, ch = bx1 - bx0, by1 - by0
        out = bytearray(cw * ch * 4)
        for y in range(ch):
            base = (by0 + y)*w
            d = y*cw*4
            for x in range(cw):
                idx = base + bx0 + x
                if own[lab[idx]] == me:
                    s2 = idx*4
                    out[d + x*4: d + x*4 + 4] = px[s2:s2+4]
        encode_png(f"{outdir}/pose-{name}.png", cw, ch, out)
        print(f"sliced {name} {cw}x{ch}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sheet":
        slice_sheet(sys.argv[2], sys.argv[3])
    else:
        cutout(sys.argv[1], sys.argv[2])
