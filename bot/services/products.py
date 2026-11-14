"""«Мои продукты»: точные данные с этикетки (на 100 г) вместо оценки Claude."""
from __future__ import annotations

import re

from .parser import normalize

NUM = r"(\d+(?:[.,]\d+)?)"
UNIT = r"(?:г|гр|грамм\w*)\.?"


def item(p: dict, grams: float) -> dict:
    k = grams / 100
    return {"name": p["name"], "grams": grams, "kcal": round(p["kcal"] * k, 1),
            "protein": round(p["protein"] * k, 1), "fat": round(p["fat"] * k, 1), "carbs": round(p["carbs"] * k, 1)}


def match_exact(text: str, products: list[dict]) -> dict | None:
    """«hi tec protein 110 г» или «110 г hi tec protein» — считается без запроса к Claude."""
    t = normalize(text)
    for p in products:
        n = re.escape(p["name"])
        m = re.match(rf"^{n}\s+{NUM}\s*{UNIT}?$", t) or re.match(rf"^{NUM}\s*{UNIT}\s+{n}$", t)
        if m:
            return item(p, float(m.group(1).replace(",", ".")))
    return None


def apply(items: list[dict], products: list[dict]) -> list[dict]:
    """Заменяет оценку Claude цифрами с этикетки для известных продуктов."""
    out = []
    for i in items:
        low = i["name"].lower()
        p = next((p for p in products if p["name"] in low or (len(low) >= 5 and low in p["name"])), None)
        out.append(item(p, i["grams"]) if p and i.get("grams") else i)
    return out
