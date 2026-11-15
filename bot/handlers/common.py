"""Общие действия, которые вызываются из меню, свободного ввода и кнопок."""
import re
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaPhoto, Message

from .. import db, keyboards as kb
from ..services import budget, llm, nutrition, panel, products, report_view, weekly
from ..services.budget import fmt
from ..services.parser import normalize


def meal_text(meal, items: list[dict]) -> str:
    head = f"{escape(meal['title'])} — " if meal["source"] == "menu" else ""
    lines = [f"<b>{head}{fmt(meal['kcal'])} ккал</b> "
             f"(Б {fmt(meal['protein'])} · Ж {fmt(meal['fat'])} · У {fmt(meal['carbs'])})"]
    for i in items:
        grams = f" {fmt(i['grams'])} г" if i["grams"] else ""
        lines.append(f"• {escape(i['name'])}{grams} — {fmt(i['kcal'])} ккал")
    return "\n".join(lines)


async def day_note(profile, day: str) -> str:
    tot = await db.day_totals(day)
    return f"📅 Записано на вчера. Итог за вчера: {fmt(tot['kcal'])} / {fmt(profile['budget'])} ккал."


async def send_meal(message: Message, profile, meal_id: int, note: str = "") -> None:
    """Карточка приёма пищи + остаток; отдельное сообщение только при первом переборе за день."""
    meal = await db.get_meal(meal_id)
    text = meal_text(meal, await db.meal_items(meal_id))
    if note:
        text += f"\n<i>{note}</i>"
    is_today = meal["date"] == db.today()
    text += "\n\n" + (await budget.left_line(profile) if is_today else await day_note(profile, meal["date"]))
    await message.answer(text, reply_markup=kb.meal_card(meal_id, can_move=is_today))
    await panel.refresh(message.bot, profile)
    if is_today:
        warn = await budget.over_warning(profile)
        if warn:
            await message.answer(warn)


async def record_kcal(message: Message, profile, kcal: float, label: str = "", day: str | None = None) -> None:
    name = label or "калории вручную"
    mid = await db.add_meal(
        [{"name": name, "grams": 0, "kcal": kcal, "protein": 0, "fat": 0, "carbs": 0}],
        source="manual", raw_text=f"{kcal:g} ккал {label}".strip(), day=day)
    await send_meal(message, profile, mid)


async def record_food_text(message: Message, profile, text: str, allow_question: bool = True,
                           day: str | None = None) -> str | None:
    """Разбирает описание еды. Возвращает уточняющий вопрос, если он нужен.

    Порядок: продукт с этикетки → сохранённый разбор → Claude.
    """
    prods = await db.products()
    key = normalize(text)
    exact = products.match_exact(text, prods)
    if exact:
        items, source, note = [exact], "product", "по этикетке, без запроса к Claude"
    elif (cached := await db.cache_get(key)) is not None:
        items, source, note = products.apply(cached, prods), "cache", "из сохранённого, без запроса к Claude"
    else:
        await message.bot.send_chat_action(message.chat.id, "typing")
        parsed = await llm.parse_food(text, allow_question, products=prods)
        if parsed.question and not parsed.items:
            return parsed.question
        if not parsed.items:
            await message.answer("Не нашёл здесь еды. Опишите, что съели, или отправьте калории: «450 ккал».")
            return None
        items, source, note = products.apply([i.model_dump() for i in parsed.items], prods), "llm", ""
        await db.cache_put(key, items)  # повтор того же текста — без запроса; правка перезапишет
    mid = await db.add_meal(items, source=source, raw_text=text, day=day)
    await send_meal(message, profile, mid, note)
    return None


async def drop_weight_reminder(bot, chat_id: int, day: str | None = None) -> None:
    st = await db.day_state(day)
    if st["remind_msg"]:
        try:
            await bot.delete_message(chat_id, st["remind_msg"])
        except TelegramBadRequest:
            pass
        await db.set_day_state(day, remind_msg=None)


async def record_weight(message: Message, profile, value: float) -> None:
    day = db.today()
    prev = await db.weight_before(db.days_ago(1))
    await db.add_weight(value)
    await drop_weight_reminder(message.bot, message.chat.id)
    w = await db.weight_on(day)
    lines = [f"⚖️ Записал: <b>{value:.1f} кг</b>", f"Тренд: {w['trend']:.1f} кг"]
    if prev:
        lines[0] += f" ({value - prev['weight']:+.1f} к прошлому взвешиванию)"
    week = await db.weight_before(db.days_ago(7))
    if week and week["date"] != day:
        lines.append(f"За 7 дней по тренду: {w['trend'] - week['trend']:+.1f} кг")
    left = w["trend"] - profile["target_weight"]
    if left > 0:
        lines.append(f"До цели {profile['target_weight']:g} кг: {left:.1f} кг")
        pts = await weekly.weight_points()
        fd = nutrition.forecast_date(w["trend"], profile["target_weight"], nutrition.weekly_rate(pts), db.now().date())
        if fd:
            lines.append(f"Прогноз: {fd.strftime('%d.%m.%Y')}")
    else:
        lines.append("🎉 Цель достигнута по тренду!")
    if prev and value - prev["weight"] >= 1.0:
        lines.append("<i>Скачок за день — это вода и содержимое кишечника, не жир. Смотрите на тренд.</i>")
    await message.answer("\n".join(lines), reply_markup=kb.weight_menu)
    await panel.refresh(message.bot, profile)
    from .polar import weight_hint
    await weight_hint(message.bot, profile)


async def record_steps(message: Message, profile, steps: int) -> None:
    w = await db.last_weight()
    kcal = nutrition.steps_kcal(steps, w["weight"] if w else profile["start_weight"])
    await db.add_activity(f"{steps} шагов", kcal)
    await message.answer(f"👟 {fmt(steps)} шагов ≈ {fmt(kcal)} ккал. В бюджет еды не добавляю: "
                         "активность учитывается в реальном расходе, который я считаю по весу.")


async def record_workout(message: Message, profile, raw: float, label: str = "", day: str | None = None) -> None:
    """Калории тренировки с часов: идут в расход дня, бюджет еды не меняют."""
    net = nutrition.workout_net(raw)
    day = day or db.today()
    imported = [w for w in await db.workouts_on(day) if w["ext_id"]]
    wid = await db.add_workout(label or "тренировка", raw, net, day)
    eaten = (await db.day_totals(day))["kcal"]
    wo = await db.workout_kcal(day)
    spent = profile["tdee"] + wo
    bal = spent - eaten
    when = "сегодня" if day == db.today() else "за вчера"
    lines = [
        f"🏋️ Тренировка {when}: {fmt(raw)} ккал с часов → в расход <b>{fmt(net)} ккал</b>",
        f"<i>−15%: обычный расход за это время уже входит в {fmt(profile['tdee'])}, а часы его тоже считают.</i>",
        f"Расход {when}: {fmt(profile['tdee'])} + {fmt(wo)} = <b>{fmt(spent)} ккал</b>",
        f"Баланс: съедено {fmt(eaten)} → <b>{'дефицит' if bal >= 0 else 'профицит'} {fmt(abs(bal))} ккал</b>",
        "Бюджет еды прежний: тренировка ускоряет похудение, а не даёт право съесть больше.",
    ]
    markup = kb.ikb([("❌ Удалить", f"wo:del:{wid}")])
    if imported:
        p = imported[-1]
        lines.append(f"\n⚠️ За этот день уже пришла тренировка из Polar: {p['descr']}, {fmt(p['raw_kcal'])} ккал. "
                     "Если это она же — уберите ручную запись, иначе калории посчитаются дважды.")
        markup = kb.ikb([("🔁 Та же — убрать ручную", f"wo:del:{wid}")], [("Разные, оставить", "wo:keep")])
    await message.answer("\n".join(lines), reply_markup=markup)
    await panel.refresh(message.bot, profile)


async def today_markup():
    extra = [[("📋 Приёмы пищи", "t:meals")]]
    st = await db.day_state()
    if db.now().hour >= 18 and st["complete"] != 1 and (await db.day_totals(db.today()))["n"]:
        extra[0].append(("✅ Всё записал", f"dc:1:{db.today()}"))
    return kb.report_menu("r:1", extra)


async def send_today(message: Message, profile) -> None:
    await message.answer(await budget.today_text(profile), reply_markup=await today_markup())


async def show_report(message: Message, profile, period: str, replace: bool = False) -> None:
    """Показывает отчёт за период. replace=True — заменить сообщение message (переключение периода).

    Telegram не превращает текст в фото и обратно, поэтому при смене типа сообщение пересоздаётся.
    """
    if period == "r:1":
        text, png, markup = await budget.today_text(profile), None, await today_markup()
    else:
        days = int(period[2:])
        text, png = await report_view.build(profile, days)
        extra = [[("📋 Выгрузить для анализа", f"ex:{days or 30}")]] if png else []
        markup = kb.report_menu(period, extra)
    is_photo = bool(message.photo) if replace else False
    try:
        if replace and png and is_photo:
            await message.edit_media(InputMediaPhoto(media=BufferedInputFile(png, "report.png"), caption=text),
                                     reply_markup=markup)
            return
        if replace and not png and not is_photo:
            await message.edit_text(text, reply_markup=markup)
            return
    except TelegramBadRequest as e:
        if "not modified" in str(e):  # то же содержимое — нечего менять
            return
        raise
    if replace:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
    if png:
        await message.answer_photo(BufferedInputFile(png, "report.png"), caption=text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def send_weekly(bot, chat_id: int, profile, with_review: bool) -> None:
    rep = await weekly.build(profile)
    markup = None
    if rep.proposal:
        t, b = rep.proposal
        markup = kb.ikb([(f"✅ Принять бюджет {fmt(b)}", f"bud:{t}:{b}")])
    await bot.send_message(chat_id, rep.text, reply_markup=markup)
    if with_review:
        try:
            review = await llm.weekly_review(_plain(rep.text))
            await bot.send_message(chat_id, "🧠 <b>Разбор</b>\n" + escape(review))
        except llm.LLMError:
            pass


def _plain(html_text: str) -> str:
    return re.sub(r"<[^>]+>", "", html_text)
