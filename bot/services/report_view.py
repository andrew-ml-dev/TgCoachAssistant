"""Отчёт за период: текст (до 3 дней) или карточка-график с подписью (неделя, всё наблюдение)."""
from __future__ import annotations

from datetime import date, timedelta

from .. import db
from . import charts, nutrition, weekly
from .budget import MONTHS, cumulative_line, fmt, period_text

CHART_PERIODS = (7, 0)


def _range_title(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.day} — {end.day} {MONTHS[end.month - 1]}"
    return f"{start.day} {MONTHS[start.month - 1]} — {end.day} {MONTHS[end.month - 1]}"


async def build(profile, days: int) -> tuple[str, bytes | None]:
    """days: 3, 7 или 0 (всё наблюдение). Возвращает (текст или подпись, png или None)."""
    if days not in CHART_PERIODS:
        return await period_text(profile, days), None

    today = db.now().date()
    if days:
        start = today - timedelta(days=days - 1)
    else:
        first = await db.first_date()
        start = date.fromisoformat(first) if first else today
    since, until = start.isoformat(), today.isoformat()
    span = [start + timedelta(i) for i in range((today - start).days + 1)]

    logged = await db.daily_kcal(since, until)
    reliable = {d["date"] for d in await db.counted_days(since, until, profile["budget"])}
    full = [d for d in logged if d["date"] in reliable and d["date"] != until]
    wrows = await db.weights(since)
    if not logged and not wrows:
        return "За этот период пока нет ни записей еды, ни взвешиваний.", None

    # расчётный вес: от первого тренда периода (или стартового веса) минус накопленный дефицит
    base = profile["start_weight"] if not days or not wrows else wrows[0]["trend"]
    base_day = start if not days or not wrows else date.fromisoformat(wrows[0]["date"])
    kcal_by = {d["date"]: d["kcal"] for d in logged}
    expected, cum = [], 0.0
    for d in span:
        if d >= base_day:
            expected.append((d, round(base - cum / nutrition.KCAL_PER_KG, 2)))
            ds = d.isoformat()
            if ds in reliable and ds != until:
                cum += profile["tdee"] - kcal_by[ds]

    # плитки
    kpis = []
    if len(wrows) >= 2:
        dw = wrows[-1]["trend"] - wrows[0]["trend"]
        kpis.append(("Вес по тренду", f"{dw:+.1f} кг".replace("-", "−"),
                     f"{wrows[0]['trend']:.1f} → {wrows[-1]['trend']:.1f}"))
    elif wrows:
        kpis.append(("Вес", f"{wrows[-1]['weight']:.1f} кг", "нужно 2+ взвешивания"))
    deficit = sum(profile["tdee"] - d["kcal"] for d in full)
    if full:
        kpis.append(("Дефицит" if deficit >= 0 else "Профицит", fmt(abs(deficit)),
                     f"ккал ≈ {-deficit / nutrition.KCAL_PER_KG:+.1f} кг".replace("-", "−")))
    else:
        kpis.append(("Дефицит", "—", "нет полных дней"))
    if full:
        avg_p = sum(d["protein"] or 0 for d in full) / len(full)
        hit = sum(1 for d in full if (d["protein"] or 0) >= profile["protein_goal"] * 0.9)
        kpis.append(("Белок в среднем", f"{fmt(avg_p)} г", f"цель {profile['protein_goal']} · {hit} из {len(full)} дн."))
    else:
        kpis.append(("Белок", "—", "нет полных дней"))

    title = "Неделя" if days else "Всё наблюдение"
    data = charts.ReportData(
        title=title,
        subtitle=f"{_range_title(start, today)} · полных дней {len(full)} из {max(0, len(span) - 1)}",
        days=span,
        weights={date.fromisoformat(r["date"]): (r["weight"], r["trend"]) for r in wrows},
        expected=expected if len(wrows) else [],
        kcal={date.fromisoformat(d["date"]): (d["kcal"], d["date"] in reliable) for d in logged},
        budget=profile["budget"], tdee=profile["tdee"], target=profile["target_weight"], today=today, kpis=kpis)
    png = charts.report_chart(data)

    # подпись: то, чего нет на картинке
    lines = [f"<b>{title}</b> · {_range_title(start, today)}",
             f"Записано дней: {len(logged)} из {len(span)}, полных (без сегодня): {len(full)}"]
    if full:
        k = len(full)
        tot = await db.period_totals([d["date"] for d in full])
        ok = sum(1 for d in full if d["kcal"] <= profile["budget"] * 1.05)
        lines.append(f"🔥 В среднем {fmt(tot['kcal'] / k)} ккал при бюджете {fmt(profile['budget'])} · "
                     f"в бюджете {ok} из {k} дн.")
        lines.append(f"Б {fmt(tot['protein'] / k)} · Ж {fmt(tot['fat'] / k)} · У {fmt(tot['carbs'] / k)} г в день")
    if not days:
        last = await db.last_weight()
        if last:
            lines.append(f"🎯 Старт {profile['start_weight']:g} → сейчас {last['trend']:.1f} → цель {profile['target_weight']:g} кг")
        rate = nutrition.weekly_rate(await weekly.weight_points())
        if rate is not None:
            fd = nutrition.forecast_date(last["trend"], profile["target_weight"], rate, today)
            lines.append(f"📉 Темп за 3 недели {rate:+.2f} кг/нед" +
                         (f", цель ≈ {fd.strftime('%d.%m.%Y')}" if fd else ", цель при таком темпе не приближается"))
        else:
            lines.append("Темп и прогноз появятся после 5 дней взвешиваний.")
        calc, _ = await weekly.real_tdee(profile)
        lines.append(f"Расход в расчётах {fmt(profile['tdee'])}" +
                     (f", по вашим данным ≈ {fmt(calc)}" if calc else " (по вашим данным — после 10 полных дней)"))
    lines.append("<i>Пунктир на графике — каким был бы вес по записанной еде. "
                 "Тренд выше пунктира — похоже на недоучёт в записях.</i>")
    caption = "\n".join(lines)
    return caption[:1024], png
