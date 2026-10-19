#!/usr/local/bin/python3
"""world_atlas_cut — the one cut that made js/world-land.js.

the map on the board wants real coastlines, not hand-drawn blobs, and it
wants them without a tile server, a key, or a fetch race. this tool is the
build step that answers that: Natural Earth's 1:110 m land mass (public
domain; the world-atlas TopoJSON wrapper is ISC) is decoded here, rounded to
two decimals (a hairline map cannot tell one kilometre from the next), and
written as a GeoJSON MultiPolygon for js/world-land.js. the runtime imports
that module — zero network, zero decode at load, and the battery reads the
same text back to prove the geometry is still the one that shipped.

usage (from server/):  python3 tools/world_atlas_cut.py ../land-110m.json
"""

import json
import os
import sys
import urllib.request

SRC = ("https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/land-110m.json"
       "  (ISC wrapper, Natural Earth data: public domain)")


def _flat(x):
    """every arc index in a ring, in order, however deeply nested — the
    TopoJSON spec lets a ring be one index or a chain of them."""
    if isinstance(x, int):
        return [x]
    out = []
    for part in x:
        out.extend(_flat(part))
    return out


def to_geojson(topo):
    tr = topo["transform"]
    sx, sy = tr["scale"]
    tx, ty = tr["translate"]

    def arc(i):
        x = y = 0
        pts = []
        for dx, dy in topo["arcs"][i if i >= 0 else ~i]:
            x += dx
            y += dy
            pts.append((round(x * sx + tx, 2), round(y * sy + ty, 2)))
        return pts if i >= 0 else pts[::-1]

    polys = []
    for geom in topo["objects"]["land"]["geometries"]:
        for poly in geom["arcs"]:            # a MultiPolygon's polygons
            rings = []
            for ring in poly:                # each ring, arcs flattened
                stitched = []
                for ref in _flat(ring):
                    seg = arc(ref)
                    stitched.extend(seg if not stitched else seg[1:])
                rings.append(stitched)
            polys.append(rings)
    return {"type": "MultiPolygon", "coordinates": polys}


def main(argv):
    src = argv[1] if len(argv) > 1 else None
    if src is None:
        with urllib.request.urlopen(SRC.split()[0], timeout=30) as resp:
            topo = json.loads(resp.read().decode("utf-8"))
    elif src == "-":
        topo = json.load(sys.stdin)
    else:
        with open(src, "rb") as fh:
            topo = json.load(fh)
    gj = to_geojson(topo)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = os.path.join(root, "js", "world-land.js")
    body = json.dumps(gj["coordinates"], ensure_ascii=False,
                      separators=(", ", ": "))
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("/* world-land — the world's coasts, 1:110 m, two decimals.\n"
                 "   %s\n"
                 "   made by server/tools/world_atlas_cut.py; the map draws\n"
                 "   this and nothing else for land. */\n"
                 "export const WORLD_LAND = {\"type\": \"MultiPolygon\", "
                 "\"coordinates\": %s};\n" % (SRC, body))
    rings = sum(len(r) for p in gj["coordinates"] for r in p)
    print("wrote", out, "—", len(gj["coordinates"]), "polygons,", rings, "points")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
