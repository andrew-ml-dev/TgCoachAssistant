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


def bonus_for(profile, workout_net: float, plan: tuple[int, int] | None = None) -> int:
    tdee, bud = plan or (profile["tdee"], profile["budget"])
    return nutrition.workout_bonus(tdee, bud, profile["pace"], workout_net)


async def plans(profile, since: str, until: str) -> dict[str, tuple[int, int]]:
    """Расход и бюджет, действовавшие в каждый день периода (из истории пересчётов).

    Без этого прошлые дни считались бы по сегодняшнему расходу, и после пересчёта бюджета
    дефицит за прошлые недели менялся бы задним числом.
    """
    hist = await db.budget_history()
    cur = (profile["tdee"], profile["budget"])
    if hist:
        cur = (hist[0][1], hist[0][2])  # дни до первой записи — по первому плану
    out, i = {}, 0
    d, end = date.fromisoformat(since), date.fromisoformat(until)
    while d <= end:
        ds = d.isoformat()
        while i < len(hist) and hist[i][0] <= ds:
            cur = (hist[i][1], hist[i][2])
            i += 1
        out[ds] = cur
        d += timedelta(days=1)
    if until >= db.today():
        out[db.today()] = (profile["tdee"], profile["budget"])  # сегодня — всегда текущий план
    return out


def day_limit(profile, plan: tuple[int, int], workout_net: float) -> int:
    """Бюджет еды дня с прибавкой за тренировки по плану того дня."""
    return plan[1] + bonus_for(profile, workout_net, plan)


async def day_allowance(profile, day: str | None = None) -> tuple[int, int, float]:
    """(бюджет еды на день с тренировками, прибавка за тренировки, ккал тренировок в расходе)."""
    wo = await db.workout_kcal(day or db.today())
    bonus = bonus_for(profile, wo)
    return profile["budget"] + bonus, bonus, wo


async def remaining(profile) -> tuple[float, float, float]:
    """(съедено, осталось, процент бюджета дня — с учётом тренировок)."""
    tot = await db.day_totals(db.today())
    eaten = tot["kcal"]
    allow, _, _ = await day_allowance(profile)
    return eaten, allow - eaten, eaten / allow * 100


def allowance_text(profile, bonus: int) -> str:
    """«1 500» или «1 500 + 688 за тренировку = 2 188»."""
    if not bonus:
        return fmt(profile["budget"])
    return f"{fmt(profile['budget'])} + {fmt(bonus)} за тренировку = {fmt(profile['budget'] + bonus)}"


async def left_line(profile) -> str:
    eaten, left, pct = await remaining(profile)
    tot = await db.day_totals(db.today())
    allow, bonus, _ = await day_allowance(profile)
    head = (f"Осталось на сегодня: <b>{fmt(left)} ккал</b>" if left >= 0
            else f"Перебор: <b>+{fmt(-left)} ккал</b>")
    if bonus:
        head += f" (в бюджете +{fmt(bonus)} за тренировку)"
    return (f"{head}\n{nutrition.bar(pct)} {fmt(eaten)} / {fmt(allow)} · "
            f"белок {fmt(tot['protein'])} г (мин {nutrition.protein_min(profile['target_weight'])}, "
            f"цель {profile['protein_goal']})")


async def week_balance(profile) -> tuple[float, int] | None:
    """Сумма отклонений от бюджета дня (с тренировками) с понедельника по вчера, только полные дни."""
    today = db.now().date()
    monday = today - timedelta(days=today.weekday())
    if monday == today:
        return None
    since, until = monday.isoformat(), db.days_ago(1)
    days = await db.counted_days(since, until, profile["budget"])
    if not days:
        return None
    wo = await db.workouts_by_day(since, until)
    pl = await plans(profile, since, until)
    return sum(d["kcal"] - day_limit(profile, pl[d["date"]], wo.get(d["date"], 0)) for d in days), len(days)


def _signed(n: float) -> str:
    return ("+" if n > 0 else "−") + fmt(abs(n))


async def over_warning(profile) -> str | None:
    """Один раз в день, когда бюджет превышен впервые."""
    _, left, pct = await remaining(profile)
    st = await db.day_state()
    if pct <= 100 or st["warned"] >= 100:
        return None
    await db.set_day_state(warned=100)
    allow, bonus, wo = await day_allowance(profile)
    eaten = allow - left
    bal = profile["tdee"] + wo - eaten
    text = (f"🔴 Бюджет дня ({fmt(allow)}{' с тренировкой' if bonus else ''}) превышен на {fmt(-left)} ккал.")
    if bal > 0:
        text += (f" Дефицит дня всё равно есть — {fmt(bal)} ккал, вес не прибавится, "
                 f"просто темп недели чуть ниже плана.")
    else:
        text += f" Это уже профицит {fmt(-bal)} ккал (≈+{round(-bal / nutrition.KCAL_PER_KG * 1000)} г)."
    wb = await week_balance(profile)
    if wb:
        text += f" С понедельника до сегодня: {_signed(wb[0])} ккал к бюджету."
    return text


async def protein_tips(gap: float, kcal_left: float) -> str | None:
    """Чем добрать белок: 1–3 позиции из того, что человек сам ест, обычными для него порциями.

    Кандидаты — «Мои продукты» и еда из записей за 45 дней, где белок даёт не меньше 30% калорий.
    Порция — та, что человек обычно берёт. Выбор жадный: больше белка на калорию, пока не закрыт разрыв.
    Считается локально, без Claude.
    """
    from statistics import median
    cand: dict[str, dict] = {}
    for p in await db.products():
        if p["kcal"] > 0 and p["protein"] * 4 / p["kcal"] >= 0.3:
            grams = (await db.recent_grams(p["name"], 1) or [p.get("pack") or 100])[0]
            cand[p["name"]] = {"grams": grams, "kcal": p["kcal"] * grams / 100, "protein": p["protein"] * grams / 100}
    hist: dict[str, list[dict]] = {}
    for i in await db.eaten_items():
        hist.setdefault(i["name"], []).append(i)
    for name, items in hist.items():
        if name in cand or "коктейл" in name:
            continue
        pg = sum(i["protein"] for i in items) / sum(i["grams"] for i in items)
        kg = sum(i["kcal"] for i in items) / sum(i["grams"] for i in items)
        if kg <= 0 or pg * 4 / kg < 0.3 or pg * 100 < 8:
            continue
        grams = round(median(i["grams"] for i in items))
        cand[name] = {"grams": grams, "kcal": kg * grams, "protein": pg * grams}
    opts, powder = [], False
    for name, c in sorted(cand.items(), key=lambda kv: -kv[1]["protein"] / kv[1]["kcal"]):
        per_g = c["protein"] / c["grams"]
        if per_g >= 0.5:  # протеиновый порошок: один на выбор достаточно, остальное — обычная еда
            if powder:
                continue
            powder = True
        # порция под разрыв, но не больше полуторной обычной: иначе совет нереалистичный
        grams = min(c["grams"] * 1.5, gap / per_g)
        grams = max(5, round(grams / 5) * 5)
        prot = per_g * grams
        if prot < 5:
            continue
        opts.append((name, grams, prot, c["kcal"] / c["grams"] * grams))
        if len(opts) == 3:
            break
    if not opts:
        return None
    parts = []
    for name, grams, prot, kcal in opts:
        short = f", +{fmt(prot)} г" if prot < gap - 2 else ""
        parts.append(f"{escape(name)} {fmt(grams)} г ({fmt(kcal)} ккал{short})")
    head = f"💡 добрать {fmt(gap)} г белка — на выбор: "
    note = " <i>Остатка калорий мало — берите самый лёгкий.</i>" if min(o[3] for o in opts) > kcal_left else ""
    return head + " · ".join(parts) + note


def _row(label: str, value: str, width: int = 28) -> str:
    """Строка моноширинной таблицы: подпись слева, число справа."""
    return f"{label}{value.rjust(max(1, width - len(label)))}"


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


async def today_text(profile) -> str:
    """Отчёт за сегодня: блоки сверху вниз по важности — калории, БЖУ, баланс, активность, вес, приёмы пищи."""
    day = db.today()
    tot = await db.day_totals(day)
    eaten, left, pct = await remaining(profile)
    goal_p = profile["protein_goal"]
    out = [f"📊 <b>{human_date(date.fromisoformat(day))}</b>", ""]

    # 1. калории — главное
    allow, bonus, _ = await day_allowance(profile)
    head = (f"осталось <b>{fmt(left)}</b> ккал" if left >= 0 else f"🔴 перебор <b>+{fmt(-left)}</b> ккал")
    out += [f"🍽 <b>Калории</b> · {head}",
            f"<code>{nutrition.bar(pct, 14)} {round(pct)}%</code>",
            f"съедено {fmt(eaten)} из {fmt(allow)}"]
    if bonus:
        out.append(f"бюджет {allowance_text(profile, bonus)}")
    out.append("")

    # 2. БЖУ: белок к цели, жиры и углеводы — доля калорий
    p, f, c = tot["protein"] or 0, tot["fat"] or 0, tot["carbs"] or 0
    macro_kcal = p * 4 + f * 9 + c * 4 or 1
    share = lambda g, k: f"{round(g * k / macro_kcal * 100)}%"
    low = nutrition.protein_min(profile["target_weight"])
    out += ["🥩 <b>БЖУ</b>",
            "<pre>"
            + f"Белок {nutrition.protein_bar(p, low, goal_p, 12)} {fmt(p):>4} г\n"
            + _row("", f"мин {low} · цель {goal_p} г") + "\n"
            + _row("Жиры", f"{fmt(f)} г · {share(f, 9):>3}") + "\n"
            + _row("Углеводы", f"{fmt(c)} г · {share(c, 4):>3}")
            + "</pre>",
            nutrition.protein_state(p, low, goal_p) if eaten else f"белок: минимум {low} г, цель {goal_p} г"]
    if eaten and p < low:
        tip = await protein_tips(low - p, left)
        if tip:
            out.append(tip)
    out.append("")

    # 3. баланс дня: расход − еда
    wo = await db.workout_kcal(day)
    bal = profile["tdee"] + wo - eaten
    rows = [_row("Расход за день", fmt(profile["tdee"]))]
    if wo:
        rows.append(_row("Тренировки", "+" + fmt(wo)))
    grams = round(abs(bal) / nutrition.KCAL_PER_KG * 1000)
    rows += [_row("Съедено", "−" + fmt(eaten)), "─" * 28,
             _row("Дефицит" if bal >= 0 else "Профицит", fmt(abs(bal))),
             _row("", f"≈ {'−' if bal >= 0 else '+'}{grams} г веса")]
    out += ["📉 <b>Баланс дня</b>", "<pre>" + "\n".join(rows) + "</pre>"]
    wb = await week_balance(profile)
    if wb:
        side = "ниже" if wb[0] <= 0 else "выше"
        out.append(f"с понедельника {side} бюджета на {fmt(abs(wb[0]))} ккал ({wb[1]} дн.)")
    out.append("")

    # 4. активность
    workouts = await db.workouts_on(day) if wo else []
    if workouts:
        out.append("🏋️ <b>Активность</b>")
        for w in workouts:
            mins = f" · {w['minutes']} мин" if w["minutes"] and str(w["minutes"]) not in w["descr"] else ""
            hr = f" · ♥ {w['hr_avg']}" if w["hr_avg"] else ""
            out.append(f"• {escape(w['descr'])}{mins}{hr} — <b>{fmt(w['kcal'])}</b> ккал")
        if workouts:
            out.append(f"<i>к еде +{fmt(bonus)}, остальные {fmt(wo - bonus)} — в дефицит</i>")
        out.append("")

    # 5. вес
    w = await db.weight_on(day)
    if w:
        week = await db.weight_before(db.days_ago(7))
        delta = f" · за 7 дн {w['trend'] - week['trend']:+.1f}".replace("-", "−") if week and week["date"] != day else ""
        out += [f"⚖️ <b>Вес</b> {w['weight']:.1f} кг · тренд {w['trend']:.1f}{delta}", ""]
    else:
        out += ["⚖️ <b>Вес</b> сегодня не внесён — отправьте число, например 92.4", ""]

    # 6. приёмы пищи
    meals = await db.meals_on(day)
    if meals:
        rows = [f"{(m['ts'] or '')[11:16]:5} {_cut(m['title'], 16):16} {fmt(m['kcal']):>5}" for m in meals]
        out += [f"🍴 <b>Приёмы пищи</b> · {len(meals)}", "<pre>" + escape("\n".join(rows)) + "</pre>"]
    else:
        out.append("🍴 Сегодня записей еды ещё нет.")
    return "\n".join(out).rstrip()


WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


async def period_text(profile, days: int) -> str:
    """Отчёт за последние N дней включая сегодня (таблица по дням + средние); days=0 — за всё наблюдение."""
    today = db.now().date()
    if days:
        start = today - timedelta(days=days - 1)
    else:
        first = await db.first_date()
        start = date.fromisoformat(first) if first else today
    since, until = start.isoformat(), today.isoformat()
    n_days = (today - start).days + 1
    title = f"{days} дн." if days else f"всё наблюдение, {n_days} дн."
    low, goal_p = nutrition.protein_min(profile["target_weight"]), profile["protein_goal"]
    out = [f"📊 <b>Отчёт: {title}</b> · {start.day} {MONTHS[start.month - 1]} — {today.day} {MONTHS[today.month - 1]}",
           ""]

    logged = await db.daily_kcal(since, until)
    reliable = {d["date"] for d in await db.counted_days(since, until, profile["budget"])}
    wo = await db.workouts_by_day(since, until)
    pl = await plans(profile, since, until)
    if not logged:
        out.append("За этот период записей еды нет.")
    if logged and n_days <= 7:
        rows = [f"{'День':8} {'ккал':>6} {'Б':>4} {'дефиц':>6}"]
        for d in logged:
            dd = date.fromisoformat(d["date"])
            is_today = d["date"] == until
            mark = "⏳" if is_today else ("✅" if d["date"] in reliable else "⚠️")
            pr = d["protein"] or 0
            rows.append(f"{WD[dd.weekday()]} {dd.strftime('%d.%m')} {fmt(d['kcal']):>6} "
                        f"{fmt(pr):>3}{'↓' if pr < low and not is_today else ' '} "
                        f"{fmt(pl[d['date']][0] + wo.get(d['date'], 0) - d['kcal']):>6} {mark}")
        out += ["📅 <b>По дням</b>", "<pre>" + "\n".join(rows) + "</pre>",
                "<i>✅ полный · ⚠️ неполный · ⏳ сегодня · ↓ белок ниже минимума</i>"
                + (" · 🏋️ тренировки уже в дефиците" if wo else ""), ""]

    full = [d for d in logged if d["date"] in reliable and d["date"] != until]
    if logged:
        out.append(f"Записано дней: {len(logged)} из {n_days}, полных (без сегодня): {len(full)}")
    if full:
        tot = await db.period_totals([d["date"] for d in full])
        k = len(full)
        ok = sum(1 for d in full if d["kcal"] <= day_limit(profile, pl[d["date"]], wo.get(d["date"], 0)) * 1.05)
        p_ok = sum(1 for d in full if (d["protein"] or 0) >= low)
        deficit = sum(pl[d["date"]][0] + wo.get(d["date"], 0) - d["kcal"] for d in full)
        avg_p = tot["protein"] / k
        out += ["",
                f"🍽 <b>В среднем за полный день</b> · {k} дн.",
                "<pre>"
                + _row("Калории", f"{fmt(tot['kcal'] / k)} / {fmt(profile['budget'])}") + "\n"
                + _row("В бюджете", f"{ok} из {k} дн.") + "\n"
                + _row("Белок", f"{fmt(avg_p)} г{'↓' if avg_p < low else ''}") + "\n"
                + _row("", f"мин {low} · цель {goal_p}") + "\n"
                + _row("Белок ≥ мин", f"{p_ok} из {k} дн.") + "\n"
                + _row("Жиры / Угл.", f"{fmt(tot['fat'] / k)} / {fmt(tot['carbs'] / k)} г")
                + "</pre>",
                f"📉 <b>Дефицит за полные дни:</b> {fmt(deficit)} ккал ≈ "
                + f"{-deficit / nutrition.KCAL_PER_KG:+.1f} кг".replace("-", "−")]
    if wo:
        bon = sum(bonus_for(profile, w, pl.get(ds)) for ds, w in wo.items())
        out.append(f"🏋️ <b>Тренировки:</b> {len(wo)} дн., {fmt(sum(wo.values()))} ккал · "
                   f"к еде +{fmt(bon)}, в дефицит {fmt(sum(wo.values()) - bon)}")
    ws = await db.weights(since)
    if len(ws) >= 2:
        out.append(f"⚖️ <b>Вес по тренду:</b> {ws[0]['trend']:.1f} → <b>{ws[-1]['trend']:.1f}</b> "
                   + f"({ws[-1]['trend'] - ws[0]['trend']:+.1f} кг, взвешиваний: {len(ws)})".replace("-", "−"))
    elif ws:
        out.append(f"⚖️ <b>Вес:</b> {ws[0]['weight']:.1f} (одно взвешивание)")
    return "\n".join(out)


async def pace_text(profile) -> str:
    """Честно: при упоре в пол бюджета темп задаёт пол, а не выбранная цифра."""
    b = nutrition.make_budget(profile["tdee"], profile["pace"], profile["floor_kcal"])
    lines = [
        f"🎯 Цель: {profile['target_weight']:g} кг",
        f"Расход: <b>{fmt(profile['tdee'])} ккал/день</b>",
        f"Бюджет еды: <b>{fmt(profile['budget'])} ккал/день</b>, белок {profile['protein_goal']} г",
    ]
    extra = nutrition.extra_activity_needed(profile["tdee"], profile["budget"], profile["pace"])
    if b.floored:
        lines.append(
            f"Без тренировок темп <b>≈{b.achievable_pace:g} кг/нед</b>: бюджет упирается в пол "
            f"{fmt(profile['floor_kcal'])} ккал, и до темпа {profile['pace']:g} кг/нед не хватает "
            f"{fmt(extra)} ккал дефицита в день.")
    else:
        lines.append(f"Темп: <b>{profile['pace']:g} кг/нед</b> (дефицит {fmt(b.wanted_deficit)} ккал/день)")
    lines += ["", "🏋️ <b>Тренировки и бюджет еды</b>"]
    if extra:
        lines.append(f"Тренировка сначала добирает эти {fmt(extra)} ккал до вашего темпа, всё сверх — "
                     "прибавляется к еде на этот день. Но к еде всегда идёт не меньше половины тренировки.")
        lines.append(f"Пример: тренировка 600 ккал → к еде +{fmt(bonus_for(profile, 600))}, "
                     f"бюджет {fmt(profile['budget'] + bonus_for(profile, 600))}.")
    else:
        lines.append("Половина калорий тренировки прибавляется к еде на этот день, вторая половина ускоряет темп "
                     "и служит запасом на ошибку часов.")
    wo = await db.workouts_by_day(db.days_ago(14), db.days_ago(1))
    if wo:
        n = max(1, min(14, (db.now().date() - date.fromisoformat(profile["created"][:10])).days))
        avg = sum(wo.values()) / n
        kept = sum(w - bonus_for(profile, w) for w in wo.values()) / n  # что осталось в дефиците
        real = max(0.0, profile["tdee"] - profile["budget"] + kept) * 7 / nutrition.KCAL_PER_KG
        lines.append(f"За 2 недели тренировки в среднем +{fmt(avg)} ккал/день → темп при бюджете с тренировками "
                     f"≈ <b>{real:.1f} кг/нед</b>")
    return "\n".join(lines)
