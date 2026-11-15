"""Дневной бюджет: экран «Сегодня», остаток, баланс недели."""
from __future__ import annotations

from datetime import date, timedelta
from html import escape

from .. import db
from . import nutrition

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def fmt(n: float) -> str:
    return f"{round(n):,}".replace(",", " ")


def human_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


async def remaining(profile) -> tuple[float, float, float]:
    """(съедено, осталось, процент бюджета)."""
    tot = await db.day_totals(db.today())
    eaten = tot["kcal"]
    return eaten, profile["budget"] - eaten, eaten / profile["budget"] * 100


async def left_line(profile) -> str:
    eaten, left, pct = await remaining(profile)
    tot = await db.day_totals(db.today())
    head = (f"Осталось на сегодня: <b>{fmt(left)} ккал</b>" if left >= 0
            else f"Перебор: <b>+{fmt(-left)} ккал</b>")
    return (f"{head}\n{nutrition.bar(pct)} {fmt(eaten)} / {fmt(profile['budget'])} · "
            f"белок {fmt(tot['protein'])} / {profile['protein_goal']} г")


async def week_balance(profile) -> tuple[float, int] | None:
    """Сумма отклонений от бюджета с понедельника по вчера (только дни с записями)."""
    today = db.now().date()
    monday = today - timedelta(days=today.weekday())
    if monday == today:
        return None
    days = await db.daily_kcal(monday.isoformat(), db.days_ago(1))
    if not days:
        return None
    return sum(d["kcal"] - profile["budget"] for d in days), len(days)


def _signed(n: float) -> str:
    return ("+" if n > 0 else "−") + fmt(abs(n))


async def over_warning(profile) -> str | None:
    """Один раз в день, когда бюджет превышен впервые."""
    _, left, pct = await remaining(profile)
    st = await db.day_state()
    if pct <= 100 or st["warned"] >= 100:
        return None
    await db.set_day_state(warned=100)
    grams = round(-left / nutrition.KCAL_PER_KG * 1000)
    text = f"🔴 Бюджет превышен на {fmt(-left)} ккал (≈{grams} г к результату недели)."
    wb = await week_balance(profile)
    if wb:
        text += f" С начала недели до этого: {_signed(wb[0])} ккал."
    return text


async def today_text(profile) -> str:
    day = db.today()
    tot = await db.day_totals(day)
    eaten, left, pct = await remaining(profile)
    lines = [
        f"<b>{human_date(date.fromisoformat(day))}</b>",
        f"🔥 {fmt(eaten)} / {fmt(profile['budget'])} ккал   {nutrition.bar(pct)} {round(pct)}%",
        (f"Осталось: <b>{fmt(left)} ккал</b>" if left >= 0 else f"Перебор: <b>+{fmt(-left)} ккал</b>"),
        f"🥩 Белок {fmt(tot['protein'])} / {profile['protein_goal']} г   "
        f"🧈 Жиры {fmt(tot['fat'])} г   🍞 Углеводы {fmt(tot['carbs'])} г",
    ]
    wo = await db.workout_kcal(day)
    bal = profile["tdee"] + wo - eaten
    spent = f"расход {fmt(profile['tdee'])}" + (f" + тренировка {fmt(wo)}" if wo else "")
    lines.append(f"⚖️ Баланс сейчас: {spent} − съедено {fmt(eaten)} = "
                 f"<b>{'дефицит' if bal >= 0 else 'профицит'} {fmt(abs(bal))} ккал</b>")
    if wo:
        parts = []
        for w in await db.workouts_on(day):
            mins = f" {w['minutes']} мин" if w["minutes"] and str(w["minutes"]) not in w["descr"] else ""
            hr = f", пульс {w['hr_avg']}" if w["hr_avg"] else ""
            parts.append(f"{escape(w['descr'])}{mins}{hr} → {fmt(w['kcal'])}")
        lines.append("🏋️ " + "; ".join(parts) + ". Идёт в дефицит, бюджет еды не растёт.")
    act = await db.activity_kcal(day)
    if act:
        lines.append(f"🏃 Шаги: ≈{fmt(act)} ккал (уже входят в расход, отдельно не прибавляю)")
    w = await db.weight_on(day)
    if w:
        week = await db.weight_before(db.days_ago(7))
        delta = f", {w['trend'] - week['trend']:+.1f} за 7 дн" if week and week["date"] != day else ""
        lines.append(f"⚖️ Вес утром: {w['weight']:.1f} (тренд {w['trend']:.1f}{delta})")
    else:
        lines.append("⚖️ Вес сегодня не внесён")
    meals = await db.meals_on(day)
    if meals:
        lines.append("")
        lines += [f"• {escape(m['title'])} — {fmt(m['kcal'])}" for m in meals]
    return "\n".join(lines)


WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


async def period_text(profile, days: int) -> str:
    """Отчёт за последние N дней включая сегодня; days=0 — за всё наблюдение."""
    today = db.now().date()
    if days:
        start = today - timedelta(days=days - 1)
    else:
        first = await db.first_date()
        start = date.fromisoformat(first) if first else today
    since, until = start.isoformat(), today.isoformat()
    n_days = (today - start).days + 1
    title = f"за всё наблюдение, {n_days} дн." if not days else f"{days} дн."
    lines = [f"<b>Отчёт: {title}</b> ({start.day} {MONTHS[start.month - 1]} — {today.day} {MONTHS[today.month - 1]})"]

    logged = await db.daily_kcal(since, until)
    reliable = {d["date"] for d in await db.counted_days(since, until, profile["budget"])}
    wo = await db.workouts_by_day(since, until)
    if not logged:
        lines.append("За этот период записей еды нет.")
    if logged and n_days <= 7:
        lines.append("")
        for d in logged:
            dd = date.fromisoformat(d["date"])
            mark = "сегодня" if d["date"] == until else ("✅" if d["date"] in reliable else "⚠️ неполный")
            gym = f" · 🏋️ {fmt(wo[d['date']])}" if d["date"] in wo else ""
            lines.append(f"{WD[dd.weekday()]} {dd.strftime('%d.%m')} — {fmt(d['kcal'])} ккал · Б {fmt(d['protein'] or 0)}{gym} · "
                         f"дефицит {fmt(profile['tdee'] + wo.get(d['date'], 0) - d['kcal'])} · {mark}")
        lines.append("")

    full = [d for d in logged if d["date"] in reliable and d["date"] != until]
    if logged:
        lines.append(f"Записано дней: {len(logged)} из {n_days}, полных (без сегодня): {len(full)}")
    if full:
        tot = await db.period_totals([d["date"] for d in full])
        k = len(full)
        ok = sum(1 for d in full if d["kcal"] <= profile["budget"] * 1.05)
        deficit = sum(profile["tdee"] + wo.get(d["date"], 0) - d["kcal"] for d in full)
        lines += [
            f"🔥 В среднем: <b>{fmt(tot['kcal'] / k)} ккал</b> при бюджете {fmt(profile['budget'])}",
            f"🥩 Б {fmt(tot['protein'] / k)} / {profile['protein_goal']} г · Ж {fmt(tot['fat'] / k)} г · "
            f"У {fmt(tot['carbs'] / k)} г",
            f"✅ В бюджете: {ok} из {k} дн.",
            f"📉 Дефицит за полные дни: {fmt(deficit)} ккал ≈ {-deficit / nutrition.KCAL_PER_KG:+.1f} кг",
        ]
    if wo:
        lines.append(f"🏋️ Тренировки: {len(wo)} дн., {fmt(sum(wo.values()))} ккал (уже в дефиците)")
    ws = await db.weights(since)
    if len(ws) >= 2:
        lines.append(f"⚖️ Вес по тренду: {ws[0]['trend']:.1f} → <b>{ws[-1]['trend']:.1f}</b> "
                     f"({ws[-1]['trend'] - ws[0]['trend']:+.1f} кг, взвешиваний: {len(ws)})")
    elif ws:
        lines.append(f"⚖️ Вес: {ws[0]['weight']:.1f} (одно взвешивание)")
    return "\n".join(lines)


async def pace_text(profile) -> str:
    """Честно: при упоре в пол бюджета темп задаёт пол, а не выбранная цифра."""
    b = nutrition.make_budget(profile["tdee"], profile["pace"], profile["floor_kcal"])
    lines = [
        f"🎯 Цель: {profile['target_weight']:g} кг",
        f"Расход: <b>{fmt(profile['tdee'])} ккал/день</b>",
        f"Бюджет еды: <b>{fmt(profile['budget'])} ккал/день</b>, белок {profile['protein_goal']} г",
    ]
    if b.floored:
        extra = nutrition.extra_activity_needed(profile["tdee"], profile["budget"], profile["pace"])
        lines.append(
            f"Темп при этом бюджете: <b>≈{b.achievable_pace:g} кг/нед</b>. Бюджет упирается в пол "
            f"{fmt(profile['floor_kcal'])} ккал, поэтому выбор темпа {profile['pace']:g} кг/нед сейчас ни на что не влияет. "
            f"Быстрее — только за счёт активности: +{fmt(extra)} ккал в день до {profile['pace']:g} кг/нед "
            f"(≈{round(extra / 4.5)} минут быстрой ходьбы).")
    else:
        lines.append(f"Темп: <b>{profile['pace']:g} кг/нед</b> (дефицит {fmt(b.wanted_deficit)} ккал/день)")
    wo = await db.workouts_by_day(db.days_ago(14), db.days_ago(1))
    if wo:
        n = max(1, min(14, (db.now().date() - date.fromisoformat(profile["created"][:10])).days))
        avg = sum(wo.values()) / n
        real = max(0.0, profile["tdee"] + avg - profile["budget"]) * 7 / nutrition.KCAL_PER_KG
        lines.append(f"🏋️ С тренировками (в среднем +{fmt(avg)} ккал/день) темп ≈ <b>{real:.1f} кг/нед</b>")
    return "\n".join(lines)
