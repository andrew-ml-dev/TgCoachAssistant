"""Классификатор входящего текста без обращения к API."""
from __future__ import annotations

import re
from dataclasses import dataclass

NUM = r"(\d{1,4}(?:[.,]\d{1,2})?)"
RE_WEIGHT = re.compile(rf"^(вес\s*)?{NUM}\s*(кг)?$", re.I)
RE_KCAL_FIRST = re.compile(r"^(\d{1,4})\s*(?:ккал|kcal|кк|кал)\.?\s*(.*)$", re.I)
RE_KCAL_LAST = re.compile(r"^(.+?)\s+(\d{1,4})\s*(?:ккал|kcal|кк|кал)\.?$", re.I)
# Тренировка: ключевое слово + калории с часов. «трен 700», «зал + дорожка 650 ккал», «бег 30 мин 400», «700 тренировка»
KW = r"(?:трен\w*|зал|бег|бегов\w*|дорожк\w*|кардио|силов\w*|сжё?г\w*|сжег\w*|сожг\w*|workout|gym)\b"
UNIT = r"(?:ккал|kcal|кк|кал)?\.?"
MID = r"(?:[^\d]|\d{1,3}(?:[.,]\d+)?\s*(?:мин\w*|км|ч|час\w*)\b)*?"  # «30 мин», «5 км», «1 час» не путать с ккал
RE_WORKOUT = re.compile(rf"^({KW}{MID})[\s:=-]*(\d{{2,4}})\s*{UNIT}$", re.I)
RE_WORKOUT_ONLY = re.compile(rf"^{KW}{MID}$", re.I)
RE_WORKOUT_LAST = re.compile(rf"^(\d{{2,4}})\s*{UNIT}\s+({KW}.*)$", re.I)
RE_STEPS = re.compile(r"^(?:шаги?\s*)?(\d[\d\s]{2,6})\s*(?:шаг\w*)?$", re.I)


@dataclass
class Parsed:
    kind: str            # weight | kcal | steps | workout | ambiguous | food
    value: float = 0
    label: str = ""


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def classify(text: str, last_weight: float | None = None) -> Parsed:
    t = text.strip()
    low = t.lower()

    if "шаг" in low:
        m = RE_STEPS.match(low)
        if m:
            return Parsed("steps", int(m.group(1).replace(" ", "")))

    m = RE_WORKOUT.match(t)
    if m and 30 <= int(m.group(2)) <= 3000:
        return Parsed("workout", int(m.group(2)), m.group(1).strip(" :=-+,"))
    m = RE_WORKOUT_LAST.match(t)
    if m and 30 <= int(m.group(1)) <= 3000:
        return Parsed("workout", int(m.group(1)), m.group(2).strip())
    if RE_WORKOUT_ONLY.match(t) and len(t) <= 40:
        return Parsed("workout", 0, t)  # без цифры — спросить калории, а не отправлять в разбор еды

    m = RE_KCAL_FIRST.match(t)
    if m:
        return Parsed("kcal", int(m.group(1)), m.group(2).strip())
    m = RE_KCAL_LAST.match(t)
    if m:
        return Parsed("kcal", int(m.group(2)), m.group(1).strip())

    m = RE_WEIGHT.match(low)
    if m:
        explicit = bool(m.group(1) or m.group(3))
        v = _num(m.group(2))
        has_decimal = "." in m.group(2) or "," in m.group(2)
        if explicit or has_decimal:
            if 30 <= v <= 300:
                return Parsed("weight", v)
            return Parsed("food", label=t)
        # целое число без единиц
        if last_weight is not None and abs(v - last_weight) <= 5:
            return Parsed("ambiguous", v)
        if last_weight is None and 30 <= v <= 250:
            return Parsed("ambiguous", v)
        if 1 <= v <= 5000:
            return Parsed("kcal", int(v))

    return Parsed("food", label=t)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).strip(" .,!")


def parse_time(text: str) -> str | None:
    m = re.match(r"^\s*(\d{1,2})[:. ]?(\d{2})?\s*$", text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return f"{h:02d}:{mi:02d}"
    return None
