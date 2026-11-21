"""«Что съесть сейчас»: самый маленький приём пищи, который добирает белок и жиры. Локально, без Claude.

Задача — не съесть остаток калорий, а закрыть минимум белка (и нижнюю границу жиров) с наименьшими
калориями: всё недоеденное уходит в дефицит. Углеводы и гарнир — только там, где без них блюда нет
(бутерброд), овощи — фиксированная порция. Белок за день делится между оставшимися приёмами.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape

from .. import db
from . import base_foods, budget, nutrition

MEAL_CAP = 700        # больше за один приём не предлагаем, даже если так выходит по белку
MIN_MEAL = 80         # остаток меньше — только «если голодно» из овощей
FAT_PER_KG = 0.6      # нижняя граница жиров, г на кг целевого веса

BY_NAME = {b.name: (i, b) for i, b in enumerate(base_foods.FOODS)}


@dataclass(frozen=True)
class Part:
    food: str
    role: str            # p — белок, c — углеводы, f — жиры, v — овощи (фиксированная порция)
    lo: float            # граммы: 0 — компонент можно убрать
    hi: float
    step: float = 10
    ins: str = ""        # «с чем» для названия: «гречкой»; пусто — в название не идёт (масло для жарки)
    per: base_foods.Base | None = None   # цифры на 100 г, если продукта нет в справочнике (блюда из разбора)

    @property
    def food100(self) -> base_foods.Base:
        return self.per or BY_NAME[self.food][1]


@dataclass(frozen=True)
class Dish:
    base: str            # «Курица» → «Курица с гречкой и огурцом» из того, что вошло
    parts: tuple[Part, ...]
    before: int = 24     # предлагать только до этого часа (завтраки)
    fixed: str = ""      # готовое название (блюда из разбора рациона)
    plan: int = 0        # id разбора, 0 — справочное блюдо
    mine: bool = False   # человек уже ест это сочетание
    slot: str = "любое"


def _egg(lo, hi):
    return Part("яйцо", "p", lo * 55, hi * 55, 55)


def _oil(hi=10, food="масло растительное"):
    return Part(food, "f", 0, hi, 5)


CUKE = Part("огурец", "v", 120, 120, 120, "огурцом")
TOMATO = Part("помидор", "v", 150, 150, 150, "помидором")
RYE = Part("хлеб ржаной", "c", 0, 60, 30, "хлебом")

DISHES: list[Dish] = [
    Dish("Омлет", (_egg(2, 4), Part("масло растительное", "f", 5, 5, 5), RYE, TOMATO)),
    Dish("Курица", (Part("куриная грудка", "p", 100, 250), _oil(), Part("гречка варёная", "c", 0, 250, 10, "гречкой"), CUKE)),
    Dish("Курица", (Part("куриная грудка", "p", 100, 250), _oil(), Part("рис варёный", "c", 0, 250, 10, "рисом"), TOMATO)),
    Dish("Курица", (Part("куриная грудка", "p", 100, 250), _oil(food="масло сливочное"),
                    Part("картофель варёный", "c", 0, 300, 50, "картошкой"), CUKE)),
    Dish("Тунец", (Part("тунец в собственном соку", "p", 65, 130, 65), _oil(),
                   Part("макароны варёные", "c", 0, 250, 10, "макаронами"), CUKE)),
    Dish("Бутерброды с тунцом", (Part("тунец в собственном соку", "p", 65, 130, 65),
                                 Part("хлеб ржаной", "c", 30, 90, 30), CUKE)),
    Dish("Творог", (Part("творог 5%", "p", 150, 300), Part("грецкие орехи", "f", 0, 20, 10, "орехами"),
                    Part("банан", "c", 0, 120, 120, "бананом"))),
    Dish("Обезжиренный творог", (Part("творог обезжиренный", "p", 150, 300),
                                 Part("сметана 15%", "f", 0, 40, 20, "сметаной"))),
    Dish("Йогурт", (Part("йогурт натуральный", "p", 150, 300, 50), Part("грецкие орехи", "f", 0, 20, 10, "орехами"),
                    Part("яблоко", "c", 0, 180, 180, "яблоком"))),
    Dish("Яйца варёные", (_egg(2, 4), CUKE)),
    Dish("Сыр", (Part("сыр твёрдый", "p", 20, 60, 20), Part("хлеб ржаной", "c", 30, 60, 30, "ржаным хлебом"), TOMATO)),
    Dish("Кефир", (Part("кефир 2.5%", "p", 250, 500, 250),)),
    Dish("Овсянка на молоке", (Part("овсяные хлопья (сухие)", "c", 40, 70), Part("молоко 2.5%", "p", 150, 250, 50),
                               Part("банан", "c", 0, 120, 120, "бананом")), before=13),
]


def title(dish: Dish, items: list[dict]) -> str:
    have = {x["name"] for x in items}
    if dish.fixed:
        gone = [p.food for p in dish.parts if p.role in ("c", "f") and p.food not in have]
        return dish.fixed + (" — без " + ", ".join(gone) if gone else "")
    words = [p.ins for p in dish.parts if p.ins and p.food100.name in have]
    if not words:
        return dish.base
    tail = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " и " + words[-1]
    if " с " in dish.base:                      # «Бутерброды с тунцом» + «и огурцом»
        return f"{dish.base} и {tail}"
    co = tail[0] in "сзшж" and tail[1] not in "аеёиоуыэюя"   # «со сметаной»
    return f"{dish.base} {'со' if co else 'с'} {tail}"


@dataclass
class Need:
    left: float          # ккал до конца дня
    meals_left: int
    kcal: float          # потолок на этот приём: остаток, но не больше MEAL_CAP
    protein: float       # белка на этот приём (минимум дня поровну между оставшимися приёмами)
    fat: float           # жиров на этот приём (до нижней границы)
    p_min_gap: float     # сколько не хватает до минимума белка за день
    p_goal_gap: float
    fat_gap: float


def fat_min(target_weight: float) -> int:
    return round(FAT_PER_KG * target_weight)


def meals_left(hour: int) -> int:
    return 3 if hour < 11 else 2 if hour < 16 else 1


def need(profile, eaten: dict, allow: float, hour: int, meal_hours: list[int] = ()) -> Need:
    """meal_hours — обычные часы приёмов пищи по записям (из разбора); без них — 3/2/1 по времени суток."""
    left = allow - eaten["kcal"]
    n = min(3, max(1, sum(1 for h in meal_hours if h >= hour))) if meal_hours else meals_left(hour)
    pmin, pgoal = nutrition.protein_min(profile["target_weight"]), profile["protein_goal"]
    p_min_gap = max(0.0, pmin - eaten["protein"])
    p_goal_gap = max(0.0, pgoal - eaten["protein"])
    fat_gap = max(0.0, fat_min(profile["target_weight"]) - eaten["fat"])
    return Need(left=left, meals_left=n, kcal=min(max(left, 0), MEAL_CAP), protein=p_min_gap / n,
                fat=fat_gap / n, p_min_gap=p_min_gap, p_goal_gap=p_goal_gap, fat_gap=fat_gap)


def _up(g: float, p: Part) -> float:
    """Порция не меньше нужной: вверх до шага, в пределах lo–hi."""
    g = math.ceil(g / p.step - 1e-9) * p.step
    return max(p.lo, min(p.hi, g))


def fit(dish: Dish, nd: Need) -> tuple[float, list[dict]] | None:
    """Наименьшие граммовки, закрывающие белок и жиры приёма. (оценка, позиции) или None."""
    per = {i: p.food100 for i, p in enumerate(dish.parts)}
    grams = {i: p.lo for i, p in enumerate(dish.parts)}   # овощи, обязательная основа (хлеб в бутерброде)

    def total(field):
        return sum(per[i].__getattribute__(field) * g / 100 for i, g in grams.items())

    pi = [i for i, p in enumerate(dish.parts) if p.role == "p" and per[i].protein]
    if pi:                                   # весь недостающий белок — главной белковой частью
        i = pi[0]
        rest = nd.protein - (total("protein") - per[i].protein * grams[i] / 100)
        grams[i] = _up(max(rest, 0) / (per[i].protein / 100), dish.parts[i])
    for i, p in enumerate(dish.parts):       # жиры — только если не хватает
        if p.role == "f" and p.hi > p.lo and per[i].fat:
            short = nd.fat - total("fat")
            if short >= 2:
                grams[i] = _up(short / (per[i].fat / 100), p)
    items = [base_foods.item(per[i], g) | {"part": i} for i, g in grams.items() if g > 0]
    kcal = sum(x["kcal"] for x in items)
    if kcal > nd.kcal + 30:
        return None
    prot = sum(x["protein"] for x in items)
    fat = sum(x["fat"] for x in items)
    pcov = min(prot, nd.protein) / nd.protein if nd.protein else 1
    fcov = min(fat, nd.fat) / nd.fat if nd.fat else 1
    # закрыть белок важнее всего, потом жиры; из закрывающих — меньше калорий
    score = 3 * pcov + 0.7 * fcov - kcal / 300
    return score, items


def _range(i: dict) -> tuple[float, float]:
    """Пределы порции в блюде из разбора: поправки разбора не размываются подгонкой.
    Овощи — ровно как в разборе; гарнир и жиры — только меньше; белок можно добавить до hi."""
    if i["role"] == "v":
        return i["grams"], i["grams"]
    if i["role"] in ("c", "f"):
        return i["lo"], i["grams"]
    return i["lo"], i["hi"]


def plan_dishes(plan: dict | None) -> list[Dish]:
    """Блюда из разбора рациона → Dish с цифрами из записей человека."""
    if not plan:
        return []
    out = []
    for d in plan.get("dishes", []):
        parts = tuple(Part(i["name"], i["role"], *_range(i), i["step"],
                           per=base_foods.Base(i["name"], i["kcal"], i["protein"], i["fat"], i["carbs"], ()))
                      for i in d["items"])
        out.append(Dish(d["title"], parts, fixed=d["title"], plan=plan["id"], mine=d["mine"], slot=d["slot"]))
    return out


def options(nd: Need, hour: int, plan: dict | None = None) -> list[tuple[int, Dish, list[dict]]]:
    """Подходящие блюда, лучшие первыми: свои из разбора — впереди, по времени суток — выше;
    сначала по одному блюду на каждый белковый продукт, потом остальные."""
    from .diet_profile import slot
    now_slot = slot(hour)
    res = []
    for k, dish in list(enumerate(plan_dishes(plan))) + list(enumerate(DISHES)):
        if hour >= dish.before:
            continue
        f = fit(dish, nd)
        if not f:
            continue
        score = f[0]
        if dish.plan:
            score += 0.5 + (0.3 if dish.mine else 0) + (0.3 if dish.slot in (now_slot, "любое") else 0)
        res.append((score, k, dish, f[1]))
    res.sort(key=lambda r: -r[0])
    first, rest, seen = [], [], set()
    for r in res:
        main = next((p.food100.name for p in r[2].parts if p.role == "p"), r[2].base)
        (rest if main in seen else first).append(r)
        seen.add(main)
    return [(k, dish, items) for _, k, dish, items in first + rest]


def encode(k: int, dish: Dish, items: list[dict]) -> str:
    """callback_data в 64 байта: справочное «ws:<блюдо>:<продукт>-<г>,…», из разбора «wp:<разбор>:<блюдо>:<часть>-<г>,…»."""
    if dish.plan:
        return f"wp:{dish.plan}:{k}:" + ",".join(f"{x['part']}-{x['grams']:g}" for x in items)
    return f"ws:{k}:" + ",".join(f"{BY_NAME[x['name']][0]}-{x['grams']:g}" for x in items)


def _pairs(rest: str) -> list[tuple[int, float]]:
    return [(int(i), float(g)) for i, g in (p.split("-") for p in rest.split(","))]


async def decode(data: str) -> tuple[str, list[dict]] | None:
    """Позиции для записи; None — разбор, к которому относится кнопка, уже не найти."""
    if data.startswith("wp:"):
        _, pid, k, rest = data.split(":", 3)
        dishes = plan_dishes(await db.get_plan(int(pid)))
        if int(k) >= len(dishes):
            return None
        dish = dishes[int(k)]
        items = [base_foods.item(dish.parts[i].food100, g) for i, g in _pairs(rest)]
        return title(dish, items), items
    _, k, rest = data.split(":", 2)
    items = [base_foods.item(base_foods.FOODS[i], g) for i, g in _pairs(rest)]
    return title(DISHES[int(k)], items), items


def _fmt_item(x: dict) -> str:
    if x["name"] not in BY_NAME:
        return f"{x['name']} {x['grams']:g} г"
    b = BY_NAME[x["name"]][1]
    name = b.name.split(" (")[0]
    if b.piece and x["grams"] % b.piece == 0:
        n = int(x["grams"] // b.piece)
        unit = "кус." if "хлеб" in b.name else "шт"
        return f"{name} {n} {unit}"
    return f"{name} {x['grams']:g} г"


async def build(profile, offset: int = 0) -> tuple[str, list[tuple[str, str]]]:
    """Текст и кнопки (подпись, callback) для «Что съесть?»."""
    fmt = budget.fmt
    eaten = await db.day_totals(db.today())
    allow, _, _ = await budget.day_allowance(profile)
    hour = db.now().hour
    plan = await db.last_plan()
    nd = need(profile, eaten, allow, hour, (plan or {}).get("meal_hours", ()))

    fat_ok = nd.fat_gap < 3
    if nd.p_min_gap <= 0 and fat_ok:
        lines = ["🍽 <b>Что съесть</b>", "✅ Белок и жиры на сегодня уже добраны — можно ничего не есть: "
                 f"оставшиеся {fmt(max(nd.left, 0))} ккал уйдут в дефицит."]
        if nd.p_goal_gap > 0:
            lines.append(f"До цели по белку ещё {fmt(nd.p_goal_gap)} г — если хочется, кефир 250 мл "
                         "(125 ккал, 7 г) или обезжиренный творог 100 г (85 ккал, 18 г).")
        lines.append("Голодно — огурец или помидор, 20–30 ккал.")
        return "\n".join(lines), []
    if nd.left < MIN_MEAL:
        head = ("Лимит на сегодня исчерпан" if nd.left <= 0 else f"Осталось всего {fmt(nd.left)} ккал") + \
            ". Если голодно — огурец или помидор (20–30 ккал)."
        if nd.p_min_gap > 0:
            head += f"\nДо минимума белка не хватает {fmt(nd.p_min_gap)} г — обезжиренный творог 100 г (85 ккал, 18 г белка)."
        return f"🍽 <b>Что съесть</b>\n{head}", []

    lines = ["🍽 <b>Что съесть сейчас</b> — минимум, чтобы добрать белок"]
    lack = []
    if nd.p_min_gap > 0:
        lack.append(f"белок {fmt(nd.p_min_gap)} г до минимума")
    if not fat_ok:
        lack.append(f"жиры {fmt(nd.fat_gap)} г")
    meals = "сейчас — всё" if nd.meals_left == 1 else f"сейчас ≈ {fmt(nd.protein)} г белка ({nd.meals_left} приёма до вечера)"
    if plan and plan.get("meal_hours") and nd.meals_left > 1:
        meals += ", ваш режим: " + ", ".join(f"{h}:00" for h in plan["meal_hours"])
    lines.append("Не хватает за день: " + ", ".join(lack) + f" · {meals}")
    if plan and plan.get("fixes"):
        lines.append(f"📌 На этой неделе: {escape(plan['fixes'][0]['short'])}")

    opts = options(nd, hour, plan)
    if not opts:
        lines.append("\nПод такой остаток ничего не подобрал.")
        return "\n".join(lines), []
    page = opts[offset:offset + 3] or opts[:3]
    offset = offset if opts[offset:offset + 3] else 0
    buttons = []
    for n, (k, dish, items) in enumerate(page, 1):
        kc = sum(x["kcal"] for x in items)
        p = sum(x["protein"] for x in items)
        f = sum(x["fat"] for x in items)
        c = sum(x["carbs"] for x in items)
        star = "⭐ " if dish.mine else ""
        lines += ["", f"<b>{n}. {star}{escape(title(dish, items))}</b> — {fmt(kc)} ккал · Б {fmt(p)} · Ж {fmt(f)} · У {fmt(c)}",
                  "   " + escape(", ".join(_fmt_item(x) for x in items))]
        buttons.append((f"✅ Съел {n}", encode(k, dish, items)))
    lines.append(f"\n<i>Остаток дня {fmt(nd.left)} ккал — всё, что не съедите, идёт в дефицит. "
                 "Съели по-другому — просто напишите, как обычно.</i>")
    if len(opts) > 3:
        buttons.append(("🔄 Другие", f"ws:more:{offset + 3}"))
    return "\n".join(lines), buttons
