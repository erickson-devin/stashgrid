"""Barcode product-name lookups (moved from app.py, SG-06). ISBN lookups join here with SG-08."""
import requests as http_client

from stashgrid.config import logger


def _lookup_open_food_facts(barcode):
    """Try Open Food Facts API. Returns product name or empty string."""
    try:
        resp = http_client.get(
            f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json",
            timeout=2,
            headers={"User-Agent": "StashGrid/1.0"}
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == 1:
                p = data.get("product", {})
                name = p.get("product_name") or p.get("generic_name") or p.get("brands")
                if name:
                    return name.strip()
    except Exception as e:
        logger.debug("Open Food Facts lookup failed for %s: %s", barcode, e)
    return ""


def _lookup_upcitemdb(barcode):
    """Try UPCitemdb API as a fallback. Returns product title or empty string."""
    try:
        resp = http_client.get(
            f"https://api.upcitemdb.com/prod/trial/lookup?upc={barcode}",
            timeout=2,
            headers={"User-Agent": "StashGrid/1.0"}
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            if items:
                title = items[0].get("title")
                if title:
                    return title.strip()
    except Exception as e:
        logger.debug("UPCitemdb lookup failed for %s: %s", barcode, e)
    return ""


def _web_lookup_name(barcode):
    """Chain Open Food Facts → UPCitemdb, mirroring scanner.py lookup_product_name.
    Only attempted for numeric barcodes (UPC/EAN). Returns empty string on failure.
    """
    if not barcode.isdigit():
        return ""
    return _lookup_open_food_facts(barcode) or _lookup_upcitemdb(barcode) or ""
