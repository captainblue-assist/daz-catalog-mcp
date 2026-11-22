#!/usr/bin/env python3
"""
daz-catalog-mcp - Phase 2 MCP server.

Exposes the catalog SQLite database to LLMs via Model Context Protocol (stdio):

    search(keywords, category?, limit?)   -> ranked compact list of matches
    get_item(id? | sku+vendor? | filename?) -> full detail for one item
    list_categories()                     -> categories with counts
    stats()                               -> library totals by vendor/category

Reuses all parsing/scoring logic from import_catalog.py (single source of truth).

Run:  python mcp_server.py        (stdio transport; register with your MCP client)
"""

import json
import os

try:  # mcp >= 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp < 2.x
    from mcp.server.fastmcp import FastMCP as _Server

import import_catalog as ic

# Config lives next to this script by default, so the server works no matter
# which directory the MCP client launches it from.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get(
    "DAZ_CATALOG_CONFIG", os.path.join(_SCRIPT_DIR, "config.json")
)

mcp = _Server("daz-catalog")


def _cfg():
    return ic.load_config(CONFIG_PATH)


def _connect(cfg):
    conn = ic.get_conn(cfg["db_path"])
    total = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    if total == 0:
        conn.close()
        raise RuntimeError(
            f"Catalog database is empty or missing ({cfg['db_path']}). "
            "Run 'python import_catalog.py import' first."
        )
    return conn


def _normalize_keywords(keywords):
    """Accept a list, or a comma/whitespace-separated string."""
    if isinstance(keywords, str):
        return ic.split_query(keywords)
    out = []
    for k in keywords:
        s = str(k).strip()
        if s:
            out.append(s)
    return out


def _compact(row, score, matched):
    vs = (row["visual_summary"] or "").strip()
    return {
        "id": row["id"],
        "product_name": row["product_name"],
        "vendor": row["vendor"],
        "sku": row["sku"],
        "classification": row["classification"],
        "score": score,
        "matched_keywords": matched,
        "visual_summary": vs[:300] + ("..." if len(vs) > 300 else ""),
    }


def _detail(row):
    try:
        attrs = json.loads(row["attributes_json"] or "{}")
    except Exception:
        attrs = {}
    try:
        keywords = json.loads(row["search_keywords"] or "[]")
    except Exception:
        keywords = []
    return {
        "id": row["id"],
        "product_name": row["product_name"],
        "filename": row["filename"],
        "vendor": row["vendor"],
        "sku": row["sku"],
        "classification": row["classification"],
        "description": (row["description"] or "").strip(),
        "visual_summary": (row["visual_summary"] or "").strip(),
        "search_keywords": keywords,
        "attributes": attrs,
        "thumbnail_path": row["thumbnail_path"],
        "product_url": row["product_url"],
        # Provenance: where the record came from and which model wrote it.
        "source_file": row["source_file"],
        "metadata_model": row["metadata_model"],
    }


@mcp.tool()
def search(
    keywords: list[str],
    category: str | None = None,
    limit: int = 20,
) -> dict:
    """Search the DAZ/Renderotica content catalog.

    OR semantics with relevance scoring: an item matches if ANY keyword appears
    in it; items matching more keywords (especially in product name / search
    keywords) rank higher. Use this to find assets for a scene concept, e.g.
    keywords ["bride", "wedding", "zombie"].

    Args:
        keywords: One or more search terms (e.g. ["hoop earrings", "bride"]).
        category: Optional filter — one of the values from list_categories()
            (e.g. "Clothes", "Props", "Poses"). Case-insensitive.
        limit: Max results to return (default 20, max 50).

    Returns:
        {"count": <total matches>, "results": [{id, product_name, vendor, sku,
        classification, score, matched_keywords, visual_summary}, ...]}
        Follow up with get_item(id) for full details.
    """
    cfg = _cfg()
    kws = _normalize_keywords(keywords)
    if not kws:
        return {"error": "No keywords provided."}
    limit = max(1, min(int(limit), cfg["max_limit"]))

    conn = _connect(cfg)
    try:
        results, total = ic.search(
            conn, kws, category=category, limit=limit, weights=cfg["field_weights"]
        )
    finally:
        conn.close()

    return {
        "count": total,
        "results": [_compact(r["row"], r["score"], r["matched"]) for r in results],
    }


@mcp.tool()
def get_item(
    id: int | None = None,
    sku: str | None = None,
    vendor: str | None = None,
    filename: str | None = None,
) -> dict:
    """Get full details for one catalog item.

    Look up by exactly one of:
        id       - internal id (from search results), e.g. 1845
        sku      - vendor SKU, optionally disambiguated with vendor ("DAZ" / "RENDEROTICA")
        filename - thumbnail filename, e.g. DAZ_3D_55815_The_Monster_Bride_HD....jpg

    Returns:
        {id, product_name, filename, vendor, sku, classification, description,
        visual_summary, search_keywords (array), attributes (object),
        thumbnail_path (local image path), product_url,
        source_file, metadata_model}
    """
    if sum(x is not None for x in (id, sku, filename)) != 1:
        return {
            "error": "Provide exactly one of: id, sku (+ optional vendor), or filename."
        }

    cfg = _cfg()
    conn = _connect(cfg)
    try:
        if id is not None:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (int(id),)).fetchone()
        elif filename is not None:
            row = conn.execute(
                "SELECT * FROM items WHERE filename = ?", (filename,)
            ).fetchone()
        else:
            sql = "SELECT * FROM items WHERE sku = ?"
            params = [str(sku)]
            if vendor:
                sql += " AND lower(vendor) = lower(?)"
                params.append(vendor)
            rows = conn.execute(sql, params).fetchall()
            row = rows[0] if len(rows) == 1 else None
            if row is None and len(rows) > 1:
                return {
                    "error": f"SKU {sku} is ambiguous ({len(rows)} matches).",
                    "candidates": [
                        {"id": r["id"], "vendor": r["vendor"], "product_name": r["product_name"]}
                        for r in rows[:10]
                    ],
                }
    finally:
        conn.close()

    if row is None:
        return {"error": f"No item found (id={id}, sku={sku}, filename={filename})."}
    return _detail(row)


@mcp.tool()
def list_categories() -> dict:
    """List all normalized categories in the catalog with item counts.

    Use this to discover valid values for the `category` filter in search(),
    e.g. {"Clothes": 865, "Environment": 731, ...}.
    """
    cfg = _cfg()
    conn = _connect(cfg)
    try:
        rows = conn.execute(
            "SELECT classification, COUNT(*) AS c FROM items "
            "GROUP BY classification ORDER BY c DESC"
        ).fetchall()
    finally:
        conn.close()
    return {"categories": {r["classification"]: r["c"] for r in rows}}


@mcp.tool()
def stats() -> dict:
    """Library overview: total items, counts by vendor and by category."""
    cfg = _cfg()
    conn = _connect(cfg)
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
        vendors = {
            r["vendor"]: r["c"]
            for r in conn.execute(
                "SELECT vendor, COUNT(*) AS c FROM items GROUP BY vendor ORDER BY c DESC"
            )
        }
        categories = {
            r["classification"]: r["c"]
            for r in conn.execute(
                "SELECT classification, COUNT(*) AS c FROM items "
                "GROUP BY classification ORDER BY c DESC"
            )
        }
    finally:
        conn.close()
    return {"total": total, "by_vendor": vendors, "by_category": categories}


if __name__ == "__main__":
    mcp.run()
