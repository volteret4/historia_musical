#!/usr/bin/env python3
"""
Build genretree.html — Music Genre Tree visualization.

Fetches genre data from musicgenretree.org/chart.html (cached locally as
.chart_cache.html) and matches albums from a local SQLite DB.

Usage:
    python build.py                    # uses musthear.db in current dir
    python build.py path/to/db.db      # explicit DB path
    python build.py --no-db            # skip DB, only genre structure

Output: genretree.html
"""
import sys, sqlite3, json, re
from pathlib import Path
import urllib.request

# ── config ──────────────────────────────────────────────────────────────────
args     = sys.argv[1:]
NO_DB    = "--no-db" in args
_DB_DEFAULT = Path.home() / "gits/pollo/rym_lastfm/db/must_hear_rym_new.db"
DB_PATH  = Path(next((a for a in args if not a.startswith("-")), _DB_DEFAULT))
CHART_URL   = "https://www.musicgenretree.org/chart.html"
CHART_CACHE = Path(".chart_cache.html")
OUTPUT      = Path("genretree.html")
NODE_W, NODE_H = 220, 100   # node dimensions in px

# Only keep genres from these 7 trees (matching the PNG URLs the user provided)
TARGET_CATS = {
    "ROCK",
    "EXPERIMENTAL",
    "ELECTRONIC DANCE",
    "HIP HOP",
    "RHYTHM & BLUES",
    "JAZZ",
    "WESTERN CLASSICAL",
}


# ── fetch chart.html ─────────────────────────────────────────────────────────
def fetch_chart() -> str:
    if CHART_CACHE.exists():
        print(f"Using cached {CHART_CACHE}")
        return CHART_CACHE.read_text(encoding="utf-8", errors="replace")
    print(f"Fetching {CHART_URL} …")
    req = urllib.request.Request(CHART_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    CHART_CACHE.write_bytes(data)
    return data.decode("utf-8", errors="replace")


# ── parse <area> tags ────────────────────────────────────────────────────────
def parse_genres(html: str) -> list[dict]:
    genres = []
    for m in re.finditer(r"<area\b([^>]*?)/?>\s*", html, re.IGNORECASE | re.DOTALL):
        raw = m.group(1)

        def attr(name: str) -> str:
            am = re.search(rf'\b{name}=["\']([^"\']*)["\']', raw, re.IGNORECASE)
            return am.group(1) if am else ""

        title = attr("title")
        if not title:
            continue
        parts = [p.strip() for p in title.split("|")]
        if len(parts) < 3:
            continue

        coords = attr("coords")
        cx, cy = 0, 0
        if coords:
            nums = [int(v) for v in re.split(r"[,\s]+", coords) if re.match(r"-?\d+$", v)]
            if len(nums) >= 4:
                cx = (nums[0] + nums[2]) // 2
                cy = (nums[1] + nums[3]) // 2

        seq_id = attr("id")
        genres.append({
            "id":      int(seq_id) if seq_id.isdigit() else len(genres),
            "number":  parts[0],
            "genre":   parts[1],
            "rec":     parts[2],
            "cat":     parts[3] if len(parts) > 3 else "",
            "fam":     parts[4] if len(parts) > 4 else "",
            "yt_href": attr("href"),
            "cx": cx,
            "cy": cy,
        })

    print(f"Parsed {len(genres)} genres from chart.html")
    return genres


# ── load albums from DB ──────────────────────────────────────────────────────
def load_albums(db_path: Path) -> dict:
    if not db_path.exists():
        print(f"DB not found at {db_path}, skipping album data.")
        return {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Locate the MusicBrainz series collection
    cur.execute("SELECT id FROM collections WHERE source_url LIKE '%8902a102%' LIMIT 1")
    row = cur.fetchone()
    if not row:
        cur.execute("""
            SELECT id FROM collections
            WHERE name LIKE '%genre%' OR name LIKE '%musthear%' OR name LIKE '%canonical%'
            LIMIT 1
        """)
        row = cur.fetchone()
    if not row:
        print("Collection not found in DB — no album data will be embedded.")
        conn.close()
        return {}

    col_id = row[0]
    cur.execute("""
        SELECT
            ca.rank,
            al.name         AS album_name,
            al.year,
            al.youtube_url, al.spotify_url, al.lastfm_url,
            al.musicbrainz_url, al.discogs_url, al.bandcamp_url,
            al.rateyourmusic_url, al.allmusic_url,
            al.cover_url,
            al.scaruffi_rating, al.aoty_user_score, al.metacritic_score,
            ar.name         AS artist_name,
            ar.country,
            ar.img_url      AS artist_img,
            ar.lastfm_url   AS artist_lastfm,
            ar.musicbrainz_url AS artist_mb,
            ar.wikipedia_url   AS artist_wiki,
            SUBSTR(am.desc_lfm_album,  1, 600)  AS desc_album,
            SUBSTR(am.desc_lfm_artist, 1, 400)  AS desc_artist
        FROM collection_albums ca
        JOIN albums  al ON ca.album_id  = al.id
        JOIN artists ar ON al.artist_id = ar.id
        LEFT JOIN album_metadata am ON al.id = am.album_id
        WHERE ca.collection_id = ?
        ORDER BY ca.rank
    """, (col_id,))

    albums = {}
    for r in cur.fetchall():
        d = {k: r[k] for k in r.keys() if r[k] is not None and r[k] != ""}
        albums[r["rank"]] = d

    conn.close()
    print(f"Loaded {len(albums)} albums from DB (collection id={col_id})")
    return albums


# ── year extraction ───────────────────────────────────────────────────────────
def extract_year(rec: str) -> "int | None":
    m = re.search(r"[(~]?(\d{4})\b", rec)
    return int(m.group(1)) if m else None


# ── deduplicate genres sharing the same name ──────────────────────────────────
def deduplicate_genres(genres: list[dict]) -> list[dict]:
    """
    Multiple <area> entries in chart.html can share the same genre name —
    each represents a different representative recording.  Merge them into
    one node keyed by the lowest genre number.  The merged node carries:
      recs       — list[{num, rec, yt, year}]  (all representative recordings)
      all_nums   — list[str]                   (every original genre number)
      start_year — int | None                  (earliest year across recs)
    """
    from collections import defaultdict

    by_key: dict[tuple, list] = defaultdict(list)
    for g in genres:
        key = (g["cat"], g["genre"].strip().lower())
        by_key[key].append(g)

    result = []
    for _key, group in by_key.items():
        group.sort(key=lambda g: (int(g["number"].strip())
                                  if g["number"].strip().isdigit() else 9999))
        canonical = group[0].copy()
        years, recs = [], []
        for gg in group:
            yr = extract_year(gg["rec"])
            recs.append({"num": gg["number"], "rec": gg["rec"],
                         "yt": gg["yt_href"], "year": yr})
            if yr:
                years.append(yr)
        canonical["recs"]       = recs
        canonical["all_nums"]   = [gg["number"] for gg in group]
        canonical["start_year"] = min(years) if years else None
        result.append(canonical)

    result.sort(key=lambda g: (int(g["number"].strip())
                               if g["number"].strip().isdigit() else 9999))
    print(f"Deduplicated: {len(genres)} → {len(result)} unique genres")
    return result


# ── parse rel_out from ejemplo.html ──────────────────────────────────────────
def parse_relations(ej_path: Path = Path("ejemplo.html")) -> dict:
    """
    Extract genre relationships from ejemplo.html.
    Returns: {num_str: [num_str, ...]}  (4-digit genre numbers)

    Uses bracket counting to find the full GENRES array (regex would stop
    at the first ]; inside the array), then brace counting for each object.
    """
    if not ej_path.exists():
        print("ejemplo.html not found — no edge data")
        return {}

    html = ej_path.read_text(encoding="utf-8", errors="replace")

    # Locate 'const GENRES = [' and find matching closing bracket
    marker = "const GENRES = ["
    pos = html.find(marker)
    if pos == -1:
        print("GENRES array not found in ejemplo.html")
        return {}

    arr_start = pos + len(marker) - 1   # points to the opening '['
    depth = 0
    arr_end = arr_start
    in_str, esc = False, False
    for i in range(arr_start, len(html)):
        c = html[i]
        if esc:
            esc = False
            continue
        if c == "\\" and in_str:
            esc = True
            continue
        if c == '"' and not esc:
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                arr_end = i
                break

    src = html[arr_start + 1 : arr_end]   # content between [ and ]

    # Split src into individual genre objects using brace counting
    objs: list[str] = []
    depth = 0
    start = None
    in_str = esc = False
    for i, c in enumerate(src):
        if esc:
            esc = False
            continue
        if c == "\\" and in_str:
            esc = True
            continue
        if c == '"' and not esc:
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                objs.append(src[start : i + 1])
                start = None

    # Phase 1: build id → num mapping from the extracted src
    id_to_num: dict[str, str] = {}
    for m in re.finditer(r'\bid:\s*"([^"]+)"[^}]*?\bnum:\s*"(\d+)"', src, re.DOTALL):
        id_to_num[m.group(1)] = m.group(2)

    # Phase 2: extract (id, rel_out) pairs.
    # Each genre object has id: before rel_out: — non-greedy match stays within one object.
    result: dict[str, list[str]] = {}
    for m in re.finditer(
        r'\bid:\s*"([^"]+)"[^}]*?\brel_out:\s*\[([\s\S]*?)\]',
        src,
    ):
        gid     = m.group(1)
        rel_str = m.group(2)
        num     = id_to_num.get(gid)
        if not num:
            continue
        rel_ids  = re.findall(r'"([\w]+)"', rel_str)
        resolved = [id_to_num[r] for r in rel_ids if r in id_to_num]
        if resolved:
            result[num] = resolved

    total = sum(len(v) for v in result.values())
    print(f"Parsed {len(id_to_num)} genres, {len(result)} with edges ({total} total) from ejemplo.html")
    return result


# ── infer connections from genre numbering ───────────────────────────────────
def infer_relations(genres: list[dict]) -> dict:
    """
    Build approximate genre tree connections from the sequential numbering.

    Within each category the genres are listed in chronological/evolutionary
    order. We group them into "decade clusters" (same tens digit in the
    4-digit number). Within each cluster the first genre is the root and all
    others are its direct children. Cluster roots link sequentially so the
    overall category forms a chain of clusters.

    Between categories we follow the broad historical evolution:
    Classical → Jazz → R&B → Hip Hop → Rock → EDM → Experimental
    """
    from collections import defaultdict

    CAT_EVOLUTION = [
        "WESTERN CLASSICAL",
        "JAZZ",
        "RHYTHM & BLUES",
        "HIP HOP",
        "ROCK",
        "ELECTRONIC DANCE",
        "EXPERIMENTAL",
    ]

    by_cat: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for g in genres:
        try:
            by_cat[g["cat"]].append((int(g["number"].strip()), g["number"].strip()))
        except (ValueError, AttributeError):
            pass
    for cat in by_cat:
        by_cat[cat].sort()

    result: dict[str, list[str]] = {}

    # Within each category — decade-cluster tree
    for cat, num_pairs in by_cat.items():
        if not num_pairs:
            continue

        decades: dict[int, list[tuple[int, str]]] = {}
        for int_n, str_n in num_pairs:
            dk = int_n // 10
            decades.setdefault(dk, []).append((int_n, str_n))

        sorted_dks  = sorted(decades.keys())
        decade_roots: dict[int, str] = {}

        for dk in sorted_dks:
            members = decades[dk]
            root_str = members[0][1]
            decade_roots[dk] = root_str
            # All non-root members are children of the decade root
            for _, str_n in members[1:]:
                result.setdefault(root_str, []).append(str_n)

        # Chain the decade roots sequentially
        for i in range(len(sorted_dks) - 1):
            root_from = decade_roots[sorted_dks[i]]
            root_to   = decade_roots[sorted_dks[i + 1]]
            result.setdefault(root_from, []).append(root_to)

    # Between categories — historical evolution
    for i in range(len(CAT_EVOLUTION) - 1):
        cat_from, cat_to = CAT_EVOLUTION[i], CAT_EVOLUTION[i + 1]
        if cat_from in by_cat and cat_to in by_cat:
            root_from = by_cat[cat_from][0][1]
            root_to   = by_cat[cat_to][0][1]
            result.setdefault(root_from, []).append(root_to)

    total = sum(len(v) for v in result.values())
    print(f"Inferred {total} edges from decade-cluster structure + category evolution")
    return result


# ── match genres → albums ────────────────────────────────────────────────────
def match(genres: list[dict], albums: dict) -> list[dict]:
    """
    Match each (possibly deduplicated) genre to all its DB albums.
    Uses every number in g["all_nums"] so merged genres collect every album.
    """
    matched = 0
    for g in genres:
        all_nums = g.get("all_nums", [g["number"]])
        alb_list = []
        for num_str in all_nums:
            try:
                k = int(num_str.strip())
                if k in albums:
                    alb_list.append(albums[k])
            except ValueError:
                pass
        g["alb_list"] = alb_list
        g["alb"] = alb_list[0] if alb_list else None   # kept for backwards compat
        if alb_list:
            matched += 1
    print(f"Matched {matched}/{len(genres)} genres by genre number")
    return genres


# ── cluster layout (grouped by category, like ejemplo.html regions) ──────────
# Preferred display order of categories
CAT_ORDER = [
    "WESTERN CLASSICAL",
    "JAZZ",
    "RHYTHM & BLUES",
    "ROCK",
    "HIP HOP",
    "ELECTRONIC DANCE",
    "EXPERIMENTAL",
]

def assign_positions(genres: list[dict]) -> list[dict]:
    from math import ceil
    from collections import defaultdict

    STEP_X      = NODE_W + 40   # horizontal step between nodes within cluster
    STEP_Y      = NODE_H + 30   # vertical step between nodes within cluster
    COLS        = 6             # max columns per cluster
    CAT_GAP_X   = 90            # horizontal gap between clusters
    CAT_GAP_Y   = 110           # vertical gap between cluster rows
    INNER_PAD   = 20            # padding inside cluster
    LABEL_H     = 24            # height of category label above cluster
    MAX_ROW_W   = 14000         # wrap clusters to next row beyond this
    ORIGIN      = 300           # starting offset

    by_cat: dict[str, list] = defaultdict(list)
    for g in genres:
        by_cat[g["cat"]].append(g)

    # Ordered list of present categories
    cats = [c for c in CAT_ORDER if c in by_cat]
    for c in sorted(by_cat.keys()):
        if c not in cats:
            cats.append(c)

    # Cluster width/height
    def cluster_dims(count: int) -> tuple[int, int]:
        cols = min(COLS, count)
        rows = ceil(count / cols) if cols else 0
        w = cols * STEP_X + 2 * INNER_PAD
        h = rows * STEP_Y + 2 * INNER_PAD + LABEL_H
        return w, h

    cur_x, cur_y = ORIGIN, ORIGIN
    row_h = 0

    for cat in cats:
        cat_genres = by_cat[cat]
        if not cat_genres:
            continue
        cw, ch = cluster_dims(len(cat_genres))

        # Wrap to next row if cluster overflows current row
        if cur_x + cw > MAX_ROW_W + ORIGIN and cur_x > ORIGIN:
            cur_x = ORIGIN
            cur_y += row_h + CAT_GAP_Y
            row_h = 0

        # Keep the original chart.html Y-order within each category
        # (genres with lower cy first)
        cat_genres.sort(key=lambda g: (g["cy"], g["cx"]))

        cols = min(COLS, len(cat_genres))
        for i, g in enumerate(cat_genres):
            col = i % cols
            row = i // cols
            g["x"] = cur_x + INNER_PAD + col * STEP_X
            g["y"] = cur_y + INNER_PAD + LABEL_H + row * STEP_Y

        row_h = max(row_h, ch)
        cur_x += cw + CAT_GAP_X

    return genres


# ── build JS data array ───────────────────────────────────────────────────────
_ALB_FIELDS = [
    "album_name", "year", "artist_name", "country",
    "cover_url", "artist_img",
    "youtube_url", "spotify_url", "lastfm_url",
    "musicbrainz_url", "discogs_url", "bandcamp_url",
    "rateyourmusic_url", "allmusic_url",
    "artist_lastfm", "artist_mb", "artist_wiki",
    "desc_album", "desc_artist",
    "scaruffi_rating", "aoty_user_score", "metacritic_score",
]

def _serialize_alb(a: dict) -> dict:
    return {k: a[k] for k in _ALB_FIELDS if a.get(k)}


def build_js_data(genres: list[dict], relations: dict) -> str:
    out = []
    for g in genres:
        rel = relations.get(g["number"], [])
        entry = {
            "id":         g["id"],
            "num":        g["number"],
            "genre":      g["genre"],
            "cat":        g["cat"],
            "fam":        g["fam"],
            "x":          g["x"],
            "y":          g["y"],
            "rel_out":    rel,
            "start_year": g.get("start_year"),
            "recs":       g.get("recs", [{"num": g["number"], "rec": g.get("rec",""),
                                          "yt": g.get("yt_href",""), "year": None}]),
        }
        alb_list = g.get("alb_list", [])
        if alb_list:
            entry["alb_list"] = [_serialize_alb(a) for a in alb_list]
        out.append(entry)
    return json.dumps(out, ensure_ascii=False)


# ── HTML template ─────────────────────────────────────────────────────────────
HTML = """\
<!doctype html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Music Genre Tree</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#0a0a0b;--bg2:#111114;--bg3:#1a1a1f;
  --border:rgba(255,255,255,0.08);--border-active:rgba(255,255,255,0.3);
  --accent:#c8a96e;--accent2:#7eb8c8;
  --text:#e8e4dc;--text2:#8a8680;--text3:#5a5650;
  --font:"DejaVu Sans","Liberation Sans","Ubuntu",sans-serif;
  --font-mono:"DejaVu Sans Mono","Liberation Mono","Ubuntu Mono",monospace;
  --font-serif:"DejaVu Serif","Liberation Serif","Georgia",serif;
}}
html,body{{width:100%;height:100%;background:var(--bg);color:var(--text);
  font-family:var(--font);font-size:13px;overflow:hidden;cursor:default}}
body::before{{content:"";position:fixed;inset:0;
  background-image:linear-gradient(rgba(255,255,255,.015) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,.015) 1px,transparent 1px);
  background-size:40px 40px;pointer-events:none;z-index:0}}
#canvas-layer{{position:fixed;inset:0;pointer-events:none}}
#graph{{position:fixed;inset:0;transform-origin:0 0;will-change:transform}}

/* ── node ── */
.node{{position:absolute;width:{nw}px;background:var(--bg2);
  border:1px solid var(--border);border-top-width:2px;border-radius:2px;
  cursor:pointer;transition:border-color .2s,box-shadow .2s,opacity .15s;user-select:none}}
.node:hover{{border-color:var(--border-active);z-index:10}}
.node.active{{border-color:var(--accent);
  box-shadow:0 0 0 1px var(--accent),0 8px 40px rgba(200,169,110,.2);z-index:20}}
.node.dimmed{{opacity:.12;pointer-events:none}}
.node-header{{padding:8px 10px 6px;border-bottom:1px solid var(--border);
  display:flex;align-items:flex-start;gap:6px}}
.node-num{{font-size:9px;font-family:var(--font-mono);color:var(--text3);
  letter-spacing:.04em;padding-top:1px;flex-shrink:0}}
.node-genre{{font-size:11px;font-weight:700;color:var(--accent);
  letter-spacing:.02em;text-transform:uppercase;line-height:1.3}}
.node-family{{font-size:9px;color:var(--text3);margin-top:1px}}
.node-body{{padding:5px 10px 7px}}
.node-year{{font-size:11px;color:var(--text2);font-family:var(--font-mono);letter-spacing:.04em}}
.node-plus{{position:absolute;top:6px;right:6px;width:18px;height:18px;
  background:transparent;border:1px solid var(--border);border-radius:50%;
  color:var(--text3);font-size:13px;line-height:1;display:flex;
  align-items:center;justify-content:center;cursor:pointer;
  transition:border-color .15s,color .15s;z-index:5}}
.node-plus:hover{{border-color:var(--accent);color:var(--accent)}}

/* ── info panel ── */
#info-panel{{position:fixed;top:0;right:0;bottom:0;width:360px;
  background:var(--bg2);border-left:1px solid var(--border);
  display:flex;flex-direction:column;z-index:200;
  transform:translateX(100%);transition:transform .3s cubic-bezier(.25,.46,.45,.94)}}
#info-panel.open{{transform:translateX(0)}}
#panel-head{{padding:16px 16px 12px;border-bottom:1px solid var(--border);flex-shrink:0}}
#panel-close{{position:absolute;top:12px;right:12px;background:none;
  border:1px solid var(--border);border-radius:2px;color:var(--text2);
  font-family:var(--font-mono);font-size:11px;padding:3px 8px;
  cursor:pointer;transition:border-color .15s,color .15s}}
#panel-close:hover{{border-color:var(--accent);color:var(--accent)}}
#panel-num{{font-size:9px;font-family:var(--font-mono);color:var(--text3)}}
#panel-genre{{font-family:var(--font-serif);font-size:22px;
  font-style:italic;color:var(--text);margin:4px 0 2px;line-height:1.2}}
#panel-cat{{font-size:10px;color:var(--text3)}}
#panel-body{{flex:1;overflow-y:auto;padding:12px 16px}}
#panel-body::-webkit-scrollbar{{width:4px}}
#panel-body::-webkit-scrollbar-thumb{{background:var(--border)}}
.panel-covers{{display:flex;gap:8px;margin-bottom:12px}}
.panel-cover{{width:100px;height:100px;object-fit:cover;border-radius:2px;
  border:1px solid var(--border)}}
.panel-artist-img{{width:70px;height:70px;object-fit:cover;border-radius:50%;
  border:1px solid var(--border);align-self:flex-end}}
.panel-section{{margin-bottom:12px}}
.panel-label{{font-size:8px;color:var(--text3);letter-spacing:.1em;
  text-transform:uppercase;margin-bottom:4px}}
.panel-album-name{{font-family:var(--font-serif);font-style:italic;
  font-size:14px;color:var(--text);line-height:1.3}}
.panel-artist{{font-size:11px;color:var(--text2);margin-top:2px}}
.panel-meta{{font-size:9px;color:var(--text3);margin-top:2px;letter-spacing:.04em}}
.panel-links{{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}}
.panel-link{{font-size:9px;letter-spacing:.05em;padding:3px 8px;
  border:1px solid var(--border);border-radius:20px;color:var(--text2);
  text-decoration:none;transition:border-color .15s,color .15s;white-space:nowrap}}
.panel-link:hover{{border-color:var(--accent);color:var(--accent)}}
.panel-yt-wrap{{position:relative;padding-top:56.25%;background:#000;
  border-radius:2px;overflow:hidden;margin-bottom:10px}}
.panel-yt-wrap iframe{{position:absolute;inset:0;width:100%;height:100%;border:0}}
.panel-desc{{font-size:10px;color:var(--text2);line-height:1.6}}
.panel-scores{{display:flex;gap:10px;flex-wrap:wrap}}
.panel-score{{text-align:center}}
.panel-score-val{{font-size:14px;font-weight:700;color:var(--accent)}}
.panel-score-lbl{{font-size:8px;color:var(--text3);letter-spacing:.06em}}
.panel-rec-item{{padding:7px 0;border-bottom:1px solid var(--border);display:flex;
  flex-direction:column;gap:4px}}
.panel-rec-item:last-child{{border-bottom:none}}
.panel-rec-text{{font-family:var(--font-serif);font-style:italic;
  font-size:12px;color:var(--text2);line-height:1.4}}
.panel-rec-yt{{font-size:9px;letter-spacing:.06em;padding:2px 7px;
  border:1px solid var(--border);border-radius:20px;color:var(--text2);
  background:none;cursor:pointer;align-self:flex-start;
  transition:border-color .15s,color .15s;font-family:var(--font)}}
.panel-rec-yt:hover{{border-color:#e00;color:#e00}}
.panel-rec-yt.active{{border-color:#e00;color:#e00}}
.panel-alb-card{{padding:8px 0;border-bottom:1px solid var(--border);display:flex;gap:10px}}
.panel-alb-card:last-child{{border-bottom:none}}
.panel-alb-cover{{width:64px;height:64px;object-fit:cover;border-radius:2px;
  border:1px solid var(--border);flex-shrink:0}}
.panel-alb-info{{flex:1;min-width:0}}
.panel-alb-name{{font-family:var(--font-serif);font-style:italic;font-size:13px;
  color:var(--text);line-height:1.3}}
.panel-alb-artist{{font-size:11px;color:var(--text2);margin-top:2px}}
.panel-alb-meta{{font-size:9px;color:var(--text3);margin-top:2px;letter-spacing:.04em}}

/* ── UI chrome ── */
#ui{{position:fixed;top:0;left:0;right:0;z-index:100;pointer-events:none}}
#topbar{{display:flex;align-items:center;justify-content:space-between;
  padding:14px 20px;pointer-events:all}}
#title-block h1{{font-family:var(--font-serif);font-size:18px;
  font-weight:400;font-style:italic;color:var(--text)}}
#title-block p{{font-size:10px;color:var(--text3);margin-top:2px}}
#search-wrap{{display:flex;align-items:center;gap:10px}}
#search{{background:var(--bg3);border:1px solid var(--border);border-radius:2px;
  padding:7px 12px;color:var(--text);font-family:var(--font);
  font-size:12px;width:220px;outline:none;transition:border-color .2s}}
#search::placeholder{{color:var(--text3)}}
#search:focus{{border-color:var(--border-active)}}
#count{{font-size:11px;color:var(--text3);white-space:nowrap}}
#hint{{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);
  font-size:10px;color:var(--text3);letter-spacing:.04em;
  pointer-events:none;z-index:100}}
#zoom-btns{{position:fixed;bottom:18px;right:20px;display:flex;flex-direction:column;
  gap:4px;z-index:100}}
.z-btn{{width:28px;height:28px;background:var(--bg3);border:1px solid var(--border);
  border-radius:2px;color:var(--text2);font-size:16px;
  font-family:var(--font);cursor:pointer;display:flex;
  align-items:center;justify-content:center;transition:border-color .15s,color .15s}}
.z-btn:hover{{border-color:var(--accent);color:var(--accent)}}
#reset-btn{{position:fixed;bottom:18px;left:20px;background:var(--bg3);
  border:1px solid var(--border);border-radius:2px;color:var(--text2);
  font-size:11px;font-family:var(--font);
  padding:5px 12px;cursor:pointer;z-index:100;
  transition:border-color .15s,color .15s}}
#reset-btn:hover{{border-color:var(--accent);color:var(--accent)}}
</style>
</head>
<body>

<div id="ui">
  <div id="topbar">
    <div id="title-block">
      <h1>Music Genre Tree</h1>
      <p>musicgenretree.org · {count} géneros</p>
    </div>
    <div id="search-wrap">
      <input id="search" type="text" placeholder="Buscar género, artista…" autocomplete="off">
      <span id="count"></span>
    </div>
  </div>
</div>

<svg id="canvas-layer" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <marker id="arr-gold" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
      <path d="M0,0 L0,6 L6,3 z" fill="rgba(200,169,110,0.7)"/>
    </marker>
    <marker id="arr-blue" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
      <path d="M0,0 L0,6 L6,3 z" fill="rgba(126,184,200,0.6)"/>
    </marker>
    <marker id="arr-dim" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
      <path d="M0,0 L0,6 L6,3 z" fill="rgba(255,255,255,0.06)"/>
    </marker>
  </defs>
</svg>
<div id="graph"></div>

<div id="info-panel">
  <div id="panel-head">
    <button id="panel-close">✕ cerrar</button>
    <div id="panel-num"></div>
    <div id="panel-genre"></div>
    <div id="panel-cat"></div>
  </div>
  <div id="panel-body">
    <div class="panel-section" id="p-yt-section" style="display:none">
      <div class="panel-yt-wrap">
        <iframe id="p-yt" allowfullscreen allow="autoplay;encrypted-media"></iframe>
      </div>
    </div>
    <div class="panel-section" id="p-recs-section">
      <div class="panel-label">Grabaciones representativas (musicgenretree.org)</div>
      <div id="p-recs-list"></div>
    </div>
    <div class="panel-section" id="p-albums-section" style="display:none">
      <div class="panel-label">Álbumes</div>
      <div id="p-albums-list"></div>
    </div>
  </div>
</div>

<div id="hint">Arrastra para mover · Rueda para zoom · Clic en nodo para explorar · + para info</div>
<div id="zoom-btns">
  <button class="z-btn" id="zin">+</button>
  <button class="z-btn" id="zout">−</button>
</div>
<button id="reset-btn">↺ Reset vista</button>

<script>
// ── DATA ──────────────────────────────────────────────────────────────────
const GENRES = {genres_json};

// ── color by category (stable hue from string hash) ──────────────────────
function catColor(cat) {{
  const PALETTE = {{
    "PREHISTORIC":"#5a3e28","ANCIENT":"#6b4226",
    "SUB-SAHARAN":"#1b4332","NORTH AFRICAN":"#2d6a4f","WEST AFRICAN":"#386641",
    "EAST AFRICAN":"#4a7c59","CENTRAL AFR":"#1b4332","SOUTHERN":"#2d5016",
    "OCEANIAN":"#005f73","SOUTHEAST AS":"#0a9396","EAST ASIAN":"#0e6b75",
    "CENTRAL ASIAN":"#006064","SOUTH ASIAN":"#0277bd",
    "MIDDLE EAST":"#7b2d00","LATIN AM":"#6d2b3d","NORTH AM":"#4a1060",
    "EUROPEAN":"#1a3a6b","JAZZ":"#7a3e00","BLUES":"#3e1f00",
    "COUNTRY":"#5c4a00","FOLK":"#2e4a1e","POP":"#5c0032","ROCK":"#6b0000",
    "ELECTRONIC":"#00205b","HIP HOP":"#2a0060","R&B":"#5c0032",
    "CLASSICAL":"#1a237e","EXPERIMENTAL":"#3e0060","EDM":"#00205b",
  }};
  const upper = cat.toUpperCase();
  for (const [k, v] of Object.entries(PALETTE)) {{
    if (upper.includes(k)) return v;
  }}
  // hash fallback
  let h = 0;
  for (let i = 0; i < cat.length; i++) {{ h = (h * 31 + cat.charCodeAt(i)) & 0xffffffff; }}
  return `hsl(${{Math.abs(h) % 360}},35%,20%)`;
}}

// ── YouTube ID extractor ──────────────────────────────────────────────────
function ytId(url) {{
  if (!url) return null;
  let m = url.match(/[?&]v=([^&]+)/);
  if (m) return m[1];
  const parts = url.split("/");
  const ytIdx = parts.findIndex(p => p === "youtu.be");
  if (ytIdx >= 0 && parts[ytIdx+1]) return parts[ytIdx+1].split("?")[0];
  const emIdx = parts.indexOf("embed");
  if (emIdx >= 0 && parts[emIdx+1]) return parts[emIdx+1].split("?")[0];
  return null;
}}

// ── render ────────────────────────────────────────────────────────────────
const graph = document.getElementById("graph");
const svgEl = document.getElementById("canvas-layer");
let tx = 0, ty = 0, scale = 0.12;

// Lookup by 4-digit num string
const byNum = {{}};
GENRES.forEach(g => {{ byNum[g.num] = g; }});

// ── edges ─────────────────────────────────────────────────────────────────
const edgeEls = {{}};

function nodeAnchors(g) {{
  const nw = {nw}, nh = 100;
  const cx = g.x + nw/2, cy = g.y + nh/2;
  return {{
    x: cx, y: cy,
    top:   {{x: cx,       y: g.y}},
    bot:   {{x: cx,       y: g.y + nh}},
    left:  {{x: g.x,      y: cy}},
    right: {{x: g.x + nw, y: cy}},
  }};
}}
function bestAnchors(a, b) {{
  const dx = b.x - a.x, dy = b.y - a.y;
  return Math.abs(dx) > Math.abs(dy)
    ? (dx > 0 ? [a.right, b.left]  : [a.left,  b.right])
    : (dy > 0 ? [a.bot,   b.top]   : [a.top,   b.bot]);
}}
function curvePath(p1, p2) {{
  const mx = (p1.x+p2.x)/2, my = (p1.y+p2.y)/2;
  const dx = p2.x-p1.x, dy = p2.y-p1.y;
  return `M${{p1.x}},${{p1.y}} Q${{mx-dy*.18}},${{my+dx*.18}} ${{p2.x}},${{p2.y}}`;
}}
function ws(pt) {{ return {{x: pt.x*scale+tx, y: pt.y*scale+ty}}; }}

GENRES.forEach(g => {{
  (g.rel_out || []).forEach(toNum => {{
    const key = g.num + "__" + toNum;
    if (edgeEls[key]) return;
    const path = document.createElementNS("http://www.w3.org/2000/svg","path");
    path.setAttribute("fill","none");
    path.setAttribute("stroke","rgba(255,255,255,0.25)");
    path.setAttribute("stroke-width","1.5");
    path.setAttribute("marker-end","url(#arr-dim)");
    path.dataset.from = g.num;
    path.dataset.to   = toNum;
    svgEl.appendChild(path);
    edgeEls[key] = path;
  }});
}});

let activeNum = null;   // 4-digit num of active genre

function drawEdges() {{
  svgEl.setAttribute("viewBox", `0 0 ${{window.innerWidth}} ${{window.innerHeight}}`);
  for (const path of Object.values(edgeEls)) {{
    const from = byNum[path.dataset.from];
    const to   = byNum[path.dataset.to];
    if (!from || !to) {{ path.setAttribute("d",""); continue; }}
    const [ap, bp] = bestAnchors(nodeAnchors(from), nodeAnchors(to));
    path.setAttribute("d", curvePath(ws(ap), ws(bp)));

    const isOut = activeNum && path.dataset.from === activeNum;
    const isIn  = activeNum && path.dataset.to   === activeNum;
    if (activeNum) {{
      if (isOut) {{
        path.setAttribute("stroke","rgba(200,169,110,.65)");
        path.setAttribute("stroke-width","1.5");
        path.setAttribute("marker-end","url(#arr-gold)");
      }} else if (isIn) {{
        path.setAttribute("stroke","rgba(126,184,200,.55)");
        path.setAttribute("stroke-width","1.5");
        path.setAttribute("marker-end","url(#arr-blue)");
      }} else {{
        path.setAttribute("stroke","rgba(255,255,255,.04)");
        path.setAttribute("stroke-width","0.8");
        path.setAttribute("marker-end","url(#arr-dim)");
      }}
    }} else {{
      path.setAttribute("stroke","rgba(255,255,255,0.22)");
      path.setAttribute("stroke-width","1.5");
      path.setAttribute("marker-end","url(#arr-dim)");
    }}
  }}
}}

function applyTransform(anim = false) {{
  graph.style.transition = anim ? "transform .5s cubic-bezier(.25,.46,.45,.94)" : "none";
  graph.style.transform  = `translate(${{tx}}px,${{ty}}px) scale(${{scale}})`;
  drawEdges();
}}

// ── category labels ───────────────────────────────────────────────────────
(function() {{
  const catBounds = {{}};
  GENRES.forEach(g => {{
    if (!g.cat) return;
    if (!catBounds[g.cat]) catBounds[g.cat] = {{minX:1e9, minY:1e9, maxX:-1}};
    const b = catBounds[g.cat];
    b.minX = Math.min(b.minX, g.x);
    b.minY = Math.min(b.minY, g.y);
    b.maxX = Math.max(b.maxX, g.x + {nw});
  }});
  for (const [cat, b] of Object.entries(catBounds)) {{
    const el = document.createElement("div");
    el.style.cssText = `position:absolute;left:${{b.minX}}px;top:${{b.minY - 22}}px;` +
      `color:rgba(255,255,255,.25);font-size:9px;font-family:var(--font-mono);` +
      `letter-spacing:.14em;text-transform:uppercase;white-space:nowrap;pointer-events:none`;
    el.textContent = cat || "OTHER";
    graph.appendChild(el);
  }}
}})();

const nodeEls = {{}};

GENRES.forEach(g => {{
  const div = document.createElement("div");
  div.className = "node";
  div.id = "n" + g.id;
  div.style.left = g.x + "px";
  div.style.top  = g.y + "px";
  div.style.borderTopColor = catColor(g.cat);

  div.innerHTML = `
    <button class="node-plus" title="Info">+</button>
    <div class="node-header">
      <div>
        <div class="node-genre">${{g.genre}}</div>
        <div class="node-family">${{g.fam || g.cat}}</div>
      </div>
    </div>
    <div class="node-body">
      <div class="node-year">${{g.start_year ? "desde " + g.start_year : ""}}</div>
    </div>`;

  div.querySelector(".node-plus").addEventListener("click", e => {{
    e.stopPropagation();
    openPanel(g);
  }});
  div.addEventListener("click", () => activateNode(g.id));

  graph.appendChild(div);
  nodeEls[g.id] = div;
}});

// ── info panel ────────────────────────────────────────────────────────────
const panel = document.getElementById("info-panel");

function show(id, visible) {{
  document.getElementById(id).style.display = visible ? "" : "none";
}}
function setText(id, val) {{
  document.getElementById(id).textContent = val || "";
}}

let activeYtBtn = null;
function loadYt(vidId, btn) {{
  if (activeYtBtn) activeYtBtn.classList.remove("active");
  activeYtBtn = btn;
  btn.classList.add("active");
  document.getElementById("p-yt").src =
    `https://www.youtube-nocookie.com/embed/${{vidId}}?autoplay=1`;
  show("p-yt-section", true);
}}

function openPanel(g) {{
  setText("panel-num",   g.num);
  setText("panel-genre", g.genre);
  document.getElementById("panel-cat").textContent =
    [g.cat, g.fam].filter(Boolean).join(" › ");

  // stop any running video
  document.getElementById("p-yt").src = "";
  show("p-yt-section", false);
  activeYtBtn = null;

  // ── recordings list ────────────────────────────────────────────────────
  const recsList = document.getElementById("p-recs-list");
  recsList.innerHTML = "";
  (g.recs || []).forEach(r => {{
    const item = document.createElement("div");
    item.className = "panel-rec-item";
    const vid = ytId(r.yt);
    item.innerHTML = `<div class="panel-rec-text">${{r.rec}}</div>` +
      (vid ? `<button class="panel-rec-yt" data-vid="${{vid}}">▶ YouTube</button>` : "");
    if (vid) {{
      item.querySelector(".panel-rec-yt").addEventListener("click", function() {{
        loadYt(this.dataset.vid, this);
      }});
    }}
    recsList.appendChild(item);
  }});

  // ── albums list (from DB) ──────────────────────────────────────────────
  const albList = document.getElementById("p-albums-list");
  albList.innerHTML = "";
  const albs = g.alb_list || [];
  if (albs.length) {{
    albs.forEach(a => {{
      const linkDefs = [
        [a.spotify_url,       "Spotify"],
        [a.lastfm_url,        "Last.fm"],
        [a.musicbrainz_url,   "MusicBrainz"],
        [a.discogs_url,       "Discogs"],
        [a.rateyourmusic_url, "RYM"],
        [a.bandcamp_url,      "Bandcamp"],
        [a.allmusic_url,      "AllMusic"],
        [a.artist_lastfm,     "Artista – Last.fm"],
        [a.artist_mb,         "Artista – MB"],
        [a.artist_wiki,       "Wikipedia"],
        [a.youtube_url,       "YouTube"],
      ].filter(([u]) => u);
      const scores = [
        [a.scaruffi_rating,  "Scaruffi"],
        [a.aoty_user_score,  "AOTY"],
        [a.metacritic_score, "Metacritic"],
      ].filter(([v]) => v != null);
      const vid = ytId(a.youtube_url);
      const card = document.createElement("div");
      card.className = "panel-alb-card";
      card.innerHTML =
        (a.cover_url ? `<img class="panel-alb-cover" src="${{a.cover_url}}" alt="">` : "") +
        `<div class="panel-alb-info">
          <div class="panel-alb-name">${{a.album_name || ""}}</div>
          <div class="panel-alb-artist">${{a.artist_name || ""}}</div>
          <div class="panel-alb-meta">${{[a.year, a.country].filter(Boolean).join(" · ")}}</div>
          ${{scores.length ? `<div class="panel-alb-meta">${{scores.map(([v,l])=>l+": "+v).join(" · ")}}</div>` : ""}}
          <div class="panel-links" style="margin-top:5px">
            ${{linkDefs.map(([u,l])=>`<a class="panel-link" href="${{u}}" target="_blank" rel="noopener">${{l}}</a>`).join("")}}
            ${{vid ? `<button class="panel-rec-yt" data-vid="${{vid}}">▶ YouTube</button>` : ""}}
          </div>
          ${{(a.desc_album||a.desc_artist) ? `<div class="panel-desc" style="margin-top:6px">${{a.desc_album||a.desc_artist}}</div>` : ""}}
        </div>`;
      if (vid) {{
        card.querySelector(".panel-rec-yt").addEventListener("click", function() {{
          loadYt(this.dataset.vid, this);
        }});
      }}
      albList.appendChild(card);
    }});
    show("p-albums-section", true);
  }} else {{
    show("p-albums-section", false);
  }}

  panel.classList.add("open");
}}

document.getElementById("panel-close").addEventListener("click", () => {{
  panel.classList.remove("open");
  document.getElementById("p-yt").src = ""; // stop video
}});

// ── node activation (dimming + edge highlight) ────────────────────────────
function deactivate() {{
  activeNum = null;
  Object.values(nodeEls).forEach(el => el.classList.remove("active","dimmed"));
  drawEdges();
}}

function activateNode(id) {{
  const g = GENRES.find(g => g.id === id);
  if (!g) return;
  // Switch directly to new node (even if another was active)
  activeNum = g.num;
  const relNums = new Set([
    g.num,
    ...(g.rel_out || []),
    ...GENRES.filter(gg => (gg.rel_out||[]).includes(g.num)).map(gg => gg.num),
  ]);

  Object.entries(nodeEls).forEach(([nid, el]) => {{
    const gg = GENRES.find(x => x.id == nid);
    el.classList.remove("active","dimmed");
    if (nid == id) el.classList.add("active");
    else if (gg && !relNums.has(gg.num)) el.classList.add("dimmed");
  }});
  drawEdges();
}}

// ── pan & zoom ────────────────────────────────────────────────────────────
let dragging = false, hasDragged = false;
let startX = 0, startY = 0, startTx = 0, startTy = 0;

document.addEventListener("mousedown", e => {{
  if (e.target.closest(".node,#ui,#zoom-btns,#reset-btn,#info-panel")) return;
  dragging = true; hasDragged = false;
  startX = e.clientX; startY = e.clientY;
  startTx = tx; startTy = ty; document.body.style.cursor = "grabbing";
}});
document.addEventListener("mousemove", e => {{
  if (!dragging) return;
  tx = startTx + (e.clientX - startX);
  ty = startTy + (e.clientY - startY);
  if (Math.hypot(e.clientX-startX, e.clientY-startY) > 4) hasDragged = true;
  applyTransform();
}});
document.addEventListener("mouseup", e => {{
  const wasDragging = dragging;
  dragging = false;
  document.body.style.cursor = "default";
  // Click on empty canvas (not a drag) → deactivate
  if (wasDragging && !hasDragged && !e.target.closest(".node,#ui,#zoom-btns,#reset-btn,#info-panel")) {{
    deactivate();
  }}
}});

document.addEventListener("wheel", e => {{
  e.preventDefault();
  const f = e.deltaY < 0 ? 1.08 : 0.92;
  const ns = Math.max(0.05, Math.min(2.5, scale * f));
  tx = e.clientX - (e.clientX - tx) * (ns / scale);
  ty = e.clientY - (e.clientY - ty) * (ns / scale);
  scale = ns; applyTransform();
}}, {{passive:false}});

document.getElementById("zin").onclick  = () => {{ scale = Math.min(2.5, scale*1.15); applyTransform(true); }};
document.getElementById("zout").onclick = () => {{ scale = Math.max(0.05, scale/1.15); applyTransform(true); }};
document.getElementById("reset-btn").onclick = () => {{
  tx = 0; ty = 0; scale = 0.12;
  deactivate();
  panel.classList.remove("open");
  document.getElementById("p-yt").src = "";
  applyTransform(true);
}};

// ── search ────────────────────────────────────────────────────────────────
document.getElementById("search").addEventListener("input", e => {{
  const q = e.target.value.toLowerCase().trim();
  const countEl = document.getElementById("count");
  if (!q) {{
    Object.values(nodeEls).forEach(el => el.classList.remove("dimmed"));
    countEl.textContent = "";
    activeNum = null;
    drawEdges();
    return;
  }}
  let hits = 0;
  GENRES.forEach(g => {{
    const recsText = (g.recs||[]).map(r=>r.rec).join(" ").toLowerCase();
    const albText  = (g.alb_list||[]).map(a=>(a.artist_name||"")+" "+(a.album_name||"")).join(" ").toLowerCase();
    const match = g.genre.toLowerCase().includes(q)
      || g.cat.toLowerCase().includes(q)
      || (g.fam||"").toLowerCase().includes(q)
      || recsText.includes(q)
      || albText.includes(q);
    nodeEls[g.id].classList.toggle("dimmed", !match);
    if (match) hits++;
  }});
  countEl.textContent = `${{hits}} resultado${{hits !== 1 ? "s" : ""}}`;
}});

// ── touch ─────────────────────────────────────────────────────────────────
let lastDist = 0;
document.addEventListener("touchstart", e => {{
  if (e.touches.length === 1) {{
    dragging = true; startX = e.touches[0].clientX; startY = e.touches[0].clientY;
    startTx = tx; startTy = ty;
  }} else if (e.touches.length === 2) {{
    lastDist = Math.hypot(e.touches[0].clientX-e.touches[1].clientX,
                          e.touches[0].clientY-e.touches[1].clientY);
  }}
}}, {{passive:true}});
document.addEventListener("touchmove", e => {{
  e.preventDefault();
  if (e.touches.length === 1 && dragging) {{
    tx = startTx + (e.touches[0].clientX - startX);
    ty = startTy + (e.touches[0].clientY - startY);
    applyTransform();
  }} else if (e.touches.length === 2) {{
    const d = Math.hypot(e.touches[0].clientX-e.touches[1].clientX,
                         e.touches[0].clientY-e.touches[1].clientY);
    scale = Math.max(0.05, Math.min(2.5, scale * d / lastDist));
    lastDist = d; applyTransform();
  }}
}}, {{passive:false}});
document.addEventListener("touchend", () => {{ dragging = false; }});

window.addEventListener("resize", drawEdges);
applyTransform();
</script>
</body>
</html>
"""


def generate_html(genres: list[dict], relations: dict) -> str:
    js_data     = build_js_data(genres, relations)
    genre_count = len(genres)
    matched     = sum(1 for g in genres if g.get("alb"))
    total_edges = sum(len(v) for v in relations.values())
    subtitle    = f"{genre_count} géneros · {matched} con datos · {total_edges} conexiones"
    return HTML.format(
        nw=NODE_W,
        count=subtitle,
        genres_json=js_data,
    )


# ── main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    html_src  = fetch_chart()
    genres    = parse_genres(html_src)

    # ── filter to only the 7 target genre trees ──────────────────────────────
    genres = [g for g in genres if g["cat"] in TARGET_CATS]
    print(f"After filter: {len(genres)} genres")

    genres    = deduplicate_genres(genres)
    relations = infer_relations(genres)

    albums = {} if NO_DB else load_albums(DB_PATH)
    genres = match(genres, albums)
    genres = assign_positions(genres)

    out = generate_html(genres, relations)
    OUTPUT.write_text(out, encoding="utf-8")
    print(f"✓ Written {OUTPUT} ({len(out)//1024} KB, {len(genres)} nodes)")

    # ── genre_yt.json — feed to yt-dlp ───────────────────────────────────
    yt_data = [
        {"num": g["number"], "genre": g["genre"], "cat": g["cat"],
         "start_year": g.get("start_year"),
         "recs": [{"num": r["num"], "rec": r["rec"], "yt": r["yt"], "year": r["year"]}
                  for r in g.get("recs", []) if r.get("yt")]}
        for g in genres
        if any(r.get("yt") for r in g.get("recs", []))
    ]
    yt_json = Path("genre_yt.json")
    yt_json.write_text(json.dumps(yt_data, ensure_ascii=False, indent=2), encoding="utf-8")
    total_yt = sum(len(d["recs"]) for d in yt_data)
    print(f"✓ Written {yt_json} ({len(yt_data)} genres, {total_yt} YouTube links)")
    print(f"  Usage: jq -r '.[].recs[].yt' genre_yt.json | yt-dlp --batch-file -")
