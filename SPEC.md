# DAZ Content Catalog — Design Spec

A queryable database of your DAZ / Renderotica 3D content, exposed to an LLM via MCP so you can ask *"what assets do I have for a scene where a bride is attacked by zombie bridesmaids?"* and get back a sensible, ranked shortlist.

---

## 1. Purpose & goals

**Problem.** Your catalog metadata already exists (LLM-generated classification, attributes, visual summary, keywords) but it's trapped in one large flat markdown file (`master_index-new.md`, ~11 MB). RAG tools like AnythingLLM chunk + embed that file, so retrieval is fuzzy similarity and "misses things." It isn't a real lookup.

**Goal.** Store the metadata in a proper database with **deterministic keyword search**, exposed through an MCP server. The LLM does the *thinking* (turning a scene concept into candidate keywords); the DB does the *finding* (fast, exact, reproducible). Two-step flow:

1. `search(keywords)` → ranked list of matching items (compact).
2. `get_item(id)` → full detail for a chosen item.

**Success criteria.** Given *"bride attacked by zombie bridesmaids,"* the LLM generates keywords like `wedding, bride, dress, veil, zombie, undead, corpse, attack, combat pose`, calls `search`, and gets back relevant items (bridal dresses, wedding props, zombie/undead characters or FX, action poses) ranked so the best multi-concept matches come first.

### Non-goals (for now)
- No semantic / embedding search in Phase 1–2 (revisit only if keyword searches come back empty too often).
- No attribute-level filters beyond `category` (e.g., era, gender) — deferred.
- No support for vendors other than DAZ and Renderotica yet.
- No web UI; access is via MCP tools (+ a small CLI in Phase 1 for validation).

---

## 2. Architecture & data flow

```
[markdown index files]          [SQLite DB: catalog.db]              [MCP server]
 master_index.md        ─┐                          ┌──────────────►   search()
 master_index-new.md    ─┴─► importer (parse +      │  items           get_item()
                            normalize + upsert)     │  (+ indexes)     list_categories()
                                                    │                  stats()
[future] scraper_app re-scrape ────────────────────┘   (upsert, idempotent)
```

- **Importer** parses the markdown blocks, normalizes the inconsistent LLM JSON, and upserts into SQLite. Idempotent — safe to re-run.
- **SQLite DB** is a single file (`catalog.db`). At ~4k rows it's tiny; search is an indexed scan + in-process relevance scoring (no FTS5 needed yet).
- **MCP server** wraps the DB and exposes the tools below. Registered with LM Studio Bionic so any session can call it.

---

## 3. Data model

### Table: `items`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | Internal id (stable, used by `get_item`). |
| `filename` | TEXT UNIQUE | Thumbnail filename — the globally unique key (e.g. `DAZ_3D_100010_Sparkly_Earrings_for_Genesis_9.jpg`). Upsert/dedupe key. |
| `vendor` | TEXT | `DAZ` / `RENDEROTICA`. |
| `sku` | TEXT | Vendor SKU (unique per vendor, not globally). |
| `product_name` | TEXT | From the `## Product:` header. |
| `classification` | TEXT | Normalized top-level category (see §3.2 controlled list). Indexed for the `category` filter. |
| `description` | TEXT | Web-scraped product description. |
| `visual_summary` | TEXT | LLM visual description of the thumbnail(s). |
| `search_keywords` | TEXT | JSON array of keywords (normalized to a list). |
| `attributes_json` | TEXT | Full normalized attribute object (for detail view + future filters). |
| `attributes_text` | TEXT | All attribute values flattened into one searchable string. |
| `product_url` | TEXT | Canonical URL (see §7). |
| `thumbnail_path` | TEXT | Absolute local path to the thumbnail image. |
| `source_file` | TEXT | Which markdown file this row came from. |
| `metadata_model` | TEXT NULL | Which LLM generated the metadata (set during re-scrape; helps track quality). |
| `updated_at` | TIMESTAMP | Last import/update time. |

**Indexes:** unique on `filename`; index on `(vendor, sku)` for SKU lookup; index on `classification`.

> **Why no FTS5 yet?** ~4k rows is trivially fast to scan in-process (sub-10 ms). We keep Phase 1 simple and still get a *true deterministic DB lookup*. FTS5 is added later only if the corpus grows large or we want sub-millisecond candidate retrieval / easy AND semantics.

### 3.1 Normalization rules (importer)
The LLM JSON is inconsistent in shape, so the importer must be tolerant:
- Strip markdown code fences; parse the `### Metadata & Attributes` block as JSON (fall back to best-effort if malformed).
- **classification** = top-level `"Classification"`, mapped through the alias table (§3.2).
- **visual_summary** = `"Visual_Summary"`.
- **search_keywords** = `"Search_Keywords"` → always normalize to a Python list (split comma-strings; keep lists as-is) → store as JSON array.
- **attributes**: merge *all* of these into one flat dict: top-level attribute keys, the nested `"Attributes"` object, and bundle info (`"Bundle_Info"` / top-level `"Contains"` / `"Theme"` / `"Primary Use"`). Store merged result in `attributes_json`; flatten every value (expanding lists) into `attributes_text`.
- **product_url** derived from vendor + sku (§7).
- **thumbnail_path** = `<thumbnail_dir>/<filename>` (dir is a config value, §8).

### 3.2 Controlled category vocabulary
Normalized to one of: `Character`, `Animal`, `Environment`, `Props`, `Clothes`, `Hair`, `Poses`, `Vehicles`, `Accessories`, `Anatomy`, `Materials`, `FX`, `Utilities`, `Bundle/Collection`, `Other`. (`Bundle/Collection` was added during Phase 1 — ~600 products carry it in the raw data and it is a useful filter for scene building.)

Alias map (examples): `"Clothes (Apparel/Armor)" → Clothes`; `"Environment (Full Scenes/Dioramas)" → Environment`; `"Props (Objects/Weapons/Furniture)" → Props`; `"Poses (Action/Static poses)" → Poses`; `"Materials (Textures/Shaders/HDRI)" → Materials`. Unknown values are kept verbatim but flagged.

---

## 4. Import strategy

- **Merge both files, "new" wins on conflict.** Confirmed overlap:
  - In both: **3,766** (use the richer `master_index-new.md` version)
  - Only in old: **238** (backfill with older metadata)
  - Only in new: **109**
  - **Total ≈ 4,113 products.**
- Importer accepts one or more source files applied in order; later occurrences of the same `filename` overwrite earlier ones. Default run = import both, old first then new (so new wins).
- Idempotent upsert on `filename`; re-running is safe and only updates changed rows + bumps `updated_at`.

---

## 5. Search design (`search`)

**Semantics:** OR across keywords with **relevance scoring**. An item matches if it contains *any* keyword; items matching *more* of your keywords — especially in high-signal fields — rank higher. (A strict-AND variant is a possible later tool, not now.)

### Scoring algorithm
```
FIELD_WEIGHTS = {
  product_name:     3.0,
  search_keywords:  3.0,
  classification:   2.0,
  visual_summary:   1.5,
  attributes_text:  1.0,
  description:      0.5,
}

for each candidate item:            # all rows, or pre-filtered by category
    score = 0; matched = []
    for kw in keywords (lowercased):
        best = max(weight of any field where `kw` appears as a case-insensitive substring)
        if best > 0:
            score += best
            matched.append(kw)
    keep item if score > 0, with (score, matched)

sort by score DESC (tie-break: product_name ASC); return top `limit`.
```

- **Matching** = case-insensitive substring (so `bride` matches *bridesmaid*, `zombie` matches *zombies*). Word-boundary matching is a tunable option if substring proves too loose.
- **Category filter** (optional): applied before scoring; item's normalized `classification` must equal the requested category (case-insensitive).
- Weights and limits are config values (§8), not hardcoded, so we can tune after seeing real results.

---

## 6. MCP interface

Server name: `daz-catalog`. Four tools (the last two are cheap and help the LLM plan queries):

### `search`
```jsonc
// params
{ "keywords": ["bride","wedding","zombie","undead"],   // required, >=1
  "category": "Clothes",                                // optional
  "limit": 20 }                                         // optional, default 20, max 50
// returns
{ "count": 47,
  "results": [
    { "id": 1839, "product_name": "...", "vendor":"DAZ", "sku":"...",
      "classification":"Clothes", "score": 6.0,
      "matched_keywords":["bride","wedding"],
      "visual_summary":"A white lace wedding gown..." } ] }
```

### `get_item`
```jsonc
// params — by id OR by sku(+vendor)
{ "id": 1839 }                 // or { "sku":"100049", "vendor":"DAZ" }
// returns full detail (see §7)
```

### `list_categories`
Returns distinct normalized categories with item counts — lets the LLM know valid `category` values and what's in the library.

### `stats`
Total items, per-category counts, per-vendor counts. Cheap orientation tool.

---

## 7. Detail view (`get_item`) contents

Returned fields: `product_name`, `filename`, `vendor`, `sku`, `classification`, `description`, `visual_summary`, `attributes` (object), `search_keywords` (array), `thumbnail_path`, `product_url`.

- **Thumbnail:** absolute local path so the harness/user can view it.
- **Product URL** (canonical, modern):
  - DAZ: `https://www.daz3d.com/catalog/product/view/id/{sku}`
  - Renderotica: `https://www.renderotica.com/store/sku/{sku}_`
- **Legacy low-poly index link** (e.g. SKUs in the old product-index range, per docs.daz3d.com): *deferred* — low value for now; can be added as an optional `legacy_index_url` field later if you ever need it.

---

## 8. Config & file layout

Proposed layout under this workspace:
```
Daz Context DB/
  SPEC.md                     <- this document
  daz-catalog/
    config.json               <- sources, thumbnail dir, weights, limits
    import_catalog.py         <- Phase 1 importer + CLI search
    catalog.db                <- generated SQLite (gitignore-able)
    mcp_server.py             <- Phase 2 MCP server
```

`config.json` (defaults):
```jsonc
{
  "sources": ["Old Code/master_index.md", "Old Code/master_index-new.md"], // applied in order, last wins
  "thumbnail_dir": "C:/Users/Adam/Documents/AnythingLLM/Daz3d/Product-Thumbnails",
  "db_path": "catalog.db",
  "field_weights": { "product_name":3.0,"search_keywords":3.0,"classification":2.0,
                     "visual_summary":1.5,"attributes_text":1.0,"description":0.5 },
  "default_limit": 20, "max_limit": 50
}
```

---

## 9. Development phases

**Phase 0 — Spec & decisions.** This document; confirm open items (§10).

**Phase 1 — Importer + SQLite DB (validate the design).**
Build `import_catalog.py`: parse both markdown files, normalize, upsert into `catalog.db`. Include a tiny **CLI search** (`python import_catalog.py search "bride wedding zombie" --category Clothes`) so we can validate relevance scoring on real data *before* any MCP work.
- Deliverables: `catalog.db` (~4,113 rows) + importer + CLI search.
- Done when: scene-style queries return sensible ranked results and spot-checked details are accurate; weights tuned to taste.

**Phase 2 — MCP server.**
Build `mcp_server.py` exposing `search`, `get_item`, `list_categories`, `stats`; register with LM Studio Bionic.
- Done when: end-to-end "ask the LLM for a scene" works in a session.

**Phase 3 — Re-scrape integration.**
Modify `scraper_app` to **upsert into the DB** (idempotent, keyed on filename) instead of appending markdown; record `metadata_model`; support re-running with a better vision model (e.g. qwen3-27b) without duplicating rows or losing data.

**Later / optional.**
Strict-AND search tool · semantic/embedding fallback when results are empty · attribute filters (era, gender…) · additional vendors · legacy index links · thumbnail display in UI.

---

## 10. Open items to confirm (defaults proposed)

1. **Default `limit`** = 20, max 50 — OK?
2. **Field weights** as in §8 — start here and tune after Phase 1? (I expect we'll nudge these once we see real rankings.)
3. **DB + code location** under `Daz Context DB/daz-catalog/` — OK, or prefer elsewhere?
4. **Thumbnail dir** = the AnythingLLM `Product-Thumbnails` path from your scraper — confirm that's still where thumbnails live (it's outside this workspace; I'll need read access to verify paths resolve).
5. **Import scope** = merge old+new with new-wins (~4,113 rows) as in §4 — OK?
