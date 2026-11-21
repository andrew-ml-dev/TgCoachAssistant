"""Кнопки главного меню и команды. Любая из них отменяет незавершённый диалог."""
import csv
import io
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import config, db, keyboards as kb
from ..services import budget, diet_review, llm, report, suggest, usage, weekly
from ..services.budget import fmt
from . import common
from .food import Food, check_can

router = Router()

HELP = (
    "<b>Как пользоваться</b>\n"
    "Вверху чата закреплена сводка дня — она обновляется сама.\n"
    "Внизу постоянная панель: ⚡ Быстрая запись — последние продукты: выбрать и ввести граммы, "
    "📊 Отчёт — сегодня и графики, 🍽 Что съесть? — 3 варианта: минимум калорий, чтобы добрать белок "
    "(и жиры до нижней границы), остальное — в дефицит; после разбора рациона — ваши блюда, запись одной кнопкой; "
    "там же «❓ Влезет ли своё?» — проверка своего блюда до еды.\n\n"
    "Всё остальное просто пишите мне:\n"
    "• <code>91.8</code> или <code>вес 92</code> — вес\n"
    "• <code>тарелка борща, 2 куска хлеба</code> — разберу и посчитаю\n"
    "• <code>450 ккал</code>, <code>обед 700 ккал</code> — калории числом\n"
    "• <code>вчера: пельмени 400 г</code> — запись за вчера\n"
    "• ⚡ Быстрая запись → ➕ Новый продукт — запомнить цифры с этикетки, дальше выбрать и ввести вес\n"
    "• <code>трен 650</code>, <code>зал + дорожка 650</code>, <code>бег 30 мин 400</code> — калории тренировки "
    "с часов. Сначала добирают дефицит до вашего темпа, остальное прибавляется к еде на этот день "
    "(к еде — не меньше половины). Подробно: ⚙️ Ещё\n"
    "• ⚙️ Ещё → ⌚ Polar — тренировки с H10 из Polar Flow приходят сами\n"
    "• <code>творог 200 г</code>, <code>2 яйца</code>, <code>хлеб 2 куска</code> — базовые продукты считаю сам, без Claude; "
    "список: ⚡ Быстрая запись → 🥚 Базовые продукты\n"
    "• база продуктов наполняется сама: что Claude разобрал раз, дальше считается по тем же цифрам "
    "(«куриное филе 150 г» — без запроса). Посмотреть и поправить всё сразу — ⚙️ Ещё → 🛠 Сервис → "
    "📗 Таблица продуктов: правите xlsx (КБЖУ, синонимы, штрихкод) и присылаете обратно\n"
    "• 🎙 голосовое — всё то же самое голосом, можно несколько дел сразу: «вес 91 и 4, на завтрак омлет из трёх яиц»\n"
    "• 📷 фото штрихкода — найду продукт в Open Food Facts, запомню и спрошу граммы; "
    "если не найду — пришлите фото таблицы пищевой ценности\n"
    "• ⚡ Быстрая запись → 🍽 По шагам — что, сколько, как приготовлено, кнопками\n"
    "• ⚙️ Ещё → 📏 Замеры — обхваты лентой и 3 складки калипером → % жира; когда угодно — та же кнопка "
    "под записанным весом; раз в неделю бот и сам предложит после утреннего веса\n\n"
    "<b>Команды</b>\n"
    "/today — отчёт: сегодня, 3 дня, неделя, всё наблюдение\n"
    "/eat — что съесть сейчас (то же, что кнопка)\n"
    "/can шаурма — влезет ли блюдо в остаток (ничего не записывает)\n"
    "/week — итоги недели и пересчёт бюджета; по воскресеньям с ними приходит разбор рациона, "
    "внепланово — ⚙️ Ещё → 🛠 Сервис → 🔁 Пересмотр рациона\n"
    "/undo — удалить последнюю запись еды\n"
    "/cost — расход API за месяц\n"
    "/export — выгрузка в CSV\n"
    "/cancel — отменить текущий вопрос"
)


# ---------- кнопки постоянной панели ----------
# Живут здесь, потому что menu.router идёт раньше роутеров с состояниями (диета, настройки, еда):
# нажатие кнопки панели всегда отменяет незавершённый вопрос, а не становится ответом на него.

@router.message(F.text.in_({kb.QUICK} | kb.OLD_QUICK))
async def panel_quick(message: Message, state: FSMContext) -> None:
    from .diet import products_msg
    await products_msg(message, state)


@router.message(F.text.in_({kb.EAT, kb.MENU}))
@router.message(Command("eat"))
async def panel_eat(message: Message, state: FSMContext, profile) -> None:
    await state.clear()
    if message.text == kb.MENU:
        await message.answer("Кнопка «Меню» стала «🍽 Что съесть?»: варианты под остаток калорий.",
                             reply_markup=kb.main_menu)
    text, buttons = await suggest.build(profile)
    await message.answer(text, reply_markup=_eat_kb(buttons))


def _eat_kb(buttons):
    eat = [b for b in buttons if not b[1].startswith("ws:more")]
    last = [b for b in buttons if b[1].startswith("ws:more")] + [("❓ Влезет ли своё?", "f:can")]
    return kb.ikb(*([eat] if eat else []), last)


@router.callback_query(F.data.startswith("ws:more:"))
async def eat_more(cb: CallbackQuery, profile) -> None:
    await cb.answer()
    text, buttons = await suggest.build(profile, int(cb.data.split(":")[2]))
    try:
        await cb.message.edit_text(text, reply_markup=_eat_kb(buttons))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("ws:") | F.data.startswith("wp:"))
async def eat_pick(cb: CallbackQuery, profile) -> None:
    got = await suggest.decode(cb.data)
    if not got:
        return await cb.answer("Варианты устарели — нажмите «🍽 Что съесть?» ещё раз", show_alert=True)
    title, items = got
    await cb.answer("Записал")
    await cb.message.edit_reply_markup(reply_markup=None)
    mid = await db.add_meal(items, source="base", title=title,
                            raw_text=title + ": " + ", ".join(f"{i['name']} {i['grams']:g} г" for i in items))
    await common.send_meal(cb.message, profile, mid, "по базе продуктов" if cb.data.startswith("wp:") else "по справочнику")


@router.message(F.text == kb.CAN)
async def panel_can(message: Message, state: FSMContext, profile) -> None:
    from .food import can_start_msg
    await state.clear()
    await message.answer("«❓ Можно ли?» теперь внутри «🍽 Что съесть?» — кнопка «❓ Влезет ли своё?».",
                         reply_markup=kb.main_menu)
    await can_start_msg(message, state, profile)


@router.message(F.text.in_({kb.TODAY, kb.OLD_TODAY}))
@router.message(Command("today"))
async def today(message: Message, state: FSMContext, profile) -> None:
    await state.clear()
    if message.text == kb.OLD_TODAY:
        await message.answer("Кнопка «Сегодня» стала «📊 Отчёт»: сегодня и другие периоды.", reply_markup=kb.main_menu)
    await common.send_today(message, profile)


@router.callback_query(F.data.startswith("r:"))
async def report_period(cb: CallbackQuery, profile) -> None:
    await cb.answer()
    # из отчёта — переключение на месте; из других карточек (например, «Прогресс» под весом) — новое сообщение
    await common.show_report(cb.message, profile, cb.data, replace=cb.message.reply_markup is not None
                             and any(b.callback_data == "r:1" for row in cb.message.reply_markup.inline_keyboard
                                     for b in row))


@router.message(F.text == kb.FOOD)
@router.message(Command("food"))
async def food(message: Message, state: FSMContext, profile) -> None:
    await state.clear()
    if message.text == kb.FOOD:
        await message.answer("Панель внизу обновлена: частые действия теперь в одно нажатие.", reply_markup=kb.main_menu)
    await message.answer(
        "Напишите, что съели («гречка с курицей 300 г»), или калории числом («450 ккал»).\n\n"
        + await budget.left_line(profile), reply_markup=kb.food_menu)


@router.message(F.text == kb.WEIGHT)
@router.message(Command("weight"))
async def weight(message: Message, state: FSMContext, profile) -> None:
    await state.clear()
    last = await db.last_weight()
    info = (f"Последний: {last['weight']:.1f} кг ({last['date'][8:]}.{last['date'][5:7]}), "
            f"тренд {last['trend']:.1f}." if last else "")
    if message.text == kb.WEIGHT:
        await message.answer("Панель внизу обновлена. Вес можно просто писать в чат числом.", reply_markup=kb.main_menu)
    await message.answer(f"Отправьте вес числом, например 91.8.\n{info}", reply_markup=kb.weight_menu)


@router.message(F.text == kb.OLD_PROGRESS)
@router.message(Command("chart"))
async def progress(message: Message, state: FSMContext, profile) -> None:
    await state.clear()
    if message.text == kb.OLD_PROGRESS:
        await message.answer("«Прогресс» переехал в «📊 Отчёт» → «Всё».", reply_markup=kb.main_menu)
    await common.show_report(message, profile, "r:0")


@router.message(F.text == kb.MORE)
async def more(message: Message, state: FSMContext, profile) -> None:
    await state.clear()
    await message.answer(await budget.pace_text(profile), reply_markup=kb.more_menu)


@router.callback_query(F.data == "s:service")
async def service(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer("🛠 Сервис", reply_markup=kb.service_menu)


@router.callback_query(F.data.startswith("dc:"))
async def day_complete(cb: CallbackQuery, profile) -> None:
    _, v, day = cb.data.split(":")
    await db.set_day_state(day, complete=int(v))
    await cb.answer("Отмечено")
    await cb.message.edit_reply_markup(reply_markup=None)
    if v == "1":
        await cb.message.answer("✅ День отмечен как полностью записанный — он пойдёт в расчёт расхода и дефицита.")
    elif day == db.today():
        await cb.message.answer("Понял, этот день не буду учитывать в расчётах расхода и дефицита. "
                                "Если допишете еду, отметьте день заново: /today → «✅ Всё записал».")
    else:
        await cb.message.answer("Понял, этот день не учитываю. Допишите забытое так: «вчера: пельмени 300 г» — "
                                "и нажмите «✅ Всё записал».", reply_markup=kb.ikb([("✅ Всё записал", f"dc:1:{day}")]))


@router.callback_query(F.data.startswith("ex:"))
async def export_for_analysis(cb: CallbackQuery, profile) -> None:
    days = int(cb.data.split(":")[1])
    await cb.answer()
    text = await report.build(profile, days)
    await cb.message.answer_document(
        BufferedInputFile(text.encode("utf-8"), f"pitanie-{db.today()}-{days}d.md"),
        caption=(f"📋 Данные за {days} дней с готовым заданием внутри. Откройте Claude (лучше самую сильную модель), "
                 "прикрепите файл и отправьте — дальше можно задавать уточняющие вопросы."))


@router.message(Command("help"))
async def help_cmd(message: Message) -> None:
    await message.answer(HELP)


@router.callback_query(F.data == "s:help")
async def help_cb(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(HELP)


@router.message(Command("week"))
async def week(message: Message, profile) -> None:
    await common.send_weekly(message.bot, message.chat.id, profile, with_review=False)


@router.callback_query(F.data.startswith("bud:"))
async def accept_budget(cb: CallbackQuery) -> None:
    _, tdee, bud = cb.data.split(":")
    await db.set_budget(int(tdee), int(bud))
    await cb.answer("Бюджет обновлён")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"Новый бюджет: <b>{fmt(int(bud))} ккал/день</b> (расход {fmt(int(tdee))}).")


@router.message(Command("can"))
async def can(message: Message, command: CommandObject, state: FSMContext, profile) -> None:
    await state.clear()
    if not command.args:
        await state.set_state(Food.can)
        return await message.answer("Что хотите съесть? Опишите блюдо.")
    await check_can(message, state, profile, command.args)


@router.message(Command("undo"))
async def undo(message: Message, profile) -> None:
    from .food import _delete
    meals = await db.meals_on(db.today())
    if not meals:
        return await message.answer("Сегодня нет записей еды для отмены.")
    meal = max(meals, key=lambda m: m["id"])
    await _delete(meal["id"])
    await message.answer(f"Удалил: {escape(meal['title'])} — {fmt(meal['kcal'])} ккал.\n\n" + await budget.left_line(profile))


@router.message(Command("cost"))
async def cost(message: Message) -> None:
    await message.answer(await cost_text())


@router.callback_query(F.data == "s:review")
async def diet_review_cb(cb: CallbackQuery, profile) -> None:
    await cb.answer()
    if await diet_review.readiness(profile):
        text, _ = await diet_review.run(profile)
        return await cb.message.answer(text)
    await cb.message.answer(f"🧠 Разбираю записи за 4 недели ({config.REVIEW_MODEL}), это до минуты…")
    await cb.bot.send_chat_action(cb.message.chat.id, "typing")
    try:
        text, _ = await diet_review.run(profile)
    except llm.LLMError as e:
        text = str(e)
    await cb.message.answer(text)


@router.callback_query(F.data == "s:usage")
async def usage_cb(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(usage.report(await db.uses()))


@router.callback_query(F.data == "s:cost")
async def cost_cb(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(await cost_text())


async def cost_text() -> str:
    u = await db.usage_month()
    lines = [f"💰 Claude API за этот месяц: <b>${u['cost']:.4f}</b> из лимита ${config.MONTHLY_LLM_BUDGET:.2f}",
             f"Запросов: {u['n']}, токенов: {fmt(u['inp'])} вход / {fmt(u['out'])} выход."]
    kinds = await db.usage_by_kind()
    if kinds:
        lines.append("По видам: " + ", ".join(f"{k['kind']} {k['n']} × ${k['cost']:.4f}" for k in kinds))
    lines.append(f"Модели: {config.MODEL}; разбор рациона — {config.REVIEW_MODEL}")
    return "\n".join(lines)


@router.message(Command("export"))
async def export(message: Message) -> None:
    await send_export(message)


@router.callback_query(F.data == "s:export")
async def export_cb(cb: CallbackQuery) -> None:
    await cb.answer()
    await send_export(cb.message)


async def send_export(message: Message) -> None:
    def to_csv(rows, cols) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])
        return buf.getvalue().encode("utf-8-sig")

    weights = await db.weights()
    meals = await db._all("SELECT date, ts, title, kcal, protein, fat, carbs, raw_text FROM meals ORDER BY ts")
    await message.answer_document(BufferedInputFile(to_csv(weights, ["date", "weight", "trend"]), "weights.csv"))
    await message.answer_document(BufferedInputFile(
        to_csv(meals, ["date", "ts", "title", "kcal", "protein", "fat", "carbs", "raw_text"]), "meals.csv"))
    meas = await db._all("SELECT date, key, value FROM measurements ORDER BY date, key")
    if meas:
        await message.answer_document(BufferedInputFile(to_csv(meas, ["date", "key", "value"]), "measurements.csv"))
