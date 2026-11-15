"""Вес: кнопки напоминаний; в конце — разбор свободного текста."""
import re
from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, keyboards as kb
from ..services import budget, llm, panel
from ..services.parser import classify
from . import common, diet
from .food import Food

router = Router()

RE_YESTERDAY = re.compile(r"^вчера[:,\s]+(.+)$", re.I | re.S)
RE_PRODUCT = re.compile(r"^продукт[:\s]+(.+)$", re.I | re.S)
WORKOUT_ASK = ("Сколько калорий показали часы за тренировку? Отправьте число, например <code>650</code>.\n"
               "В следующий раз можно сразу одним сообщением: <code>трен 650</code>, <code>зал + дорожка 650</code>. "
               "Отмена: /cancel")


class Workout(StatesGroup):
    kcal = State()


# ---------- тренировки ----------

@router.callback_query(F.data == "wo:ask")
async def workout_ask(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Workout.kcal)
    await state.update_data(wo_label="тренировка", wo_day=None)
    await cb.answer()
    await cb.message.answer(WORKOUT_ASK)


@router.message(Workout.kcal, F.text)
async def workout_kcal(message: Message, state: FSMContext, profile) -> None:
    m = re.search(r"\d{2,4}", message.text)
    if not m or not 30 <= int(m.group()) <= 3000:
        return await message.answer("Нужно число калорий с часов, например 650. Отмена: /cancel")
    data = await state.get_data()
    await state.clear()
    await common.record_workout(message, profile, int(m.group()), data.get("wo_label") or "тренировка",
                                data.get("wo_day"))


@router.callback_query(F.data == "wo:keep")
async def workout_keep(cb: CallbackQuery) -> None:
    await cb.answer("Оставил обе")
    await cb.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("wo:del:"))
async def workout_del(cb: CallbackQuery, profile) -> None:
    wid = int(cb.data.split(":")[2])
    w = await db.get_workout(wid)
    await cb.answer("Удалено" if w else "Уже удалено")
    if w:
        await db.delete_workout(wid)
        await cb.message.edit_text(f"❌ Тренировка удалена ({w['raw_kcal'] or w['kcal']:g} ккал с часов).\n\n"
                                   + await budget.left_line(profile))
        await panel.refresh(cb.bot, profile)


@router.callback_query(F.data == "w:undo")
async def weight_undo(cb: CallbackQuery) -> None:
    await db.delete_weight(db.today())
    await cb.answer("Сегодняшний вес удалён")


@router.callback_query(F.data == "w:snooze")
async def weight_snooze(cb: CallbackQuery) -> None:
    until = (db.now() + timedelta(hours=1)).isoformat(timespec="minutes")
    await db.set_day_state(snooze_until=until, remind_msg=None)
    await cb.answer("Напомню через час")
    await cb.message.delete()


@router.callback_query(F.data == "w:skip")
async def weight_skip(cb: CallbackQuery) -> None:
    await db.set_day_state(weight_skipped=1, remind_msg=None)
    await cb.answer("Сегодня больше не напоминаю")
    await cb.message.delete()


@router.callback_query(F.data.startswith("amb:"))
async def ambiguous(cb: CallbackQuery, profile) -> None:
    _, kind, val = cb.data.split(":")
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    if kind == "w":
        await common.record_weight(cb.message, profile, float(val))
    else:
        await common.record_kcal(cb.message, profile, float(val))


# ---------- свободный ввод (последний обработчик) ----------

@router.message(F.text)
async def free_text(message: Message, state: FSMContext, profile) -> None:
    text, day = message.text.strip(), None
    m = RE_PRODUCT.match(text)
    if m:
        return await diet.save_product(message, m.group(1))
    m = RE_YESTERDAY.match(text)
    if m:
        text, day = m.group(1).strip(), db.days_ago(1)

    last = await db.last_weight()
    p = classify(text, last["weight"] if last else None)
    if p.kind == "weight":
        if day:
            await db.add_weight(p.value, day)
            return await message.answer(f"⚖️ Записал вес за вчера: {p.value:.1f} кг.")
        return await common.record_weight(message, profile, p.value)
    if p.kind == "kcal":
        return await common.record_kcal(message, profile, p.value, p.label, day)
    if p.kind == "workout":
        if not p.value:
            await state.set_state(Workout.kcal)
            await state.update_data(wo_label=p.label, wo_day=day)
            return await message.answer(WORKOUT_ASK)
        return await common.record_workout(message, profile, p.value, p.label, day)
    if p.kind == "steps":
        return await common.record_steps(message, profile, int(p.value))
    if p.kind == "ambiguous":
        return await message.answer("Это вес или калории?", reply_markup=kb.ambiguous(p.value))
    try:
        question = await common.record_food_text(message, profile, text, day=day)
    except llm.LLMError as e:
        return await message.answer(str(e))
    if question:
        await state.set_state(Food.clarify)
        await state.update_data(text=text, day=day)
        await message.answer(f"❓ {question}")


@router.message()
async def other(message: Message) -> None:
    await message.answer("Понимаю только текст: вес, калории или описание еды. "
                         "Голосом удобно через диктовку на клавиатуре телефона.")
