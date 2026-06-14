import os
import json
import threading
from flask import Flask
from telebot import TeleBot, types

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATA_FILE = "data.json"
bot = TeleBot(TOKEN, parse_mode="HTML")

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is working ✅"


def run_site():
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def load_data():
    if not os.path.exists(DATA_FILE):
        return {
            "users": {},
            "tasks": {},
            "last_task_id": 37,
            "last_request_id": 0
        }

    with open(DATA_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def get_user(data, user_id):
    user_id = str(user_id)

    if user_id not in data["users"]:
        data["users"][user_id] = {
            "balance": 0,
            "done_tasks": [],
            "waiting_task": None
        }

    return data["users"][user_id]


def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📋 Задания", "💰 Баланс")
    kb.row("💸 Вывод", "ℹ️ Помощь")
    return kb


def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Добавить задание", "📋 Все задания")
    kb.row("📨 Заявки", "🏠 Меню")
    return kb


@bot.message_handler(commands=["start"])
def start(message):
    data = load_data()
    get_user(data, message.from_user.id)
    save_data(data)

    bot.send_message(
        message.chat.id,
        "👋 <b>Добро пожаловать в GMP Zadanik!</b>\n\n"
        "Выполняй задания, отправляй скрин и получай GMP.",
        reply_markup=main_menu()
    )


@bot.message_handler(commands=["admin"])
def admin(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    bot.send_message(message.chat.id, "🔐 <b>Админ-панель</b>", reply_markup=admin_menu())


@bot.message_handler(func=lambda m: m.text == "📋 Задания")
def tasks(message):
    data = load_data()
    user = get_user(data, message.from_user.id)

    kb = types.InlineKeyboardMarkup()
    found = False

    for task_id, task in data["tasks"].items():
        if task_id not in user["done_tasks"]:
            found = True
            kb.add(types.InlineKeyboardButton(
                f"✅ Задание #{task_id} — {task['reward']} GMP",
                callback_data=f"task_{task_id}"
            ))

    save_data(data)

    if not found:
        return bot.send_message(message.chat.id, "✅ Сейчас нет новых заданий.")

    bot.send_message(message.chat.id, "📋 <b>Доступные задания:</b>", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("task_"))
def open_task(call):
    data = load_data()
    task_id = call.data.split("_")[1]
    task = data["tasks"].get(task_id)

    if not task:
        return bot.answer_callback_query(call.id, "Задание не найдено.")

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔗 Перейти", url=task["link"]))
    kb.add(types.InlineKeyboardButton("✅ Я выполнил", callback_data=f"done_{task_id}"))

    bot.send_message(
        call.message.chat.id,
        f"✅ <b>Задание #{task_id}</b>\n\n"
        f"{task['text']}\n\n"
        f"💰 Награда: <b>{task['reward']} GMP</b>\n\n"
        f"После выполнения нажми ✅ Я выполнил.",
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("done_"))
def done_task(call):
    data = load_data()
    user = get_user(data, call.from_user.id)
    task_id = call.data.split("_")[1]

    user["waiting_task"] = task_id
    save_data(data)

    bot.send_message(call.message.chat.id, "📸 Отправь скриншот выполнения задания.")


@bot.message_handler(content_types=["photo"])
def photo(message):
    data = load_data()
    user = get_user(data, message.from_user.id)

    task_id = user.get("waiting_task")
    if not task_id:
        return bot.send_message(message.chat.id, "❌ Сначала выбери задание.")

    task = data["tasks"].get(task_id)
    if not task:
        return bot.send_message(message.chat.id, "❌ Задание не найдено.")

    data["last_request_id"] += 1
    request_id = data["last_request_id"]

    user["waiting_task"] = None
    save_data(data)

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"yes_{message.from_user.id}_{task_id}_{request_id}"),
        types.InlineKeyboardButton("❌ Отказать", callback_data=f"no_{message.from_user.id}_{task_id}_{request_id}")
    )

    bot.send_message(message.chat.id, "✅ Скрин отправлен админу на проверку.")

    bot.send_photo(
        ADMIN_ID,
        message.photo[-1].file_id,
        caption=
        f"📨 <b>Новая заявка #{request_id}</b>\n\n"
        f"✅ Задание: #{task_id}\n"
        f"💰 Награда: {task['reward']} GMP\n"
        f"👤 Пользователь: @{message.from_user.username or 'нет username'}\n"
        f"🆔 ID: <code>{message.from_user.id}</code>",
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("yes_") or c.data.startswith("no_"))
def check_request(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "Нет доступа.")

    data = load_data()
    action, user_id, task_id, request_id = call.data.split("_")

    user = get_user(data, user_id)
    task = data["tasks"].get(task_id)

    if not task:
        return bot.answer_callback_query(call.id, "Задание не найдено.")

    if action == "yes":
        if task_id not in user["done_tasks"]:
            user["balance"] += int(task["reward"])
            user["done_tasks"].append(task_id)

        save_data(data)

        bot.send_message(
            user_id,
            f"🎉 Задание #{task_id} одобрено!\n"
            f"💰 Начислено: <b>{task['reward']} GMP</b>"
        )

        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption=f"✅ Заявка #{request_id} одобрена."
        )

    else:
        save_data(data)

        bot.send_message(user_id, f"❌ Задание #{task_id} отклонено.")
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption=f"❌ Заявка #{request_id} отклонена."
        )


@bot.message_handler(func=lambda m: m.text == "💰 Баланс")
def balance(message):
    data = load_data()
    user = get_user(data, message.from_user.id)
    save_data(data)

    bot.send_message(message.chat.id, f"💰 <b>Твой баланс:</b> {user['balance']} GMP")


@bot.message_handler(func=lambda m: m.text == "💸 Вывод")
def withdraw(message):
    data = load_data()
    user = get_user(data, message.from_user.id)
    save_data(data)

    if user["balance"] <= 0:
        return bot.send_message(message.chat.id, "❌ У тебя нет GMP для вывода.")

    bot.send_message(
        message.chat.id,
        f"✅ Заявка на вывод создана.\n\n"
        f"💰 Сумма: <b>{user['balance']} GMP</b>\n"
        f"⏳ Ожидай выплату в течение 24 часов."
    )

    bot.send_message(
        ADMIN_ID,
        f"💸 <b>Новая заявка на вывод</b>\n\n"
        f"👤 @{message.from_user.username or 'нет username'}\n"
        f"🆔 <code>{message.from_user.id}</code>\n"
        f"💰 {user['balance']} GMP"
    )


@bot.message_handler(func=lambda m: m.text == "ℹ️ Помощь")
def help_message(message):
    bot.send_message(
        message.chat.id,
        "ℹ️ <b>Как пользоваться:</b>\n\n"
        "1. Нажми 📋 Задания\n"
        "2. Выполни задание\n"
        "3. Отправь скрин\n"
        "4. Жди проверку админа"
    )


@bot.message_handler(func=lambda m: m.text == "➕ Добавить задание")
def add_task_info(message):
    if message.from_user.id != ADMIN_ID:
        return

    bot.send_message(
        message.chat.id,
        "➕ <b>Добавление задания:</b>\n\n"
        "Пиши так:\n\n"
        "<code>/addtask Текст задания | ссылка | награда</code>\n\n"
        "Пример:\n"
        "<code>/addtask Зайти в бота и подписаться на каналы | https://t.me/example | 1</code>"
    )


@bot.message_handler(commands=["addtask"])
def add_task(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    text = message.text.replace("/addtask", "", 1).strip()
    parts = [p.strip() for p in text.split("|")]

    if len(parts) != 3:
        return bot.send_message(message.chat.id, "❌ Неверный формат.")

    task_text, link, reward = parts

    try:
        reward = int(reward)
    except:
        return bot.send_message(message.chat.id, "❌ Награда должна быть числом.")

    data = load_data()
    data["last_task_id"] += 1
    task_id = str(data["last_task_id"])

    data["tasks"][task_id] = {
        "text": task_text,
        "link": link,
        "reward": reward
    }

    save_data(data)

    bot.send_message(
        message.chat.id,
        f"✅ <b>Задание #{task_id} создано</b>\n\n"
        f"🔗 Ссылка:\n{link}\n\n"
        f"{task_text}\n\n"
        f"💰 Награда: {reward} GMP"
    )


@bot.message_handler(func=lambda m: m.text == "📋 Все задания")
def all_tasks(message):
    if message.from_user.id != ADMIN_ID:
        return

    data = load_data()

    if not data["tasks"]:
        return bot.send_message(message.chat.id, "Заданий пока нет.")

    text = "📋 <b>Все задания:</b>\n\n"

    for task_id, task in data["tasks"].items():
        text += f"#{task_id} — {task['reward']} GMP\n{task['text']}\n\n"

    bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda m: m.text == "📨 Заявки")
def requests(message):
    if message.from_user.id != ADMIN_ID:
        return

    bot.send_message(message.chat.id, "📨 Новые заявки приходят сюда автоматически.")


@bot.message_handler(func=lambda m: m.text == "🏠 Меню")
def menu(message):
    bot.send_message(message.chat.id, "🏠 Главное меню", reply_markup=main_menu())


@bot.message_handler(func=lambda m: True)
def unknown(message):
    bot.send_message(message.chat.id, "👇 Выбери кнопку в меню.", reply_markup=main_menu())


if __name__ == "__main__":
    if not TOKEN:
        print("❌ BOT_TOKEN не найден.")
        exit()

    threading.Thread(target=run_site).start()

    print("✅ Bot started")
    bot.infinity_polling(skip_pending=True)
