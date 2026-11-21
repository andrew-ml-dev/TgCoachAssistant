"""Разбор рациона: факты из записей (diet_profile) → Sonnet → поправки и блюда «под меня».

Модель даёт только названия продуктов, роли и граммы. Калорийность и БЖУ на 100 г подставляются
из записей человека или из справочника base_foods; незнакомые названия отбрасываются.
"""
from __future__ import annotations

from html import escape

from .. import db
from . import base_foods, diet_profile, llm, nutrition
from .budget import fmt
from .parser import normalize

MIN_FULL_DAYS = 5     # полных дней за 14 — меньше разбирать нечего


async def readiness(profile) -> int:
    """Сколько полных дней не хватает до разбора (0 — можно)."""
    full = await db.counted_days(db.days_ago(14), db.days_ago(1), profile["budget"])
    return max(0, MIN_FULL_DAYS - len(full))


def _food_line(f: dict) -> str:
    tags = [t for t, on in (("протеин", f["powder"]), ("овощ", f["veg"])) if on]
    return (f"- {f['name']}: {f['times']} раз за {f['days']} дн., обычно {f['grams']} г, {f['slot']}; "
            f"на 100 г {f['kcal']} ккал, Б {f['protein']:g} Ж {f['fat']:g} У {f['carbs']:g}"
            + (f" ({', '.join(tags)})" if tags else ""))


def facts_text(profile, data: dict, prev: dict | None, since_prev: list[str]) -> str:
    from .suggest import fat_min
    st = data["stats"]
    tw = profile["target_weight"]
    lines = [
        "ЦЕЛИ",
        f"Бюджет еды {profile['budget']} ккал/день (в дни тренировок больше), темп {profile['pace']} кг/нед, "
        f"белок минимум {nutrition.protein_min(tw)} г, цель {profile['protein_goal']} г, "
        f"жиры не меньше {fat_min(tw)} г.",
        f"Предпочтения пользователя: {profile['diet_prefs'] or 'не указаны'}.",
        "",
        f"СРЕДНИЕ ЗА ПОЛНЫЕ ДНИ ({st['full_days']} дн. из {st['days_logged']} с записями)",
    ]
    if st["full_days"]:
        lines += [
            f"{st['kcal']} ккал, Б {st['protein']} Ж {st['fat']} У {st['carbs']} г; "
            f"белок из протеинового порошка {st['powder_share']}%; овощей {st['veg_g']} г/день.",
            f"Приёмов пищи в день {st['sittings_per_day']}, чаще всего в часы: "
            + ", ".join(f"{h}:00" for h, _ in st["sitting_hours"]) + f"; самый большой приём — {st['biggest_share']}% дня.",
            f"Без отдельных протеиновых коктейлей — {st['meals_per_day']} приёма еды в день, обычно в "
            + ", ".join(f"{h}:00" for h in st["meal_hours"]) + ".",
        ]
    lines += ["", "НАЙДЕННЫЕ ПЕРЕКОСЫ"] + ([f"- {x}" for x in data["flags"]] or ["- явных нет"])
    lines += ["", "ПРОДУКТЫ ПОЛЬЗОВАТЕЛЯ (за 4 недели, по частоте)"] + [_food_line(f) for f in data["foods"][:30]]
    if data["meals"]:
        lines += ["", "ПОВТОРЯЮЩИЕСЯ ПРИЁМЫ ПИЩИ"] + [
            f"- {' + '.join(f'{n} {g} г' for n, g in m['items'].items())}: {m['times']} раз, ≈{m['kcal']} ккал, {m['slot']}"
            for m in data["meals"][:12]]
    lines += ["", "СПРАВОЧНИК (можно предлагать, цифры знает бот)", ", ".join(b.name for b in base_foods.FOODS)]
    if prev:
        lines += ["", f"ПРОШЛЫЙ РАЗБОР ({prev['created'][:10]})"] + [
            f"- {f['change']}" for f in prev.get("fixes", [])]
        lines.append("Что ел с тех пор: " + (", ".join(since_prev[:25]) or "записей нет"))
    return "\n".join(lines)


def _known(data: dict) -> dict[str, dict]:
    """Название → цифры на 100 г: сначала свои записи, потом справочник."""
    out = {b.name: {"kcal": b.kcal, "protein": b.protein, "fat": b.fat, "carbs": b.carbs, "piece": b.piece}
           for b in base_foods.FOODS}
    for f in data["foods"]:
        out[f["name"]] = {k: f[k] for k in ("kcal", "protein", "fat", "carbs")} | {"piece": 0}
    return out


def resolve(review: llm.DietReview, data: dict) -> list[dict]:
    """Блюда из ответа модели → только известные продукты с нашими цифрами; без белковой части — отбрасываются."""
    known = _known(data)
    dishes = []
    for d in review.dishes:
        items = []
        for it in d.items:
            name = normalize(it.name)
            per = known.get(name)
            if not per or per["kcal"] <= 0 or it.grams <= 0:
                continue
            grams = float(it.grams)
            lo = max(0.0, min(float(it.lo), grams))
            hi = min(max(float(it.hi), grams), grams * 3)
            step = per["piece"] or (50 if hi > 400 else 10)
            items.append({"name": name, "role": it.role, "grams": grams, "lo": lo, "hi": hi, "step": step,
                          **{k: per[k] for k in ("kcal", "protein", "fat", "carbs")}})
        if any(i["role"] == "p" for i in items):
            dishes.append({"title": d.title.strip()[:40], "slot": d.slot, "mine": d.from_history, "items": items})
    return dishes


async def run(profile) -> tuple[str, dict | None]:
    """Разбор сейчас: (сообщение, сохранённый план или None)."""
    short = await readiness(profile)
    if short:
        return (f"🧠 Для разбора рациона нужно ещё {short} полн. дн. записей за последние 2 недели "
                "(день считается полным, если записано ≥60% бюджета или отмечено «✅ Всё записал»)."), None
    data = await diet_profile.collect(profile)
    prev = await db.last_plan()
    since_prev = []
    if prev:
        seen: dict[str, int] = {}
        for r in data["rows"]:
            if r["ts"] >= prev["created"] and r["name"]:
                seen[normalize(r["name"])] = seen.get(normalize(r["name"]), 0) + 1
        since_prev = [n for n, _ in sorted(seen.items(), key=lambda kv: -kv[1])]
    review = await llm.diet_review(facts_text(profile, data, prev, since_prev))
    dishes = resolve(review, data)
    plan = {"summary": review.summary, "fixes": [f.model_dump() for f in review.fixes[:3]], "dishes": dishes,
            "meal_hours": data["stats"].get("meal_hours", [])}
    plan["id"] = await db.save_plan(plan)
    return review_text(plan), plan


def review_text(plan: dict) -> str:
    lines = ["🧠 <b>Разбор рациона</b>", escape(plan["summary"])]
    if plan["fixes"]:
        lines += ["", "<b>Что поправить на этой неделе</b>"]
        for i, f in enumerate(plan["fixes"], 1):
            lines.append(f"{i}. <b>{escape(f['change'])}</b>\n   <i>{escape(f['problem'])} — {escape(f['why'])}</i>")
    if plan["dishes"]:
        lines += ["", "<b>Ваши блюда</b> (⭐ — вы их уже едите); «🍽 Что съесть?» подгонит порции под остаток:"]
        for d in plan["dishes"]:
            kc = sum(i["kcal"] * i["grams"] / 100 for i in d["items"])
            p = sum(i["protein"] * i["grams"] / 100 for i in d["items"])
            parts = ", ".join(f"{i['name']} {fmt(i['grams'])} г" for i in d["items"])
            lines.append(f"{'⭐' if d['mine'] else '•'} {escape(d['title'])} — {fmt(kc)} ккал, Б {fmt(p)}: {escape(parts)}")
    return "\n".join(lines)
