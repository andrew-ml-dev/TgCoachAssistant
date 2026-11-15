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
from ..services import budget, llm, report, weekly
from ..services.budget import fmt
from . import common
from .food import Food, check_can

router = Router()

HELP = (
    "<b>Как пользоваться</b>\n"
    "Вверху чата закреплена сводка дня — она обновляется сама.\n"
    "Внизу постоянная панель: ⚡ Быстрая запись — ваши продукты и повтор недавнего, "
    "📊 Отчёт — сегодня и графики, 🥗 Меню — план на день, ❓ Можно ли? — проверка до еды.\n\n"
    "Всё остальное просто пишите мне:\n"
    "• <code>91.8</code> или <code>вес 92</code> — вес\n"
    "• <code>тарелка борща, 2 куска хлеба</code> — разберу и посчитаю\n"
    "• <code>450 ккал</code>, <code>обед 700 ккал</code> — калории числом\n"
    "• <code>вчера: пельмени 400 г</code> — запись за вчера\n"
    "• ⚡ Быстрая запись → ➕ Новый продукт — запомнить цифры с этикетки, дальше выбрать и ввести вес\n"
    "• <code>трен 650</code>, <code>зал + дорожка 650</code>, <code>бег 30 мин 400</code> — калории тренировки "
    "с часов: идут в расход и дефицит, бюджет еды не меняют\n"
    "• <code>8000 шагов</code> — шаги\n\n"
    "<b>Команды</b>\n"
    "/today — отчёт: сегодня, 3 дня, неделя, всё наблюдение\n"
    "/menu — меню на сегодня под ваш бюджет и белок\n"
    "/can шаурма — влезет ли блюдо в остаток (ничего не записывает)\n"
    "/week — итоги недели и пересчёт бюджета\n"
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


@router.message(F.text == kb.MENU)
async def panel_menu(message: Message, state: FSMContext, profile) -> None:
    from .diet import menu_cmd
    await menu_cmd(message, state, profile)


@router.message(F.text == kb.CAN)
async def panel_can(message: Message, state: FSMContext, profile) -> None:
    from .food import can_start_msg
    await state.clear()
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
    else:
        await cb.message.answer("Понял, этот день не буду учитывать в расчётах расхода и дефицита. "
                                "Если допишете еду, отметьте день заново: /today → «✅ Всё записал».")


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


@router.callback_query(F.data == "s:cost")
async def cost_cb(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(await cost_text())


async def cost_text() -> str:
    u = await db.usage_month()
    return (f"💰 Claude API за этот месяц: <b>${u['cost']:.4f}</b> из лимита ${config.MONTHLY_LLM_BUDGET:.2f}\n"
            f"Запросов: {u['n']}, токенов: {fmt(u['inp'])} вход / {fmt(u['out'])} выход.\n"
            f"Модель: {config.MODEL}")


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
