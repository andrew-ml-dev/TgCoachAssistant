from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

# Постоянная панель снизу. Порядок — по частоте: сверху то, что нажимается каждый день.
# Вес и еда текстом кнопок не требуют — просто пишутся в чат (подсказка в поле ввода).
QUICK, TODAY, EAT, MORE = ("⚡ Быстрая запись", "📊 Отчёт", "🍽 Что съесть?", "⚙️ Ещё")
CAN = "❓ Можно ли?"   # убрана с панели: теперь кнопка внутри «🍽 Что съесть?»
# Старые подписи: у пользователя может остаться прежняя клавиатура
FOOD, WEIGHT, OLD_TODAY, OLD_PROGRESS, MENU = "🍽 Еда", "⚖️ Вес", "📊 Сегодня", "📈 Прогресс", "🥗 Меню"
OLD_QUICK = {"📦 Мои продукты", "🕘 Повторить"}

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=QUICK), KeyboardButton(text=EAT)],
        [KeyboardButton(text=TODAY), KeyboardButton(text=MORE)],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="92.4 · борщ и 2 куска хлеба · 450 ккал",
)


def ikb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[B(text=t, callback_data=d) for t, d in row] for row in rows])


def meal_card(meal_id: int, can_move: bool = True) -> InlineKeyboardMarkup:
    row = [("✏️ Исправить", f"m:edit:{meal_id}"), ("❌ Удалить", f"m:del:{meal_id}")]
    if can_move:
        row.append(("📅 На вчера", f"m:yday:{meal_id}"))
    return ikb(row)


food_menu = ikb([("⚡ Быстрая запись", "s:products"), ("❓ Можно ли?", "f:can")])

PERIODS = [("Сегодня", "r:1"), ("3 дня", "r:3"), ("Неделя", "r:7"), ("Всё", "r:0")]


def report_menu(current: str, extra: list[list[tuple[str, str]]] = ()) -> InlineKeyboardMarkup:
    """Переключатель периода; текущий период отмечен точкой."""
    row = [(("• " + t) if d == current else t, d) for t, d in PERIODS]
    return ikb(*extra, row)

weight_menu = ikb([("📈 Прогресс", "r:0"), ("📏 Замеры", "s:measure")], [("↩️ Отменить сегодняшний", "w:undo")])

weight_remind = ikb([("⏰ Отложить на час", "w:snooze"), ("🚫 Сегодня пропускаю", "w:skip")])


more_menu = ikb(
    [("🎯 Цель", "s:goal"), ("⏰ Напоминания", "s:remind")],
    [("🥗 Диета и протеин", "s:diet"), ("⌚ Polar", "s:polar")],
    [("📏 Замеры", "s:measure"), ("🛠 Сервис", "s:service")],
)

service_menu = ikb(
    [("🔁 Пересмотр рациона", "s:review"), ("📗 Таблица продуктов", "s:table")],
    [("💰 Расход API", "s:cost"), ("📤 Экспорт CSV", "s:export")],
    [("📈 Чем пользуюсь", "s:usage")],
    [("👤 Профиль заново", "s:profile"), ("❓ Помощь", "s:help")],
)


def day_done(day: str) -> InlineKeyboardMarkup:
    return ikb([("✅ Всё записал", f"dc:1:{day}"), ("❌ Не всё", f"dc:-1:{day}")])


def ambiguous(value: float) -> InlineKeyboardMarkup:
    v = f"{value:g}"
    return ikb([(f"⚖️ Вес {v} кг", f"amb:w:{v}"), (f"🔥 {v} ккал", f"amb:k:{v}")])
