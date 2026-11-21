"""Замеры раз в неделю: обхваты лентой и складки калипером → % жира, жировая и «сухая» масса.

% жира — Джексон–Поллок по 3 складкам (мужчины: грудь, живот, бедро; женщины: трицепс, над подвздошной
костью, бедро) и формула Сири. Абсолютная точность калипера ±3–4%, но динамика у одного человека,
одним калипером и в одних точках — надёжная: именно её и показываем.
"""
from __future__ import annotations

from dataclasses import dataclass

INTERVAL_DAYS = 7


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    unit: str            # см — лента, мм — калипер
    hint: str


GIRTHS = [
    Site("waist", "Талия", "см", "на уровне пупка, на спокойном выдохе, живот не втягивать"),
    Site("chest", "Грудь", "см", "по самым выступающим точкам, лента под мышками, горизонтально"),
    Site("hips", "Бёдра", "см", "по самой широкой части ягодиц, ноги вместе"),
    Site("thigh", "Бедро", "см", "правое, посередине между пахом и коленом, нога расслаблена"),
    Site("arm", "Рука", "см", "правая, расслабленная, по самой широкой части бицепса"),
]
FOLDS = {
    "m": [
        Site("sf_chest", "Складка на груди", "мм", "диагональная, посередине между подмышкой и соском"),
        Site("sf_abdomen", "Складка на животе", "мм", "вертикальная, в 2 см справа от пупка"),
        Site("sf_thigh", "Складка на бедре", "мм", "вертикальная, спереди, посередине между пахом и коленом"),
    ],
    "f": [
        Site("sf_triceps", "Складка на трицепсе", "мм", "вертикальная, сзади плеча, посередине между плечом и локтем"),
        Site("sf_suprailiac", "Складка над тазовой костью", "мм", "диагональная, сразу над гребнем тазовой кости сбоку"),
        Site("sf_thigh", "Складка на бедре", "мм", "вертикальная, спереди, посередине между пахом и коленом"),
    ],
}
LIMITS = {"см": (15, 200), "мм": (2, 80)}


def sites(sex: str) -> list[Site]:
    return GIRTHS + FOLDS.get(sex, FOLDS["m"])


def name_of(key: str) -> str:
    return next((s.name for s in GIRTHS + FOLDS["m"] + FOLDS["f"] if s.key == key), key)


def body_fat(sex: str, age: int, vals: dict[str, float]) -> float | None:
    folds = [vals.get(s.key) for s in FOLDS.get(sex, FOLDS["m"])]
    if not all(folds):
        return None
    s = sum(folds)
    if sex == "f":
        bd = 1.0994921 - 0.0009929 * s + 0.0000023 * s * s - 0.0001392 * age
    else:
        bd = 1.10938 - 0.0008267 * s + 0.0000016 * s * s - 0.0002574 * age
    return round(495 / bd - 450, 1)


def _delta(a: float, b: float, unit: str) -> str:
    d = a - b
    return "±0" if abs(d) < 0.05 else f"{d:+.1f}"


def report(profile, sessions: list[tuple[str, dict]], weight: float | None) -> str:
    """sessions — [(дата, {ключ: значение})] по возрастанию; последний — текущий замер."""
    day, cur = sessions[-1]
    prev = sessions[-2][1] if len(sessions) > 1 else None
    first_day, first = sessions[0]
    lines = [f"📏 <b>Замеры {day[8:10]}.{day[5:7]}</b>"]
    for s in sites(profile["sex"]):
        if s.key not in cur:
            continue
        line = f"{s.name}: <b>{cur[s.key]:g} {s.unit}</b>"
        if prev and s.key in prev:
            line += f" ({_delta(cur[s.key], prev[s.key], s.unit)} к прошлому"
            if len(sessions) > 2 and s.key in first:
                line += f", {_delta(cur[s.key], first[s.key], s.unit)} с начала"
            line += ")"
        lines.append(line)
    bf = body_fat(profile["sex"], profile["age"], cur)
    if bf is not None:
        lines.append("")
        line = f"🧈 Жир по калиперу: <b>{bf:g}%</b>"
        if weight:
            fat, lean = weight * bf / 100, weight * (1 - bf / 100)
            line += f" · жир {fat:.1f} кг, без жира {lean:.1f} кг (при весе {weight:.1f})"
        lines.append(line)
        old = [(d, body_fat(profile["sex"], profile["age"], v)) for d, v in sessions[:-1]]
        old = [(d, b) for d, b in old if b is not None]
        if old:
            lines.append(f"<i>Было {old[-1][1]:g}% ({old[-1][0][8:10]}.{old[-1][0][5:7]}) → {bf - old[-1][1]:+.1f}%. "
                         "Калипер врёт в абсолюте на ±3%, но динамику показывает честно.</i>")
    elif any(k.startswith("sf_") for k in cur):
        lines.append("\n<i>% жира считаю, только когда записаны все 3 складки.</i>")
    return "\n".join(lines)


def weekly_line(profile, sessions: list[tuple[str, dict]]) -> str | None:
    """Одна строка для итогов недели: талия и % жира к прошлому замеру."""
    if len(sessions) < 2:
        return None
    (_, a), (_, b) = sessions[-2], sessions[-1]
    parts = []
    if "waist" in a and "waist" in b:
        parts.append(f"талия {a['waist']:g} → {b['waist']:g} см ({_delta(b['waist'], a['waist'], 'см')})")
    fa, fb = body_fat(profile["sex"], profile["age"], a), body_fat(profile["sex"], profile["age"], b)
    if fa is not None and fb is not None:
        parts.append(f"жир {fa:g} → {fb:g}%")
    return "📏 Замеры: " + ", ".join(parts) if parts else None
