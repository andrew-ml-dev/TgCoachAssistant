"""Отчёт за период: текст (до 3 дней) или карточка-график с подписью (неделя, всё наблюдение)."""
from __future__ import annotations

from datetime import date, timedelta

from .. import db
from . import budget, charts, nutrition, weekly
from .budget import MONTHS, fmt, period_text

CHART_PERIODS = (7, 0)


def _range_title(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.day} — {end.day} {MONTHS[end.month - 1]}"
    return f"{start.day} {MONTHS[start.month - 1]} — {end.day} {MONTHS[end.month - 1]}"


def _week_line(profile, r: dict, target: float) -> str | None:
    """Итог недели с понедельника: сколько набрано против цели и что нужно в оставшиеся дни."""
    if not target or not r["days"]:
        return None
    gap = r["cum"] - r["tgt"]
    head = (f"🎯 <b>Дефицит {'недели' if not r['left'] else 'с понедельника'}</b>: {fmt(r['cum'])} из {fmt(r['tgt'])} "
            f"({r['days']} дн.)").replace("-", "−")
    if not r["left"]:
        return head + (f" · перевыполнено на {fmt(gap)}" if gap >= 0 else f" · недобор {fmt(-gap)}") + \
            f" ≈ {gap / nutrition.KCAL_PER_KG:+.2f} кг".replace("-", "−")
    need = target - gap / r["left"]
    days_txt = "сегодня" if r["left"] == 1 else f"в оставшиеся {r['left']} дн. с сегодняшним"
    tail = (f"запас {fmt(gap)} → хватит ≈ {fmt(max(0, need))} в день {days_txt}" if gap >= 0 else
            f"отстаю на {fmt(-gap)} → чтобы догнать к вс, нужно ≈ {fmt(need)} в день {days_txt}")
    if need > profile["tdee"] - profile["budget"] + 50:
        tail += f" (едой в рамках бюджета максимум ≈ {fmt(profile['tdee'] - profile['budget'])}, остальное — тренировки)"
    return f"{head}\n{tail}"


async def build(profile, days: int) -> tuple[str, bytes | None]:
    """days: 3, 7 или 0 (всё наблюдение). Возвращает (текст или подпись, png или None)."""
    if days not in CHART_PERIODS:
        return await period_text(profile, days), None

    today = db.now().date()
    end = today
    if days:
        # неделя — календарная, с понедельника; в понедельник показываем прошлую целиком
        start = today - timedelta(days=today.weekday())
        if start == today:
            start, end = today - timedelta(days=7), today - timedelta(days=1)
    else:
        first = await db.first_date()
        start = date.fromisoformat(first) if first else today
    since, until = start.isoformat(), end.isoformat()
    span = [start + timedelta(i) for i in range((end - start).days + 1)]
    sunday = end + timedelta(days=6 - end.weekday())
    axis = [start + timedelta(i) for i in range((sunday - start).days + 1)]

    logged = await db.daily_kcal(since, until)
    reliable = {d["date"] for d in await db.counted_days(since, until, profile["budget"])}
    full = [d for d in logged if d["date"] in reliable and d["date"] != today.isoformat()]
    wrows = [r for r in await db.weights(since) if r["date"] <= until]
    wo = await db.workouts_by_day(since, until)
    pl = await budget.plans(profile, since, until)
    if not logged and not wrows:
        return "За этот период пока нет ни записей еды, ни взвешиваний.", None

    # расчётный вес: от первого тренда периода (или стартового веса) минус накопленный дефицит
    # профиль могли пересоздать, и start_weight тогда — вес на момент пересоздания, поэтому берём первое взвешивание
    base = wrows[0]["trend"] if wrows else profile["start_weight"]
    base_day = date.fromisoformat(wrows[0]["date"]) if wrows else start
    kcal_by = {d["date"]: d["kcal"] for d in logged}
    expected, cum = [], 0.0
    for d in span:
        if d >= base_day:
            expected.append((d, round(base - cum / nutrition.KCAL_PER_KG, 2)))
            ds = d.isoformat()
            if ds in reliable and ds != today.isoformat():
                cum += pl[ds][0] + wo.get(ds, 0) - kcal_by[ds]

    # плитки
    kpis = []
    if len(wrows) >= 2:
        dw = wrows[-1]["trend"] - wrows[0]["trend"]
        kpis.append(("Вес по тренду", f"{dw:+.1f} кг".replace("-", "−"),
                     f"{wrows[0]['trend']:.1f} → {wrows[-1]['trend']:.1f}"))
    elif wrows:
        kpis.append(("Вес", f"{wrows[-1]['weight']:.1f} кг", "нужно 2+ взвешивания"))
    deficit = sum(pl[d["date"]][0] + wo.get(d["date"], 0) - d["kcal"] for d in full)
    if full:
        kpis.append(("Дефицит" if deficit >= 0 else "Профицит", fmt(abs(deficit)),
                     f"ккал ≈ {-deficit / nutrition.KCAL_PER_KG:+.1f} кг".replace("-", "−")))
    else:
        kpis.append(("Дефицит", "—", "нет полных дней"))
    if full:
        avg_p = sum(d["protein"] or 0 for d in full) / len(full)
        low = nutrition.protein_min(profile["target_weight"])
        hit = sum(1 for d in full if (d["protein"] or 0) >= low)
        kpis.append(("Белок в среднем", f"{fmt(avg_p)} г", f"мин {low} · ≥ мин {hit} из {len(full)} дн."))
    else:
        kpis.append(("Белок", "—", "нет полных дней"))

    title = ("Прошлая неделя" if end < today else "Эта неделя") if days else "Всё наблюдение"
    data = charts.ReportData(
        title=title,
        subtitle=f"{_range_title(start, end)} · полных дней {len(full)} из {len([x for x in span if x < today])}",
        days=axis,
        weights={date.fromisoformat(r["date"]): (r["weight"], r["trend"]) for r in wrows},
        expected=expected if len(wrows) else [],
        kcal={date.fromisoformat(d["date"]): (d["kcal"], d["date"] in reliable) for d in logged},
        budget=profile["budget"], tdee=profile["tdee"], target=profile["target_weight"], today=today, kpis=kpis,
        workouts={date.fromisoformat(k): v for k, v in wo.items()},
        target_deficit=nutrition.make_budget(profile["tdee"], profile["pace"], 0).wanted_deficit,
        tdees={date.fromisoformat(k): v[0] for k, v in pl.items()})
    png = charts.report_chart(data)

    # подпись: то, чего нет на картинке; блоки как в дневном отчёте, лимит Telegram — 1024 символа
    low = nutrition.protein_min(profile["target_weight"])
    lines = [f"📊 <b>{title}</b> · {_range_title(start, end)}",
             f"Записано {len(logged)} из {len(span)} дн., полных (без сегодня): {len(full)}"]
    week = _week_line(profile, charts.week_runs(data)[-1], data.target_deficit)
    if week:
        lines += ["", week]
    if full:
        k = len(full)
        tot = await db.period_totals([d["date"] for d in full])
        ok = sum(1 for d in full if d["kcal"] <= budget.day_limit(profile, pl[d["date"]], wo.get(d["date"], 0)) * 1.05)
        p_ok = sum(1 for d in full if (d["protein"] or 0) >= low)
        lines += ["",
                  f"🍽 <b>Средний полный день</b>",
                  f"{fmt(tot['kcal'] / k)} ккал из {fmt(profile['budget'])} · в бюджете {ok} из {k} дн.",
                  f"🥩 Белок {fmt(tot['protein'] / k)} г (мин {low}, цель {profile['protein_goal']}) · "
                  f"≥ мин {p_ok} из {k} дн.",
                  f"Ж {fmt(tot['fat'] / k)} · У {fmt(tot['carbs'] / k)} г"]
    if wo:
        bon = sum(budget.bonus_for(profile, v, pl.get(k)) for k, v in wo.items())
        lines.append(f"🏋️ Тренировки: {len(wo)} дн., {fmt(sum(wo.values()))} ккал · к еде +{fmt(bon)}, "
                     f"в дефицит {fmt(sum(wo.values()) - bon)}")
    if not days:
        lines += ["", "🎯 <b>Прогресс</b>"]
        last = await db.last_weight()
        if last:
            first_w = wrows[0]["weight"] if wrows else profile["start_weight"]
            lines.append(f"Старт {first_w:g} → сейчас <b>{last['trend']:.1f}</b> → цель {profile['target_weight']:g} кг")
        rate = nutrition.weekly_rate(await weekly.weight_points())
        if rate is not None:
            fd = nutrition.forecast_date(last["trend"], profile["target_weight"], rate, today)
            lines.append(f"Темп за 3 недели {rate:+.2f} кг/нед".replace("-", "−") +
                         (f", цель ≈ {fd.strftime('%d.%m.%Y')}" if fd else ", цель при таком темпе не приближается"))
        else:
            lines.append("Темп и прогноз появятся после 5 дней взвешиваний.")
        calc, _ = await weekly.real_tdee(profile)
        lines.append(f"Расход в расчётах {fmt(profile['tdee'])}" +
                     (f", по вашим данным ≈ {fmt(calc)}" if calc else " (по данным — после 10 полных дней)"))
    lines += ["", "<i>Оранжевый пунктир — каким был бы вес по записанной еде; тренд выше него — похоже на недоучёт. "
              "Внизу — дефицит, накопленный с понедельника, против цели темпа; неполные дни не считаются.</i>"]
    caption = "\n".join(lines)
    return caption[:1024], png
