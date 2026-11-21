"""База продуктов: одно название — одни цифры на 100 г.

Наполняется сама: всё, что Claude разобрал впервые, запоминается; дальше тот же продукт считается
по базе — одинаково и без запроса. Этикетка и штрихкод важнее оценки, ручная правка — важнее всего.
У одного названия может быть несколько записей (разные упаковки с разными штрихкодами) — тогда
берётся та, что использовалась последней.
"""
from __future__ import annotations

import re

from .. import db
from . import products as prod_mod
from .parser import normalize

NUM = r"(\d+(?:[.,]\d+)?)"
UNIT = r"(?:г|гр|грамм\w*)\.?"
SOURCES = {"label": "этикетка", "barcode": "штрихкод", "manual": "вручную", "fix": "исправлено",
           "claude": "оценка Claude", "history": "из записей"}


def keys(row: dict) -> list[str]:
    return [normalize(row["name"])] + [normalize(a) for a in (row.get("aliases") or "").split(",") if a.strip()]


def index(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        for k in keys(r):
            if k:
                out.setdefault(k, []).append(r)
    return out


def pick(cands: list[dict]) -> dict:
    """Из нескольких записей с одним названием: последняя использованная, затем закреплённая, затем частая."""
    return max(cands, key=lambda r: (r.get("last_used") or "", r["pinned"], r["uses"]))


def lookup(name: str, idx: dict[str, list[dict]]) -> dict | None:
    c = idx.get(normalize(name))
    return pick(c) if c else None


def item(row: dict, grams: float) -> dict:
    return prod_mod.item(row, grams)


def match_text(text: str, idx: dict[str, list[dict]]) -> tuple[dict, dict] | None:
    """«куриное филе 150 г» / «150 г куриное филе» → (позиция, запись базы)."""
    t = normalize(text)
    if m := re.fullmatch(rf"(.+?)\s+{NUM}\s*({UNIT})?", t):
        name, num, unit = m.group(1), m.group(2), m.group(3)
    elif m := re.fullmatch(rf"{NUM}\s*({UNIT})\s+(.+)", t):
        name, num, unit = m.group(3), m.group(1), m.group(2)
    else:
        return None
    if not unit and float(num.replace(",", ".")) < 20:   # «яйца 2» — это штуки, не граммы
        return None
    row = lookup(name, idx)
    grams = float(num.replace(",", "."))
    if not row or not 1 <= grams <= 3000:
        return None
    return item(row, grams), row


def per100(i: dict) -> dict:
    k = 100 / i["grams"]
    return {f: round(i[f] * k, 1) for f in ("kcal", "protein", "fat", "carbs")}


def apply(items: list[dict], idx: dict[str, list[dict]]) -> tuple[list[dict], list[dict]]:
    """Позиции из разбора → цифры из базы для известных продуктов. (позиции, использованные записи)."""
    out, used = [], []
    for i in items:
        row = lookup(i["name"], idx) if i.get("grams") else None
        if row:
            out.append(item(row, i["grams"]) | {"name": i["name"]})
            used.append(row)
        else:
            out.append(i)
    return out, used


async def learn(items: list[dict], idx: dict[str, list[dict]], source: str = "claude") -> int:
    """Новые продукты из разбора — в базу (не в быструю запись). Возвращает, сколько добавлено."""
    n = 0
    for i in items:
        name = normalize(i["name"])
        if not name or name in idx or not i.get("grams") or i["grams"] <= 0 or i["kcal"] <= 0:
            continue
        p = per100(i)
        if p["kcal"] > 950:
            continue
        pid = await db.add_product(name, p["kcal"], p["protein"], p["fat"], p["carbs"], source=source, pinned=0)
        idx[name] = [await db.get_product(pid)]
        n += 1
    return n


async def correct(items: list[dict], idx: dict[str, list[dict]]) -> list[str]:
    """После правки приёма пищи: если калорийность продукта на 100 г заметно другая — правка идёт в базу.
    Закреплённые (этикетка, штрихкод) не трогаем: там цифры точные, ошибка скорее в граммах."""
    changed = []
    for i in items:
        if not i.get("grams") or i["grams"] <= 0:
            continue
        row = lookup(i["name"], idx)
        p = per100(i)
        if row and not row["pinned"] and row["kcal"] and abs(p["kcal"] / row["kcal"] - 1) > 0.1:
            await db.update_product(row["id"], source="fix", **p)
            changed.append(row["name"])
    await learn(items, idx, "fix")
    return changed
