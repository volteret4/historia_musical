# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-file HTML/CSS/JavaScript interactive visualization of musical genre history and relationships. No build tools, frameworks, or dependencies — open `ejemplo.html` directly in any browser.

## Development

**Run:** Open `ejemplo.html` in a browser.

**Pre-commit hooks:** Uses Gitleaks for secret detection.
```bash
pre-commit install   # one-time setup
```

No linting, no tests, no build steps.

## Architecture

Everything lives in `ejemplo.html` (~3000 lines). The sections, in order:

1. **`<style>`** (lines 7–401) — All CSS: node card styles, edge animations, search bar, zoom controls, active/dimmed states.

2. **`<body>` HTML** (lines 338–401) — Two key layers inside `#world`:
   - `#canvas` (SVG) — renders Bézier curve edges between nodes
   - `#graph` (div) — holds all genre node cards as positioned `<div>`s

3. **`GENRES[]` data array** (lines ~407–2284) — ~150 genre objects with fields:
   - `id`, `num`, `genre`, `region`, `artist`, `title`, `year`
   - `rel_out[]` — array of `id`s this genre connects to

4. **`REGION_COLOR{}`** (lines ~2287–2327) — maps region strings to hex colors.

5. **`assignPositions()`** (lines ~2336–2600) — manual layout: groups genres by region cluster, positions nodes on a grid using constants `NW=280`, `NH=130`, `GAPX=60`, `GAPY=50`.

6. **Rendering** (lines ~2615–2810) — creates DOM node cards and SVG edges:
   - `drawEdges()` — draws Bézier paths via `bestAnchors()` + `curvePath()`
   - `makeEdge()` — creates/manages individual SVG `<path>` elements

7. **Interaction** (lines ~2826–3004):
   - Global state: `tx`, `ty` (pan), `scale` (zoom, range 0.15–2.5), `activeId`
   - `activateNode(id)` — highlights selected genre and its direct relations, dims others
   - `applyTransform()` — applies CSS `transform` to `#graph` and `#canvas`
   - Search filters across `genre`, `artist`, `title`, `region` fields
   - Touch events: single-finger pan, two-finger pinch zoom

## Key Conventions

- Node position is set as `el.style.left` / `el.style.top` (pixels, absolute within `#graph`).
- Edges are SVG `<path>` elements inside `#canvas`; both `#graph` and `#canvas` share the same CSS transform so they stay in sync.
- `rel_out` is directional but edges are drawn for all connections visible in the active node's neighborhood.
- Region strings in `GENRES` must have a matching key in `REGION_COLOR` or nodes fall back to a default color.
