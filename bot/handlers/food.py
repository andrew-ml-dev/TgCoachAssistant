"""Еда: карточка приёма пищи, правки, мои блюда, недавнее, «можно ли?»."""
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, keyboards as kb
from ..services import budget, llm, products
from ..services.budget import fmt
from ..services.parser import normalize
from . import common

router = Router()


class Food(StatesGroup):
    clarify = State()
    edit = State()
    can = State()


# ---------- карточка приёма пищи ----------

async def _delete(meal_id: int) -> None:
    meal = await db.get_meal(meal_id)
    if meal and meal["source"] in ("llm", "cache") and meal["raw_text"]:
        await db.cache_del(normalize(meal["raw_text"]))  # неудачный разбор не должен повторяться из кеша
    await db.delete_meal(meal_id)


@router.callback_query(F.data.startswith("m:del:"))
async def meal_del(cb: CallbackQuery, profile) -> None:
    await _delete(int(cb.data.split(":")[2]))
    await cb.answer("Удалено")
    await cb.message.edit_text("❌ Запись удалена.\n\n" + await budget.left_line(profile))


@router.callback_query(F.data.startswith("m:yday:"))
async def meal_to_yesterday(cb: CallbackQuery, profile) -> None:
    meal = await db.get_meal(int(cb.data.split(":")[2]))
    if not meal or meal["date"] != db.today():
        return await cb.answer("Эту запись уже нельзя перенести")
    yday = db.days_ago(1)
    await db.move_meal(meal["id"], yday)
    await cb.answer("Перенёс на вчера")
    meal = await db.get_meal(meal["id"])
    await cb.message.edit_text(
        common.meal_text(meal, await db.meal_items(meal["id"])) + "\n\n" + await common.day_note(profile, yday)
        + "\n" + await budget.left_line(profile), reply_markup=kb.meal_card(meal["id"], can_move=False))


@router.callback_query(F.data.startswith("m:edit:"))
async def meal_edit(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Food.edit)
    await state.update_data(meal_id=int(cb.data.split(":")[2]))
    await cb.answer()
    await cb.message.answer(
        "Что исправить? Например: «хлеба было 3 куска», «котлета 200 г», «без масла».\n"
        "Или отправьте число — итоговые калории приёма пищи. Отмена: /cancel")


@router.message(Food.edit, F.text)
async def meal_edit_apply(message: Message, state: FSMContext, profile) -> None:
    meal_id = (await state.get_data())["meal_id"]
    meal = await db.get_meal(meal_id)
    await state.clear()
    if not meal:
        return await message.answer("Эта запись уже удалена.")
    items = await db.meal_items(meal_id)
    text = message.text.strip()
    if text.replace(" ", "").isdigit() and meal["kcal"] > 0:
        # Число — новый итог; позиции масштабируются пропорционально, без запроса к Claude
        k = int(text.replace(" ", "")) / meal["kcal"]
        for i in items:
            for f in ("grams", "kcal", "protein", "fat", "carbs"):
                i[f] = round(i[f] * k, 1)
    else:
        try:
            await message.bot.send_chat_action(message.chat.id, "typing")
            parsed = await llm.correct_food(items, text)
        except llm.LLMError as e:
            return await message.answer(str(e))
        if not parsed.items:
            return await message.answer("Не понял поправку, запись оставил как была.")
        items = [i.model_dump() for i in parsed.items]
    await db.replace_meal_items(meal_id, items)
    if meal["raw_text"]:
        await db.cache_put(normalize(meal["raw_text"]), items)  # правка запоминается
    await common.send_meal(message, profile, meal_id, "исправлено")


# ---------- уточняющий вопрос ----------

@router.message(Food.clarify, F.text)
async def clarify(message: Message, state: FSMContext, profile) -> None:
    data = await state.get_data()
    await state.clear()
    try:
        await common.record_food_text(message, profile, f"{data['text']}. Уточнение: {message.text}",
                                      allow_question=False, day=data.get("day"))
    except llm.LLMError as e:
        await message.answer(str(e))


# ---------- повторить ----------

@router.callback_query(F.data == "f:recent")
async def recent(cb: CallbackQuery) -> None:
    from .diet import quick_screen
    await cb.answer()
    await quick_screen(cb.message)


@router.callback_query(F.data.startswith("rep:"))
async def repeat(cb: CallbackQuery, profile) -> None:
    meal = await db.get_meal(int(cb.data.split(":")[1]))
    await cb.answer()
    if not meal:
        return
    mid = await db.add_meal(await db.meal_items(meal["id"]), source="repeat", raw_text=meal["raw_text"])
    await common.send_meal(cb.message, profile, mid)


@router.callback_query(F.data == "t:meals")
async def meals_today(cb: CallbackQuery) -> None:
    await cb.answer()
    meals = await db.meals_on(db.today())
    if not meals:
        return await cb.message.answer("Сегодня записей ещё нет.")
    lines, rows = [], []
    for m in meals:
        lines.append(common.meal_text(m, await db.meal_items(m["id"])))
        rows.append([(f"✏️ {m['title'][:22]} · {fmt(m['kcal'])}", f"m:edit:{m['id']}"), ("❌", f"m:del2:{m['id']}")])
    await cb.message.answer("\n\n".join(lines), reply_markup=kb.ikb(*rows))


@router.callback_query(F.data.startswith("m:del2:"))
async def meal_del_from_list(cb: CallbackQuery, profile) -> None:
    await _delete(int(cb.data.split(":")[2]))
    await cb.answer("Удалено")
    await cb.message.answer("❌ Запись удалена.\n\n" + await budget.left_line(profile))


# ---------- «можно ли?» ----------

@router.message(F.text == kb.CAN)
async def can_start_msg(message: Message, state: FSMContext, profile) -> None:
    await state.set_state(Food.can)
    _, left, _ = await budget.remaining(profile)
    await message.answer(f"Что хотите съесть? Опишите блюдо — скажу, влезет ли в остаток ({fmt(left)} ккал). "
                         "Ничего не запишу, пока не нажмёте «Записать». Отмена: /cancel")


@router.callback_query(F.data == "f:can")
async def can_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Food.can)
    await cb.answer()
    await cb.message.answer("Что хотите съесть? Опишите блюдо. Отмена: /cancel")


@router.message(Food.can, F.text)
async def can_text(message: Message, state: FSMContext, profile) -> None:
    await state.clear()
    await check_can(message, state, profile, message.text)


async def check_can(message: Message, state: FSMContext, profile, text: str) -> None:
    items = await db.cache_get(normalize(text))
    if items is None:
        try:
            await message.bot.send_chat_action(message.chat.id, "typing")
            parsed = await llm.parse_food(text, allow_question=False, products=await db.products())
        except llm.LLMError as e:
            return await message.answer(str(e))
        items = products.apply([i.model_dump() for i in parsed.items], await db.products())
    if not items:
        return await message.answer("Не понял, что это за блюдо. Опишите подробнее.")
    kcal = sum(i["kcal"] for i in items)
    protein = sum(i["protein"] for i in items)
    _, left, _ = await budget.remaining(profile)
    after = left - kcal
    verdict = (f"✅ Влезает: после этого останется {fmt(after)} ккал." if after >= 0
               else f"🔴 Не влезает: будет перебор на {fmt(-after)} ккал.")
    if 0 <= after < profile["budget"] * 0.1:
        verdict += " Но на остаток дня почти ничего не останется."
    await state.update_data(can_items=items, can_text=text)
    await message.answer(
        f"«{escape(text)}» ≈ <b>{fmt(kcal)} ккал</b>, белок {fmt(protein)} г.\n"
        f"Остаток сейчас: {fmt(left)} ккал.\n{verdict}",
        reply_markup=kb.ikb([("➕ Записать", "can:add")]))


@router.callback_query(F.data == "can:add")
async def can_add(cb: CallbackQuery, state: FSMContext, profile) -> None:
    data = await state.get_data()
    await cb.answer()
    if not data.get("can_items"):
        return await cb.message.answer("Оценка устарела, отправьте блюдо ещё раз.")
    await state.update_data(can_items=None)
    await cb.message.edit_reply_markup(reply_markup=None)
    mid = await db.add_meal(data["can_items"], source="llm", raw_text=data["can_text"])
    await common.send_meal(cb.message, profile, mid)
