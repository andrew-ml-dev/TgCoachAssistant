"""Счётчик: какими кнопками, командами и способами ввода человек пользуется. Только локально.

Нужен, чтобы решать «убрать или оставить» по фактам: через месяц видно, что не нажималось ни разу.
"""
from __future__ import annotations

from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb

# ключ → подпись; порядок — как показывать «не пользовались»
FEATURES = {
    kb.QUICK: "⚡ Быстрая запись", kb.EAT: "🍽 Что съесть?", kb.TODAY: "📊 Отчёт", kb.MORE: "⚙️ Ещё",
    "text": "еда/вес/тренировка текстом", "voice": "🎙 голосовые", "photo": "📷 штрихкод или этикетка",
    "pr:use": "📦 продукт из быстрой записи", "rep": "🕘 повтор недавнего", "bf": "🥚 базовые продукты",
    "st": "🍽 по шагам", "ws": "✅ съел вариант из «Что съесть?»", "wp": "✅ съел своё блюдо из разбора",
    "ws:more": "🔄 другие варианты", "f:can": "❓ влезет ли своё", "/can": "/can",
    "r:1": "отчёт: сегодня", "r:3": "отчёт: 3 дня", "r:7": "отчёт: неделя", "r:0": "отчёт: всё",
    "t:meals": "📋 приёмы пищи", "m:edit": "✏️ исправить запись", "m:del": "❌ удалить запись",
    "m:yday": "📅 перенести на вчера", "/undo": "/undo", "dc": "✅ / ❌ день записан",
    "s:review": "🔁 пересмотр рациона", "s:table": "📗 таблица продуктов", "xlsx": "📗 загрузка таблицы",
    "s:goal": "🎯 цель", "s:remind": "⏰ напоминания", "s:diet": "🥗 диета и протеин", "s:polar": "⌚ Polar",
    "s:cost": "💰 расход API", "s:export": "📤 экспорт CSV", "ex": "📋 файл для анализа", "/week": "/week",
}


def key(event) -> str | None:
    if isinstance(event, Message):
        if event.voice or event.audio:
            return "voice"
        if event.photo or (event.document and (event.document.mime_type or "").startswith("image/")):
            return "photo"
        if event.document and (event.document.file_name or "").lower().endswith(".xlsx"):
            return "xlsx"
        t = (event.text or "").strip()
        if not t:
            return None
        if t.startswith("/"):
            return t.split()[0].split("@")[0].lower()
        if t in FEATURES or t in (kb.CAN, kb.MENU, kb.FOOD, kb.WEIGHT, kb.OLD_TODAY, kb.OLD_PROGRESS):
            return t
        return "text"
    if isinstance(event, CallbackQuery) and event.data:
        parts = event.data.split(":")
        two = ":".join(parts[:2])
        if two in FEATURES or parts[0] in ("r", "s"):
            return two
        return parts[0] if parts[0] in FEATURES else two if len(parts) > 1 and parts[1].isalpha() else parts[0]
    return None


def report(rows: list[dict]) -> str:
    counts = {r["key"]: r["n"] for r in rows}
    used = sorted(counts.items(), key=lambda kv: -kv[1])
    lines = ["📈 <b>Чем вы пользуетесь</b> (за 30 дней)"]
    lines += [f"{n} — {FEATURES.get(k, k)}" for k, n in used[:25]] or ["пока ничего не записано"]
    never = [label for k, label in FEATURES.items() if k not in counts]
    if never:
        lines += ["", "<b>Ни разу:</b> " + ", ".join(never)]
    lines.append("\n<i>Считается локально, только чтобы убирать из бота то, чем вы не пользуетесь.</i>")
    return "\n".join(lines)
