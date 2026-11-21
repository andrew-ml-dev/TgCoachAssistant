"""Что человек на самом деле ест: продукты, порции, приёмы пищи и явные перекосы. Локально, без Claude.

Это сырьё для недельного разбора рациона (llm.diet_review) и для «Что съесть?» — там блюда из разбора,
а цифры на 100 г берутся отсюда, из записей человека, а не из ответа модели.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from statistics import median

from .. import db
from . import nutrition
from .parser import normalize

MERGE_MIN = 20                 # записи с разницей до 20 минут — один приём пищи
VEG_WORDS = ("огур", "помид", "томат", "салат", "капуст", "морков", "перец", "брокк", "кабач", "корнишон",
             "зелень", "овощ", "шпинат", "редис", "свекл", "цветная", "фасоль стручк", "спарж", "баклаж")


def slot(hour: int) -> str:
    return "утро" if hour < 11 else "день" if hour < 16 else "вечер"


def is_veg(name: str) -> bool:
    return any(w in name for w in VEG_WORDS)


def is_powder(item: dict) -> bool:
    return item["grams"] > 0 and item["protein"] / item["grams"] >= 0.5


def _ts(row) -> datetime:
    return datetime.fromisoformat(row["ts"])


def sittings(rows: list[dict]) -> list[dict]:
    """Приёмы пищи: записи одного дня с разницей ≤ 20 минут склеиваются (пюре, филе и хлеб за 2 минуты — обед)."""
    meals: dict[int, dict] = {}
    for r in rows:
        m = meals.setdefault(r["meal_id"], {"date": r["date"], "ts": _ts(r), "items": []})
        if r["grams"] > 0 and r["name"]:
            m["items"].append(r)
    out: list[dict] = []
    for m in sorted(meals.values(), key=lambda x: x["ts"]):
        if not m["items"]:
            continue
        last = out[-1] if out else None
        if last and last["date"] == m["date"] and (m["ts"] - last["end"]).total_seconds() <= MERGE_MIN * 60:
            last["items"] += m["items"]
            last["end"] = m["ts"]
        else:
            out.append({"date": m["date"], "ts": m["ts"], "end": m["ts"], "items": list(m["items"])})
    for s in out:
        s["kcal"] = sum(i["kcal"] for i in s["items"])
        s["slot"] = slot(s["ts"].hour)
    return out


def my_foods(rows: list[dict]) -> list[dict]:
    """Продукты по нормализованному названию: частота, обычная порция, медиана на 100 г, разброс калорий."""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["grams"] > 0 and r["name"]:
            by[normalize(r["name"])].append(r)
    total = sum(r["kcal"] for v in by.values() for r in v) or 1
    out = []
    for name, items in by.items():
        per = [i["kcal"] / i["grams"] * 100 for i in items]
        out.append({
            "name": name,
            "days": len({i["date"] for i in items}),
            "times": len(items),
            "grams": round(median(i["grams"] for i in items)),
            "kcal": round(median(per)),
            "protein": round(median(i["protein"] / i["grams"] * 100 for i in items), 1),
            "fat": round(median(i["fat"] / i["grams"] * 100 for i in items), 1),
            "carbs": round(median(i["carbs"] / i["grams"] * 100 for i in items), 1),
            "spread": round(max(per) / min(per), 2) if min(per) > 0 else 1.0,
            "slot": Counter(slot(_ts(i).hour) for i in items).most_common(1)[0][0],
            "share": round(sum(i["kcal"] for i in items) / total * 100),
            "powder": is_powder(items[0]),
            "veg": is_veg(name),
        })
    out.sort(key=lambda f: (-f["times"], -f["share"]))
    return out


def my_meals(sits: list[dict], min_times: int = 2) -> list[dict]:
    """Повторяющиеся сочетания продуктов в одном приёме: «пюре + котлеты» — готовые шаблоны блюд."""
    combos: dict[frozenset, list[dict]] = defaultdict(list)
    for s in sits:
        names = frozenset(normalize(i["name"]) for i in s["items"])
        if len(names) >= 2:
            combos[names].append(s)
    out = []
    for names, ss in combos.items():
        if len(ss) < min_times:
            continue
        grams = {n: round(median(i["grams"] for s in ss for i in s["items"] if normalize(i["name"]) == n))
                 for n in sorted(names)}
        out.append({"items": grams, "times": len(ss), "kcal": round(median(s["kcal"] for s in ss)),
                    "slot": Counter(s["slot"] for s in ss).most_common(1)[0][0]})
    out.sort(key=lambda m: -m["times"])
    return out


async def collect(profile, days: int = 28) -> dict:
    """Всё для разбора: продукты, приёмы пищи, средние за полные дни, перекосы."""
    rows = await db.eaten_rows(db.days_ago(days))
    full = {d["date"] for d in await db.counted_days(db.days_ago(days), db.days_ago(1), profile["budget"])}
    sits = sittings(rows)
    foods = my_foods(rows)
    fr = [r for r in rows if r["date"] in full and r["grams"] > 0]
    fs = [s for s in sits if s["date"] in full]
    n = len(full)
    st: dict = {"full_days": n, "days_logged": len({r["date"] for r in rows})}
    if n:
        k = {x: sum(r[x] for r in fr) / n for x in ("kcal", "protein", "fat", "carbs")}
        powder_p = sum(r["protein"] for r in fr if is_powder(r))
        veg_g = sum(r["grams"] for r in fr if is_veg(normalize(r["name"])))
        by_day: dict[str, list[dict]] = defaultdict(list)
        for s in fs:
            by_day[s["date"]].append(s)
        biggest = [max(x["kcal"] for x in ss) / max(1, sum(x["kcal"] for x in ss)) for ss in by_day.values()]
        # настоящие приёмы пищи — без отдельных протеиновых коктейлей
        food_days = [sorted((x for x in ss if not all(is_powder(i) for i in x["items"])), key=lambda x: x["ts"])
                     for ss in by_day.values()]
        food_days = [d for d in food_days if d]
        per_day = round(median(len(d) for d in food_days)) if food_days else 0
        hours = [round(median(d[i]["ts"].hour for d in food_days if len(d) > i)) for i in range(per_day)]
        st.update({
            "kcal": round(k["kcal"]), "protein": round(k["protein"]), "fat": round(k["fat"]), "carbs": round(k["carbs"]),
            "powder_share": round(powder_p / max(1, sum(r["protein"] for r in fr)) * 100),
            "veg_g": round(veg_g / n),
            "sittings_per_day": round(len(fs) / n, 1),
            "sitting_hours": sorted(Counter(s["ts"].hour for s in fs).most_common(4)),
            "biggest_share": round(median(biggest) * 100) if biggest else 0,
            "meals_per_day": per_day, "meal_hours": hours,
        })
    return {"rows": rows, "sittings": sits, "foods": foods, "meals": my_meals(sits), "stats": st,
            "flags": flags(profile, st, foods)}


def flags(profile, st: dict, foods: list[dict]) -> list[str]:
    """Явные перекосы по правилам — факты для разбора (модель считает хуже, чем код)."""
    out = []
    if not st.get("full_days"):
        return out
    from .suggest import fat_min
    if st["powder_share"] > 50:
        out.append(f"протеиновый порошок даёт {st['powder_share']}% всего белка")
    if st["veg_g"] < 200:
        out.append(f"овощей в среднем {st['veg_g']} г в день (норма от 300–400 г)")
    pmin = nutrition.protein_min(profile["target_weight"])
    if st["protein"] < pmin:
        out.append(f"белок в среднем {st['protein']} г — ниже минимума {pmin} г")
    if st["fat"] < fat_min(profile["target_weight"]):
        out.append(f"жиры в среднем {st['fat']} г — ниже нижней границы {fat_min(profile['target_weight'])} г")
    if st["biggest_share"] > 50:
        out.append(f"самый большой приём — {st['biggest_share']}% калорий дня")
    for f in foods:
        portion = f["kcal"] * f["grams"] / 100
        if f["times"] >= 3 and portion > 200 and f["kcal"] and f["protein"] * 4 / f["kcal"] < 0.12:
            out.append(f"«{f['name']}»: {f['times']} раз, порция ≈{round(portion)} ккал и почти без белка")
        if f["times"] >= 2 and f["spread"] > 1.25:
            out.append(f"«{f['name']}» записан с разной калорийностью (разница ×{f['spread']}) — "
                       "стоит сохранить его как свой продукт с этикетки")
    return out
