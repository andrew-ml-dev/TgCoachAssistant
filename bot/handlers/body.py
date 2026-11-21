"""📏 Замеры: по одной точке за шаг — число или «пропустить». Раз в неделю бот сам предлагает после веса."""
import re
from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, keyboards as kb
from ..services import body

router = Router()


class Measure(StatesGroup):
    value = State()


STEP_KB = kb.ikb([("⏭ Пропустить", "bm:skip"), ("✅ Закончить", "bm:done")])
HOWTO = ("Утром, натощак, до тренировки — каждую неделю в одних условиях.\n"
         "Лента — прилегает, но не врезается. Калипер — правая сторона: оттяните складку пальцами, "
         "калипер на 1 см ниже пальцев, читайте через 2 сек. Каждую складку 2 раза, пишите среднее.")


async def due() -> bool:
    """Пора ли замеры: прошлые — не меньше недели назад (или ещё не было)."""
    s = await db.measure_sessions()
    return not s or (db.now().date() - date.fromisoformat(s[-1][0])).days >= body.INTERVAL_DAYS


async def offer(message: Message) -> None:
    """После утреннего веса: раз в неделю предложить замеры. Не чаще раза в день."""
    if not await due() or not await db.mark_flag("measure_offer", db.today()):
        return
    await message.answer("📏 Неделя с прошлых замеров — замерим? 5 обхватов лентой и 3 складки калипером, 3 минуты.",
                         reply_markup=kb.ikb([("📏 Начать", "bm:go"), ("Не сегодня", "bm:no")]))


async def _ask(message: Message, state: FSMContext, i: int, profile) -> None:
    s = body.sites(profile["sex"])[i]
    await state.set_state(Measure.value)
    await state.update_data(i=i)
    await message.answer(f"<b>{s.name}</b>, {s.unit} — {s.hint}.", reply_markup=STEP_KB)


async def start(message: Message, state: FSMContext, profile) -> None:
    await state.clear()
    sessions = await db.measure_sessions()
    head = "📏 <b>Замеры</b>\n" + (HOWTO if len(sessions) < 3 else "Те же точки и условия, что в прошлый раз.")
    await message.answer(head)
    await _ask(message, state, 0, profile)


async def _next(message: Message, state: FSMContext, profile) -> None:
    i = (await state.get_data()).get("i", 0) + 1
    if i < len(body.sites(profile["sex"])):
        await _ask(message, state, i, profile)
    else:
        await finish(message, state, profile)


async def finish(message: Message, state: FSMContext, profile) -> None:
    await state.clear()
    sessions = await db.measure_sessions()
    if not sessions or sessions[-1][0] != db.today():
        await message.answer("Ничего не записал. Замеры: ⚙️ Ещё → 📏 Замеры.")
        return
    w = await db.last_weight()
    await message.answer(body.report(profile, sessions, w["trend"] if w else None),
                         reply_markup=kb.ikb([("📏 Переписать", "bm:go")]))


@router.callback_query(F.data == "bm:go")
@router.callback_query(F.data == "s:measure")
async def go(cb: CallbackQuery, state: FSMContext, profile) -> None:
    await cb.answer()
    if cb.data == "bm:go":
        await cb.message.edit_reply_markup(reply_markup=None)
    await start(cb.message, state, profile)


@router.callback_query(F.data == "bm:no")
async def no(cb: CallbackQuery) -> None:
    await cb.answer("Хорошо, спрошу завтра")
    await cb.message.edit_reply_markup(reply_markup=None)


@router.callback_query(Measure.value, F.data == "bm:skip")
async def skip(cb: CallbackQuery, state: FSMContext, profile) -> None:
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await _next(cb.message, state, profile)


@router.callback_query(F.data == "bm:done")
async def done(cb: CallbackQuery, state: FSMContext, profile) -> None:
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await finish(cb.message, state, profile)


@router.message(Measure.value, F.text)
async def value(message: Message, state: FSMContext, profile) -> None:
    i = (await state.get_data()).get("i", 0)
    s = body.sites(profile["sex"])[i]
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(?:см|мм)?\s*", message.text)
    lo, hi = body.LIMITS[s.unit]
    v = float(m.group(1).replace(",", ".")) if m else 0
    if not lo <= v <= hi:
        return await message.answer(f"Нужно число в {s.unit}, от {lo} до {hi}. Или «⏭ Пропустить».",
                                    reply_markup=STEP_KB)
    await db.save_measure(db.today(), s.key, v)
    await _next(message, state, profile)
