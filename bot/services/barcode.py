"""Штрихкод с фото (zxing-cpp, локально) и поиск продукта в Open Food Facts."""
from __future__ import annotations

import asyncio
import logging
from io import BytesIO

import aiohttp
from PIL import Image, ImageOps

from .parser import normalize

log = logging.getLogger(__name__)
OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
OFF_FIELDS = "product_name,product_name_pl,product_name_en,brands,nutriments,product_quantity,product_quantity_unit"
HEADERS = {"User-Agent": "WeightBot/1.0 (personal Telegram bot)"}


def _decode(data: bytes) -> str | None:
    import zxingcpp
    img = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("L")
    for r in zxingcpp.read_barcodes(img):
        # товарные коды: EAN-8, UPC-A, EAN-13, GTIN-14; QR и прочее пропускаем
        if r.text.isdigit() and len(r.text) in (8, 12, 13, 14):
            return r.text
    return None


async def decode(data: bytes) -> str | None:
    return await asyncio.to_thread(_decode, data)


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_off(p: dict) -> dict | None:
    """Ответ Open Food Facts → {name, kcal, protein, fat, carbs, pack}; kcal=None, если нет пищевой ценности."""
    title = p.get("product_name_pl") or p.get("product_name") or p.get("product_name_en") or ""
    brand = (p.get("brands") or "").split(",")[0].strip()
    if brand and brand.lower() not in title.lower():
        title = f"{brand} {title}"
    name = normalize(title)[:40].strip()
    if not name:
        return None
    n = p.get("nutriments") or {}
    kcal = _num(n.get("energy-kcal_100g"))
    if kcal is None and _num(n.get("energy_100g")) is not None:
        kcal = round(_num(n.get("energy_100g")) / 4.184, 1)  # дано только в кДж
    pack = _num(p.get("product_quantity"))
    if (p.get("product_quantity_unit") or "g").lower() not in ("g", "ml") or not pack or not 5 <= pack <= 3000:
        pack = None
    return {"name": name, "kcal": kcal if kcal and kcal > 0 else None, "protein": _num(n.get("proteins_100g")) or 0,
            "fat": _num(n.get("fat_100g")) or 0, "carbs": _num(n.get("carbohydrates_100g")) or 0, "pack": pack}


async def lookup(code: str) -> dict | None:
    try:
        async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(OFF_URL.format(code=code), params={"fields": OFF_FIELDS}) as r:
                if r.status == 404:
                    return None
                body = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        log.warning("Open Food Facts недоступен: %s", e)
        return None
    if body.get("status") != 1:
        return None
    return parse_off(body.get("product") or {})
