# daz-catalog-mcp

A queryable catalog of DAZ / Renderotica 3D content, exposed to an LLM via an MCP server so you can ask "what assets do I have for a scene where a bride is attacked by zombie bridesmaids?" and get back a ranked shortlist.

Instead of dumping all metadata into one big markdown file for RAG (fuzzy, misses things), this stores the per-product metadata in SQLite with deterministic keyword search. The LLM does the thinking (scene to keywords); the database does the finding.

## How it works
Two-step lookup exposed over MCP:
1. `search(keywords[], category?, limit?)` - OR across keywords, ranked by relevance. Returns a compact list of matches.
2. `get_item(id | sku+vendor)` - full detail for one item (description, attributes, visual summary, thumbnail path, product URL).

Plus helpers: `list_categories`, `stats`.

## Repository layout
| File | Purpose |
|---|---|
| SPEC.md | Full design spec + development phases |
| import_catalog.py | Phase 1 - parses the markdown index, normalizes metadata, upserts into SQLite; includes a CLI search for validation |
| mcp_server.py | Phase 2 - MCP server exposing the tools above |
| config.example.json | Template config (copy to config.json) |
| catalog.db | Generated SQLite database (not committed) |

## Getting started
1. Copy `config.example.json` to `config.json` and fill in your local paths (`sources`, `thumbnail_dir`).
2. Run the importer: `python import_catalog.py import`
3. Validate search from the CLI: `python import_catalog.py search "bride wedding zombie" --category Clothes`

## Status / phases
- Phase 0 - Spec & decisions (done)
- Phase 1 - Importer + SQLite DB, validate design (next)
- Phase 2 - MCP server
- Phase 3 - Re-scrape integration (idempotent upsert, track metadata model)

## Notes
- Only DAZ and Renderotica are supported for now.
- The generated `catalog.db` and local `config.json` are gitignored; no content data is committed to this repo.
