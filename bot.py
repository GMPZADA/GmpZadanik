import os
import json
import base64
import threading
import requests
from flask import Flask
from telebot import TeleBot, types

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")  # пример: GMPZADA/GmpZadanik
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_FILE = "data.json"

LOCAL_DATA_FILE = "data.json"

bot = TeleBot(TOKEN, parse_mode="HTML")
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is working ✅"


def run_site():
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def empty_data():
    return {
        "users": {},
        "tasks": {},
        "last_task_id": 37,
        "withdraws": {},
        "last_withdraw_id": 0
    }


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }


def github_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"


def load_data():
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            r = requests.get(
                github_url(),
                headers=github_headers(),
                params={"ref": GITHUB_BRANCH},
                timeout=10
            )

            if r.status_code == 200:
                content = r.json()["content"]
                text = base64.b64decode(content).decode("utf-8")
                return json.loads(text)

            if r.status_code == 404:
                data = empty_data()
                save_data(data)
                return data

            print("GitHub load error:", r.text)
        except Exception as e:
            print("GitHub load exception:", e)

    if os.path.exists(LOCAL_DATA_FILE):
        with open(LOCAL_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    return empty_data()


def save_data(data):
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            sha = None

            r = requests.get(
                github_url(),
                headers=github_headers(),
                params={"ref": GITHUB_BRANCH},
                timeout=10
            )

            if r.status_code == 200:
                sha = r.json().get("sha")

            content = json.dumps(data, ensure_ascii=False, indent=2)
            payload = {
                "message": "update bot data",
                "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
                "branch": GITHUB_BRANCH
            }

            if sha:
                payload["sha"] = sha

            pr = requests.put(github_url(), headers=github_headers(), json=payload, timeout=10)

            if pr.status_code not in (200, 201):
                print("GitHub save error:", pr.text)

        except Exception as e:
            print("GitHub save exception:", e)

    with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user(data, user_id):
    uid = str(user_id)
    if uid not in data["users"]:
        data["users"][uid] = {
            "balance": 0,
            "done_tasks": [],
            "waiting_task": None,
            "withdraw_pending": False,
            "withdraw_step": None,
            "withdraw_to": None
        }
    return data["users"][uid]


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
    user = get_user(data, call.from_user.id)
    task_id = call.data.split("_")[1]

    if task_id in user["done_tasks"]:
        return bot.answer_callback_query(call.id, "Ты уже сделал это задание.")

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

    if task_id in user["done_tasks"]:
        return bot.answer_callback_query(call.id, "Ты уже сделал это задание.")

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

    if task_id in user["done_tasks"]:
        user["waiting_task"] = None
        save_data(data)
        return bot.send_message(message.chat.id, "✅ Ты уже выполнил это задание.")

    task = data["tasks"].get(task_id)
    if not task:
        user["waiting_task"] = None
        save_data(data)
        return bot.send_message(message.chat.id, "❌ Задание не найдено.")

    user["waiting_task"] = None
    save_data(data)

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"yes_{message.from_user.id}_{task_id}"),
        types.InlineKeyboardButton("❌ Отказать", callback_data=f"no_{message.from_user.id}_{task_id}")
    )

    bot.send_message(message.chat.id, "✅ Скрин отправлен админу на проверку.")

    bot.send_photo(
        ADMIN_ID,
        message.photo[-1].file_id,
        caption=
        f"📨 <b>Новая заявка</b>\n\n"
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
    action, user_id, task_id = call.data.split("_")

    user = get_user(data, user_id)
    task = data["tasks"].get(task_id)

    if not task:
        return bot.answer_callback_query(call.id, "Задание не найдено.")

    if action == "yes":
        if task_id not in user["done_tasks"]:
            user["balance"] += int(task["reward"])
            user["done_tasks"].append(task_id)

        save_data(data)

        bot.send_message(user_id, f"🎉 Задание #{task_id} одобрено!\n💰 Начислено: <b>{task['reward']} GMP</b>")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id, caption="✅ Заявка одобрена.")

    else:
        save_data(data)
        bot.send_message(user_id, f"❌ Задание #{task_id} отклонено.")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id, caption="❌ Заявка отклонена.")


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

    if user["balance"] <= 0:
        return bot.send_message(message.chat.id, "❌ У тебя нет GMP для вывода.")

    if user.get("withdraw_pending"):
        return bot.send_message(message.chat.id, "⏳ У тебя уже есть заявка на вывод. Ожидай.")

    user["withdraw_step"] = "username"
    user["withdraw_to"] = None
    save_data(data)

    bot.send_message(
        message.chat.id,
        "💸 <b>Вывод GMP</b>\n\n"
        "👤 Напиши username/ID, куда вывести GMP.\n\n"
        "Пример: <code>@username</code>"
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("payyes_") or c.data.startswith("payno_"))
def pay_check(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "Нет доступа.")

    data = load_data()
    action, wid = call.data.split("_")
    w = data["withdraws"].get(wid)

    if not w or w["status"] != "wait":
        return bot.answer_callback_query(call.id, "Заявка уже обработана.")

    user = get_user(data, w["user_id"])
    amount = int(w["amount"])

    if action == "payyes":
        user["withdraw_pending"] = False

        # заявка удаляется из data.json, чтобы файл не засорялся
        if wid in data["withdraws"]:
            del data["withdraws"][wid]

        save_data(data)

        bot.send_message(w["user_id"], f"✅ Выплата #{wid} подтверждена.\n💰 Выплачено: <b>{amount} GMP</b>")
        bot.edit_message_text("✅ Выплата подтверждена.", call.message.chat.id, call.message.message_id)

    else:
        # если отказал — возвращаем GMP обратно на баланс
        user["balance"] += amount
        user["withdraw_pending"] = False

        if wid in data["withdraws"]:
            del data["withdraws"][wid]

        save_data(data)

        bot.send_message(w["user_id"], f"❌ Заявка на вывод #{wid} отклонена.\n💰 <b>{amount} GMP</b> возвращены на баланс.")
        bot.edit_message_text("❌ Выплата отклонена. GMP возвращены.", call.message.chat.id, call.message.message_id)


@bot.message_handler(func=lambda m: m.text == "ℹ️ Помощь")
def help_message(message):
    bot.send_message(message.chat.id, "ℹ️ Выбери 📋 Задания, выполни задание, отправь скрин и жди проверку.")


@bot.message_handler(func=lambda m: m.text == "➕ Добавить задание")
def add_task_info(message):
    if message.from_user.id != ADMIN_ID:
        return

    bot.send_message(
        message.chat.id,
        "➕ <b>Добавление задания:</b>\n\n"
        "<code>/addtask Текст задания | ссылка | награда</code>\n\n"
        "Пример:\n"
        "<code>/addtask Зайти в бота и подписаться | https://t.me/example | 1</code>"
    )


@bot.message_handler(commands=["addtask"])
def add_task(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    text = message.text.replace("/addtask", "", 1).strip()
    parts = [p.strip() for p in text.split("|")]

    if len(parts) != 3:
        return bot.send_message(message.chat.id, "❌ Формат:\n/addtask Текст | ссылка | награда")

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
def requests_msg(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.send_message(message.chat.id, "📨 Новые заявки приходят сюда автоматически.")


@bot.message_handler(func=lambda m: m.text == "🏠 Меню")
def menu(message):
    bot.send_message(message.chat.id, "🏠 Главное меню", reply_markup=main_menu())


@bot.message_handler(func=lambda m: True)
def withdraw_steps(message):
    data = load_data()
    user = get_user(data, message.from_user.id)

    step = user.get("withdraw_step")

    if step == "username":
        withdraw_to = message.text.strip()

        if len(withdraw_to) < 3:
            return bot.send_message(message.chat.id, "❌ Напиши нормальный username/ID для вывода.")

        user["withdraw_to"] = withdraw_to
        user["withdraw_step"] = "amount"
        save_data(data)

        return bot.send_message(
            message.chat.id,
            f"💰 Твой баланс: <b>{user['balance']} GMP</b>\n\n"
            "Напиши сколько GMP вывести.\n"
            "Можно написать число или <code>все</code>."
        )

    if step == "amount":
        text = message.text.strip().lower()

        if text in ["все", "all", "всё"]:
            amount = int(user["balance"])
        else:
            try:
                amount = int(text)
            except:
                return bot.send_message(message.chat.id, "❌ Напиши число. Например: <code>10</code>")

        if amount <= 0:
            return bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0.")

        if amount > int(user["balance"]):
            return bot.send_message(
                message.chat.id,
                f"❌ Недостаточно GMP.\nТвой баланс: <b>{user['balance']} GMP</b>"
            )

        data["last_withdraw_id"] += 1
        wid = str(data["last_withdraw_id"])

        withdraw_to = user.get("withdraw_to") or "не указано"

        # GMP списываются сразу, чтобы нельзя было спамить выводами на одну сумму
        user["balance"] -= amount
        user["withdraw_pending"] = True
        user["withdraw_step"] = None
        user["withdraw_to"] = None

        data["withdraws"][wid] = {
            "user_id": message.from_user.id,
            "username": message.from_user.username or "",
            "to": withdraw_to,
            "amount": amount,
            "status": "wait"
        }

        save_data(data)

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ Выплачено", callback_data=f"payyes_{wid}"),
            types.InlineKeyboardButton("❌ Отказать", callback_data=f"payno_{wid}")
        )

        bot.send_message(
            message.chat.id,
            f"✅ Заявка на вывод #{wid} создана.\n\n"
            f"👤 Куда: <b>{withdraw_to}</b>\n"
            f"💰 Сумма: <b>{amount} GMP</b>\n\n"
            "GMP уже списаны с баланса и ожидают проверки."
        )

        return bot.send_message(
            ADMIN_ID,
            f"💸 <b>Новая заявка на вывод #{wid}</b>\n\n"
            f"👤 Пользователь: @{message.from_user.username or 'нет username'}\n"
            f"🆔 ID: <code>{message.from_user.id}</code>\n"
            f"📤 Куда вывести: <b>{withdraw_to}</b>\n"
            f"💰 Сумма: <b>{amount} GMP</b>",
            reply_markup=kb
        )

    return unknown(message)




if __name__ == "__main__":
    if not TOKEN:
        print("❌ BOT_TOKEN не найден.")
        exit()

    threading.Thread(target=run_site, daemon=True).start()
    print("✅ Bot started")
    bot.infinity_polling(skip_pending=True)
