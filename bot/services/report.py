"""Выгрузка данных в Markdown для разбора в сильной модели (без запроса к API)."""
from __future__ import annotations

from datetime import date, timedelta

from .. import db
from . import nutrition
from .budget import fmt

PROMPT = """## Задание

Ты опытный спортивный диетолог. Ниже — мои реальные данные из трекера питания: профиль, цифры по дням и всё, \
что я ел. Цель — снижать вес максимально эффективно, сохраняя мышцы, и так, чтобы режим был выполнимым долго.

Разбери:
1. Соблюдение бюджета калорий и белка; в какие дни и почему срываюсь.
2. Состав рациона: источники белка, сытость, скрытые калории (масло, соусы, напитки), что даёт много калорий при малой пользе.
3. Режим питания (число приёмов и время — см. предпочтения): как распределены калории и белок между приёмами.
4. Правдоподобность записей: где похоже на недоучёт (сравни с динамикой веса и расчётным весом).
5. Темп снижения веса относительно ожидаемого и реалистичность цели.
6. Тренировки (столбец «Тренировка»): хватает ли еды и белка в дни тренировок, чтобы сохранять силу и мышцы.

Ответ: сначала 3–5 самых важных выводов, затем конкретные изменения на следующую неделю — \
что заменить, на что, в каких количествах. Без общих советов. Если данных мало или чего-то не хватает — скажи, \
что именно записывать. Можешь задать мне уточняющие вопросы.
"""

COMPLETE = {1: "да", -1: "нет", 0: "?"}


def grams_txt(i: dict) -> str:
    return f" {fmt(i['grams'])} г" if i["grams"] else ""


async def build(profile, days: int) -> str:
    today = db.now().date()
    since = (today - timedelta(days=days - 1)).isoformat()
    until = today.isoformat()
    b = nutrition.make_budget(profile["tdee"], profile["pace"], profile["floor_kcal"])

    out = [f"# Данные трекера питания: {since} — {until} ({days} дн.)", "", PROMPT, "## Профиль", ""]
    last = await db.last_weight()
    out += [
        f"- Пол: {'мужской' if profile['sex'] == 'm' else 'женский'}, возраст {profile['age']}, рост {profile['height_cm']:g} см",
        f"- Вес на старте: {profile['start_weight']:g} кг ({profile['created']}), сейчас по тренду: "
        f"{last['trend']:.1f} кг" if last else f"- Вес на старте: {profile['start_weight']:g} кг",
        f"- Цель: {profile['target_weight']:g} кг",
        f"- Расход в расчётах: {profile['tdee']} ккал/день; бюджет еды: {profile['budget']} ккал/день; "
        f"цель по белку: {profile['protein_goal']} г/день",
        f"- Темп при текущем бюджете: ≈{b.achievable_pace:g} кг/нед (желаемый {profile['pace']:g})",
        f"- Предпочтения в еде: {profile['diet_prefs'] or 'не указаны'}",
        f"- Не ест: {profile['diet_dislikes'] or 'не указано'}",
        f"- Протеин: {profile['shake_servings']} порц./день по {profile['shake_protein']:g} г белка",
        "",
        "Вес «тренд» — сглаженное значение без шума от воды. «День записан»: да — подтверждено, что записано всё; "
        "нет — записано не всё; ? — не отмечено. «Тренировка» — калории с часов (Polar H10 / Huawei) после поправки "
        f"×{nutrition.WORKOUT_NET:g} на базовый обмен, который уже входит в расход. "
        "«Дефицит» = расход в расчётах + тренировка − съедено.",
        "",
        "## По дням",
        "",
        "| Дата | Вес | Тренд | Ккал | Белок | Жиры | Углев. | Тренировка | Дефицит | День записан |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    weights = {r["date"]: r for r in await db.weights(since)}
    flags = {r["date"]: r["complete"] for r in await db._all(
        "SELECT date, complete FROM day_state WHERE date >= ?", since)}
    wo = await db.workouts_by_day(since, until)
    d = date.fromisoformat(since)
    while d <= today:
        ds = d.isoformat()
        tot = await db.day_totals(ds)
        w = weights.get(ds)
        gym = fmt(wo[ds]) if ds in wo else ""
        bal = profile["tdee"] + wo.get(ds, 0) - tot["kcal"] if tot["n"] else None
        out.append(
            f"| {ds} | {w['weight']:.1f} | {w['trend']:.1f} | " if w else f"| {ds} | | | ")
        out[-1] += (f"{fmt(tot['kcal'])} | {fmt(tot['protein'])} | {fmt(tot['fat'])} | {fmt(tot['carbs'])} | {gym} | "
                    f"{(f'{bal:+.0f}') if bal is not None else ''} | {COMPLETE.get(flags.get(ds, 0), '?')} |"
                    if tot["n"] else f"нет записей | | | | {gym} | | |")
        d += timedelta(days=1)

    out += ["", "## Что ел", ""]
    d = date.fromisoformat(since)
    while d <= today:
        meals = await db.meals_on(d.isoformat())
        if meals:
            out.append(f"### {d.isoformat()} — {fmt(sum(m['kcal'] for m in meals))} ккал")
            for m in meals:
                items = await db.meal_items(m["id"])
                parts = "; ".join(
                    f"{i['name']}{grams_txt(i)} — {fmt(i['kcal'])} ккал "
                    f"(Б {i['protein']:g}, Ж {i['fat']:g}, У {i['carbs']:g})" for i in items)
                out.append(f"- {m['ts'][11:16]}: {parts}")
            out.append("")
        d += timedelta(days=1)

    prods = await db.products()
    if prods:
        out += ["## Продукты с точными данными с этикетки (на 100 г)", ""]
        out += [f"- {p['name']}: {p['kcal']:g} ккал, Б {p['protein']:g}, Ж {p['fat']:g}, У {p['carbs']:g}" for p in prods]
        out.append("")
    out.append("Калорийность продуктов без этикетки — оценка по описанию, погрешность ±20–30%.")
    return "\n".join(out)
