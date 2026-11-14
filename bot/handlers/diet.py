"""Меню на день: утренняя рассылка, /menu, отметка съеденного, настройки диеты и протеина."""
import re
import secrets
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, keyboards as kb
from ..services import llm, products
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


def dislikes_of(profile) -> list[str]:
    return [x for x in (profile["diet_dislikes"] or "").split(", ") if x]


def _clean(name: str) -> str:
    return re.sub(r"\s*\(.*?\)", "", name).strip().lower()


def _is_shake(item: dict) -> bool:
    return "протеин" in item["name"].lower()


async def generate(profile) -> dict:
    day = db.today()
    tot = await db.day_totals(day)
    kcal = profile["budget"] - tot["kcal"]
    if kcal < 150:
        raise llm.LLMError("Бюджет на сегодня почти исчерпан, меню составлять уже не на что.")
    protein = max(0, profile["protein_goal"] - tot["protein"])
    yday = await db.get_menu(db.days_ago(1))
    avoid = [i["name"] for m in yday["meals"] for i in m["items"]] if yday else []
    sh = await shake_item(profile)
    parsed = await llm.day_menu(
        kcal=kcal, protein=protein, prefs=profile["diet_prefs"] or "", shakes=profile["shake_servings"],
        shake_protein=sh["protein"], shake_kcal=sh["kcal"], avoid=avoid,
        hour=db.now().hour, eaten_kcal=tot["kcal"],
        dislikes=dislikes_of(profile), likes=await db.top_foods())
    if not parsed.meals:
        raise llm.LLMError("Не получилось составить меню, попробуйте ещё раз.")
    data = {"meals": [m.model_dump() for m in parsed.meals], "tip": parsed.tip, "eaten": [],
            "v": secrets.token_hex(2),  # версия меню: кнопки старых сообщений после «заново» не сработают
            "kcal": round(kcal), "protein": round(protein)}
    await db.save_menu(day, data)
    return data


def menu_text(data: dict) -> str:
    lines = [f"🥗 <b>Меню на сегодня</b> (план: {fmt(data['kcal'])} ккал, белок от {data['protein']} г)"]
    tk = tp = 0
    for idx, m in enumerate(data["meals"]):
        k, p = sum(i["kcal"] for i in m["items"]), sum(i["protein"] for i in m["items"])
        tk, tp = tk + k, tp + p
        mark = "✅ " if idx in data["eaten"] else ""
        lines.append(f"\n{mark}<b>{escape(m['title'])}</b> — {fmt(k)} ккал, белок {fmt(p)} г")
        lines += [f"• {escape(i['name'])} {fmt(i['grams'])} г — {fmt(i['kcal'])}" for i in m["items"]]
    lines.append(f"\nИтого: <b>{fmt(tk)} ккал</b>, белок {fmt(tp)} г")
    if data.get("tip"):
        lines.append(f"💡 {escape(data['tip'])}")
    lines.append("<i>Съели по плану — нажмите кнопку, запишу сам. Съели другое — просто напишите.</i>")
    return "\n".join(lines)


def menu_kb(data: dict, day: str):
    v = data.get("v", "0")
    rows = [[(f"✅ Съел: {m['title']}"[:28], f"mn:eat:{day}:{v}:{i}"), ("🔁 Заменить", f"mn:sw:{day}:{v}:{i}")]
            for i, m in enumerate(data["meals"]) if i not in data["eaten"]]
    rows.append([("🔄 Всё меню заново", "mn:new"), ("⚙️ Настроить", "s:diet")])
    return kb.ikb(*rows)


async def send_menu(bot: Bot, chat_id: int, profile, regenerate: bool = False) -> None:
    day = db.today()
    data = None if regenerate else await db.get_menu(day)
    if data is None:
        await bot.send_chat_action(chat_id, "typing")
        data = await generate(profile)
    await bot.send_message(chat_id, menu_text(data), reply_markup=menu_kb(data, day))


async def _show(message: Message, profile, regenerate: bool = False) -> None:
    try:
        await send_menu(message.bot, message.chat.id, profile, regenerate)
    except llm.LLMError as e:
        await message.answer(str(e))


@router.message(Command("menu"))
async def menu_cmd(message: Message, state: FSMContext, profile) -> None:
    await state.clear()
    await _show(message, profile)


@router.callback_query(F.data == "mn:show")
async def menu_show(cb: CallbackQuery, profile) -> None:
    await cb.answer()
    await _show(cb.message, profile)


@router.callback_query(F.data == "mn:new")
async def menu_new(cb: CallbackQuery, profile) -> None:
    await cb.answer("Составляю другое…")
    await _show(cb.message, profile, regenerate=True)


@router.callback_query(F.data.startswith("mn:eat:"))
async def menu_eat(cb: CallbackQuery, profile) -> None:
    _, _, day, v, idx = cb.data.split(":")
    idx = int(idx)
    data = await db.get_menu(day)
    if day != db.today() or not data or data.get("v", "0") != v or idx >= len(data["meals"]):
        return await cb.answer("Это меню уже неактуально, откройте /menu", show_alert=True)
    if idx in data["eaten"]:
        return await cb.answer("Уже записано")
    meal = data["meals"][idx]
    data["eaten"].append(idx)
    await db.save_menu(day, data)
    mid = await db.add_meal(meal["items"], source="menu", title=meal["title"],
                            raw_text=", ".join(i["name"] for i in meal["items"]))
    await cb.answer("Записал")
    await cb.message.edit_text(menu_text(data), reply_markup=menu_kb(data, day))
    await common.send_meal(cb.message, profile, mid)


async def _menu_for(cb: CallbackQuery, day: str, v: str, idx: int) -> dict | None:
    data = await db.get_menu(day)
    if (day != db.today() or not data or data.get("v", "0") != v
            or idx >= len(data["meals"]) or idx in data["eaten"]):
        await cb.answer("Это меню уже неактуально, откройте /menu", show_alert=True)
        return None
    return data


@router.callback_query(F.data.startswith("mn:sw:"))
async def swap_ask(cb: CallbackQuery) -> None:
    """Один вопрос в момент замены: что именно не нравится. Ответ запоминается навсегда."""
    _, _, day, v, idx = cb.data.split(":")
    idx = int(idx)
    data = await _menu_for(cb, day, v, idx)
    if not data:
        return
    meal = data["meals"][idx]
    rows = [[(f"👎 Не ем: {_clean(i['name'])}"[:40], f"mn:dl:{day}:{v}:{idx}:{n}")]
            for n, i in enumerate(meal["items"]) if not _is_shake(i)]
    rows.append([("🔁 Всё ем, просто другое", f"mn:dl:{day}:{v}:{idx}:x"), ("← Назад", f"mn:back:{day}")])
    await cb.answer()
    await cb.message.edit_text(
        menu_text(data) + f"\n\n❓ <b>Что не так в «{escape(meal['title'])}»?</b> "
        "Что не едите — запомню и больше не предложу.", reply_markup=kb.ikb(*rows))


@router.callback_query(F.data.startswith("mn:back:"))
async def swap_back(cb: CallbackQuery) -> None:
    day = cb.data.split(":")[2]
    data = await db.get_menu(day)
    await cb.answer()
    if data:
        await cb.message.edit_text(menu_text(data), reply_markup=menu_kb(data, day))


@router.callback_query(F.data.startswith("mn:dl:"))
async def swap_do(cb: CallbackQuery, profile) -> None:
    _, _, day, v, idx, item = cb.data.split(":")
    idx = int(idx)
    data = await _menu_for(cb, day, v, idx)
    if not data:
        return
    meal = data["meals"][idx]
    dislikes = dislikes_of(profile)
    if item != "x" and int(item) < len(meal["items"]):
        name = _clean(meal["items"][int(item)]["name"])
        if name not in dislikes:
            dislikes = (dislikes + [name])[-40:]
            await db.update_profile(diet_dislikes=", ".join(dislikes))
    await cb.answer("Подбираю замену…")
    shakes = [i for i in meal["items"] if _is_shake(i)]
    rest = [i for i in meal["items"] if not _is_shake(i)]
    try:
        new = await llm.replace_meal(
            meal["title"], sum(i["kcal"] for i in rest), sum(i["protein"] for i in rest),
            [i["name"] for i in rest], dislikes, profile["diet_prefs"] or "")
    except llm.LLMError as e:
        await cb.message.answer(str(e))
        return
    data["meals"][idx] = {"title": meal["title"], "items": [i.model_dump() for i in new.items] + shakes}
    await db.save_menu(day, data)
    await cb.message.edit_text(menu_text(data), reply_markup=menu_kb(data, day))


@router.callback_query(F.data == "d:cleardl")
async def clear_dislikes(cb: CallbackQuery) -> None:
    await db.update_profile(diet_dislikes="")
    await cb.answer("Список очищен")
    text, markup = await settings_text(await db.get_profile())
    await cb.message.edit_text(text, reply_markup=markup)


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
        f"Предпочтения: {escape(profile['diet_prefs']) or 'не заданы'}\n"
        f"Не предлагаю: {escape(profile['diet_dislikes']) or 'пока пусто (отмечается кнопкой «🔁 Заменить» под меню)'}\n"
        f"Утреннее меню: {'включено, приходит в ' + profile['wake_time'] if profile['menu_enabled'] else 'выключено'}"
    )
    markup = kb.ikb(
        [("✍️ Предпочтения в еде", "d:prefs")] + ([("🗑 Очистить «не ем»", "d:cleardl")] if profile["diet_dislikes"] else []),
        [("🥤 0 порций", "d:sh:0"), ("1 порция", "d:sh:1"), ("2 порции", "d:sh:2")],
        [("🥤 Какой протеин", "d:sp"), ("⚖️ Белка в порции", "d:shp")],
        [
         ("🔕 Выключить меню" if profile["menu_enabled"] else "🔔 Включить меню", "d:toggle")],
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


@router.callback_query(F.data == "d:toggle")
async def toggle(cb: CallbackQuery, profile) -> None:
    await db.update_profile(menu_enabled=0 if profile["menu_enabled"] else 1)
    await cb.answer("Сохранено")
    text, markup = await settings_text(await db.get_profile())
    await cb.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "d:sp")
async def shake_pick(cb: CallbackQuery) -> None:
    await cb.answer()
    prods = await db.products()
    rows = [[(p["name"][:40], f"d:spn:{p['id']}")] for p in prods]
    rows.append([("Без этикетки (оценка)", "d:spn:x")])
    await cb.message.answer("Какой протеин вы пьёте? Его этикетка пойдёт в меню. "
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
        "Напишите одним сообщением, что учитывать в меню. Например: «не ем рыбу и грибы, на завтрак люблю "
        "овсянку, 3 приёма пищи, обедаю в офисе — нужно что-то с собой, готовить не люблю».\n"
        "Чтобы сбросить, отправьте «нет». Отмена: /cancel")


@router.message(Diet.prefs, F.text)
async def prefs_set(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = message.text.strip()[:500]
    await db.update_profile(diet_prefs="" if text.lower() in ("нет", "-") else text)
    await message.answer("Запомнил. Учту в следующем меню; обновить сегодняшнее: /menu → «🔄 Всё меню заново».")


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


async def quick_screen(message: Message) -> None:
    """«⚡ Быстрая запись»: продукты с этикеткой (выбрать → ввести вес) и повтор недавнего (одно нажатие)."""
    prods = await db.products()
    recent = await db.recent_meals(limit=6)
    if not prods and not recent:
        await message.answer(
            "<b>⚡ Быстрая запись</b>\n"
            "Здесь будут:\n"
            "• 📦 ваши продукты с цифрами с этикетки — выбрать и ввести вес;\n"
            "• 🕘 то, что вы уже записывали, — повтор в одно нажатие.\n\n"
            "Пока пусто: запишите еду текстом или добавьте продукт с упаковки.",
            reply_markup=kb.ikb([("➕ Новый продукт", "pr:add")]))
        return
    rows = [[(f"📦 {p['name'][:30]} · {p['kcal']:g}/100 г", f"pr:use:{p['id']}")] for p in prods]
    rows += [[(f"🕘 {m['raw_text'][:30]} · {fmt(m['kcal'])}", f"rep:{m['id']}")] for m in recent]
    last = [("➕ Новый продукт", "pr:add")]
    if prods:
        last.append(("🗑 Удалить продукт", "pr:manage"))
    rows.append(last)
    hint = []
    if prods:
        hint.append("📦 — выбрать продукт и ввести вес")
    if recent:
        hint.append("🕘 — записать ещё раз как было")
    await message.answer("<b>⚡ Быстрая запись</b>\n" + "\n".join(hint), reply_markup=kb.ikb(*rows))


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
        await message.answer("«Мои продукты» и «Повторить» объединены в «⚡ Быстрая запись».", reply_markup=kb.main_menu)
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
    await state.set_state(Diet.grams)
    await state.update_data(product=p["name"])
    await cb.answer()
    quick = [(f"{g:g} г", f"pr:g:{p['id']}:{g:g}") for g in await db.recent_grams(p["name"])]
    await cb.message.answer(f"Сколько граммов «{escape(p['name'])}»? Отправьте число."
                            + (" Или выберите прошлую порцию:" if quick else ""),
                            reply_markup=kb.ikb(quick) if quick else None)


async def _record_product(message: Message, profile, name: str, grams: float) -> None:
    p = next((x for x in await db.products() if x["name"] == name), None)
    if not p:
        await message.answer("Этот продукт уже удалён.")
        return
    mid = await db.add_meal([products.item(p, grams)], source="product", raw_text=f"{name} {grams:g} г")
    await common.send_meal(message, profile, mid, "по этикетке")


@router.callback_query(F.data.startswith("pr:g:"))
async def product_quick(cb: CallbackQuery, state: FSMContext, profile) -> None:
    _, _, pid, g = cb.data.split(":")
    p = await db.get_product(int(pid))
    await state.clear()
    await cb.answer()
    if not p:
        return await cb.message.answer("Этот продукт уже удалён.")
    await _record_product(cb.message, profile, p["name"], float(g))


@router.message(Diet.grams, F.text)
async def product_grams(message: Message, state: FSMContext, profile) -> None:
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)", message.text)
    g = float(m.group(1).replace(",", ".")) if m else 0
    if not 1 <= g <= 2000:
        return await message.answer("Введите вес в граммах числом, например 30. Отмена: /cancel")
    name = (await state.get_data())["product"]
    await state.clear()
    await _record_product(message, profile, name, g)


@router.callback_query(F.data == "pr:add")
async def product_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Diet.product)
    await cb.answer()
    await cb.message.answer(
        "Напишите название и скопируйте таблицу с этикетки (можно на любом языке), например:\n"
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
