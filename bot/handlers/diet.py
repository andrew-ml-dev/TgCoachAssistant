"""Настройки диеты и протеина, «Мои продукты» (цифры с этикетки)."""
import re
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, keyboards as kb
from ..services import foodbase, llm, products
from ..services.budget import fmt
from ..services.parser import normalize
from . import common

router = Router()


class Diet(StatesGroup):
    product = State()
    grams = State()
    prefs = State()
    shake_protein = State()


async def shake_item(profile) -> dict:
    """Одна порция протеина на воде.

    Если среди «Моих продуктов» есть протеин — считается по этикетке, иначе ≈5 ккал на 1 г белка.
    """
    p = profile["shake_protein"]
    prods = await db.products()
    prod = next((x for x in prods if x["name"] == profile["shake_product"]), None)
    if prod is None and not profile["shake_product"]:  # не выбран — первый похожий на протеин
        keys = ("protein", "протеин", "wpc", "whey", "isolate", "изолят")
        prod = next((x for x in prods if any(k in x["name"] for k in keys)), None)
    if prod and prod["protein"] > 0:
        it = products.item(prod, round(p / prod["protein"] * 100))
        it["name"] = f"протеиновый коктейль ({prod['name']})"
        return it
    return {"name": "протеиновый коктейль", "grams": round(p / 0.8), "kcal": round(p * 5),
            "protein": p, "fat": round(p * 0.08, 1), "carbs": round(p * 0.12, 1)}


# ---------- настройки диеты ----------

async def settings_text(profile) -> tuple[str, object]:
    sh = await shake_item(profile)
    n = profile["shake_servings"]
    from_shake = round(n * sh["protein"])
    text = (
        "<b>Диета и протеин</b>\n"
        f"Цель по белку: <b>{profile['protein_goal']} г/день</b> (2 г на кг целевого веса).\n"
        f"Протеин: <b>{escape(sh['name'].removeprefix('протеиновый коктейль').strip(' ()') or 'по оценке, без этикетки')}</b>, "
        f"{n} порц. в день по {sh['protein']:g} г белка (≈{round(sh['kcal'])} ккал на воде) — это {from_shake} г, "
        f"остальные {max(0, profile['protein_goal'] - from_shake)} г из еды.\n"
        f"Предпочтения (учитываются в разборе рациона): {escape(profile['diet_prefs']) or 'не заданы'}"
    )
    markup = kb.ikb(
        [("✍️ Предпочтения в еде", "d:prefs")],
        [("🥤 0 порций", "d:sh:0"), ("1 порция", "d:sh:1"), ("2 порции", "d:sh:2")],
        [("🥤 Какой протеин", "d:sp"), ("⚖️ Белка в порции", "d:shp")],
    )
    return text, markup


@router.callback_query(F.data == "s:diet")
async def diet_settings(cb: CallbackQuery, profile) -> None:
    await cb.answer()
    text, markup = await settings_text(profile)
    await cb.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("d:sh:"))
async def set_servings(cb: CallbackQuery) -> None:
    await db.update_profile(shake_servings=int(cb.data.split(":")[2]))
    await cb.answer("Сохранено")
    text, markup = await settings_text(await db.get_profile())
    await cb.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "d:sp")
async def shake_pick(cb: CallbackQuery) -> None:
    await cb.answer()
    prods = await db.products()
    rows = [[(p["name"][:40], f"d:spn:{p['id']}")] for p in prods]
    rows.append([("Без этикетки (оценка)", "d:spn:x")])
    await cb.message.answer("Какой протеин вы пьёте? По его этикетке считаю порцию. "
                            "Нет в списке — добавьте: ⚡ Быстрая запись → ➕ Новый продукт.", reply_markup=kb.ikb(*rows))


@router.callback_query(F.data.startswith("d:spn:"))
async def shake_pick_set(cb: CallbackQuery) -> None:
    v = cb.data.split(":")[2]
    p = await db.get_product(int(v)) if v != "x" else None
    name = p["name"] if p else "-"
    await db.update_profile(shake_product=name)
    await cb.answer("Сохранено")
    text, markup = await settings_text(await db.get_profile())
    await cb.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "d:prefs")
async def prefs_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Diet.prefs)
    await cb.answer()
    await cb.message.answer(
        "Напишите одним сообщением, что учитывать в разборе рациона. Например: «не ем рыбу и грибы, "
        "на завтрак люблю овсянку, обедаю в офисе — нужно что-то с собой, готовить не люблю».\n"
        "Чтобы сбросить, отправьте «нет». Отмена: /cancel")


@router.message(Diet.prefs, F.text)
async def prefs_set(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = message.text.strip()[:500]
    await db.update_profile(diet_prefs="" if text.lower() in ("нет", "-") else text)
    await message.answer("Запомнил. Учту в следующем разборе рациона (⚙️ Ещё → 🛠 Сервис → 🔁 Пересмотр рациона).")


@router.callback_query(F.data == "d:shp")
async def shp_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Diet.shake_protein)
    await cb.answer()
    await cb.message.answer("Сколько граммов белка в одной порции вашего протеина? "
                            "Смотрите на банке, обычно 20–27 г на мерную ложку. Отмена: /cancel")


@router.message(Diet.shake_protein, F.text)
async def shp_set(message: Message, state: FSMContext) -> None:
    try:
        v = float(message.text.replace(",", ".").strip())
    except ValueError:
        v = 0
    if not 10 <= v <= 60:
        return await message.answer("Введите число от 10 до 60, например 24.")
    await state.clear()
    await db.update_profile(shake_protein=v)
    text, markup = await settings_text(await db.get_profile())
    await message.answer(text, reply_markup=markup)


# ---------- мои продукты (цифры с этикетки) ----------
# Зачем: Claude угадывает состав по названию и для фирменных продуктов ошибается в разы.
# Этикетка вносится один раз, дальше продукт выбирается кнопкой и вводится только вес.

def _product_line(p: dict) -> str:
    return (f"{escape(p['name'])} — на 100 г: {p['kcal']:g} ккал, "
            f"Б {p['protein']:g} · Ж {p['fat']:g} · У {p['carbs']:g}")


async def save_product(message: Message, text: str) -> None:
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
        p = await llm.parse_product(text)
    except llm.LLMError as e:
        await message.answer(str(e))
        return
    name = normalize(p.name)
    if not p.ok or not name or p.kcal <= 0:
        await message.answer("Не нашёл калорийности. Скопируйте таблицу с этикетки или напишите, например: "
                             "«Hi Tec Protein, на 100 г 378 ккал, белок 79».")
        return
    pid = await db.add_product(name, round(p.kcal, 1), round(p.protein, 1), round(p.fat, 1), round(p.carbs, 1))
    await message.answer(
        "📦 Запомнил: " + _product_line({**p.model_dump(), "name": name}) +
        "\nЗаписывать: ⚡ Быстрая запись → выбрать → ввести вес.",
        reply_markup=kb.ikb([(f"➕ Записать сейчас", f"pr:use:{pid}")]))


QUICK_LIMIT = 14


async def quick_products() -> list[dict]:
    """Последние съеденные продукты из базы, без повторов, плюс закреплённые (этикетка, штрихкод)."""
    rows = await db.foods()
    idx = foodbase.index(rows)
    out, seen = [], set()
    for name in await db.recent_food_names():
        p = foodbase.lookup(name, idx)
        if p and p["id"] not in seen:
            seen.add(p["id"])
            out.append(p)
        if len(out) >= QUICK_LIMIT:
            break
    out += [p for p in rows if p["pinned"] and p["id"] not in seen]
    return out


async def quick_screen(message: Message) -> None:
    """«⚡ Быстрая запись»: последние продукты (цифры на 100 г уже известны) → выбрать → ввести граммы."""
    prods = await quick_products()
    rows = [[(f"{p['name'][:28]} · {p['kcal']:g} ккал · Б {p['protein']:g}", f"pr:use:{p['id']}")] for p in prods]
    rows.append([("🥚 Базовые продукты: яйца, творог, крупы…", "bf:list")])
    last = [("➕ Новый продукт", "pr:add"), ("🍽 По шагам", "st:go")]
    if await db.products():
        last.append(("🗑 Удалить продукт", "pr:manage"))
    rows.append(last)
    rows.append([("🏋️ Тренировка — калории с часов", "wo:ask")])
    if prods:
        text = ("<b>⚡ Быстрая запись</b>\nВыберите продукт — останется ввести граммы. "
                "Цифры на 100 г; свежие сверху.\n📷 фото штрихкода — найду продукт и запомню")
    else:
        text = ("<b>⚡ Быстрая запись</b>\nЗдесь будут продукты, которые вы ели: выбрать и ввести граммы.\n"
                "Пока пусто: запишите еду текстом или голосом, пришлите фото штрихкода или добавьте продукт с упаковки.")
    await message.answer(text, reply_markup=kb.ikb(*rows))


async def products_screen(message: Message, manage: bool = False) -> None:
    if not manage:
        await quick_screen(message)
        return
    prods = await db.products()
    rows = [[(f"🗑 {p['name'][:30]}", f"pr:del:{p['id']}")] for p in prods]
    rows.append([("← Назад", "s:products")])
    await message.answer("Какой продукт удалить?\n" + "\n".join("• " + _product_line(p) for p in prods),
                         reply_markup=kb.ikb(*rows))


async def products_msg(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.text in kb.OLD_QUICK:
        await message.answer("«Мои продукты» и «Повторить» объединены в «⚡ Быстрая запись» — там ваши последние продукты.", reply_markup=kb.main_menu)
    await quick_screen(message)


@router.callback_query(F.data == "s:products")
async def products_list(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await quick_screen(cb.message)


@router.callback_query(F.data == "pr:manage")
async def products_manage(cb: CallbackQuery) -> None:
    await cb.answer()
    await products_screen(cb.message, manage=True)


@router.callback_query(F.data.startswith("pr:use:"))
async def product_use(cb: CallbackQuery, state: FSMContext) -> None:
    p = await db.get_product(int(cb.data.split(":")[2]))
    if not p:
        return await cb.answer("Продукт не найден — откройте «⚡ Быстрая запись» заново", show_alert=True)
    await cb.answer()
    await ask_grams(cb.message, state, p)


async def ask_grams(message: Message, state: FSMContext, p: dict, head: str = "") -> None:
    """Вопрос «сколько граммов»: число текстом или кнопки — вся упаковка и прошлые порции."""
    await state.set_state(Diet.grams)
    await state.update_data(product=p["id"])
    rows = []
    if p.get("pack"):
        rows.append([(f"📦 Вся упаковка · {p['pack']:g} г", f"pr:g:{p['id']}:{p['pack']:g}")])
    recent = [g for g in await db.recent_grams(p["name"]) if g != p.get("pack")]
    if recent:
        rows.append([(f"{g:g} г", f"pr:g:{p['id']}:{g:g}") for g in recent])
    await message.answer(head + f"Сколько граммов «{escape(p['name'])}»? Отправьте число.\n"
                         f"<i>на 100 г: {p['kcal']:g} ккал, Б {p['protein']:g} · Ж {p['fat']:g} · У {p['carbs']:g}</i>"
                         + (" Или выберите:" if rows else ""),
                         reply_markup=kb.ikb(*rows) if rows else None)


async def _record_product(message: Message, profile, pid: int, grams: float) -> None:
    p = await db.get_product(pid)
    if not p:
        await message.answer("Этот продукт уже удалён.")
        return
    mid = await db.add_meal([products.item(p, grams)], source="product", raw_text=f"{p['name']} {grams:g} г")
    await db.touch_products([pid])   # из нескольких упаковок с одним названием по тексту берётся последняя
    note = {"barcode": "по штрихкоду", "label": "по этикетке"}.get(p["source"], "по базе продуктов")
    await common.send_meal(message, profile, mid, note)


@router.callback_query(F.data.startswith("pr:g:"))
async def product_quick(cb: CallbackQuery, state: FSMContext, profile) -> None:
    _, _, pid, g = cb.data.split(":")
    p = await db.get_product(int(pid))
    await state.clear()
    await cb.answer()
    if not p:
        return await cb.message.answer("Этот продукт уже удалён.")
    await _record_product(cb.message, profile, p["id"], float(g))


@router.message(Diet.grams, F.text)
async def product_grams(message: Message, state: FSMContext, profile) -> None:
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)", message.text)
    g = float(m.group(1).replace(",", ".")) if m else 0
    if not 1 <= g <= 2000:
        return await message.answer("Введите вес в граммах числом, например 30. Отмена: /cancel")
    pid = (await state.get_data())["product"]
    await state.clear()
    await _record_product(message, profile, pid, g)


@router.callback_query(F.data == "pr:add")
async def product_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Diet.product)
    await cb.answer()
    await cb.message.answer(
        "Проще всего — пришлите фото штрихкода или таблицы пищевой ценности.\n"
        "Или напишите название и скопируйте таблицу с этикетки (можно на любом языке), например:\n"
        "«Hi Tec Protein» + таблица пищевой ценности.\n"
        "Подойдёт и коротко: «творог Piątnica, на 100 г 98 ккал, белок 17». Отмена: /cancel")


@router.message(Diet.product, F.text)
async def product_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await save_product(message, message.text)


@router.callback_query(F.data.startswith("pr:del:"))
async def product_del(cb: CallbackQuery) -> None:
    await db.delete_product(int(cb.data.split(":")[2]))
    await cb.answer("Удалено")
    await cb.message.delete()
    await products_screen(cb.message)
