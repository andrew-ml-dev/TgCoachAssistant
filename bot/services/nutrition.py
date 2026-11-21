"""Чистые расчёты: BMR, тренд веса, бюджет, адаптивный расход, прогноз."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

KCAL_PER_KG = 7700
TREND_ALPHA = 0.25
DEFAULT_FLOOR = {"m": 1500, "f": 1200}


def bmr(sex: str, weight: float, height_cm: float, age: int) -> float:
    """Миффлин — Сан Жеор."""
    base = 10 * weight + 6.25 * height_cm - 5 * age
    return base + 5 if sex == "m" else base - 161


def initial_tdee(sex: str, weight: float, height_cm: float, age: int, activity: float) -> int:
    return round(bmr(sex, weight, height_cm, age) * activity)


def protein_goal(target_weight: float) -> int:
    return round(target_weight * 2.0)


def protein_min(target_weight: float) -> int:
    """Нижняя граница: 1.6 г на кг целевого веса. Ниже при дефиците заметно растёт потеря мышц,
    выше польза для мышц почти не прибавляется (мета-анализ Morton et al., 2018)."""
    return round(target_weight * 1.6)


def protein_bar(p: float, low: float, goal: float, width: int = 10) -> str:
    """Шкала до цели с отметкой минимума: ▓▓▓▓░░░░┊░░"""
    filled = min(width, round(p / goal * width)) if goal else 0
    cells = ["▓"] * filled + ["░"] * (width - filled)
    cells.insert(min(width, round(low / goal * width)), "┊")
    return "".join(cells)


def protein_state(p: float, low: float, goal: float) -> str:
    if p < low:
        return f"🔴 до минимума ещё <b>{round(low - p)} г</b>"
    if p < goal:
        return f"🟡 минимум есть, до цели {round(goal - p)} г"
    return "✅ белок в норме"


@dataclass
class Budget:
    budget: int           # сколько есть в день
    wanted_deficit: int   # дефицит под желаемый темп
    floored: bool         # упёрлись в пол
    achievable_pace: float  # кг/нед при этом бюджете (без учёта активности)


def make_budget(tdee: float, pace_kg_week: float, floor: int) -> Budget:
    wanted = pace_kg_week * KCAL_PER_KG / 7
    raw = tdee - wanted
    budget = max(floor, raw)
    return Budget(
        budget=round(budget),
        wanted_deficit=round(wanted),
        floored=raw < floor,
        achievable_pace=round(max(0.0, tdee - budget) * 7 / KCAL_PER_KG, 2),
    )


def extra_activity_needed(tdee: float, budget: float, pace_kg_week: float) -> int:
    """Сколько ккал активности в день не хватает до желаемого темпа."""
    return max(0, round(pace_kg_week * KCAL_PER_KG / 7 - (tdee - budget)))


def workout_bonus(tdee: float, budget: float, pace_kg_week: float, net: float) -> int:
    """Сколько ккал тренировок дня добавить к бюджету еды.

    Сначала тренировка добирает дефицит до выбранного темпа (когда бюджет упёрся в пол, его не хватает),
    остальное — к еде, но к еде идёт не меньше половины: так тренировка всегда ощутима,
    а половина остаётся запасом на ошибку часов.
    """
    if net <= 0:
        return 0
    return round(min(net, max(net * 0.5, net - extra_activity_needed(tdee, budget, pace_kg_week))))


def compute_trends(points: list[tuple[date, float]]) -> list[float]:
    """Экспоненциальный тренд; пропуски дней учитываются степенью сглаживания."""
    trends: list[float] = []
    prev_t, prev_d = None, None
    for d, w in points:
        if prev_t is None:
            t = w
        else:
            gap = max(1, (d - prev_d).days)
            k = 1 - (1 - TREND_ALPHA) ** gap
            t = prev_t + k * (w - prev_t)
        trends.append(round(t, 2))
        prev_t, prev_d = t, d
    return trends


def slope_per_day(points: list[tuple[date, float]]) -> float | None:
    """Наклон веса методом наименьших квадратов, кг/день (отрицательный — вес падает).

    Считается по сырым взвешиваниям: в отличие от разности значений тренда,
    регрессия не запаздывает и не занижает потерю в первые недели.
    """
    if len(points) < 2:
        return None
    xs = [(d - points[0][0]).days for d, _ in points]
    ys = [w for _, w in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def adaptive_tdee(avg_kcal: float, kg_per_day: float) -> int:
    """Реальный расход = съеденное + энергия потерянного веса (kg_per_day < 0 при потере)."""
    return round(min(5000, max(1200, avg_kcal - kg_per_day * KCAL_PER_KG)))


def smooth_tdee(old: float, new: float) -> int:
    return round(0.3 * old + 0.7 * new)


def weekly_rate(points: list[tuple[date, float]], window_days: int = 21) -> float | None:
    """Темп по взвешиваниям за окно, кг/нед (отрицательный — вес падает)."""
    if len(points) < 3:
        return None
    end_d = points[-1][0]
    win = [p for p in points if (end_d - p[0]).days <= window_days]
    if len(win) < 3 or (end_d - win[0][0]).days < 5:
        return None
    slope = slope_per_day(win)
    return None if slope is None else round(slope * 7, 2)


def forecast_date(current_trend: float, target: float, rate_per_week: float | None, today: date) -> date | None:
    if rate_per_week is None or rate_per_week >= -0.05 or current_trend <= target:
        return None
    return today + timedelta(days=round((current_trend - target) / -rate_per_week * 7))


# Часы (Polar, Huawei) считают тренировку вместе с базовым обменом за это время, а он уже есть в расходе;
# пульсовые оценки к тому же склонны завышать. Поэтому в расход идёт 85% цифры с часов.
# Остальную систематическую ошибку поправит адаптивный расход: он вычитает учтённые тренировки.
WORKOUT_NET = 0.85


def workout_net(raw_kcal: float) -> int:
    return round(raw_kcal * WORKOUT_NET)


def bar(pct: float, width: int = 10) -> str:
    filled = min(width, round(pct / 100 * width))
    return "▓" * filled + "░" * (width - filled)
