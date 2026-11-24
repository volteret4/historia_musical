#!/usr/bin/env python3
"""
Build genretree.html from mermaid diagram files in mermaid_diagrams/.

Each .md file contains one ```mermaid flowchart``` block.
  Node syntax:  ID(Name. YYYY)  ID[Name. YYYY]  ID{Name. YYYY}
  Edge syntax:  A --> B --> C
  Subgraphs group related genres within a diagram.

Usage:
    python build.py           # scan mermaid_diagrams/, optionally load DB
    python build.py --no-db   # skip database album data

Output: genretree.html
"""
import sys, json, re
from pathlib import Path
from collections import defaultdict

# ── config ────────────────────────────────────────────────────────────────────
args        = sys.argv[1:]
NO_DB       = "--no-db" in args
_DB_DEFAULT = Path.home() / "gits/pollo/rym_lastfm/db/must_hear_rym_new.db"
DB_PATH     = Path(next((a for a in args if not a.startswith("-")), _DB_DEFAULT))
MERMAID_DIR = Path("mermaid_diagrams")
OUTPUT      = Path("genretree.html")
NODE_W      = 210          # node width in px
NODE_H      = 55           # approximate node height for edge anchors

# Left-to-right diagram arrangement; unknown diagrams appended alphabetically
DIAGRAM_ORDER = ["classical", "jazz", "rnb", "hiphop", "rock", "edm", "experimental"]


# ── mermaid parsing ───────────────────────────────────────────────────────────
def parse_mermaid_label(text: str) -> "tuple[str, int | None]":
    """
    'Classical Marches.1700' → ('Classical Marches', 1700)
    'Gregorian Cant. 750'   → ('Gregorian Cant',    750)
    'Baroque Music'         → ('Baroque Music',      None)
    """
    text = text.strip()
    m = re.search(r"[.\s](\d{3,4})\s*$", text)
    if m:
        return text[: m.start()].strip().rstrip("."), int(m.group(1))
    return text, None


# Matches Mermaid node shapes: ID(…) ID[…] ID{…} ID((…)) ID([…])
_NODE_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_]*)\s*"
    r"(\(\(|\(\[|\{|\(|\[)"
    r"([^)\]}\n]+?)"
    r"(\)\)|\]\)|\}|\)|\])"
)


def _parse_seg(part: str) -> "tuple[str|None, str|None, int|None, str|None]":
    """One edge-chain segment → (id, name, year, shape) or (id, None, None, None)."""
    part = part.strip()
    nm = _NODE_RE.match(part)
    if nm:
        name, year = parse_mermaid_label(nm.group(3))
        return nm.group(1), name, year, nm.group(2)
    bare = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\s*$", part)
    if bare:
        return bare.group(1), None, None, None
    return None, None, None, None


def parse_mermaid_file(path: Path) -> dict:
    """
    Returns {name, nodes, edges, subgraphs} or {} if no mermaid block found.
      nodes     : {local_id → {id, name, year, shape, subgraph}}
      edges     : [(from_id, to_id)]
      subgraphs : {subgraph_name → [local_id, …]}
    """
    text = path.read_text(encoding="utf-8")
    mm = re.search(r"```mermaid\s*([\s\S]*?)```", text)
    if not mm:
        return {}
    content = mm.group(1)

    nodes: dict[str, dict] = {}
    edges: list[tuple] = []
    subgraphs: dict[str, list] = {}
    cur_sg = None

    def _register(nid, name, year, shape):
        if nid in nodes:
            return
        nodes[nid] = {"id": nid, "name": name or nid, "year": year,
                      "shape": shape or "(", "subgraph": cur_sg}
        if cur_sg is not None:
            subgraphs.setdefault(cur_sg, [])
            if nid not in subgraphs[cur_sg]:
                subgraphs[cur_sg].append(nid)

    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("%%"):
            continue
        if re.match(r"^(flowchart|graph)\b", line, re.I):
            continue

        sg_m = re.match(r"^subgraph\s+(.*)", line)
        if sg_m:
            cur_sg = sg_m.group(1).strip().strip('"')
            subgraphs.setdefault(cur_sg, [])
            continue
        if line == "end":
            cur_sg = None
            continue

        if "-->" in line or ".->" in line:
            parts = re.split(r"\s*--[->\.]+\s*", line)
            seg_ids = []
            for part in parts:
                nid, name, year, shape = _parse_seg(part)
                if nid is None:
                    continue
                _register(nid, name, year, shape)
                seg_ids.append(nid)
            for i in range(len(seg_ids) - 1):
                edges.append((seg_ids[i], seg_ids[i + 1]))
        else:
            nm = _NODE_RE.match(line)
            if nm:
                name, year = parse_mermaid_label(nm.group(3))
                _register(nm.group(1), name, year, nm.group(2))

    # Mark cross-refs: diamond {…} nodes with no outgoing edges
    out_ids = {e[0] for e in edges}
    for nid, n in nodes.items():
        n["is_crossref"] = n["shape"] == "{" and nid not in out_ids

    return {"name": path.stem, "nodes": nodes, "edges": edges, "subgraphs": subgraphs}


def parse_mermaid_diagrams(dir_path: Path) -> "tuple[list[dict], dict]":
    """
    Parse all .md files.  Returns (genres_list, relations_dict) ready for
    assign_positions() and build_js_data().
    """
    md_files = sorted(dir_path.glob("*.md"))
    if not md_files:
        print(f"No .md files found in {dir_path}")
        return [], {}

    file_map = {f.stem: f for f in md_files}
    ordered = [file_map[n] for n in DIAGRAM_ORDER if n in file_map]
    ordered += [f for f in sorted(md_files) if f.stem not in {p.stem for p in ordered}]

    # Pass 1: parse
    parsed = []
    for path in ordered:
        data = parse_mermaid_file(path)
        if not data or not data.get("nodes"):
            print(f"  skipped {path.name}")
            continue
        print(f"  {path.name}: {len(data['nodes'])} nodes, {len(data['edges'])} edges")
        parsed.append(data)

    # Pass 2: assign global IDs
    all_genres: list[dict] = []
    seq_id = 0
    xref_map: dict[str, str] = {}   # diagram_name_lower → first global_num
    diag_globals = []

    for data in parsed:
        diag_name = data["name"]
        local_to_global: dict[str, str] = {}
        first_num = None

        # Subgraph insertion order → index for layout sorting
        sg_order = list(data["subgraphs"].keys())
        sg_idx   = {sg: i for i, sg in enumerate(sg_order)}

        for local_id, n in data["nodes"].items():
            global_num = f"{diag_name}_{local_id}"
            local_to_global[local_id] = global_num
            if first_num is None:
                first_num = global_num

            sg = n.get("subgraph") or ""
            genre = {
                "id":         seq_id,
                "num":        global_num,
                "number":     global_num,
                "genre":      n["name"],
                "year":       n["year"],
                "start_year": n["year"],
                "cat":        diag_name,
                "fam":        sg,
                "fam_idx":    sg_idx.get(sg, 999),
                "shape":      n.get("shape", "("),
                "is_crossref": n.get("is_crossref", False),
                "recs":       [{"num": global_num, "rec": n["name"],
                                "yt": "", "year": n["year"]}],
                "all_nums":   [global_num],
                "alb_list":   [],
                "alb":        None,
            }
            all_genres.append(genre)
            seq_id += 1

        if first_num:
            xref_map[diag_name.lower()] = first_num

        diag_globals.append({
            "name":  diag_name,
            "local": local_to_global,
            "first": first_num,
            "edges": data["edges"],
            "nodes": data["nodes"],
        })

    # Pass 3: resolve edges (with cross-diagram resolution)
    all_global_nums = {g["num"] for g in all_genres}
    relations: dict[str, list] = {}

    for dg in diag_globals:
        local = dg["local"]
        nodes = dg["nodes"]
        for (from_id, to_id) in dg["edges"]:
            from_num = local.get(from_id)
            if not from_num:
                continue
            to_num = local.get(to_id)

            # Cross-diagram: if target is a cross-ref node, try to find diagram root
            if to_num and nodes.get(to_id, {}).get("is_crossref"):
                node_name = nodes[to_id]["name"].lower()
                for key, root_num in xref_map.items():
                    if key in node_name or node_name in key:
                        to_num = root_num
                        break

            if (from_num and to_num
                    and from_num in all_global_nums
                    and to_num in all_global_nums):
                relations.setdefault(from_num, [])
                if to_num not in relations[from_num]:
                    relations[from_num].append(to_num)

    total = sum(len(v) for v in relations.values())
    print(f"Total: {len(all_genres)} nodes, {total} edges "
          f"from {len(parsed)} diagram(s)")
    return all_genres, relations


# ── parse ejemplo.html as a data source ──────────────────────────────────────
def parse_ejemplo(ej_path: Path = Path("ejemplo.html")) -> "tuple[list[dict], dict]":
    """
    Extract GENRES and rel_out from ejemplo.html.
    Returns (genres_list, relations_dict) in the same format as
    parse_mermaid_diagrams(), ready for assign_positions().
    """
    if not ej_path.exists():
        print(f"{ej_path} not found")
        return [], {}

    html = ej_path.read_text(encoding="utf-8", errors="replace")

    # ── extract raw GENRES array text via bracket counting ────────────────
    marker = "const GENRES = ["
    pos = html.find(marker)
    if pos == -1:
        print("GENRES array not found")
        return [], {}

    arr_start = pos + len(marker) - 1
    depth = 0; arr_end = arr_start; in_str = esc = False
    for i in range(arr_start, len(html)):
        c = html[i]
        if esc:              esc = False;  continue
        if c == "\\" and in_str: esc = True; continue
        if c == '"' and not esc: in_str = not in_str; continue
        if in_str:           continue
        if c == "[":         depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:  arr_end = i; break

    src = html[arr_start + 1 : arr_end]

    # ── split into individual JS object strings via brace counting ────────
    obj_strs: list[str] = []
    depth = 0; start = None; in_str = esc = False
    for i, c in enumerate(src):
        if esc:              esc = False;  continue
        if c == "\\" and in_str: esc = True; continue
        if c == '"' and not esc: in_str = not in_str; continue
        if in_str:           continue
        if c == "{":
            if depth == 0:  start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                obj_strs.append(src[start : i + 1])
                start = None

    def _str_attr(obj: str, name: str) -> str:
        m = re.search(rf'\b{name}\s*:\s*"([^"]*)"', obj)
        return m.group(1) if m else ""

    def _list_attr(obj: str, name: str) -> list:
        m = re.search(rf'\b{name}\s*:\s*(\[[\s\S]*?\])', obj)
        return re.findall(r'"([^"]+)"', m.group(1)) if m else []

    # ── parse each object ─────────────────────────────────────────────────
    raw: list[dict] = []
    for obj in obj_strs:
        gid = _str_attr(obj, "id")
        if not gid:
            continue
        raw.append({
            "id":      gid,
            "num":     _str_attr(obj, "num"),
            "genre":   _str_attr(obj, "genre"),
            "region":  _str_attr(obj, "region"),
            "artist":  _str_attr(obj, "artist"),
            "title":   _str_attr(obj, "title"),
            "year":    _str_attr(obj, "year"),
            "yt":      _str_attr(obj, "yt"),
            "rel_out": _list_attr(obj, "rel_out"),
        })

    if not raw:
        print("No genres parsed from ejemplo.html")
        return [], {}

    id_to_num = {g["id"]: g["num"] for g in raw}

    # ── build genres list in our unified format ───────────────────────────
    genres: list[dict] = []
    relations: dict[str, list] = {}

    for i, rg in enumerate(raw):
        num      = rg["num"]
        resolved = [id_to_num[rid] for rid in rg["rel_out"] if rid in id_to_num]

        # Try to extract a numeric year for layout ordering
        ys = rg["year"]
        ym = re.search(r"(\d{3,4})\s*(?:AD|CE|BC)?$", ys) or re.search(r"\b(\d{3,4})\b", ys)
        year_num = int(ym.group(1)) if ym else None

        genres.append({
            "id":         i,
            "num":        num,
            "number":     num,
            "genre":      rg["genre"],
            "year":       year_num,
            "year_str":   ys,          # original string ("2.5M–5000 BC")
            "start_year": year_num,
            "cat":        rg["region"],
            "fam":        "",          # ejemplo.html has no subgraphs
            "fam_idx":    0,
            "shape":      "(",
            "is_crossref": False,
            "recs":       [{"num": num,
                            "rec": " — ".join(filter(None, [rg["artist"], rg["title"]])),
                            "yt":  rg["yt"],
                            "year": year_num}],
            "all_nums":   [num],
            "alb_list":   [],
            "alb":        None,
        })
        if resolved:
            relations[num] = resolved

    total = sum(len(v) for v in relations.values())
    print(f"Parsed {len(genres)} genres, {total} edges from {ej_path.name}")
    return genres, relations


# ── load albums from DB ───────────────────────────────────────────────────────
def load_albums(db_path: Path) -> dict:
    import sqlite3
    if not db_path.exists():
        print(f"DB not found at {db_path}, skipping.")
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id FROM collections WHERE source_url LIKE '%8902a102%' LIMIT 1")
    row = cur.fetchone()
    if not row:
        conn.close()
        return {}
    col_id = row[0]
    cur.execute("""
        SELECT ca.rank,
               al.name AS album_name, al.year,
               al.youtube_url, al.spotify_url, al.lastfm_url,
               al.musicbrainz_url, al.discogs_url, al.bandcamp_url,
               al.rateyourmusic_url, al.allmusic_url,
               al.cover_url, al.scaruffi_rating, al.aoty_user_score, al.metacritic_score,
               ar.name AS artist_name, ar.country, ar.img_url AS artist_img,
               ar.lastfm_url AS artist_lastfm, ar.musicbrainz_url AS artist_mb,
               ar.wikipedia_url AS artist_wiki,
               SUBSTR(am.desc_lfm_album,  1, 600) AS desc_album,
               SUBSTR(am.desc_lfm_artist, 1, 400) AS desc_artist
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
    print(f"Loaded {len(albums)} albums (collection id={col_id})")
    return albums


# ── layout: two-level (subgraph blocks → nodes inside) ───────────────────────
def assign_positions(genres: list[dict], relations: dict) -> list[dict]:
    """
    1. Build a subgraph-level DAG and assign each subgraph a (col, row) slot
       using topological levels, so subgraph blocks never overlap.
    2. Within each block, sort nodes by topological order and lay them out
       in a single column so intra-block arrows flow top→bottom.
    """
    from collections import deque

    # ── tunables ─────────────────────────────────────────────────────────────
    N_STEP_X  = NODE_W + 24    # node → node horiz spacing inside a block
    N_STEP_Y  = NODE_H + 44    # node → node vert  spacing inside a block
    N_COLS    = 2              # max columns of nodes inside one subgraph block
    BLK_PAD   = 22             # padding around nodes inside a block
    BLK_LBL   = 20             # height of the subgraph label inside the block
    BLK_GAP_X = 70             # horizontal gap between blocks at the same level
    BLK_GAP_Y = 80             # vertical gap between levels of blocks
    DIAG_GAP  = 250            # horizontal gap between whole diagrams
    ORIGIN_X  = 300
    ORIGIN_Y  = 300

    by_cat: dict[str, list] = defaultdict(list)
    for g in genres:
        by_cat[g["cat"]].append(g)

    cats = [c for c in DIAGRAM_ORDER if c in by_cat]
    cats += [c for c in sorted(by_cat) if c not in cats]

    cur_diag_x = ORIGIN_X

    for cat in cats:
        cat_genres = by_cat[cat]
        cat_nums   = {g["num"] for g in cat_genres}
        by_num_c   = {g["num"]: g for g in cat_genres}

        # ── group genres by subgraph ──────────────────────────────────────
        by_sg: dict[str, list] = defaultdict(list)
        for g in cat_genres:
            by_sg[g["fam"]].append(g)
        all_sgs = list(by_sg.keys())

        # ── inter-subgraph adjacency ──────────────────────────────────────
        sg_ch:  dict[str, set] = defaultdict(set)   # sg → child sgs
        sg_par: dict[str, set] = defaultdict(set)   # sg → parent sgs
        for g in cat_genres:
            for to_num in relations.get(g["num"], []):
                if to_num not in cat_nums:
                    continue
                to_sg = by_num_c[to_num]["fam"]
                if to_sg != g["fam"]:
                    sg_ch[g["fam"]].add(to_sg)
                    sg_par[to_sg].add(g["fam"])

        # ── topological level for each subgraph ───────────────────────────
        sg_in_deg = {sg: len(sg_par[sg]) for sg in all_sgs}
        sg_lv: dict[str, int] = {}
        q: deque = deque()
        for sg in all_sgs:
            if sg_in_deg[sg] == 0:
                sg_lv[sg] = 0
                q.append(sg)
        if not q:
            sg_lv[all_sgs[0]] = 0
            q.append(all_sgs[0])
        while q:
            sg = q.popleft()
            for ch in sg_ch[sg]:
                new_lv = sg_lv[sg] + 1
                if ch not in sg_lv or sg_lv[ch] < new_lv:
                    sg_lv[ch] = new_lv
                    q.append(ch)
        for sg in all_sgs:
            if sg not in sg_lv:
                sg_lv[sg] = 0

        # ── pre-compute per-subgraph level structure ──────────────────────
        # (needed to get accurate block dimensions before block placement)
        sg_level_data: dict[str, dict] = {}  # sg → {nodes_by_level, max_wide, num_levels}

        for sg, sg_nodes in by_sg.items():
            sg_nums = {g["num"] for g in sg_nodes}
            sg_ch_l: dict[str, list] = defaultdict(list)
            sg_in_l: dict[str, int]  = defaultdict(int)
            for g in sg_nodes:
                for to in relations.get(g["num"], []):
                    if to in sg_nums:
                        sg_ch_l[g["num"]].append(to)
                        sg_in_l[to] += 1

            roots = [g["num"] for g in sg_nodes if sg_in_l[g["num"]] == 0]
            if not roots:
                roots = [sg_nodes[0]["num"]]

            # BFS max-depth level assignment
            lv_map: dict[str, int] = {}
            dq: deque = deque()
            for r in roots:
                lv_map[r] = 0
                dq.append(r)
            while dq:
                num = dq.popleft()
                for ch in sg_ch_l[num]:
                    new_lv = lv_map[num] + 1
                    if ch not in lv_map or lv_map[ch] < new_lv:
                        lv_map[ch] = new_lv
                        dq.append(ch)
            for g in sg_nodes:
                if g["num"] not in lv_map:
                    lv_map[g["num"]] = 0

            by_lv_l: dict[int, list] = defaultdict(list)
            for g in sg_nodes:
                by_lv_l[lv_map[g["num"]]].append(g)

            max_wide  = max(len(v) for v in by_lv_l.values())
            num_levels = max(by_lv_l.keys()) + 1
            sg_level_data[sg] = {
                "lv_map":     lv_map,
                "by_lv":      by_lv_l,
                "max_wide":   max_wide,
                "num_levels": num_levels,
                "ch":         sg_ch_l,
            }

        # ── compute block pixel dimensions using actual level structure ────
        def blk_size(sg: str) -> tuple:
            ld   = sg_level_data[sg]
            cols = ld["max_wide"]   # no cap — block width = widest level
            w    = cols * N_STEP_X - (N_STEP_X - NODE_W) + 2 * BLK_PAD
            h    = ld["num_levels"] * N_STEP_Y - (N_STEP_Y - NODE_H) + 2 * BLK_PAD + BLK_LBL
            return w, h

        # ── group subgraphs by level ──────────────────────────────────────
        by_lv: dict[int, list] = defaultdict(list)
        for sg in all_sgs:
            by_lv[sg_lv[sg]].append(sg)

        # Sort subgraphs within each level by fam_idx (mermaid file order)
        sg_fam_idx = {}
        for g in cat_genres:
            sg_fam_idx[g["fam"]] = min(sg_fam_idx.get(g["fam"], 999),
                                       g.get("fam_idx", 999))
        for lv in by_lv:
            by_lv[lv].sort(key=lambda sg: sg_fam_idx.get(sg, 999))

        # ── compute cumulative y per level (based on tallest block) ───────
        lv_y: dict[int, float] = {}
        cur_y = ORIGIN_Y
        for lv in sorted(by_lv.keys()):
            lv_y[lv] = cur_y
            max_h = max(blk_size(sg)[1] for sg in by_lv[lv])
            cur_y += max_h + BLK_GAP_Y

        # ── assign block x positions per level ────────────────────────────
        sg_blk_x: dict[str, float] = {}   # left edge of each subgraph block
        diag_total_w = 0

        for lv in sorted(by_lv.keys()):
            sgs_at_lv = by_lv[lv]
            # Sort by parent block x for less crossing
            if lv > 0:
                sgs_at_lv.sort(key=lambda sg: (
                    sum(sg_blk_x.get(p, 0) + blk_size(p)[0]/2
                        for p in sg_par[sg]) /
                    max(len(sg_par[sg]), 1)
                ))
            total_w = (sum(blk_size(sg)[0] for sg in sgs_at_lv)
                       + (len(sgs_at_lv) - 1) * BLK_GAP_X)
            diag_total_w = max(diag_total_w, total_w)

        # Second pass: lay out x centering each level over diag_total_w
        for lv in sorted(by_lv.keys()):
            sgs_at_lv = by_lv[lv]
            total_w = (sum(blk_size(sg)[0] for sg in sgs_at_lv)
                       + (len(sgs_at_lv) - 1) * BLK_GAP_X)
            bx = cur_diag_x + (diag_total_w - total_w) / 2
            for sg in sgs_at_lv:
                sg_blk_x[sg] = bx
                bx += blk_size(sg)[0] + BLK_GAP_X

        # ── place nodes inside each block (row = depth level) ────────────
        for sg, sg_nodes in by_sg.items():
            bx       = sg_blk_x.get(sg, cur_diag_x)
            by_start = lv_y[sg_lv[sg]]
            bw, _    = blk_size(sg)
            ld       = sg_level_data[sg]
            cols     = ld["max_wide"]   # matches blk_size → no overflow
            node_area_w = cols * N_STEP_X - (N_STEP_X - NODE_W)

            # Sort nodes within each level by avg parent x (reduces crossings)
            pos_x: dict[str, float] = {}
            for lv in sorted(ld["by_lv"].keys()):
                lv_nodes = ld["by_lv"][lv]
                if lv > 0:
                    lv_nodes.sort(key=lambda g: (
                        sum(pos_x.get(p, 0)
                            for p in ld["ch"]            # ch maps parent→[children]
                            if g["num"] in ld["ch"][p])  # find parents of g
                        or pos_x.get(g["num"], 0)
                    ))
                n = len(lv_nodes)
                row_w = (n - 1) * N_STEP_X + NODE_W
                start_x = bx + BLK_PAD + (node_area_w - row_w) / 2
                for i, node_g in enumerate(lv_nodes):
                    x = start_x + i * N_STEP_X
                    y = by_start + BLK_LBL + BLK_PAD + lv * N_STEP_Y
                    node_g["x"] = x
                    node_g["y"] = y
                    pos_x[node_g["num"]] = x

        cur_diag_x += diag_total_w + DIAG_GAP

    return genres


# ── build JS data ─────────────────────────────────────────────────────────────
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
        entry = {
            "id":         g["id"],
            "num":        g["num"],
            "genre":      g["genre"],
            "year":       g.get("year"),
            "year_str":   g.get("year_str", ""),
            "start_year": g.get("start_year"),
            "cat":        g["cat"],
            "fam":        g.get("fam", ""),
            "fam_idx":    g.get("fam_idx", 999),
            "x":          g["x"],
            "y":          g["y"],
            "rel_out":    relations.get(g["num"], []),
            "recs":       g.get("recs", []),
        }
        if g.get("alb_list"):
            entry["alb_list"] = [_serialize_alb(a) for a in g["alb_list"]]
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
  cursor:pointer;transition:border-color .2s,box-shadow .2s,opacity .15s;
  user-select:none;padding:7px 26px 7px 9px}}
.node:hover{{border-color:var(--border-active);z-index:10}}
.node.active{{border-color:var(--accent);
  box-shadow:0 0 0 1px var(--accent),0 8px 40px rgba(200,169,110,.2);z-index:20}}
.node.dimmed{{opacity:.1;pointer-events:none}}
.node-title{{font-size:11px;font-weight:700;color:var(--accent);
  letter-spacing:.02em;text-transform:uppercase;line-height:1.35}}
.node-title .yr{{color:var(--text3);font-weight:400;font-family:var(--font-mono);
  font-size:10px;margin-right:4px}}
.node-sub{{font-size:9px;color:var(--text3);margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.node-plus{{position:absolute;top:6px;right:6px;width:17px;height:17px;
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
#panel-year{{font-size:11px;font-family:var(--font-mono);color:var(--text3)}}
#panel-genre{{font-family:var(--font-serif);font-size:22px;
  font-style:italic;color:var(--text);margin:3px 0 2px;line-height:1.2}}
#panel-cat{{font-size:10px;color:var(--text3)}}
#panel-body{{flex:1;overflow-y:auto;padding:12px 16px}}
#panel-body::-webkit-scrollbar{{width:4px}}
#panel-body::-webkit-scrollbar-thumb{{background:var(--border)}}
.panel-section{{margin-bottom:14px}}
.panel-label{{font-size:8px;color:var(--text3);letter-spacing:.1em;
  text-transform:uppercase;margin-bottom:5px}}
.panel-rec-item{{padding:6px 0;border-bottom:1px solid var(--border);display:flex;
  flex-direction:column;gap:4px}}
.panel-rec-item:last-child{{border-bottom:none}}
.panel-rec-text{{font-family:var(--font-serif);font-style:italic;
  font-size:12px;color:var(--text2);line-height:1.4}}
.panel-rec-yt{{font-size:9px;letter-spacing:.06em;padding:2px 7px;
  border:1px solid var(--border);border-radius:20px;color:var(--text2);
  background:none;cursor:pointer;align-self:flex-start;
  transition:border-color .15s,color .15s;font-family:var(--font)}}
.panel-rec-yt:hover,.panel-rec-yt.active{{border-color:#e00;color:#e00}}
.panel-yt-wrap{{position:relative;padding-top:56.25%;background:#000;
  border-radius:2px;overflow:hidden;margin-bottom:10px}}
.panel-yt-wrap iframe{{position:absolute;inset:0;width:100%;height:100%;border:0}}
.panel-alb-card{{padding:8px 0;border-bottom:1px solid var(--border);display:flex;gap:10px}}
.panel-alb-card:last-child{{border-bottom:none}}
.panel-alb-cover{{width:64px;height:64px;object-fit:cover;border-radius:2px;
  border:1px solid var(--border);flex-shrink:0}}
.panel-alb-info{{flex:1;min-width:0}}
.panel-alb-name{{font-family:var(--font-serif);font-style:italic;font-size:13px;
  color:var(--text);line-height:1.3}}
.panel-alb-artist{{font-size:11px;color:var(--text2);margin-top:2px}}
.panel-alb-meta{{font-size:9px;color:var(--text3);margin-top:2px;letter-spacing:.04em}}
.panel-links{{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px}}
.panel-link{{font-size:9px;letter-spacing:.05em;padding:3px 8px;
  border:1px solid var(--border);border-radius:20px;color:var(--text2);
  text-decoration:none;transition:border-color .15s,color .15s;white-space:nowrap}}
.panel-link:hover{{border-color:var(--accent);color:var(--accent)}}
.panel-desc{{font-size:10px;color:var(--text2);line-height:1.6;margin-top:6px}}
.panel-scores{{display:flex;gap:10px;flex-wrap:wrap}}
.panel-score{{text-align:center}}
.panel-score-val{{font-size:14px;font-weight:700;color:var(--accent)}}
.panel-score-lbl{{font-size:8px;color:var(--text3);letter-spacing:.06em}}

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
    <script defer src="https://cloud.umami.is/script.js" data-website-id="5d84fd6c-0760-4a0c-a2d0-ffabb82179f5"></script>
</head>
<body>

<div id="ui">
  <div id="topbar">
    <div id="title-block">
      <h1>Music Genre Tree</h1>
      <p>{subtitle}</p>
    </div>
    <div id="search-wrap">
      <input id="search" type="text" placeholder="Buscar género, año, subgénero…" autocomplete="off">
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
    <div id="panel-year"></div>
    <div id="panel-genre"></div>
    <div id="panel-cat"></div>
  </div>
  <div id="panel-body">
    <div class="panel-section" id="p-yt-section" style="display:none">
      <div class="panel-yt-wrap">
        <iframe id="p-yt" allowfullscreen allow="autoplay;encrypted-media"></iframe>
      </div>
    </div>
    <div class="panel-section" id="p-recs-section" style="display:none">
      <div class="panel-label">Grabaciones representativas</div>
      <div id="p-recs-list"></div>
    </div>
    <div class="panel-section" id="p-albums-section" style="display:none">
      <div class="panel-label">Álbumes</div>
      <div id="p-albums-list"></div>
    </div>
  </div>
</div>

<div id="hint">Arrastra · Rueda para zoom · Clic en nodo · + para info · Clic en fondo para deseleccionar</div>
<div id="zoom-btns">
  <button class="z-btn" id="zin">+</button>
  <button class="z-btn" id="zout">−</button>
</div>
<button id="reset-btn">↺ Reset vista</button>

<script>
// ── DATA ──────────────────────────────────────────────────────────────────
const GENRES = {genres_json};

// ── category color ────────────────────────────────────────────────────────
const CAT_COLORS = {{
  classical:"#1a237e", jazz:"#7a3e00", rnb:"#5c0032",
  hiphop:"#2a0060", rock:"#6b0000", edm:"#00205b", experimental:"#3e0060",
}};
function catColor(cat) {{
  if (CAT_COLORS[cat]) return CAT_COLORS[cat];
  let h = 0;
  for (let i = 0; i < cat.length; i++) h = (h * 31 + cat.charCodeAt(i)) & 0xffffffff;
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

// ── render setup ──────────────────────────────────────────────────────────
const graph = document.getElementById("graph");
const svgEl = document.getElementById("canvas-layer");
let tx = 0, ty = 0, scale = 0.15;

const byNum = {{}};
GENRES.forEach(g => {{ byNum[g.num] = g; }});

// ── edges ─────────────────────────────────────────────────────────────────
const edgeEls = {{}};
const NW = {nw}, NH = {nh};

function nodeAnchors(g) {{
  const cx = g.x + NW/2, cy = g.y + NH/2;
  return {{
    x: cx, y: cy,
    top:   {{x: cx,      y: g.y}},
    bot:   {{x: cx,      y: g.y + NH}},
    left:  {{x: g.x,     y: cy}},
    right: {{x: g.x + NW, y: cy}},
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
    path.setAttribute("stroke","rgba(255,255,255,0.22)");
    path.setAttribute("stroke-width","1.5");
    path.setAttribute("marker-end","url(#arr-dim)");
    path.dataset.from = g.num;
    path.dataset.to   = toNum;
    svgEl.appendChild(path);
    edgeEls[key] = path;
  }});
}});

let activeNum = null;

function drawEdges() {{
  svgEl.setAttribute("viewBox", `0 0 ${{window.innerWidth}} ${{window.innerHeight}}`);
  for (const path of Object.values(edgeEls)) {{
    const from = byNum[path.dataset.from];
    const to   = byNum[path.dataset.to];
    if (!from || !to) {{ path.setAttribute("d",""); continue; }}
    const [ap, bp] = bestAnchors(nodeAnchors(from), nodeAnchors(to));
    path.setAttribute("d", curvePath(ws(ap), ws(bp)));

    if (activeNum) {{
      const isOut = path.dataset.from === activeNum;
      const isIn  = path.dataset.to   === activeNum;
      if (isOut) {{
        path.setAttribute("stroke","rgba(200,169,110,.65)");
        path.setAttribute("stroke-width","1.8");
        path.setAttribute("marker-end","url(#arr-gold)");
      }} else if (isIn) {{
        path.setAttribute("stroke","rgba(126,184,200,.55)");
        path.setAttribute("stroke-width","1.8");
        path.setAttribute("marker-end","url(#arr-blue)");
      }} else {{
        path.setAttribute("stroke","rgba(255,255,255,.03)");
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

// ── subgraph boxes + diagram/subgraph labels ──────────────────────────────
(function() {{
  const PAD = 16, LBL = 22;

  // Compute subgraph bounding boxes
  const famBounds = {{}};
  GENRES.forEach(g => {{
    if (!g.fam) return;
    const key = g.cat + "\x00" + g.fam;
    if (!famBounds[key])
      famBounds[key] = {{fam: g.fam, cat: g.cat,
        minX:1e9, minY:1e9, maxX:-1e9, maxY:-1e9}};
    const b = famBounds[key];
    b.minX = Math.min(b.minX, g.x);
    b.minY = Math.min(b.minY, g.y);
    b.maxX = Math.max(b.maxX, g.x + NW);
    b.maxY = Math.max(b.maxY, g.y + NH);
  }});

  // Render boxes first (so nodes appear on top)
  for (const [, b] of Object.entries(famBounds)) {{
    const box = document.createElement("div");
    box.style.cssText =
      `position:absolute;` +
      `left:${{b.minX - PAD}}px;top:${{b.minY - LBL - PAD}}px;` +
      `width:${{b.maxX - b.minX + PAD*2}}px;` +
      `height:${{b.maxY - b.minY + LBL + PAD*2}}px;` +
      `border:1px solid rgba(255,255,255,0.07);border-radius:4px;` +
      `background:rgba(255,255,255,0.018);pointer-events:none`;
    graph.appendChild(box);

    const lbl = document.createElement("div");
    lbl.style.cssText =
      `position:absolute;` +
      `left:${{b.minX - PAD + 7}}px;top:${{b.minY - LBL - PAD + 6}}px;` +
      `color:rgba(255,255,255,.22);font-size:8px;font-family:var(--font-mono);` +
      `letter-spacing:.13em;text-transform:uppercase;pointer-events:none`;
    lbl.textContent = b.fam;
    graph.appendChild(lbl);
  }}

  // Diagram name labels (above everything)
  const diagBounds = {{}};
  GENRES.forEach(g => {{
    if (!diagBounds[g.cat]) diagBounds[g.cat] = {{minX:1e9, minY:1e9}};
    const b = diagBounds[g.cat];
    b.minX = Math.min(b.minX, g.x);
    b.minY = Math.min(b.minY, g.y);
  }});
  for (const [cat, b] of Object.entries(diagBounds)) {{
    const el = document.createElement("div");
    el.style.cssText =
      `position:absolute;left:${{b.minX - PAD}}px;top:${{b.minY - LBL - PAD - 28}}px;` +
      `color:rgba(255,255,255,.45);font-size:12px;font-family:var(--font-mono);` +
      `letter-spacing:.2em;text-transform:uppercase;font-weight:700;pointer-events:none`;
    el.textContent = cat;
    graph.appendChild(el);
  }}
}})();

// ── node elements ─────────────────────────────────────────────────────────
const nodeEls = {{}};

GENRES.forEach(g => {{
  const div = document.createElement("div");
  div.className = "node";
  div.id = "n" + g.id;
  div.style.left = g.x + "px";
  div.style.top  = g.y + "px";
  div.style.borderTopColor = catColor(g.cat);

  const yrTxt = g.year_str || (g.year ? String(g.year) : "");
  const yr = yrTxt ? `<span class="yr">${{yrTxt}}</span>` : "";
  div.innerHTML =
    `<button class="node-plus" title="Info">+</button>` +
    `<div class="node-title">${{yr}}${{g.genre}}</div>` +
    `<div class="node-sub">${{g.fam || g.cat}}</div>`;

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
  setText("panel-year",  g.year ? String(g.year) : "");
  setText("panel-genre", g.genre);
  document.getElementById("panel-cat").textContent =
    [g.cat, g.fam].filter(Boolean).join(" › ");

  document.getElementById("p-yt").src = "";
  show("p-yt-section", false);
  activeYtBtn = null;

  // Recordings
  const recsList = document.getElementById("p-recs-list");
  recsList.innerHTML = "";
  const recs = (g.recs || []).filter(r => r.rec && r.rec !== g.genre);
  if (recs.length) {{
    recs.forEach(r => {{
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
    show("p-recs-section", true);
  }} else {{
    show("p-recs-section", false);
  }}

  // Albums from DB
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
          <div class="panel-links">
            ${{linkDefs.map(([u,l])=>`<a class="panel-link" href="${{u}}" target="_blank" rel="noopener">${{l}}</a>`).join("")}}
            ${{vid ? `<button class="panel-rec-yt" data-vid="${{vid}}">▶ YouTube</button>` : ""}}
          </div>
          ${{(a.desc_album||a.desc_artist) ? `<div class="panel-desc">${{a.desc_album||a.desc_artist}}</div>` : ""}}
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
  document.getElementById("p-yt").src = "";
}});

// ── node activation ───────────────────────────────────────────────────────
function deactivate() {{
  activeNum = null;
  Object.values(nodeEls).forEach(el => el.classList.remove("active","dimmed"));
  drawEdges();
}}

function activateNode(id) {{
  const g = GENRES.find(g => g.id === id);
  if (!g) return;
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
  const was = dragging; dragging = false;
  document.body.style.cursor = "default";
  if (was && !hasDragged && !e.target.closest(".node,#ui,#zoom-btns,#reset-btn,#info-panel")) {{
    deactivate();
  }}
}});

document.addEventListener("wheel", e => {{
  e.preventDefault();
  const f  = e.deltaY < 0 ? 1.08 : 0.92;
  const ns = Math.max(0.05, Math.min(2.5, scale * f));
  tx = e.clientX - (e.clientX - tx) * (ns / scale);
  ty = e.clientY - (e.clientY - ty) * (ns / scale);
  scale = ns; applyTransform();
}}, {{passive:false}});

document.getElementById("zin").onclick  = () => {{ scale = Math.min(2.5,scale*1.15); applyTransform(true); }};
document.getElementById("zout").onclick = () => {{ scale = Math.max(0.05,scale/1.15); applyTransform(true); }};
document.getElementById("reset-btn").onclick = () => {{
  tx = 0; ty = 0; scale = 0.15;
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
    countEl.textContent = ""; activeNum = null; drawEdges(); return;
  }}
  let hits = 0;
  GENRES.forEach(g => {{
    const albText = (g.alb_list||[]).map(a =>
      [(a.artist_name||""),(a.album_name||"")].join(" ")).join(" ").toLowerCase();
    const match =
      g.genre.toLowerCase().includes(q) ||
      g.cat.toLowerCase().includes(q) ||
      (g.fam||"").toLowerCase().includes(q) ||
      (g.year ? String(g.year).includes(q) : false) ||
      albText.includes(q);
    nodeEls[g.id].classList.toggle("dimmed", !match);
    if (match) hits++;
  }});
  countEl.textContent = `${{hits}} resultado${{hits !== 1 ? "s" : ""}}`;
}});

// ── touch ─────────────────────────────────────────────────────────────────
let lastDist = 0;
document.addEventListener("touchstart", e => {{
  if (e.touches.length === 1) {{
    dragging = true; hasDragged = false;
    startX = e.touches[0].clientX; startY = e.touches[0].clientY;
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
    if (Math.hypot(e.touches[0].clientX-startX, e.touches[0].clientY-startY) > 4)
      hasDragged = true;
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
    n           = len(genres)
    total_edges = sum(len(v) for v in relations.values())
    diagrams    = sorted({g["cat"] for g in genres})
    subtitle    = (f"musicgenretree.org · {n} géneros · {total_edges} conexiones · "
                   f"{len(diagrams)} diagrama(s): {', '.join(diagrams)}")
    return HTML.format(
        nw=NODE_W,
        nh=NODE_H,
        subtitle=subtitle,
        genres_json=js_data,
    )


# ── main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    src_idx = next((i for i, a in enumerate(args) if a == "--source"), None)
    SOURCE  = args[src_idx + 1] if src_idx is not None and src_idx + 1 < len(args) else "mermaid"

    if SOURCE == "ejemplo":
        ej_path  = Path(next((a for a in args if a.endswith(".html")), "ejemplo.html"))
        out_path = ej_path.with_stem(ej_path.stem + "_td")
        genres, relations = parse_ejemplo(ej_path)
        if not genres:
            raise SystemExit("No genre data parsed from ejemplo.html")
    else:
        out_path = OUTPUT
        print(f"Scanning {MERMAID_DIR}/…")
        genres, relations = parse_mermaid_diagrams(MERMAID_DIR)
        if not genres:
            raise SystemExit("No genre data — add .md files to mermaid_diagrams/")

    if not NO_DB and SOURCE != "ejemplo":
        load_albums(DB_PATH)   # future: name-based matching

    genres = assign_positions(genres, relations)

    out = generate_html(genres, relations)
    out_path.write_text(out, encoding="utf-8")
    print(f"✓ Written {out_path} ({len(out)//1024} KB, {len(genres)} nodes)")
