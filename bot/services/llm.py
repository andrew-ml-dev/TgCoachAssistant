"""Обращения к Claude: разбор еды, оценка активности, недельный разбор, идеи."""
from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel

from .. import config, db

log = logging.getLogger(__name__)
_client: anthropic.AsyncAnthropic | None = None


class LLMError(Exception):
    """Сообщение пригодно для показа пользователю."""


class FoodItem(BaseModel):
    name: str
    grams: float
    kcal: float
    protein: float
    fat: float
    carbs: float


class FoodParse(BaseModel):
    items: list[FoodItem]
    question: str  # пустая строка, если уточнение не нужно


class MenuMeal(BaseModel):
    title: str
    items: list[FoodItem]


class DayMenu(BaseModel):
    meals: list[MenuMeal]
    tip: str


class Product(BaseModel):
    ok: bool        # false, если в тексте нет калорийности
    name: str
    kcal: float     # всё на 100 г
    protein: float
    fat: float
    carbs: float


PRODUCT_SYSTEM = """Пользователь диктует данные с этикетки продукта. Извлеки название (как написал пользователь, \
строчными буквами, без слов «продукт», «запомни») и пищевую ценность на 100 г: ккал, белки, жиры, углеводы. \
Если цифры даны на порцию другого размера — пересчитай на 100 г. Если жиры или углеводы не названы — \
оцени их по типу продукта так, чтобы сходилась калорийность. Если в тексте нет ни калорий, ни белка — ok=false."""

FOOD_SYSTEM = """Ты диетолог-нутрициолог. Пользователь описывает съеденное по-русски. \
Разбей описание на продукты и оцени для каждого массу в граммах (в готовом виде), калории, белки, жиры, углеводы.

Правила:
- Если порция не указана, бери типовую домашнюю порцию (тарелка супа 300 г, кусок хлеба 30 г, \
котлета 100 г, стакан 250 мл, столовая ложка масла 15 г) и не переспрашивай.
- Учитывай способ приготовления и скрытые калории: масло при жарке, заправки, сахар в напитках.
- При сомнении оценивай калорийность ближе к верхней границе: люди чаще недооценивают съеденное.
- Названия продуктов короткие, по-русски, со строчной буквы.
- Поле question заполняй только если без ответа оценка может ошибиться более чем вдвое \
(например «поел в ресторане» без состава). В этом случае items оставь пустым. Иначе question — пустая строка.
- Если текст вообще не про еду, верни пустой items и question с просьбой описать еду."""

MENU_SYSTEM = """Ты диетолог. Составь меню для человека, который худеет и хочет сохранить мышцы.

Требования:
- Сумма калорий всех приёмов пищи — в пределах ±5% от заданной. Белка — не меньше заданного. \
Перед ответом пересчитай суммы и поправь порции, если не сходится.
- Простые продукты из обычного супермаркета, готовка не дольше 20 минут.
- Сытно при малой калорийности: нежирный белок, много овощей, клетчатка; минимум масла и сахара.
- Каждый продукт — отдельная позиция: название со строчной буквы, масса в граммах в готовом виде, ккал, белки, жиры, углеводы. \
Для круп и макарон допиши в названии сухой вес, например «гречка (60 г сухой)».
- Число приёмов пищи и их время бери из предпочтений пользователя: если он ест 2 раза в день или держит \
интервальное голодание — ровно столько приёмов, без перекусов, и весь объём калорий и белка распредели между ними. \
Если ничего не сказано — 3–4 приёма. Если указано, что часть дня прошла, планируй только оставшиеся.
- title приёма пищи — короткое название: Завтрак, Обед, Ужин, Перекус; если пользователь назвал время, добавь его, например «Обед, 12:00».
- Протеиновый коктейль включи ровно в указанном количестве порций, позицией «протеиновый коктейль» \
с точно указанными белком и калориями на порцию. Если порций 0 — не включай.
- Белка — не меньше заданного, но и не больше чем на 15% сверх: лишний белок съедает калории, нужные для сытности.
- Строго соблюдай предпочтения и ограничения пользователя. Не повторяй вчерашние блюда.
- tip — один практический совет на день, не длиннее 15 слов."""

COACH_SYSTEM = """Ты тренер по снижению веса. По данным за неделю дай короткий разбор по-русски: \
1–2 наблюдения о паттернах и 2–3 конкретных совета на следующую неделю. \
Без воды, морализаторства и общих фраз, не более 120 слов, обычный текст без разметки. \
Если темп потери выше 1.5% веса в неделю или калорий меньше 1200 — прямо скажи, что это чересчур."""

def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise LLMError("Не задан ANTHROPIC_API_KEY. Можно вносить калории числом: «450 ккал».")
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def _check_budget() -> None:
    used = await db.usage_month()
    if used["cost"] >= config.MONTHLY_LLM_BUDGET:
        raise LLMError(
            f"Месячный лимит API (${config.MONTHLY_LLM_BUDGET:.2f}) исчерпан. "
            "Вносите калории числом: «450 ккал обед»."
        )


async def _call(kind: str, system: str, user: str, schema=None, max_tokens: int = 4000):
    await _check_budget()
    client = _get_client()
    kwargs = dict(
        model=config.MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "low"},
    )
    try:
        if schema is not None:
            resp = await client.messages.parse(output_format=schema, **kwargs)
        else:
            resp = await client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        raise LLMError("Ключ Anthropic API не принят. Проверьте ANTHROPIC_API_KEY в .env.")
    except anthropic.RateLimitError:
        raise LLMError("Claude сейчас перегружен, попробуйте через минуту.")
    except anthropic.APIStatusError as e:
        log.error("Claude API %s: %s", e.status_code, e.message)
        raise LLMError(f"Ошибка Claude API ({e.status_code}). Попробуйте позже или внесите калории числом.")
    except anthropic.APIConnectionError:
        raise LLMError("Нет связи с Claude API. Попробуйте позже или внесите калории числом.")

    await db.add_usage(kind, resp.usage.input_tokens, resp.usage.output_tokens)
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise LLMError("Не получилось разобрать сообщение. Попробуйте описать иначе.")
    if schema is not None:
        if resp.parsed_output is None:
            raise LLMError("Не получилось разобрать сообщение. Попробуйте описать иначе.")
        return resp.parsed_output
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def parse_food(text: str, allow_question: bool = True, products: list[dict] = ()) -> FoodParse:
    user = text if allow_question else text + "\n\n(Уточнений не задавай, дай лучшую оценку.)"
    if products:
        user += ("\n\nДанные с этикеток пользователя (на 100 г). Если продукт из этого списка есть в описании, "
                 "назови его точно так же и считай по этим цифрам:\n" + "\n".join(
                     f"- {p['name']}: {p['kcal']:g} ккал, Б {p['protein']:g}, Ж {p['fat']:g}, У {p['carbs']:g}"
                     for p in products))
    return await _call("food", FOOD_SYSTEM, user, FoodParse)


async def parse_product(text: str) -> Product:
    return await _call("product", PRODUCT_SYSTEM, text, Product, 2000)


async def correct_food(items: list[dict], correction: str) -> FoodParse:
    lines = "\n".join(f"- {i['name']}, {i['grams']:.0f} г, {i['kcal']:.0f} ккал" for i in items)
    user = (f"Текущий разбор приёма пищи:\n{lines}\n\nПоправка пользователя: {correction}\n\n"
            "Верни исправленный полный список. Уточнений не задавай.")
    return await _call("food_fix", FOOD_SYSTEM, user, FoodParse)


async def weekly_review(summary: str) -> str:
    return await _call("weekly", COACH_SYSTEM, summary, max_tokens=3000)


async def day_menu(kcal: float, protein: float, prefs: str, shakes: int, shake_protein: float,
                   shake_kcal: float, avoid: list[str], hour: int, eaten_kcal: float,
                   dislikes: list[str] = (), likes: list[str] = ()) -> DayMenu:
    parts = [f"Калории на меню: {kcal:.0f} ккал. Белок: не меньше {protein:.0f} г."]
    if eaten_kcal > 0 or hour >= 12:
        parts.append(f"Сейчас {hour}:00, сегодня уже съедено {eaten_kcal:.0f} ккал — это меню только на остаток дня.")
    parts.append(f"Протеиновый коктейль: {shakes} порц. (одна порция: {shake_protein:.0f} г белка, {shake_kcal:.0f} ккал).")
    parts.append(f"Предпочтения и ограничения: {prefs or 'нет, обычная домашняя еда'}.")
    if dislikes:
        parts.append("Никогда не предлагай (пользователь это не ест): " + ", ".join(dislikes) + ".")
    if likes:
        parts.append("Пользователь сам часто ест: " + ", ".join(likes) + ". Опирайся на привычные ему продукты.")
    if avoid:
        parts.append("Вчера было: " + ", ".join(avoid[:25]) + ".")
    return await _call("menu", MENU_SYSTEM, "\n".join(parts), DayMenu, max_tokens=8000)


async def replace_meal(title: str, kcal: float, protein: float, old: list[str],
                       dislikes: list[str], prefs: str) -> MenuMeal:
    parts = [
        f"Замени один приём пищи в меню: {title}. Было: {', '.join(old)}.",
        f"Нужен другой вариант на {kcal:.0f} ккал (±5%) и не меньше {protein:.0f} г белка, из других основных продуктов.",
        "Протеиновый коктейль не добавляй.",
        f"Предпочтения и ограничения: {prefs or 'нет'}.",
    ]
    if dislikes:
        parts.append("Никогда не предлагай (пользователь это не ест): " + ", ".join(dislikes) + ".")
    return await _call("menu_swap", MENU_SYSTEM, "\n".join(parts), MenuMeal, max_tokens=4000)
