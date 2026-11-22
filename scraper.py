#!/usr/bin/env python3
"""
daz-catalog-mcp - Phase 3 re-scrape tool.

Scans the thumbnail directory, scrapes each product's vendor page (description +
gallery images), asks a local vision LLM (LM Studio, OpenAI-compatible API) to
classify and describe the product, then upserts the result into catalog.db --
idempotent, keyed on filename. Each row records which model generated its
metadata (metadata_model), so re-running with a better vision model updates rows
in place without duplicating them or losing data.

All normalization/upsert logic is reused from import_catalog.py (single source
of truth): scraper-written rows are identical in shape to markdown-imported ones.

Usage:
    python scraper.py                                  # incremental: skip files already in DB
    python scraper.py --model <lm-studio-model-id>     # pick the vision model
    python scraper.py --force                          # reprocess even if already in DB
    python scraper.py --only FILE1.jpg FILE2.png       # process just these files
    python scraper.py --retry-log scraping_process.log # retry items that FAILED last run
    python scraper.py --limit 5                        # stop after N processed items

Config keys (config.json): lm_studio_url, model_name. CLI --model/--url override.
"""

import argparse
import base64
import logging
import os
import random
import re
import sys
import time

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

from import_catalog import extract_json, get_conn, load_config, normalize_item, upsert_item


# --------------------------------------------------------------------------- #
# Logging (file + console)
# --------------------------------------------------------------------------- #

LOG_FILE = "scraping_process.log"

logger = logging.getLogger("DazScraper")
logger.setLevel(logging.INFO)

_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
_file_handler.setFormatter(_formatter)
_file_handler.setLevel(logging.INFO)

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(message)s"))  # uncluttered console output
_stream_handler.setLevel(logging.INFO)

logger.addHandler(_file_handler)
logger.addHandler(_stream_handler)


# --------------------------------------------------------------------------- #
# Configuration defaults
# --------------------------------------------------------------------------- #

DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"

IMAGE_EXTS = (".png", ".jpg", ".jpeg")

# One failure line format per failed item; --retry-log parses these.
FAILED_RE = re.compile(r"FAILED (\S+):")


def extract_failed_filenames(log_path):
    """Parse '  > FAILED <filename>: reason' lines from a previous run's log."""
    failed, seen = [], set()
    if not os.path.exists(log_path):
        logger.error(f"Log file {log_path} not found.")
        return failed
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            m = FAILED_RE.search(line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                failed.append(m.group(1))
    return failed


# --------------------------------------------------------------------------- #
# Filename parsing (unchanged from the original scraper)
# --------------------------------------------------------------------------- #

def parse_filename(filename):
    """
    Parses: DAZ_3D_115306_Jason_for_Genesis_9  or  Renderotica_SKU_Name...
    Returns: (Vendor, SKU, Product_Name)
    """
    name_part = os.path.splitext(filename)[0]
    parts = name_part.split("_")

    if name_part.startswith("DAZ_3D"):
        vendor = "DAZ"
        sku = parts[2]  # index 2 is the number
        product_name = " ".join(parts[3:]).replace("_", " ")
        return vendor, sku, product_name

    elif name_part.startswith("Renderotica"):
        vendor = "RENDEROTICA"
        sku = parts[1]  # Renderotica_SKU_Name
        product_name = " ".join(parts[2:]).replace("_", " ")
        return vendor, sku, product_name

    return None, None, None


# --------------------------------------------------------------------------- #
# Web scraping (unchanged from the original scraper)
# --------------------------------------------------------------------------- #

def scrape_description(vendor, sku, product_name):
    """Fetches the vendor page; returns (description_text, [image_urls])."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        if vendor == "DAZ":
            url = f"https://www.daz3d.com/catalog/product/view/id/{sku}"
        else:  # Renderotica
            url = f"https://www.renderotica.com/store/sku/{sku}_"

        response = requests.get(url, headers=headers, timeout=10)

        # 1. Check standard HTTP status codes
        if response.status_code != 200:
            return "SCRAPE_FAILED", None

        # Redirect / home-page detection (soft 404 on DAZ)
        if response.url in ("https://www.daz3d.com/", "https://www.daz3d.com"):
            logger.info(f"  > Detected redirect to DAZ homepage for SKU {sku}. Item likely does not exist.")
            return "SCRAPE_FAILED", None

        soup = BeautifulSoup(response.text, "html.parser")

        # Content-based soft-404 detection (secondary safety net)
        title_text = soup.title.string.strip() if soup.title else ""
        body_preview = soup.get_text(separator=" ", strip=True)[:500]
        if any(kw in title_text or kw in body_preview for kw in ["404", "not found", "Not Found"]):
            return "SCRAPE_FAILED", None

        # --- DAZ LOGIC ---
        if vendor == "DAZ":
            description_li = soup.find("li", class_="description")
            if description_li:
                clean_text = description_li.get_text(separator=" ", strip=True).strip('" ')
                logger.debug(f"> Extracted Description: {clean_text[:100]}...")
            else:
                clean_text = None

            main_img = soup.find("img", id="main-image")
            main_url = main_img["src"] if main_img and main_img.get("src") else None

            thumbnails = soup.find_all("img", class_="zoomable")
            gallery_urls = []
            for thumb in thumbnails:
                t_src = thumb.get("src")
                if not t_src:
                    continue
                clean_thumb_url = t_src.split("?")[0]
                # Unwrap Cloudflare-resized URLs to the real image URL
                if "/cdn-cgi/image/" in clean_thumb_url:
                    parts = clean_thumb_url.split("https://")
                    if len(parts) > 1:
                        clean_thumb_url = "https://" + parts[1]
                if clean_thumb_url.startswith("http"):
                    gallery_urls.append(clean_thumb_url)
                else:
                    logger.warning(f"> Found thumbnail but couldn't resolve real URL: {t_src}")

            image_urls = [main_url] + gallery_urls
            return clean_text, [u for u in image_urls if u and u.startswith("http")]

        # --- RENDEROTICA LOGIC ---
        panes_div = soup.find("div", class_="product-panes")
        if panes_div:
            first_pane = panes_div.find("div", class_="pane")
            if first_pane:
                raw_text = first_pane.get_text(separator=" ", strip=True)
                clean_text = " ".join(raw_text.split())

                img_div = soup.find("div", class_="primary-image")
                primary_image_url = None
                if img_div:
                    img_tag = img_div.find("img")
                    if img_tag and img_tag.get("src"):
                        primary_image_url = img_tag["src"]
                        if not primary_image_url.startswith("http"):
                            if primary_image_url.startswith("//"):
                                primary_image_url = "https:" + primary_image_url
                            else:
                                primary_image_url = f"https://www.renderotica.com{primary_image_url}"

                gallery_urls = []
                nav_div = soup.find("div", class_="image-navigation")
                if nav_div:
                    for thumb in nav_div.find_all("img"):
                        t_src = thumb.get("src")
                        if not t_src:
                            continue
                        clean_t_src = t_src.split("?")[0]
                        if not clean_t_src.startswith("http"):
                            if clean_t_src.startswith("//"):
                                clean_t_src = "https:" + clean_t_src
                            else:
                                clean_t_src = f"https://www.renderotica.com{clean_t_src}"
                        gallery_urls.append(clean_t_src)

                all_images = [primary_image_url] + gallery_urls
                return clean_text, [u for u in all_images if u and u.startswith("http")]

        # Fallback if specific structures aren't found
        return product_name, None

    except Exception as e:
        logger.error(f"Error scraping {vendor} data for SKU {sku}: {str(e)}")
        return "SCRAPE_FAILED", None


# --------------------------------------------------------------------------- #
# Image handling (unchanged from the original scraper)
# --------------------------------------------------------------------------- #

def encode_image(image_path):
    """Encodes a local image to base64 as a single clean line."""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8").replace("\n", "").replace("\r", "")


def download_and_encode_remote_image(url):
    """Downloads an image from a URL and returns it as a base64 data URI."""
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            encoded_string = base64.b64encode(response.content).decode("utf-8").replace("\n", "").replace("\r", "")
            return f"data:image/jpeg;base64,{encoded_string}"
    except Exception as e:
        logger.error(f"Error downloading remote image: {e}")
    return None


def process_image_list(urls):
    """Takes a list of URLs, downloads them, returns base64 data URIs."""
    processed = []
    for url in urls or []:
        if not url:
            continue
        b64_data = download_and_encode_remote_image(url)
        if b64_data:
            processed.append(b64_data)
    return processed


# --------------------------------------------------------------------------- #
# LLM metadata generation (prompt unchanged from the original scraper)
# --------------------------------------------------------------------------- #

PROMPT_TEMPLATE = """
Analyze the provided images and web description to catalog a 3D product asset for a searchable database.

### STEP 1: CLASSIFICATION
First, determine the primary category of the asset. You must choose exactly ONE from this list:
- Character (Human)
- Animal
- Environment (Full Scenes/Dioramas)
- Props (Objects/Weapons/Furniture)
- Clothes (Apparel/Armor)
- Hair (Wigs/Hair pieces)
- Poses (Action/Static poses)
- Vehicles (Cars/Ships/etc.)
- Accessories (Jewelry/Glasses/etc.)
- Anatomy (Body parts/Textures)
- Materials (Textures/Shaders/HDRI) 
- FX (Particles/Smoke/Magic)   
- Utilities (Software/Scripts/Tools)
- Other (Specify reason)

### STEP 2: ATTRIBUTE EXTRACTION
Extract specific attributes based on the category. 
**IMPORTANT:** Use the provided Web Description for technical details (compatibility, era, etc.). 
**DO NOT** include marketing fluff like "amazing" or "must-have".

**IF CHARACTER:**
- Gender: (Male/Female/Unisex)
- Age Group: (Child, Teen, Young Adult, Middle Aged, Elderly)
- Ethnicity/Style: (Caucasian, Asian, Afro-Caribbean, Stylized, etc.)
- Build: (Slim, Athletic, Heavy, Muscular, etc.)

**IF ANIMAL:**
- Species/Breed: 
- Era: (Prehistoric, Modern, Future, Mythical)
- Setting: (Sci-Fi, Fantasy, Realistic, Cartoon)
- Size: (Small, Medium, Large)
- Behaviour (Aggressive, Docile, Flying, Crawling, etc.)

**IF ENVIRONMENT or PROPS:**
- Era: (Ancient, Medieval, Victorian, Modern, Sci-Fi)
- Setting: (Interior, Exterior, Urban, Nature, Space)
- Mood/Atmosphere: (Dark, Bright, Cozy, Industrial, Magical)
- Material/Texture: (Stone, Wood, Metal, Organic, etc.)
- Condition: (Pristine, Weathered, Damaged, etc.)

**IF CLOTHES:**
- Target Gender: (Male/Female/Unisex)
- Style: (Casual, Formal, Armor, Fantasy, Sci-Fi)
- Setting Compatibility: (Modern, Fantasy, etc.)
- Era: 
- Material: (Leather, Cloth, Metal, etc.)
- Complexity: (Simple, Ornate, Intricate, Heavy, etc)

**IF HAIR:**
- Length: (Short, Medium, Long)
- Texture: (Straight, Curly, Wavy, Braided)
- Gender Suitability: (Male/Female/Unisex)
- Style: (Modern, Fantasy, etc.)

**IF POSES:**
- Type: (Standing, Sitting, Action, Relaxed, etc.)
- Target Gender: (Male/Female/Unisex)

**IF VEHICLES:**
- Type: (Car, Bike, Spaceship, Boat, etc.)
- Era: (Classic, Modern, Futuristic)
- Setting: (Urban, Off-road, Space, etc.)
- Condition: (Pristine, Weathered, Damaged, etc.)

**IF ACCESSORIES:**
- Type: (Jewelry, Glasses, Hats, etc.)
- Style: (Modern, Fantasy, Sci-Fi, etc.)
- Target Gender: (Male/Female/Unisex)
- Setting: (Modern, Fantasy, etc.)
- Material: (Leather, Cloth, Metal, etc.)
- Complexity: (Simple, Ornate, Intricate, Heavy, etc)
- Scale: (Handheld, Wearable, Large, etc.)

**IF ANATOMY:**
- Body Part: (Head, Torso, Arms, Legs, Hands, Feet, etc.)
- Style: (Realistic, Stylized, Fantasy, Sci-Fi)

**IF Utilities:**
- Compatibility: (Which software or models it is designed for)
- Problems Solved: (What specific issues or needs does this asset address?)

**IF MATERIALS or FX:**
- Type: (Metal, Wood, Skin, Fire, Smoke, Magic)
- Era/Setting: (Modern, Fantasy, Sci-Fi)
- Usage: (Texture, Overlay, Lighting)
- Opacity: (Transparent, Translucent, Opaque, etc.)
- Intensity: (Faint, Bright, Blinding, Glowing, etc.)

**IF BUNDLE/COLLECTION:**
- Contains: (List all items included in this pack)
- Theme: (e.s., Medieval, Cyberpunk)
- Primary Use: (e.s., Scene building, Character dressing)

### STEP 3: VISUAL ANALYSIS & RAG ENRICHMENT
This is the most important step for searchability.
1. **Product Focus**: Use the description and product name to determine the nature of the product. Ignore background details in the images that are unrelated to the core product.
2. **Visual_Summary**: Describe exactly what is visible in the images (e.g., "A weathered iron helmet with a red plume"). Use descriptive, evocative language.
3. **Search_Keywords**: Generate a comma-separated list of highly specific keywords. Include synonyms, related era terms, and material names (e.s., "medieval", "knight", "steel", "battle-worn").

### OUTPUT FORMAT
Return the result as a clean JSON object. Do not use markdown code blocks. 
The JSON must include all extracted attributes AND the 'Visual_Summary' and 'Search_Keywords'.


WEB DESCRIPTION CONTEXT:
    {web_text}
"""


def generate_metadata(client, model, image_payloads, web_text):
    """Ask the vision LLM for product metadata JSON. Returns raw text or None."""
    prompt = PROMPT_TEMPLATE.format(web_text=web_text)
    content_list = [{"type": "text", "text": prompt}]
    if image_payloads:
        for img in image_payloads:
            content_list.append({"type": "image_url", "image_url": {"url": img}})

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content_list}],
        )
        return (response.choices[0].message.content or "").strip() or None
    except Exception as e:
        logger.error(f"LLM Error: {str(e)}")
        return None


# --------------------------------------------------------------------------- #
# Per-item pipeline: scrape -> images -> LLM -> normalize -> upsert
# --------------------------------------------------------------------------- #

def process_file(conn, cfg, client, model, filename):
    """Scrape + describe one product and upsert it into the DB. True on success."""
    vendor, sku, prod_name = parse_filename(filename)
    if not vendor:
        logger.info(f"  > FAILED {filename}: cannot parse vendor/SKU from filename")
        return False

    # 1. Scrape web data (description + gallery image URLs)
    web_desc, remote_image_urls = scrape_description(vendor, sku, prod_name)
    if web_desc == "SCRAPE_FAILED":
        logger.info(f"  > FAILED {filename}: web scrape failed (item may not exist)")
        return False

    # 2. Prepare image payloads: vendor gallery first, local thumbnail as fallback
    payloads = process_image_list(remote_image_urls) if remote_image_urls else []
    if not payloads and cfg.get("thumbnail_dir"):
        img_path = os.path.join(cfg["thumbnail_dir"], filename)
        if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
            logger.info("  > Encoding local image fallback...")
            payloads = [f"data:image/jpeg;base64,{encode_image(img_path)}"]

    # 3. Generate metadata with the vision LLM (text-only if no images survived)
    if payloads:
        logger.info(f"  > Generating metadata with {len(payloads)} image(s)...")
    else:
        logger.info("  > No image available. Generating text-only metadata.")
    raw = generate_metadata(client, model, payloads or None, web_desc)
    if raw is None:
        logger.info(f"  > FAILED {filename}: LLM call failed")
        return False

    meta = extract_json(raw)
    if not isinstance(meta, dict):
        logger.info(f"  > FAILED {filename}: LLM output was not parseable JSON")
        return False

    # 4. Normalize (shared with the markdown importer) and upsert into the DB
    item = normalize_item(
        {
            "product_name": prod_name,
            "filename": filename,
            "vendor": vendor,
            "sku": sku,
            "description": web_desc.strip(),
            "meta": meta,
        },
        source_file="scraper",
        thumbnail_dir=cfg["thumbnail_dir"],
    )
    item["metadata_model"] = model
    upsert_item(conn, item)
    conn.commit()  # durable progress: safe to interrupt / crash mid-run

    logger.info(f"  > Done. ({item['classification']})")
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="daz-catalog-mcp Phase 3 re-scrape tool")
    ap.add_argument("--config", default="config.json", help="path to config.json")
    ap.add_argument("--model", default=None,
                    help="LM Studio model id (overrides config \"model_name\")")
    ap.add_argument("--url", default=None,
                    help=f"OpenAI-compatible API base URL (default {DEFAULT_LM_STUDIO_URL})")
    ap.add_argument("--force", action="store_true",
                    help="reprocess files even if they are already in the DB")
    ap.add_argument("--only", nargs="+", metavar="FILE", default=None,
                    help="process only these thumbnail filenames (bypasses skip logic)")
    ap.add_argument("--retry-log", dest="retry_log", default=None, metavar="PATH",
                    help="parse a previous run's log and retry its FAILED items")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N processed items (for test runs)")
    args = ap.parse_args(argv)

    if args.only and args.retry_log:
        logger.error("Use either --only or --retry-log, not both.")
        return 2

    cfg = load_config(args.config)
    model = args.model or cfg.get("model_name")
    if not model:
        logger.error('No model set. Pass --model <id> or set "model_name" in config.json.')
        return 2
    url = args.url or cfg.get("lm_studio_url", DEFAULT_LM_STUDIO_URL)

    if not cfg.get("thumbnail_dir"):
        logger.error("config.json has no thumbnail_dir; nothing to scan.")
        return 2

    # Determine which files to process.
    only = None
    if args.retry_log:
        only = extract_failed_filenames(args.retry_log)
        logger.info(f"Retry mode: {len(only)} failed item(s) found in {args.retry_log}")
        if not only:
            return 0
    elif args.only:
        only = args.only

    images = only or [f for f in os.listdir(cfg["thumbnail_dir"]) if f.lower().endswith(IMAGE_EXTS)]

    conn = get_conn(cfg["db_path"])
    existing = {r[0] for r in conn.execute("SELECT filename FROM items")}
    client = OpenAI(base_url=url, api_key="lm-studio")

    mode = "FORCE (reprocess all)" if args.force else ("explicit list" if only else "incremental (skip existing)")
    logger.info(f"Model: {model}")
    logger.info(f"API:   {url}")
    logger.info(f"DB:    {cfg['db_path']} ({len(existing)} rows)")
    logger.info(f"Mode:  {mode} - {len(images)} candidate file(s)\n")

    ok = failed = skipped = processed = 0
    for filename in images:
        if not only and not args.force and filename in existing:
            logger.info(f"--- Skipping {filename} (already in DB) ---")
            skipped += 1
            continue

        delay = random.uniform(0.5, 2.0)
        time.sleep(delay)

        logger.info(f"--- Processing {filename} ---")
        if process_file(conn, cfg, client, model, filename):
            ok += 1
        else:
            failed += 1
        processed += 1

        if args.limit and processed >= args.limit:
            logger.info(f"--limit {args.limit} reached; stopping.")
            break
        if processed % 100 == 0:
            logger.info("  > Taking a long 'human' break (15s)...")
            time.sleep(15)

    conn.close()
    logger.info(f"\nScrape complete: {ok} upserted, {failed} failed, {skipped} skipped.")
    if failed:
        logger.info(f"Re-run with --retry-log {LOG_FILE} to retry the failures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
