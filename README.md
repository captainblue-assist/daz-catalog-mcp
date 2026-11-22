# daz-catalog-mcp

A queryable catalog of DAZ / Renderotica 3D content, exposed to an LLM via an MCP server so you can ask "what assets do I have for a scene where a bride is attacked by zombie bridesmaids?" and get back a ranked shortlist.

Instead of dumping all metadata into one big markdown file for RAG (fuzzy, misses things), this stores the per-product metadata in SQLite with deterministic keyword search. The LLM does the thinking (scene to keywords); the database does the finding.

## How it works
Two-step lookup exposed over MCP:
1. `search(keywords[], category?, limit?)` - OR across keywords, ranked by relevance. Returns a compact list of matches.
2. `get_item(id | sku+vendor)` - full detail for one item (description, attributes, visual summary, thumbnail path, product URL).

Plus helpers: `list_categories`, `stats`.

## Example

Prompt sent to an LLM with this MCP server registered (fresh session, no other context):

> I want to create a scene in which a bride is attacked by zombie bridesmaids. What assets do I have that would help with this topic?

The agent searches the catalog (`search` across several keyword sets), pulls details on the top candidates (`get_item`), and returns:

Great news — your library has everything needed for this scene. Here's what I found, organized by role:

### 👰 The Bride
- **The Bride Wedding Gown for Genesis 8 Female** (SKU 48869) — strapless white lace gown with veil and pink rose bouquet; the most "bride" option you own
- **dForce White Wedding Outfit for Genesis 8 Female** (SKU 53017) — long-sleeved lace gown with dramatic train, sheer veil, plus a burgundy/black variation if you want a darker twist

### 🧟 Zombie Bridesmaids
You don't have a dedicated "bridesmaid dress" product, but the trick is to reuse your wedding gowns on multiple figures and decay them:
- **Horrifying Skins for Genesis 3 and 8 Female** (SKU 48659) — necrotic, infected, infested flesh textures; ideal zombie skin
- **Wound Makeup Artist for Genesis 8** (SKU 61855) — bloody cuts, split lips, deep slashes from scratches to heavy lacerations
- **Iray Wound Kit** (SKU 46953) — wound decals (bruises, cuts, grazes) that apply without UV work
- **SY Splats, Drips and Splatters for dForce** (SKU 52869) — blood splatter/drip decals to stain the dresses and floor
- **M3D Horror Eyes for Genesis 9** (SKU 91787) — glowing or bloody eye presets if you go G9

### 🎭 Poses
- **A Zombie Survival Guide for West Park Ablutions** (SKU 67425) — humans in fear/combat/survival interacting with zombies; closest match to your exact scenario
- **Ancient Swarm for Genesis 8** (SKU 53527) — characters struggling against pale zombie-like creatures, predatory movement
- **Z Fear Itself Poses for Genesis 3 and 8** (SKU 64835) — modular terrified poses (limb/hand/face partials) for the bride's panic
- **Perilous Drama Poses for Genesis 8 Female** (SKU 71493) — 40 distress/fear/drama poses, some cowering or on the floor
- **Z Crawling and Creeping Utility Pose Mega Set** (SKU 93767) — crawling/creeping partials to make bridesmaids lurch at her
- **Fighting Dirty Poses** (SKU 98057) — aggressive hair-pulling, eye-gouging grapples for the attack itself

### ⛪ Setting
- **Sacrament Iray Addon** (SKU 37661) — Gothic cathedral interior with pews and stained glass: a proper wedding venue
- **Mortis for Sacrament Iray Addon** (SKU 62781) — the *same* cathedral but abandoned, ivy-overgrown, and eerie. Using Mortis gives you "wedding church gone wrong" in one environment
- Alternatives: **Garden Wedding** (SKU 52157, outdoor gazebo ceremony) or **Enchanted Ballroom** (SKU 36659, reception hall with tables and chandeliers)

### 🎁 Bonus find
- **The Monster Bride HD for Genesis 8 Females** (SKU 55815) — a horror bride character: pale wounded skin, bloodstains, tattered lace garments. Could serve as the *leader* of the zombie bridesmaids, or even an alternate "corrupted" bride

### Suggested build
Genesis 8 figures throughout for compatibility: **Sacrament/Mortis cathedral** → bride in **The Bride Wedding Gown** with a **Z Fear Itself** pose → 3–5 bridesmaids in recolored wedding gowns + **Horrifying Skins** + **Wound Makeup Artist**, posed from **Ancient Swarm** / **Z Crawling and Creeping**, blood splatters via **SY Splats, Drips and Splatters**.

*Sample output from a test run against a ~4,100-product library (DAZ + Renderotica).*

## Repository layout
| File | Purpose |
|---|---|
| SPEC.md | Full design spec + development phases |
| import_catalog.py | Phase 1 - parses the markdown index, normalizes metadata, upserts into SQLite; includes a CLI search for validation |
| mcp_server.py | Phase 2 - MCP server exposing the tools above |
| scraper.py | Phase 3 - re-scrapes vendor pages + a local vision LLM and upserts refreshed metadata (idempotent, keyed on filename) |
| config.example.json | Template config (copy to config.json) |
| requirements.txt | Python dependencies (mcp, requests, beautifulsoup4, openai) |
| catalog.db | Generated SQLite database (not committed) |

## Getting started
1. Copy `config.example.json` to `config.json` and fill in your local paths (`sources`, `thumbnail_dir`).
2. Run the importer: `python import_catalog.py import`
3. Validate search from the CLI: `python import_catalog.py search "bride wedding zombie" --category Clothes`
4. Inspect one item (by id, filename, or SKU): `python import_catalog.py show 55815`

> **Where are the thumbnails?** `thumbnail_dir` must point at a folder where each product's thumbnail sits directly, named by its catalog filename (e.g. `DAZ_3D_55815_The_Monster_Bride_HD....jpg`) — both the importer and scraper do a flat `<dir>/<filename>` lookup. In a DAZ 3D install these thumbnails live under your **`My DAZ 3D Library/Runtime/Support`** folder (nested by vendor/product), so you typically point at a flattened copy of them rather than that tree directly. On this server the flattened set is `C:/Users/Adam/Documents/AnythingLLM/Daz3d/Product-Thumbnails`; the live originals are under `C:\Users\Public\Documents\My DAZ 3D Library\Runtime\Support`.

## Registering the MCP server (Phase 2)
`mcp_server.py` speaks MCP over stdio and reuses the importer's config/scoring code.

1. Create a venv and install deps: `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`
   (on Linux/macOS use `.venv/bin/pip`)
2. Add an MCP server to your client (e.g. LM Studio Bionic) with:
   - **Name:** `daz-catalog`
   - **Command:** `<repo>/.venv/Scripts/python.exe` (absolute path; `.venv/bin/python` on Linux/macOS)
   - **Args:** `<repo>/mcp_server.py` (absolute path)

The server reads `config.json` from its own directory, so it works regardless of the client's working directory. Set the `DAZ_CATALOG_CONFIG` environment variable to point at a different config.

Tools: `search`, `get_item`, `list_categories`, `stats` (see SPEC.md §6).

## Re-scraping with a vision model (Phase 3)

`scraper.py` regenerates metadata for products by scraping each vendor page (description + gallery images), asking a local **vision LLM** (any OpenAI-compatible server, e.g. LM Studio at `http://localhost:1234/v1`) to classify and describe the product, then upserting the result into `catalog.db`. It is idempotent and keyed on filename, so re-running it — even with a better vision model later — updates rows in place without duplicating or losing data. Each row records which model wrote it (`metadata_model`).

It reuses all normalization/upsert logic from `import_catalog.py`, so scraper-written rows are identical in shape to markdown-imported ones (including the `"Category"`/`"Classification"` alias handling).

### Usage
```bash
# incremental: only process thumbnails not already in the DB (e.g. new purchases)
python scraper.py --model <lm-studio-model-id>

# reprocess everything with a better model (after upgrading your vision LLM)
python scraper.py --force --model <better-model-id>

# just these files
python scraper.py --only DAZ_3D_55815_....jpg Renderotica_67091_....png

# retry only the items that FAILED in a previous run (parses scraping_process.log)
python scraper.py --retry-log scraping_process.log

# stop after N items (test runs)
python scraper.py --limit 5
```

- **Model / API:** pass `--model <id>` and/or `--url <base-url>`, or set `model_name` and `lm_studio_url` in `config.json`.
- **Modes:** default is *incremental* (skip filenames already present); `--force` reprocesses all; `--only FILE…` targets specific files; `--retry-log PATH` retries a previous run's failures. Progress commits per item, so an interrupted run resumes cleanly.
- **Logging:** writes to `scraping_process.log` (gitignored) and the console; on any failure it lists which items failed and how to retry them.

> Note: re-running the markdown importer (`import_catalog.py import`) after a scraper run resets those rows' `metadata_model` back to NULL, since the model that wrote the original index files is unknown. Scrape again if you want provenance recorded.

## Status / phases
- Phase 0 - Spec & decisions (done)
- Phase 1 - Importer + SQLite DB, validate design (done)
- Phase 2 - MCP server (done)
- Phase 3 - Re-scrape integration (idempotent upsert, track metadata model) (done)

## Notes
- Only DAZ and Renderotica are supported for now.
- The generated `catalog.db` and local `config.json` are gitignored; no content data is committed to this repo.
