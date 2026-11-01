"""
Telegram-бот для управления личными финансами и накоплениями.
Стек: Python 3.10+, aiogram 3.x, SQLite (встроенный sqlite3).

Запуск:
    pip install -r requirements.txt
    export BOT_TOKEN="токен_от_BotFather"      # Windows (PowerShell): $env:BOT_TOKEN="..."
    python bot.py
"""
import asyncio
import logging
import math
import os
import sqlite3
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "finance.db")
CATEGORIES = ["Еда", "Транспорт", "Развлечения", "Дом", "Здоровье", "Прочее"]

# ──────────────────────────── База данных ────────────────────────────

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id     INTEGER PRIMARY KEY,
        goal_name   TEXT    NOT NULL,
        goal_amount INTEGER NOT NULL,
        months      INTEGER NOT NULL,
        income      INTEGER NOT NULL,
        mandatory   INTEGER NOT NULL,
        saved       INTEGER NOT NULL DEFAULT 0,
        started_at  TEXT    NOT NULL
    );
    CREATE TABLE IF NOT EXISTS expenses (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        amount      INTEGER NOT NULL,
        category    TEXT    NOT NULL,
        description TEXT,
        irregular   INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT    NOT NULL
    );
    """
)

USER_FIELDS = {"goal_name", "goal_amount", "months", "income", "mandatory"}


def get_user(uid: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def save_user(uid, name, amount, months, income, mandatory) -> None:
    db.execute(
        "INSERT OR REPLACE INTO users "
        "(user_id, goal_name, goal_amount, months, income, mandatory, saved, started_at) "
        "VALUES (?,?,?,?,?,?,0,?)",
        (uid, name, amount, months, income, mandatory, datetime.now().isoformat()),
    )
    db.commit()


def update_user_field(uid: int, field: str, value) -> None:
    assert field in USER_FIELDS
    db.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, uid))
    if field == "months":  # новый срок — отсчёт месяцев начинается заново
        db.execute("UPDATE users SET started_at=? WHERE user_id=?", (datetime.now().isoformat(), uid))
    db.commit()


def add_expense(uid, amount, category, description=None, irregular=False) -> None:
    db.execute(
        "INSERT INTO expenses (user_id, amount, category, description, irregular, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (uid, amount, category, description, int(irregular), datetime.now().isoformat()),
    )
    db.commit()


def spent_this_month(uid: int) -> int:
    ym = datetime.now().strftime("%Y-%m")
    row = db.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM expenses "
        "WHERE user_id=? AND substr(created_at,1,7)=?",
        (uid, ym),
    ).fetchone()
    return row["s"]


# ──────────────────────────── Расчёты и тексты ────────────────────────────


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", "\u00a0")


def parse_int(text: str, allow_zero: bool = False) -> Optional[int]:
    """Целое неотрицательное число или None (буквы, минус, дробные — отклоняются)."""
    s = (text or "").strip().replace(" ", "").replace("\u00a0", "")
    if not s.isdigit() or len(s) > 10:
        return None
    n = int(s)
    if n == 0 and not allow_zero:
        return None
    return n


def calc(u) -> tuple[int, int, int]:
    free = u["income"] - u["mandatory"]
    need = int(u["goal_amount"] / u["months"] + 0.5)
    allowed = free - need
    return free, need, allowed


def report(u) -> str:
    free, need, allowed = calc(u)
    goal, months = u["goal_amount"], u["months"]
    if allowed >= 0:
        return (
            f"🎯 Цель: {u['goal_name']}\n"
            f"📅 Срок: {months} мес.\n\n"
            f"💸 Необходимо откладывать: {fmt(need)} ₽/мес.\n"
            f"💼 Свободно после обязательных расходов: {fmt(free)} ₽/мес.\n"
            f"✅ Вы можете тратить: {fmt(allowed)} ₽/мес."
        )
    lines = [
        f"⚠️ Накопить {fmt(goal)} ₽ за {months} мес. не получится.\n",
        f"Необходимо откладывать: {fmt(need)} ₽/мес.",
        f"Свободно после обязательных расходов: {fmt(free)} ₽/мес.",
        f"Дефицит: {fmt(need - free)} ₽/мес.",
    ]
    if free > 0:
        m = math.ceil(goal / free)
        new_need = math.ceil(goal / m)
        lines += [
            "\nРекомендуем:",
            f"• Увеличить срок до {m} мес. (тогда нужно откладывать {fmt(new_need)} ₽/мес.)",
            f"• Или уменьшить сумму цели до {fmt(free * months)} ₽",
        ]
    else:
        lines.append("\nОбязательные расходы не меньше дохода. Пересмотрите доход или расходы: /income")
    return "\n".join(lines)


def month_status(u) -> str:
    _, _, allowed = calc(u)
    spent = spent_this_month(u["user_id"])
    left = allowed - spent
    text = f"🗓 Потрачено в этом месяце: {fmt(spent)} ₽\n"
    if left >= 0:
        text += f"Остаток допустимых трат: {fmt(left)} ₽"
    else:
        text += (
            f"⚠️ Вы превысили допустимые траты на {fmt(-left)} ₽. "
            f"Накопление цели может сдвинуться."
        )
    return text


def progress_text(u) -> str:
    goal, saved = u["goal_amount"], u["saved"]
    pct = int(saved * 100 / goal)
    elapsed = (datetime.now() - datetime.fromisoformat(u["started_at"])).days // 30
    left = max(u["months"] - elapsed, 0)
    bar_len = 10
    filled = min(bar_len, pct * bar_len // 100)
    bar = "▓" * filled + "░" * (bar_len - filled)
    return (
        f"🎯 {u['goal_name']}\n"
        f"Накоплено: {fmt(saved)} / {fmt(goal)} ₽\n"
        f"Прогресс: {pct} %\n{bar}\n"
        f"Осталось месяцев: {left}"
    )


# ──────────────────────────── Клавиатуры ────────────────────────────

BTN_EXPENSE = "➕ Трата"
BTN_IRREGULAR = "🧾 Разовая трата"
BTN_CALC = "📊 Расчёт"
BTN_PROGRESS = "📈 Прогресс"
BTN_DEPOSIT = "💰 Отложить"
BTN_HISTORY = "📜 История"
BTN_GOAL = "🎯 Цель"
BTN_INCOME = "💼 Доход"
BTN_RESET = "🔄 Сброс"


def main_kb() -> ReplyKeyboardMarkup:
    rows = [
        [BTN_EXPENSE, BTN_IRREGULAR],
        [BTN_CALC, BTN_PROGRESS, BTN_DEPOSIT],
        [BTN_HISTORY, BTN_GOAL, BTN_INCOME],
        [BTN_RESET],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
    )


def categories_kb() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=c, callback_data=f"cat:{c}") for c in CATEGORIES]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 3] for i in range(0, len(buttons), 3)])


def yes_no_kb(yes: str, no: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да", callback_data=yes),
            InlineKeyboardButton(text="❌ Нет", callback_data=no),
        ]]
    )


# ──────────────────────────── Состояния (FSM) ────────────────────────────


class Setup(StatesGroup):
    name = State()
    amount = State()
    months = State()
    income = State()
    mandatory = State()


class Exp(StatesGroup):
    amount = State()
    category = State()


class Irr(StatesGroup):
    amount = State()
    desc = State()


class Edit(StatesGroup):
    value = State()


class Dep(StatesGroup):
    amount = State()


router = Router()

NUM_HINT = "Введите целое положительное число, например: 15000"


async def start_setup(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Setup.name)
    await message.answer(
        "Давайте настроим цель накопления.\n\nНа что копим? Напишите название, например: «Новый ноутбук»",
        reply_markup=ReplyKeyboardRemove(),
    )


async def ensure_setup(message: Message, state: FSMContext) -> Optional[sqlite3.Row]:
    u = get_user(message.from_user.id)
    if u:
        return u
    await message.answer("Сначала нужно настроить цель накопления.")
    await start_setup(message, state)
    return None


# ──────────────────── Команды и кнопки главного меню ────────────────────
# Зарегистрированы ДО обработчиков состояний, поэтому работают и как «отмена».


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    u = get_user(message.from_user.id)
    if u:
        await message.answer(
            f"С возвращением! Ваша цель: «{u['goal_name']}».", reply_markup=main_kb()
        )
        return
    await message.answer(
        "👋 Привет! Я помогу накопить на вашу цель: посчитаю, сколько откладывать "
        "каждый месяц и сколько можно тратить."
    )
    await start_setup(message, state)


@router.message(Command("goal"))
@router.message(F.text == BTN_GOAL)
async def cmd_goal(message: Message, state: FSMContext):
    await state.clear()
    u = await ensure_setup(message, state)
    if not u:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data="edit:goal_name"),
         InlineKeyboardButton(text="✏️ Сумма", callback_data="edit:goal_amount"),
         InlineKeyboardButton(text="✏️ Срок", callback_data="edit:months")],
    ])
    await message.answer(report(u), reply_markup=kb)


@router.message(Command("income"))
@router.message(F.text == BTN_INCOME)
async def cmd_income(message: Message, state: FSMContext):
    await state.clear()
    u = await ensure_setup(message, state)
    if not u:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Доход", callback_data="edit:income"),
         InlineKeyboardButton(text="✏️ Обязательные расходы", callback_data="edit:mandatory")],
    ])
    await message.answer(
        f"💼 Доход: {fmt(u['income'])} ₽/мес.\n"
        f"🏠 Обязательные расходы: {fmt(u['mandatory'])} ₽/мес.",
        reply_markup=kb,
    )


@router.message(Command("progress"))
@router.message(F.text == BTN_PROGRESS)
async def cmd_progress(message: Message, state: FSMContext):
    await state.clear()
    u = await ensure_setup(message, state)
    if u:
        await message.answer(progress_text(u), reply_markup=main_kb())


@router.message(Command("calc"))
@router.message(F.text == BTN_CALC)
async def cmd_calc(message: Message, state: FSMContext):
    await state.clear()
    u = await ensure_setup(message, state)
    if u:
        await message.answer(f"{report(u)}\n\n{month_status(u)}", reply_markup=main_kb())


@router.message(Command("history"))
@router.message(F.text == BTN_HISTORY)
async def cmd_history(message: Message, state: FSMContext):
    await state.clear()
    u = await ensure_setup(message, state)
    if not u:
        return
    rows = db.execute(
        "SELECT * FROM expenses WHERE user_id=? ORDER BY id DESC LIMIT 10", (u["user_id"],)
    ).fetchall()
    if not rows:
        await message.answer("История трат пока пуста.", reply_markup=main_kb())
        return
    lines, buttons = ["📜 Последние траты:\n"], []
    for r in rows:
        date = datetime.fromisoformat(r["created_at"]).strftime("%d.%m")
        desc = f" — {r['description']}" if r["description"] else ""
        lines.append(f"#{r['id']} · {date} · {r['category']}{desc} · {fmt(r['amount'])} ₽")
        buttons.append([InlineKeyboardButton(
            text=f"🗑 Удалить #{r['id']} ({fmt(r['amount'])} ₽)", callback_data=f"del:{r['id']}"
        )])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(Command("reset"))
@router.message(F.text == BTN_RESET)
async def cmd_reset(message: Message, state: FSMContext):
    await state.clear()
    if not await ensure_setup(message, state):
        return
    await message.answer(
        "⚠️ Все данные (цель, доходы, траты, накопления) будут удалены. Продолжить?",
        reply_markup=yes_no_kb("resetyes", "resetno"),
    )


@router.message(Command("expense"))
@router.message(F.text == BTN_EXPENSE)
async def cmd_expense(message: Message, state: FSMContext):
    await state.clear()
    if not await ensure_setup(message, state):
        return
    await state.set_state(Exp.amount)
    await message.answer("Введите сумму траты, ₽:")


@router.message(Command("irregular"))
@router.message(F.text == BTN_IRREGULAR)
async def cmd_irregular(message: Message, state: FSMContext):
    await state.clear()
    if not await ensure_setup(message, state):
        return
    await state.set_state(Irr.amount)
    await message.answer("Введите сумму разовой траты, ₽:")


@router.message(Command("deposit"))
@router.message(F.text == BTN_DEPOSIT)
async def cmd_deposit(message: Message, state: FSMContext):
    await state.clear()
    if not await ensure_setup(message, state):
        return
    await state.set_state(Dep.amount)
    await message.answer("Сколько вы отложили на цель, ₽?")


# ──────────────────────────── Первичная настройка ────────────────────────────


@router.message(Setup.name, F.text)
async def setup_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name or len(name) > 100:
        await message.answer("Название должно быть текстом до 100 символов. Попробуйте ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(Setup.amount)
    await message.answer("Какая сумма нужна, ₽?")


@router.message(Setup.amount, F.text)
async def setup_amount(message: Message, state: FSMContext):
    n = parse_int(message.text)
    if n is None:
        await message.answer(f"❗ Не понял. {NUM_HINT}")
        return
    await state.update_data(amount=n)
    await state.set_state(Setup.months)
    await message.answer("За сколько месяцев хотите накопить?")


@router.message(Setup.months, F.text)
async def setup_months(message: Message, state: FSMContext):
    n = parse_int(message.text)
    if n is None or n > 600:
        await message.answer("❗ Введите срок целым числом месяцев, например: 6")
        return
    await state.update_data(months=n)
    await state.set_state(Setup.income)
    await message.answer("Ваш ежемесячный доход, ₽?")


@router.message(Setup.income, F.text)
async def setup_income(message: Message, state: FSMContext):
    n = parse_int(message.text)
    if n is None:
        await message.answer(f"❗ Не понял. {NUM_HINT}")
        return
    await state.update_data(income=n)
    await state.set_state(Setup.mandatory)
    await message.answer(
        "Ежемесячные обязательные расходы, ₽ (аренда, коммуналка, еда, кредиты и т. п.)?"
    )


@router.message(Setup.mandatory, F.text)
async def setup_mandatory(message: Message, state: FSMContext):
    n = parse_int(message.text, allow_zero=True)
    if n is None:
        await message.answer("❗ Введите целое число (0 или больше), например: 30000")
        return
    d = await state.get_data()
    save_user(message.from_user.id, d["name"], d["amount"], d["months"], d["income"], n)
    await state.clear()
    u = get_user(message.from_user.id)
    await message.answer(report(u), reply_markup=main_kb())


# ──────────────────────────── Редактирование параметров ────────────────────────────

EDIT_PROMPTS = {
    "goal_name": ("Введите новое название цели:", "text"),
    "goal_amount": ("Введите новую сумму цели, ₽:", "pos"),
    "months": ("Введите новый срок, мес.:", "pos"),
    "income": ("Введите новый ежемесячный доход, ₽:", "pos"),
    "mandatory": ("Введите новые обязательные расходы, ₽/мес.:", "zero"),
}


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":", 1)[1]
    if field not in EDIT_PROMPTS or not get_user(cb.from_user.id):
        await cb.answer()
        return
    await state.set_state(Edit.value)
    await state.update_data(field=field)
    await cb.message.answer(EDIT_PROMPTS[field][0])
    await cb.answer()


@router.message(Edit.value, F.text)
async def edit_value(message: Message, state: FSMContext):
    field = (await state.get_data())["field"]
    kind = EDIT_PROMPTS[field][1]
    if kind == "text":
        value = message.text.strip()
        if not value or len(value) > 100:
            await message.answer("❗ Название должно быть текстом до 100 символов.")
            return
    else:
        value = parse_int(message.text, allow_zero=(kind == "zero"))
        if value is None or (field == "months" and value > 600):
            await message.answer(f"❗ Не понял. {NUM_HINT}")
            return
    update_user_field(message.from_user.id, field, value)
    await state.clear()
    u = get_user(message.from_user.id)
    await message.answer("✅ Сохранено.\n\n" + report(u), reply_markup=main_kb())


# ──────────────────────────── Траты ────────────────────────────


@router.message(Exp.amount, F.text)
async def exp_amount(message: Message, state: FSMContext):
    n = parse_int(message.text)
    if n is None:
        await message.answer(f"❗ Не понял. {NUM_HINT}")
        return
    await state.update_data(amount=n)
    await state.set_state(Exp.category)
    await message.answer("Выберите категорию:", reply_markup=categories_kb())


@router.message(Exp.category)
async def exp_category_text(message: Message):
    await message.answer("Выберите категорию кнопкой выше 👆")


@router.callback_query(Exp.category, F.data.startswith("cat:"))
async def exp_category(cb: CallbackQuery, state: FSMContext):
    category = cb.data.split(":", 1)[1]
    amount = (await state.get_data())["amount"]
    add_expense(cb.from_user.id, amount, category)
    await state.clear()
    u = get_user(cb.from_user.id)
    await cb.message.edit_text(f"✅ Записано: {category} — {fmt(amount)} ₽")
    await cb.message.answer(month_status(u), reply_markup=main_kb())
    await cb.answer()


@router.message(Irr.amount, F.text)
async def irr_amount(message: Message, state: FSMContext):
    n = parse_int(message.text)
    if n is None:
        await message.answer(f"❗ Не понял. {NUM_HINT}")
        return
    await state.update_data(amount=n)
    await state.set_state(Irr.desc)
    await message.answer("Опишите трату текстом (например: «Подарок на день рождения»):")


@router.message(Irr.desc, F.text)
async def irr_desc(message: Message, state: FSMContext):
    desc = message.text.strip()
    if not desc or len(desc) > 200:
        await message.answer("❗ Описание — текст до 200 символов.")
        return
    amount = (await state.get_data())["amount"]
    add_expense(message.from_user.id, amount, "Разовая", desc, irregular=True)
    await state.clear()
    u = get_user(message.from_user.id)
    await message.answer(
        f"✅ Разовая трата записана: {desc} — {fmt(amount)} ₽\n\n{month_status(u)}",
        reply_markup=main_kb(),
    )


@router.message(Dep.amount, F.text)
async def dep_amount(message: Message, state: FSMContext):
    n = parse_int(message.text)
    if n is None:
        await message.answer(f"❗ Не понял. {NUM_HINT}")
        return
    db.execute("UPDATE users SET saved = saved + ? WHERE user_id=?", (n, message.from_user.id))
    db.commit()
    await state.clear()
    u = get_user(message.from_user.id)
    extra = "\n\n🎉 Цель достигнута!" if u["saved"] >= u["goal_amount"] else ""
    await message.answer(progress_text(u) + extra, reply_markup=main_kb())


# ──────────────────── Подтверждения: удаление траты и сброс ────────────────────


@router.callback_query(F.data.startswith("del:"))
async def cb_delete_ask(cb: CallbackQuery):
    exp_id = cb.data.split(":", 1)[1]
    await cb.message.answer(
        f"Удалить трату #{exp_id}?", reply_markup=yes_no_kb(f"delyes:{exp_id}", "delno")
    )
    await cb.answer()


@router.callback_query(F.data.startswith("delyes:"))
async def cb_delete_yes(cb: CallbackQuery):
    exp_id = int(cb.data.split(":", 1)[1])
    cur = db.execute("DELETE FROM expenses WHERE id=? AND user_id=?", (exp_id, cb.from_user.id))
    db.commit()
    await cb.message.edit_text("🗑 Трата удалена." if cur.rowcount else "Трата не найдена.")
    await cb.answer()


@router.callback_query(F.data == "delno")
async def cb_delete_no(cb: CallbackQuery):
    await cb.message.edit_text("Удаление отменено.")
    await cb.answer()


@router.callback_query(F.data == "resetyes")
async def cb_reset_yes(cb: CallbackQuery, state: FSMContext):
    db.execute("DELETE FROM expenses WHERE user_id=?", (cb.from_user.id,))
    db.execute("DELETE FROM users WHERE user_id=?", (cb.from_user.id,))
    db.commit()
    await cb.message.edit_text("Все данные удалены.")
    await cb.answer()
    await start_setup(cb.message, state)


@router.callback_query(F.data == "resetno")
async def cb_reset_no(cb: CallbackQuery):
    await cb.message.edit_text("Сброс отменён.")
    await cb.answer()


# ──────────────────────────── Прочий текст ────────────────────────────


@router.message(StateFilter(None))
async def fallback(message: Message, state: FSMContext):
    if await ensure_setup(message, state):
        await message.answer("Пользуйтесь кнопками меню 👇", reply_markup=main_kb())


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (переменная окружения).")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
