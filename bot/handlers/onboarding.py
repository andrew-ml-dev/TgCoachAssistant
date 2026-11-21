"""Онбординг /start и настройки профиля (цель, темп, пол бюджета, напоминания)."""
from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db
from ..keyboards import ikb, main_menu
from ..services import budget, nutrition, panel
from ..services.parser import parse_time

router = Router()
settings_router = Router()  # подключается после меню, чтобы кнопки меню отменяли диалог


class Onb(StatesGroup):
    sex = State()
    age = State()
    height = State()
    weight = State()
    target = State()
    activity = State()
    pace = State()
    wake = State()


class Settings(StatesGroup):
    target = State()
    floor = State()
    wake = State()


ACTIVITY_KB = ikb(
    [("🪑 Сидячая работа, мало движения", "onb:act:1.2")],
    [("🚶 Много хожу в течение дня", "onb:act:1.375")],
    [("🧍 Работа на ногах", "onb:act:1.55")],
    [("🏗 Тяжёлый физический труд", "onb:act:1.725")],
)
PACE_KB = ikb(
    [("0.5 кг/нед", "pace:0.5"), ("1 кг/нед", "pace:1")],
    [("1.5 кг/нед", "pace:1.5"), ("2 кг/нед", "pace:2")],
)


def _number(text: str, lo: float, hi: float) -> float | None:
    try:
        v = float(text.replace(",", ".").strip())
    except (ValueError, AttributeError):
        return None
    return v if lo <= v <= hi else None


async def begin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Onb.sex)
    await message.answer(
        "Привет! Я помогу худеть: веду вес и калории, считаю дневной бюджет и напоминаю взвеситься.\n\n"
        "Несколько вопросов, чтобы рассчитать норму. Ваш пол?",
        reply_markup=ikb([("Мужской", "onb:sex:m"), ("Женский", "onb:sex:f")]),
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, profile) -> None:
    if profile:
        await state.clear()
        await message.answer(
            "Профиль уже настроен. Пишите вес, калории или что съели — я разберусь.\n"
            "Изменить данные: ⚙️ Ещё → 👤 Профиль заново.", reply_markup=main_menu)
        return
    await begin(message, state)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, profile) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu if profile else None)


@router.callback_query(F.data == "s:profile")
async def redo_profile(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await begin(cb.message, state)


@router.callback_query(Onb.sex, F.data.startswith("onb:sex:"))
async def got_sex(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(sex=cb.data.split(":")[2])
    await state.set_state(Onb.age)
    await cb.answer()
    await cb.message.answer("Возраст (полных лет)?")


@router.message(Onb.age)
async def got_age(message: Message, state: FSMContext) -> None:
    v = _number(message.text, 14, 100)
    if v is None:
        return await message.answer("Введите возраст числом, например 34.")
    await state.update_data(age=int(v))
    await state.set_state(Onb.height)
    await message.answer("Рост в сантиметрах?")


@router.message(Onb.height)
async def got_height(message: Message, state: FSMContext) -> None:
    v = _number(message.text, 120, 230)
    if v is None:
        return await message.answer("Введите рост в сантиметрах, например 180.")
    await state.update_data(height=v)
    await state.set_state(Onb.weight)
    await message.answer("Текущий вес в кг?")


@router.message(Onb.weight)
async def got_weight(message: Message, state: FSMContext) -> None:
    v = _number(message.text, 35, 300)
    if v is None:
        return await message.answer("Введите вес в кг, например 92.5.")
    await state.update_data(weight=v)
    await state.set_state(Onb.target)
    await message.answer("Целевой вес в кг?")


@router.message(Onb.target)
async def got_target(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    v = _number(message.text, 35, 300)
    if v is None or v >= data["weight"]:
        return await message.answer("Целевой вес должен быть числом меньше текущего, например 80.")
    await state.update_data(target=v)
    await state.set_state(Onb.activity)
    await message.answer("Активность в обычный день, <b>без тренировок</b>? Тренировки записываются отдельно "
                         "калориями с часов (<code>трен 650</code>) и прибавляются к расходу в тот день.",
                         reply_markup=ACTIVITY_KB)


@router.callback_query(Onb.activity, F.data.startswith("onb:act:"))
async def got_activity(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(activity=float(cb.data.split(":")[2]))
    await state.set_state(Onb.pace)
    await cb.answer()
    await cb.message.answer("Желаемый темп похудения?", reply_markup=PACE_KB)


@router.callback_query(Onb.pace, F.data.startswith("pace:"))
async def got_pace(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(pace=float(cb.data.split(":")[1]))
    await state.set_state(Onb.wake)
    await cb.answer()
    await cb.message.answer("Во сколько напоминать взвеситься утром? Например 7:30.")


@router.message(Onb.wake)
async def got_wake(message: Message, state: FSMContext) -> None:
    t = parse_time(message.text or "")
    if not t:
        return await message.answer("Введите время в формате ЧЧ:ММ, например 7:30.")
    d = await state.get_data()
    await state.clear()
    tdee = nutrition.initial_tdee(d["sex"], d["weight"], d["height"], d["age"], d["activity"])
    floor = nutrition.DEFAULT_FLOOR[d["sex"]]
    b = nutrition.make_budget(tdee, d["pace"], floor)
    await db.save_profile(
        chat_id=message.chat.id, sex=d["sex"], age=d["age"], height_cm=d["height"],
        start_weight=d["weight"], target_weight=d["target"], activity=d["activity"],
        pace=d["pace"], floor_kcal=floor, tdee=tdee, budget=b.budget,
        protein_goal=nutrition.protein_goal(d["target"]), wake_time=t,
    )
    await db.set_budget(tdee, b.budget)
    await db.add_weight(d["weight"])
    profile = await db.get_profile()
    await message.answer(
        "Готово! Ваш план:\n\n" + await budget.pace_text(profile) +
        "\n\nРасход пока рассчитан по формуле. Через 2 недели записей я вычислю реальный "
        "по вашим данным и уточню бюджет.\n\n"
        "Как пользоваться: просто пишите мне.\n"
        "• <code>91.8</code> — вес\n• <code>450 ккал</code> — калории числом\n"
        "• <code>тарелка борща и 2 куска хлеба</code> — разберу сам\n• <code>трен 650</code> — тренировка с часов, часть калорий прибавится к еде\n\n"
        "Вверху чата я закреплю сводку дня, внизу — панель с частыми действиями.",
        reply_markup=main_menu,
    )
    await panel.refresh(message.bot)


@router.message(StateFilter(Onb))
async def onb_fallback(message: Message) -> None:
    await message.answer("Выберите вариант кнопкой в сообщении выше.")


# ---------- настройки ----------

async def _apply_budget(message: Message, **changes) -> None:
    await db.update_profile(**changes)
    p = await db.get_profile()
    b = nutrition.make_budget(p["tdee"], p["pace"], p["floor_kcal"])
    await db.set_budget(p["tdee"], b.budget)
    await message.answer("Обновлено.\n\n" + await budget.pace_text(await db.get_profile()), reply_markup=main_menu)


GOAL_PACE_KB = ikb(
    [("0.5 кг/нед", "gp:0.5"), ("0.75 кг/нед", "gp:0.75"), ("1 кг/нед", "gp:1")],
    [("1.5 кг/нед", "gp:1.5"), ("2 кг/нед", "gp:2")],
)


@settings_router.callback_query(F.data == "s:goal")
async def goal(cb: CallbackQuery, state: FSMContext, profile) -> None:
    await state.clear()
    await cb.answer()
    await cb.message.answer(await budget.pace_text(profile), reply_markup=ikb(
        [("🎯 Целевой вес", "g:target"), ("🐢 Темп", "g:pace")],
        [("🧮 Пол бюджета", "s:floor")]))


@settings_router.callback_query(F.data == "g:target")
async def goal_target_ask(cb: CallbackQuery, state: FSMContext, profile) -> None:
    await state.set_state(Settings.target)
    await cb.answer()
    await cb.message.answer(f"Целевой вес сейчас {profile['target_weight']:g} кг. Новый целевой вес? Отмена: /cancel")


@settings_router.message(Settings.target)
async def goal_target(message: Message, state: FSMContext) -> None:
    v = _number(message.text, 35, 300)
    if v is None:
        return await message.answer("Введите вес числом, например 80. Отмена: /cancel")
    await state.clear()
    await _apply_budget(message, target_weight=v, protein_goal=nutrition.protein_goal(v))


@settings_router.callback_query(F.data == "g:pace")
async def goal_pace_ask(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer("Желаемый темп? Если бюджет упрётся в пол, реальный темп будет ниже — я покажу какой.",
                            reply_markup=GOAL_PACE_KB)


@settings_router.callback_query(F.data.startswith("gp:"))
async def goal_pace(cb: CallbackQuery) -> None:
    await cb.answer()
    await _apply_budget(cb.message, pace=float(cb.data.split(":")[1]))


@settings_router.callback_query(F.data == "s:floor")
async def floor(cb: CallbackQuery, state: FSMContext, profile) -> None:
    await state.set_state(Settings.floor)
    await cb.answer()
    await cb.message.answer(
        f"Пол бюджета сейчас {profile['floor_kcal']} ккал: ниже него дневной бюджет не опускается. "
        "Введите новое значение (не меньше 1000). Отмена: /cancel")


@settings_router.message(Settings.floor)
async def floor_set(message: Message, state: FSMContext, profile) -> None:
    v = _number(message.text, 1000, 4000)
    if v is None:
        return await message.answer("Введите число от 1000 до 4000. Отмена: /cancel")
    await state.clear()
    if v < nutrition.DEFAULT_FLOOR[profile["sex"]]:
        await message.answer("⚠️ Это ниже обычно рекомендуемого минимума. Долго держаться на таком "
                             "уровне без наблюдения врача не стоит: теряются мышцы и замедляется расход.")
    await _apply_budget(message, floor_kcal=int(v))


@settings_router.callback_query(F.data == "s:remind")
async def remind(cb: CallbackQuery, state: FSMContext, profile) -> None:
    await state.set_state(Settings.wake)
    await cb.answer()
    await cb.message.answer(
        f"Сейчас напоминаю взвеситься с {profile['wake_time']} каждые 30 минут до {profile['cutoff_time']}. "
        "Новое время начала (ЧЧ:ММ)? Отмена: /cancel")


@settings_router.message(Settings.wake)
async def remind_set(message: Message, state: FSMContext) -> None:
    t = parse_time(message.text or "")
    if not t:
        return await message.answer("Введите время в формате ЧЧ:ММ, например 7:30.")
    await state.clear()
    await db.update_profile(wake_time=t)
    await message.answer(f"Буду напоминать с {t}.", reply_markup=main_menu)
