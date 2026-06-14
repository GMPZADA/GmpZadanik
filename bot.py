import asyncio
import json
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = "ВСТАВЬ_ТОКЕН"
ADMIN_ID = 7837011810  # свой Telegram ID

DATA_FILE = "data.json"
REWARD = 1


bot = Bot(TOKEN)
dp = Dispatcher()


def load_data():
    if not os.path.exists(DATA_FILE):
        return {
            "users": {},
            "tasks": {},
            "submits": {},
            "next_task_id": 38,
            "next_submit_id": 1
        }

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user(data, user_id):
    user_id = str(user_id)

    if user_id not in data["users"]:
        data["users"][user_id] = {
            "balance": 0,
            "done_tasks": [],
            "pending_tasks": [],
            "state": None
        }

    return data["users"][user_id]


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Задания", callback_data="tasks")
    kb.button(text="💰 Баланс", callback_data="balance")
    kb.button(text="📤 Вывести", callback_data="withdraw")
    kb.adjust(1)
    return kb.as_markup()


def admin_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить задание", callback_data="admin_add_task")
    kb.button(text="📋 Список заданий", callback_data="admin_tasks")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(CommandStart())
async def start(message: Message):
    data = load_data()
    get_user(data, message.from_user.id)
    save_data(data)

    text = (
        "👋 Добро пожаловать!\n\n"
        "Выполняй задания и получай GMP.\n\n"
        "💰 За 1 задание: 1 GMP"
    )

    await message.answer(text, reply_markup=main_menu())

    if message.from_user.id == ADMIN_ID:
        await message.answer("👑 Админ-панель", reply_markup=admin_menu())


@dp.message(Command("admin"))
async def admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer("👑 Админ-панель", reply_markup=admin_menu())


@dp.callback_query(F.data == "balance")
async def balance(call: CallbackQuery):
    data = load_data()
    user = get_user(data, call.from_user.id)

    await call.message.answer(
        f"💰 Баланс: {user['balance']} GMP\n"
        f"✅ Выполнено заданий: {len(user['done_tasks'])}"
    )
    await call.answer()


@dp.callback_query(F.data == "tasks")
async def show_tasks(call: CallbackQuery):
    data = load_data()
    user = get_user(data, call.from_user.id)

    available = []

    for task_id, task in data["tasks"].items():
        if task_id not in user["done_tasks"] and task_id not in user["pending_tasks"]:
            available.append((task_id, task))

    if not available:
        await call.message.answer("📭 Сейчас нет доступных заданий.")
        await call.answer()
        return

    for task_id, task in available:
        kb = InlineKeyboardBuilder()
        kb.button(text="📸 Отправить скриншот", callback_data=f"send_screen_{task_id}")

        await call.message.answer(
            f"📋 ЗАДАНИЕ #{task_id}\n\n"
            f"🔗 Ссылка:\n{task['link']}\n\n"
            f"📌 Что нужно сделать:\n{task['text']}\n\n"
            f"💰 Награда: {task['reward']} GMP\n\n"
            f"📸 ОТПРАВЬ СКРИНШОТ",
            reply_markup=kb.as_markup()
        )

    await call.answer()


@dp.callback_query(F.data.startswith("send_screen_"))
async def send_screen(call: CallbackQuery):
    task_id = call.data.split("_")[2]

    data = load_data()
    user = get_user(data, call.from_user.id)

    if task_id in user["done_tasks"]:
        await call.message.answer("✅ Вы уже выполнили это задание.")
        await call.answer()
        return

    if task_id in user["pending_tasks"]:
        await call.message.answer("⏳ Это задание уже на проверке.")
        await call.answer()
        return

    user["state"] = f"waiting_screen_{task_id}"
    save_data(data)

    await call.message.answer(
        "📸 Отправьте скриншот выполнения задания.\n\n"
        "После отправки задание уйдёт админу на проверку."
    )
    await call.answer()


@dp.message(F.photo)
async def photo_handler(message: Message):
    data = load_data()
    user = get_user(data, message.from_user.id)

    state = user.get("state")

    if not state or not state.startswith("waiting_screen_"):
        await message.answer("❌ Сначала выберите задание.")
        return

    task_id = state.replace("waiting_screen_", "")

    if task_id not in data["tasks"]:
        await message.answer("❌ Задание не найдено.")
        user["state"] = None
        save_data(data)
        return

    submit_id = str(data["next_submit_id"])
    data["next_submit_id"] += 1

    user["pending_tasks"].append(task_id)
    user["state"] = None

    data["submits"][submit_id] = {
        "user_id": message.from_user.id,
        "task_id": task_id,
        "status": "waiting"
    }

    save_data(data)

    task = data["tasks"][task_id]

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"approve_{submit_id}")
    kb.button(text="❌ Отклонить", callback_data=f"reject_{submit_id}")
    kb.adjust(1)

    username = message.from_user.username
    user_text = f"@{username}" if username else "без username"

    await bot.send_photo(
        ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=(
            f"📸 Новая проверка\n\n"
            f"👤 Пользователь: {user_text}\n"
            f"🆔 ID: {message.from_user.id}\n"
            f"📋 Задание #{task_id}\n"
            f"💰 Награда: {task['reward']} GMP\n\n"
            f"Проверь, пришла ли рефка."
        ),
        reply_markup=kb.as_markup()
    )

    await message.answer(
        "⏳ Задание отправлено на проверку.\n"
        "Ожидайте одобрения."
    )


@dp.callback_query(F.data.startswith("approve_"))
async def approve(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return

    submit_id = call.data.split("_")[1]
    data = load_data()

    if submit_id not in data["submits"]:
        await call.answer("Заявка не найдена")
        return

    submit = data["submits"][submit_id]

    if submit["status"] != "waiting":
        await call.answer("Заявка уже обработана")
        return

    user_id = str(submit["user_id"])
    task_id = submit["task_id"]

    user = get_user(data, user_id)
    task = data["tasks"][task_id]

    user["balance"] += task["reward"]

    if task_id not in user["done_tasks"]:
        user["done_tasks"].append(task_id)

    if task_id in user["pending_tasks"]:
        user["pending_tasks"].remove(task_id)

    submit["status"] = "approved"

    save_data(data)

    await bot.send_message(
        int(user_id),
        f"✅ Задание #{task_id} одобрено!\n\n"
        f"💰 Начислено: {task['reward']} GMP"
    )

    await call.message.edit_caption(
        caption=call.message.caption + "\n\n✅ Одобрено"
    )

    await call.answer()


@dp.callback_query(F.data.startswith("reject_"))
async def reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return

    submit_id = call.data.split("_")[1]
    data = load_data()

    if submit_id not in data["submits"]:
        await call.answer("Заявка не найдена")
        return

    submit = data["submits"][submit_id]

    if submit["status"] != "waiting":
        await call.answer("Заявка уже обработана")
        return

    user_id = str(submit["user_id"])
    task_id = submit["task_id"]

    user = get_user(data, user_id)

    if task_id in user["pending_tasks"]:
        user["pending_tasks"].remove(task_id)

    submit["status"] = "rejected"

    save_data(data)

    await bot.send_message(
        int(user_id),
        f"❌ Задание #{task_id} отклонено.\n\n"
        "Можете попробовать выполнить заново."
    )

    await call.message.edit_caption(
        caption=call.message.caption + "\n\n❌ Отклонено"
    )

    await call.answer()


@dp.callback_query(F.data == "admin_add_task")
async def admin_add_task(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return

    data = load_data()
    user = get_user(data, call.from_user.id)
    user["state"] = "admin_wait_link"
    save_data(data)

    await call.message.answer("🔗 Отправь ссылку для задания:")
    await call.answer()


@dp.callback_query(F.data == "admin_tasks")
async def admin_tasks(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return

    data = load_data()

    if not data["tasks"]:
        await call.message.answer("📭 Заданий нет.")
        await call.answer()
        return

    for task_id, task in data["tasks"].items():
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Удалить", callback_data=f"delete_task_{task_id}")

        await call.message.answer(
            f"📋 Задание #{task_id}\n\n"
            f"🔗 Ссылка:\n{task['link']}\n\n"
            f"📌 Текст:\n{task['text']}\n\n"
            f"💰 Награда: {task['reward']} GMP",
            reply_markup=kb.as_markup()
        )

    await call.answer()


@dp.callback_query(F.data.startswith("delete_task_"))
async def delete_task(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return

    task_id = call.data.split("_")[2]
    data = load_data()

    if task_id in data["tasks"]:
        del data["tasks"][task_id]
        save_data(data)
        await call.message.edit_text(f"❌ Задание #{task_id} удалено.")
    else:
        await call.message.answer("Задание не найдено.")

    await call.answer()


@dp.callback_query(F.data == "withdraw")
async def withdraw(call: CallbackQuery):
    data = load_data()
    user = get_user(data, call.from_user.id)

    if user["balance"] <= 0:
        await call.message.answer("❌ У вас нет GMP для вывода.")
        await call.answer()
        return

    await call.message.answer(
        f"📤 Вывод GMP\n\n"
        f"💰 Ваш баланс: {user['balance']} GMP\n\n"
        "Для вывода напишите админу."
    )
    await call.answer()


@dp.message(F.text)
async def text_handler(message: Message):
    data = load_data()
    user = get_user(data, message.from_user.id)
    text = message.text.strip()

    if message.from_user.id == ADMIN_ID:
        if user.get("state") == "admin_wait_link":
            user["temp_link"] = text
            user["state"] = "admin_wait_text"
            save_data(data)

            await message.answer("📌 Отправь текст задания:")
            return

        if user.get("state") == "admin_wait_text":
            task_text = text
            task_id = str(data["next_task_id"])
            data["next_task_id"] += 1

            link = user.get("temp_link")

            data["tasks"][task_id] = {
                "link": link,
                "text": task_text,
                "reward": REWARD
            }

            user["state"] = None
            user.pop("temp_link", None)

            save_data(data)

            await message.answer(
                f"✅ Задание #{task_id} создано\n\n"
                f"🔗 Ссылка:\n{link}\n\n"
                f"{task_text}\n\n"
                f"💰 Награда: {REWARD} GMP"
            )
            return

    await message.answer("Выберите действие:", reply_markup=main_menu())


async def main():
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
