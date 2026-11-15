"""Недельный отчёт: статистика, адаптивный расход, предложение нового бюджета."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .. import db
from . import nutrition
from .budget import fmt


@dataclass
class Weekly:
    text: str
    proposal: tuple[int, int] | None  # (tdee, budget)


async def weight_points(days: int = 60) -> list[tuple[date, float]]:
    rows = await db.weights(db.days_ago(days))
    return [(date.fromisoformat(r["date"]), r["weight"]) for r in rows]


async def real_tdee(profile) -> tuple[int | None, str]:
    """Расход по данным за 14 дней. Возвращает (значение или None, пояснение)."""
    since, until = db.days_ago(14), db.days_ago(1)
    days = await db.counted_days(since, until, profile["budget"])
    pts = [p for p in await weight_points(14) if p[0].isoformat() <= until]
    span = (pts[-1][0] - pts[0][0]).days if pts else 0
    if len(days) < 10 or len(pts) < 7 or span < 10:
        return None, (f"Для расчёта реального расхода нужно за последние 14 дней: 10 полностью записанных дней "
                      f"(есть {len(days)}) и 7 взвешиваний с разбросом от 10 дней (есть {len(pts)}, {span} дн.).")
    avg = sum(d["kcal"] for d in days) / len(days)
    wo = await db.workouts_by_day(since, until)
    avg_wo = sum(wo.get(d["date"], 0) for d in days) / len(days)
    slope = nutrition.slope_per_day(pts)
    # вес = съедено − (расход без тренировок + тренировки), поэтому тренировки вычитаются
    calc = nutrition.adaptive_tdee(avg - avg_wo, slope)
    wo_txt = f", тренировки в среднем {fmt(avg_wo)} ккал/день" if avg_wo else ""
    return calc, (f"В среднем съедено {fmt(avg)} ккал/день{wo_txt}, вес менялся на {slope * 7:+.2f} кг/нед. "
                  f"Отсюда реальный расход без тренировок ≈ {fmt(calc)} ккал/день "
                  f"(в расчётах сейчас {fmt(profile['tdee'])}).")


async def build(profile) -> Weekly:
    since, until = db.days_ago(7), db.days_ago(1)
    days = await db.counted_days(since, until, profile["budget"])
    logged = len(await db.daily_kcal(since, until))
    lines = ["<b>Итоги недели</b>"]

    if days:
        avg = sum(d["kcal"] for d in days) / len(days)
        avg_p = sum(d["protein"] or 0 for d in days) / len(days)
        ok = sum(1 for d in days if d["kcal"] <= profile["budget"] * 1.05)
        lines += [
            f"🔥 Средние калории: {fmt(avg)} при бюджете {fmt(profile['budget'])} "
            f"(полных дней: {len(days)}/7, с записями: {logged})",
            f"✅ В бюджете: {ok} из {len(days)} дн.",
            f"🥩 Средний белок: {fmt(avg_p)} / {profile['protein_goal']} г",
        ]
    else:
        lines.append("Полностью записанных дней за неделю нет." if logged else "Записей еды за неделю нет.")

    wo = await db.workouts_by_day(since, until)
    if wo:
        lines.append(f"🏋️ Тренировки: {len(wo)} дн., всего {fmt(sum(wo.values()))} ккал "
                     f"(в среднем +{fmt(sum(wo.values()) / 7)} ккал/день к расходу)")

    pts = await weight_points()
    last = await db.last_weight()
    if last and len(pts) >= 2:
        week_ago = await db.weight_before(db.days_ago(7))
        if week_ago and week_ago["date"] != last["date"]:
            lines.append(f"⚖️ Тренд веса: {week_ago['trend']:.1f} → {last['trend']:.1f} кг "
                         f"({last['trend'] - week_ago['trend']:+.1f})")
        rate = nutrition.weekly_rate(pts)
        if rate is not None:
            lines.append(f"📉 Фактический темп: {rate:+.2f} кг/нед (цель −{profile['pace']:g})")
            fd = nutrition.forecast_date(last["trend"], profile["target_weight"], rate, db.now().date())
            if fd:
                lines.append(f"🎯 Цель {profile['target_weight']:g} кг при таком темпе: {fd.strftime('%d.%m.%Y')}")
            if -rate > last["trend"] * 0.015 and len(pts) >= 14:
                lines.append("⚠️ Темп выше 1.5% веса в неделю. Так теряются мышцы; стоит поднять калории на 200–300.")
        two_weeks = await db.weight_before(db.days_ago(14))
        first = pts[0][0]
        if two_weeks and (db.now().date() - first).days >= 14 and abs(last["trend"] - two_weeks["trend"]) < 0.2:
            lines.append("⏸ Плато: тренд не меняется две недели. Проверьте точность записей (масло, перекусы, "
                         "напитки) и взвешивайте порции; если всё точно — бюджет будет пересчитан ниже.")

    proposal = None
    calc, why = await real_tdee(profile)
    lines += ["", why]
    if calc is not None:
        new_tdee = nutrition.smooth_tdee(profile["tdee"], calc)
        b = nutrition.make_budget(new_tdee, profile["pace"], profile["floor_kcal"])
        if abs(b.budget - profile["budget"]) >= 50 or abs(new_tdee - profile["tdee"]) >= 100:
            proposal = (new_tdee, b.budget)
            lines.append(f"Предлагаю обновить: расход {fmt(new_tdee)}, бюджет <b>{fmt(b.budget)} ккал/день</b> "
                         f"(достижимый темп {b.achievable_pace:g} кг/нед).")
    return Weekly("\n".join(lines), proposal)
