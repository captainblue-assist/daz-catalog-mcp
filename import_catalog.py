#!/usr/bin/env python3
"""
daz-catalog-mcp - Phase 1 importer + search CLI.

Parses the markdown product index files, normalizes the (inconsistent)
LLM-generated metadata into a stable schema, and upserts everything into a
single SQLite database. Also provides a small CLI to run relevance-ranked
keyword searches so we can validate the design before building the MCP server.

Phase 1 uses only the Python standard library (no third-party deps).

Usage:
    python import_catalog.py import
    python import_catalog.py search "bride wedding zombie" --category Clothes --limit 20
    python import_catalog.py categories
    python import_catalog.py stats
"""

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DEFAULT_WEIGHTS = {
    "product_name": 3.0,
    "search_keywords": 3.0,
    "classification": 2.0,
    "visual_summary": 1.5,
    "attributes_text": 1.0,
    "description": 0.5,
}

# Map the raw (often parenthetical) classification strings to a controlled set.
CATEGORY_ALIASES = {
    "character": "Character",
    "animal": "Animal",
    "environment": "Environment",
    "prop": "Props",
    "props": "Props",
    "clothes": "Clothes",
    "clothing": "Clothes",
    "apparel": "Clothes",
    "hair": "Hair",
    "pose": "Poses",
    "poses": "Poses",
    "vehicle": "Vehicles",
    "vehicles": "Vehicles",
    "accessory": "Accessories",
    "accessories": "Accessories",
    "jewelry": "Accessories",
    "anatomy": "Anatomy",
    "material": "Materials",
    "materials": "Materials",
    "texture": "Materials",
    "fx": "FX",
    "effect": "FX",
    "effects": "FX",
    "bundle/collection": "Bundle/Collection",
    "bundles/collection": "Bundle/Collection",
    "bundle": "Bundle/Collection",
    "characters": "Character",
    "animals": "Animal",
    "utility": "Utilities",
    "utilities": "Utilities",
    "other": "Other",
}


def load_config(path="config.json"):
    cfg_path = os.path.abspath(path)
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    base = os.path.dirname(cfg_path)

    def resolve(p):
        if not p:
            return p
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    cfg["_base"] = base
    cfg["sources"] = [resolve(s) for s in cfg.get("sources", [])]
    cfg["thumbnail_dir"] = resolve(cfg.get("thumbnail_dir", ""))
    cfg["db_path"] = resolve(cfg.get("db_path", "catalog.db"))

    weights = dict(DEFAULT_WEIGHTS)
    weights.update(cfg.get("field_weights", {}))
    cfg["field_weights"] = weights
    cfg.setdefault("default_limit", 20)
    cfg.setdefault("max_limit", 50)
    return cfg


# --------------------------------------------------------------------------- #
# Markdown parsing + normalization
# --------------------------------------------------------------------------- #

def _extract_json(text):
    """Find and parse the first {...} JSON object in text (tolerant)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except Exception:
        # Best-effort repair: drop trailing commas before } or ]
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(repaired)
        except Exception:
            return None


def normalize_category(raw):
    if not raw:
        return "Other"
    base = re.split(r"\(", str(raw))[0].strip().lower()
    return CATEGORY_ALIASES.get(base, str(raw).strip())


def normalize_keywords(val):
    """Normalize Search_Keywords (list or comma-string) to a list of strings."""
    if val is None:
        return []
    if isinstance(val, list):
        out = []
        for x in val:
            s = str(x).strip()
            if s:
                out.append(s)
        return out
    if isinstance(val, str):
        return [p.strip() for p in val.split(",") if p.strip()]
    return []


def flatten_for_search(obj, out):
    """Recursively collect every scalar value (dicts/lists expanded)."""
    if isinstance(obj, dict):
        for v in obj.values():
            flatten_for_search(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            flatten_for_search(v, out)
    else:
        s = str(obj).strip()
        if s:
            out.append(s)


def make_product_url(vendor, sku):
    v = (vendor or "").upper().strip()
    if not sku:
        return None
    if v == "DAZ":
        return f"https://www.daz3d.com/catalog/product/view/id/{sku}"
    if v in ("RENDEROTICA", "RENDROTIKA"):
        return f"https://www.renderotica.com/store/sku/{sku}_"
    return None


def parse_block(block):
    """Parse one product block (text starting at the product-name line)."""
    lines = block.split("\n")
    product_name = lines[0].strip()

    def grab(label):
        m = re.search(r"\*\*" + label + r":\*\*\s*(.+)", block)
        return m.group(1).strip() if m else None

    filename = grab("Filename")
    vendor = grab("Vendor")
    sku = grab("SKU")

    # Description: between '### Description' and the metadata header.
    description = ""
    dm = re.search(r"(?im)^###\s*Description\s*$", block)
    mm = re.search(r"(?im)^###\s*Metadata.*?Attributes\s*$", block)
    if dm:
        start = dm.end()
        end = mm.start() if (mm and mm.start() > start) else len(block)
        description = block[start:end].strip()

    # Metadata JSON: after the '### Metadata & Attributes' header.
    meta = None
    if mm:
        meta = _extract_json(block[mm.end():])

    return {
        "product_name": product_name,
        "filename": filename,
        "vendor": vendor,
        "sku": sku,
        "description": description,
        "meta": meta,
    }


def parse_markdown(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    parts = re.split(r"(?m)^##\s*Product:\s*", text)
    items = []
    for part in parts:
        if not part.strip():
            continue
        parsed = parse_block(part)
        if parsed["filename"]:  # skip preamble / malformed blocks
            items.append(parsed)
    return items


def normalize_item(parsed, source_file, thumbnail_dir):
    meta = parsed.get("meta") or {}
    classification = normalize_category(meta.get("Classification"))
    visual_summary = str(meta.get("Visual_Summary", "") or "").strip()
    keywords = normalize_keywords(meta.get("Search_Keywords"))

    # Everything except the three handled fields becomes "attributes".
    attrs = {
        k: v for k, v in meta.items()
        if k not in ("Classification", "Visual_Summary", "Search_Keywords")
    }
    flat = []
    flatten_for_search(attrs, flat)
    attributes_text = " ".join(flat)

    filename = parsed["filename"]
    thumbnail_path = (
        os.path.join(thumbnail_dir, filename) if (thumbnail_dir and filename) else None
    )

    return {
        "filename": filename,
        "vendor": parsed.get("vendor"),
        "sku": parsed.get("sku"),
        "product_name": parsed.get("product_name"),
        "classification": classification,
        "description": parsed.get("description", ""),
        "visual_summary": visual_summary,
        "search_keywords": json.dumps(keywords, ensure_ascii=False),
        "attributes_json": json.dumps(attrs, ensure_ascii=False) if attrs else "{}",
        "attributes_text": attributes_text,
        "product_url": make_product_url(parsed.get("vendor"), parsed.get("sku")),
        "thumbnail_path": thumbnail_path,
        "source_file": os.path.basename(source_file),
    }


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT UNIQUE NOT NULL,
    vendor TEXT,
    sku TEXT,
    product_name TEXT,
    classification TEXT,
    description TEXT,
    visual_summary TEXT,
    search_keywords TEXT,
    attributes_json TEXT,
    attributes_text TEXT,
    product_url TEXT,
    thumbnail_path TEXT,
    source_file TEXT,
    metadata_model TEXT,
    updated_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_items_vendor_sku ON items(vendor, sku);
CREATE INDEX IF NOT EXISTS idx_items_classification ON items(classification);
"""

COLUMNS = [
    "filename", "vendor", "sku", "product_name", "classification",
    "description", "visual_summary", "search_keywords", "attributes_json",
    "attributes_text", "product_url", "thumbnail_path", "source_file",
]


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_item(conn, item):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cols = COLUMNS + ["updated_at"]
    vals = [item.get(c) for c in COLUMNS] + [now]
    placeholders = ", ".join("?" * len(cols))
    updates = ", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "filename")
    sql = (
        f"INSERT INTO items ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(filename) DO UPDATE SET {updates}, updated_at=excluded.updated_at"
    )
    conn.execute(sql, vals)


def run_import(cfg):
    conn = get_conn(cfg["db_path"])
    for src in cfg["sources"]:
        if not os.path.isfile(src):
            print(f"[warn] source not found: {src}")
            continue
        parsed_items = parse_markdown(src)
        n_no_meta = sum(1 for p in parsed_items if not p.get("meta"))
        for p in parsed_items:
            item = normalize_item(p, src, cfg["thumbnail_dir"])
            upsert_item(conn, item)
        conn.commit()
        print(f"[import] {os.path.basename(src)}: {len(parsed_items)} products "
              f"({n_no_meta} without parseable metadata)")
    total = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    print(f"[done] database now has {total} unique products -> {cfg['db_path']}")
    conn.close()


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def rank_items(conn, keywords, category=None, weights=None):
    """Score every (optionally category-filtered) row; return full ranked list."""
    weights = weights or DEFAULT_WEIGHTS
    sql = (
        "SELECT id, product_name, vendor, sku, classification, visual_summary, "
        "description, search_keywords, attributes_text FROM items"
    )
    params = []
    if category:
        sql += " WHERE lower(classification) = lower(?)"
        params.append(category)
    rows = conn.execute(sql, params).fetchall()

    results = []
    for r in rows:
        fields = {
            "product_name": (r["product_name"] or "").lower(),
            "search_keywords": (r["search_keywords"] or "").lower(),
            "classification": (r["classification"] or "").lower(),
            "visual_summary": (r["visual_summary"] or "").lower(),
            "attributes_text": (r["attributes_text"] or "").lower(),
            "description": (r["description"] or "").lower(),
        }
        score = 0.0
        matched = []
        for kw in keywords:
            k = kw.lower().strip()
            if not k:
                continue
            best = 0.0
            for fname, w in weights.items():
                if k in fields[fname] and w > best:
                    best = w
            if best > 0:
                score += best
                matched.append(kw)
        if score > 0:
            results.append({"score": round(score, 2), "matched": matched, "row": r})

    results.sort(key=lambda x: (-x["score"], (x["row"]["product_name"] or "").lower()))
    return results


def search(conn, keywords, category=None, limit=20, weights=None):
    all_results = rank_items(conn, keywords, category=category, weights=weights)
    return all_results[:limit], len(all_results)


def print_search(results, total):
    if not results:
        print("No matches.")
        return
    for i, res in enumerate(results, 1):
        r = res["row"]
        print(f"{i}. [{res['score']}] {r['product_name']}")
        print(f"   {r['vendor']} / SKU {r['sku']}  |  {r['classification']}")
        print(f"   matched: {', '.join(res['matched'])}")
        vs = (r["visual_summary"] or "").strip()
        if vs:
            snippet = vs[:140] + ("..." if len(vs) > 140 else "")
            print(f"   {snippet}")
    print(f"\n({len(results)} shown of {total} matches)")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def find_item(conn, lookup, vendor=None):
    """Find an item by id, filename, or SKU (optionally scoped to a vendor)."""
    if lookup.isdigit():
        row = conn.execute("SELECT * FROM items WHERE id = ?", (int(lookup),)).fetchone()
        if row:
            return row
    row = conn.execute(
        "SELECT * FROM items WHERE filename = ?", (lookup,)
    ).fetchone()
    if row:
        return row
    sql = "SELECT * FROM items WHERE sku = ?"
    params = [lookup]
    if vendor:
        sql += " AND lower(vendor) = lower(?)"
        params.append(vendor)
    rows = conn.execute(sql, params).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        print(f"SKU {lookup} is ambiguous ({len(rows)} matches):")
        for r in rows:
            print(f"  id={r['id']}  {r['vendor']}  {r['product_name']}")
        print("Use 'show <id>' or add --vendor to disambiguate.")
    return None


def show_detail(row):
    attrs = {}
    try:
        attrs = json.loads(row["attributes_json"] or "{}")
    except Exception:
        pass
    keywords = []
    try:
        keywords = json.loads(row["search_keywords"] or "[]")
    except Exception:
        pass

    print(f"id: {row['id']}")
    print(f"name: {row['product_name']}")
    print(f"vendor/sku: {row['vendor']} / {row['sku']}")
    print(f"category: {row['classification']}")
    print(f"filename: {row['filename']}")
    print(f"thumbnail: {row['thumbnail_path']}")
    print(f"product_url: {row['product_url']}")
    if row["visual_summary"]:
        print(f"\nVisual summary:\n{row['visual_summary'].strip()}")
    if row["description"]:
        print(f"\nDescription:\n{row['description'].strip()}")
    if keywords:
        print(f"\nKeywords: {', '.join(keywords)}")
    if attrs:
        print("\nAttributes:")
        for k, v in attrs.items():
            if isinstance(v, (dict, list)):
                print(f"  {k}: {json.dumps(v, ensure_ascii=False)}")
            else:
                print(f"  {k}: {v}")


def split_query(query):
    """Split a CLI query into keywords: commas first, else whitespace."""
    if "," in query:
        return [p.strip() for p in query.split(",") if p.strip()]
    return query.split()


def main(argv=None):
    ap = argparse.ArgumentParser(description="daz-catalog-mcp importer + search CLI")
    ap.add_argument("--config", default="config.json", help="path to config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("import", help="parse markdown sources into the SQLite DB")

    p_search = sub.add_parser("search", help="run a relevance-ranked keyword search")
    p_search.add_argument(
        "query",
        help='keywords, e.g. "bride wedding zombie" or "hoop earrings, bride"',
    )
    p_search.add_argument("--category", default=None)
    p_search.add_argument("--limit", type=int, default=None)

    p_show = sub.add_parser("show", help="full detail for one item (by id, filename, or SKU)")
    p_show.add_argument("lookup", help="item id, filename, or vendor SKU")
    p_show.add_argument("--vendor", default=None, help="disambiguate a SKU lookup (DAZ / RENDEROTICA)")

    sub.add_parser("categories", help="list categories with counts")
    sub.add_parser("stats", help="show library stats")

    args = ap.parse_args(argv)
    cfg = load_config(args.config)

    if args.cmd == "import":
        run_import(cfg)
        return

    conn = get_conn(cfg["db_path"])

    if args.cmd == "search":
        keywords = split_query(args.query)
        limit = args.limit or cfg["default_limit"]
        limit = max(1, min(limit, cfg["max_limit"]))
        results, total = search(
            conn, keywords, category=args.category,
            limit=limit, weights=cfg["field_weights"],
        )
        print(f"Query: {keywords}  (category={args.category})")
        print_search(results, total)

    elif args.cmd == "show":
        row = find_item(conn, args.lookup, vendor=args.vendor)
        if not row:
            print(f"No item found for: {args.lookup}")
            return
        show_detail(row)

    elif args.cmd == "categories":
        rows = conn.execute(
            "SELECT classification, COUNT(*) c FROM items "
            "GROUP BY classification ORDER BY c DESC"
        ).fetchall()
        for r in rows:
            print(f"{r['c']:>5}  {r['classification']}")

    elif args.cmd == "stats":
        total = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        vendors = conn.execute(
            "SELECT vendor, COUNT(*) c FROM items GROUP BY vendor ORDER BY c DESC"
        ).fetchall()
        print(f"Total products: {total}")
        print("By vendor:")
        for r in vendors:
            print(f"  {r['c']:>5}  {r['vendor']}")

    conn.close()


if __name__ == "__main__":
    main()
