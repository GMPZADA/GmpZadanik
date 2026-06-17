import os
import json
import base64
import threading
import time
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
BONUS_AMOUNT = 0.2
BONUS_COOLDOWN = 24 * 60 * 60

# Keep Alive: пингует сайт каждые 5 минут
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL", "https://gmpzadanik.onrender.com/")
KEEPALIVE_INTERVAL = 5 * 60

bot = TeleBot(TOKEN, parse_mode="HTML")
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is working ✅"


def run_site():
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def auto_ping():
    # Пинг сайта каждые 5 минут, чтобы Web Service был живой
    time.sleep(30)
    while True:
        try:
            r = requests.get(KEEPALIVE_URL, timeout=15)
            print(f"KeepAlive ping: {r.status_code}")
        except Exception as e:
            print("KeepAlive error:", e)
        time.sleep(KEEPALIVE_INTERVAL)

def chat_button(message):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "💬 Перейти в чат",
            url="https://t.me/+4aLpL-ixZnZiYjMy"
        )
    )

    bot.send_message(
        message.chat.id,
        "💬 <b>Это наш чат для общения ✨</b>\n\n"
        "Общайся, знакомься и задавай вопросы 💖\n\n"
        "Заходи, не стесняйся! 😌",
        reply_markup=kb
    )



def empty_data():
    return {
        "users": {},
        "tasks": {},
        "submits": {},
        "withdraws": {},
        "last_task_id": 37,
        "last_submit_id": 0,
        "last_withdraw_id": 0,
        "start_text": (
            "💎 <b>Добро пожаловать в Заработок GMP!</b>\n\n"
            "📋 Выполняй задания\n"
            "📸 Проходи проверку\n"
            "💰 Зарабатывай GMP\n\n"
            "🔥 Выплачено более 500 000+ GMP\n"
            "⚡ Быстрые проверки\n"
            "🛡 Безопасные выплаты\n\n"
            "👇 Выбери действие:"
        )
    }


def fix_data(data):
    base = empty_data()
    for key, value in base.items():
        if key not in data:
            data[key] = value

    for uid, user in data.get("users", {}).items():
        user.setdefault("balance", 0)
        user.setdefault("done_tasks", [])
        user.setdefault("pending_tasks", [])
        user.setdefault("waiting_task", None)
        user.setdefault("withdraw_pending", False)
        user.setdefault("withdraw_step", None)
        user.setdefault("withdraw_to", None)
        user.setdefault("last_bonus", 0)
        user.setdefault("completed_tasks", len(user.get("done_tasks", [])))
        user.setdefault("total_earned", float(user.get("balance", 0)) if float(user.get("balance", 0)) > 0 else 0)
        user.setdefault("withdraw_count", 0)
        user.setdefault("withdrawn_total", 0)
        user.setdefault("fines_total", 0)

    for task_id, task in data.get("tasks", {}).items():
        task.setdefault("active", True)

    return data


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
                return fix_data(json.loads(text))

            if r.status_code == 404:
                data = empty_data()
                save_data(data)
                return data

            print("GitHub load error:", r.status_code, r.text)
        except Exception as e:
            print("GitHub load exception:", e)

    if os.path.exists(LOCAL_DATA_FILE):
        with open(LOCAL_DATA_FILE, "r", encoding="utf-8") as f:
            return fix_data(json.load(f))

    return empty_data()


def save_data(data):
    data = fix_data(data)

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
                print("GitHub save error:", pr.status_code, pr.text)

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
            "pending_tasks": [],
            "waiting_task": None,
            "withdraw_pending": False,
            "withdraw_step": None,
            "withdraw_to": None,
            "last_bonus": 0,
            "completed_tasks": 0,
            "total_earned": 0,
            "withdraw_count": 0,
            "withdrawn_total": 0,
            "fines_total": 0
        }

    user = data["users"][uid]
    user.setdefault("balance", 0)
    user.setdefault("done_tasks", [])
    user.setdefault("pending_tasks", [])
    user.setdefault("waiting_task", None)
    user.setdefault("withdraw_pending", False)
    user.setdefault("withdraw_step", None)
    user.setdefault("withdraw_to", None)
    user.setdefault("last_bonus", 0)
    user.setdefault("completed_tasks", len(user.get("done_tasks", [])))
    user.setdefault("total_earned", float(user.get("balance", 0)) if float(user.get("balance", 0)) > 0 else 0)
    user.setdefault("withdraw_count", 0)
    user.setdefault("withdrawn_total", 0)
    user.setdefault("fines_total", 0)
    return user


def cancel_user_states(user):
    user["waiting_task"] = None
    user["withdraw_step"] = None
    user["withdraw_to"] = None


def format_gmp(amount):
    if isinstance(amount, float) and amount.is_integer():
        return str(int(amount))
    return str(round(amount, 2)).replace(".0", "")



def build_profile_text(user_id, user, username=None, admin_view=False):
    balance = float(user.get("balance", 0))
    completed = int(user.get("completed_tasks", len(user.get("done_tasks", []))))
    withdrawn_total = float(user.get("withdrawn_total", 0))
    withdraw_count = int(user.get("withdraw_count", 0))
    pending_count = len(user.get("pending_tasks", []))
    fines_total = float(user.get("fines_total", 0))

    uname = username or user.get("username") or "нет username"

    debt_text = ""
    if balance < 0:
        debt_text = (
            f"\n\n⚠️ <b>Долг:</b> {format_gmp(abs(balance))} GMP\n"
            "Сначала погасите долг заданиями, потом будет доступен вывод."
        )

    title = "🖥 <b>Профиль игрока</b>" if admin_view else "🖥 <b>Мой Профиль</b>"

    return (
        f"{title}\n\n"
        f"💰 <b>Баланс</b>\n"
        f"{format_gmp(balance)} GMP\n\n"
        f"├ 🆔 Айди: <code>{user_id}</code>\n"
        f"├ 👤 Username: <b>{uname}</b>\n"
        f"├ ✅ Заданий сделал: <b>{completed}</b>\n"
        f"├ ⏳ На проверке: <b>{pending_count}</b>\n"
        f"├ 📤 Выведено: <b>{format_gmp(withdrawn_total)} GMP</b>\n"
        f"├ 📦 Выплат: <b>{withdraw_count}</b>\n"
        f"└ ⚠️ Штрафов: <b>{format_gmp(fines_total)} GMP</b>"
        f"{debt_text}"
    )


def safe_send(user_id, text):
    try:
        bot.send_message(user_id, text)
        return True
    except Exception:
        return False



def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📋 Задания", "💰 Баланс")
    kb.row("🖥 Профиль", "🎉 Бонус")
    kb.row("💸 Вывод", "💬 Общение")
    kb.row("ℹ️ Помощь")
    return kb


def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Добавить задание", "📋 Все задания")
    kb.row("📨 Заявки", "🏠 Меню")
    return kb


@bot.message_handler(commands=["start"])
def start(message):
    data = load_data()
    user = get_user(data, message.from_user.id)
    user["username"] = message.from_user.username or ""
    cancel_user_states(user)
    save_data(data)

    bot.send_message(message.chat.id, data["start_text"], reply_markup=main_menu())


@bot.message_handler(commands=["admin"])
def admin(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")
    bot.send_message(message.chat.id, "🔐 <b>Админ-панель</b>", reply_markup=admin_menu())


@bot.message_handler(func=lambda m: m.text == "🏠 Меню")
def menu(message):
    data = load_data()
    user = get_user(data, message.from_user.id)
    cancel_user_states(user)
    save_data(data)
    bot.send_message(message.chat.id, "🏠 Главное меню", reply_markup=main_menu())


@bot.message_handler(func=lambda m: m.text == "💬 Общение")
def chat_message(message):
    chat_button(message)


@bot.message_handler(func=lambda m: m.text == "ℹ️ Помощь")
def help_message(message):
    bot.send_message(
        message.chat.id,
        "ℹ️ <b>Как работает бот:</b>\n\n"
        "1. Нажми 📋 Задания\n"
        "2. Открой задание и выполни его\n"
        "3. Нажми ✅ Я выполнил\n"
        "4. Отправь скрин\n"
        "5. После проверки получишь GMP\n\n"
        "🎉 Бонус можно получать 1 раз в 24 часа.\n"
        "💸 Для вывода нажми «Вывод», укажи куда вывести и сумму."
    )


@bot.message_handler(func=lambda m: m.text == "💰 Баланс")
def balance(message):
    data = load_data()
    user = get_user(data, message.from_user.id)
    save_data(data)

    my_withdraws = [w for w in data.get("withdraws", {}).values() if str(w.get("user_id")) == str(message.from_user.id) and w.get("status") == "wait"]
    pending = f"\n⏳ Заявок на вывод: {len(my_withdraws)}" if my_withdraws else ""
    bot.send_message(message.chat.id, f"💰 <b>Твой баланс:</b> {format_gmp(user['balance'])} GMP{pending}")



@bot.message_handler(func=lambda m: m.text in ["🖥 Профиль", "👤 Профиль"])
def my_profile(message):
    data = load_data()
    user = get_user(data, message.from_user.id)
    user["username"] = message.from_user.username or ""
    save_data(data)

    username = f"@{message.from_user.username}" if message.from_user.username else "нет username"
    bot.send_message(
        message.chat.id,
        build_profile_text(message.from_user.id, user, username=username)
    )


@bot.message_handler(commands=["profile", "user"])
def admin_profile(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.send_message(
            message.chat.id,
            "❌ Формат:\n<code>/profile user_id</code>\n\nПример:\n<code>/profile 7837011810</code>"
        )

    user_id = parts[1].strip().replace("@", "")
    data = load_data()

    if user_id not in data.get("users", {}):
        return bot.send_message(message.chat.id, "❌ Пользователь не найден в data.json.")

    user = get_user(data, user_id)
    username = user.get("username") or "нет username"
    if username and username != "нет username" and not username.startswith("@"):
        username = "@" + username

    save_data(data)
    bot.send_message(message.chat.id, build_profile_text(user_id, user, username=username, admin_view=True))


@bot.message_handler(func=lambda m: m.text == "🎉 Бонус")
def daily_bonus(message):
    data = load_data()
    user = get_user(data, message.from_user.id)

    now = int(time.time())
    last_bonus = int(user.get("last_bonus", 0))
    left = BONUS_COOLDOWN - (now - last_bonus)

    if left > 0:
        hours = left // 3600
        minutes = (left % 3600) // 60
        save_data(data)
        return bot.send_message(
            message.chat.id,
            f"⏳ Бонус уже получен.\nПриходи через: <b>{hours}ч {minutes}м</b>"
        )

    user["balance"] = round(float(user["balance"]) + BONUS_AMOUNT, 2)
    user["total_earned"] = round(float(user.get("total_earned", 0)) + BONUS_AMOUNT, 2)
    user["last_bonus"] = now
    save_data(data)

    bot.send_message(
        message.chat.id,
        f"🎉 <b>Ежедневный бонус получен!</b>\n\n"
        f"💰 Начислено: <b>{BONUS_AMOUNT} GMP</b>\n"
        f"💎 Баланс: <b>{format_gmp(user['balance'])} GMP</b>"
    )


@bot.message_handler(func=lambda m: m.text == "📋 Задания")
def tasks(message):
    data = load_data()
    user = get_user(data, message.from_user.id)

    kb = types.InlineKeyboardMarkup()
    found = False

    for task_id, task in data["tasks"].items():
        if not task.get("active", True):
            continue
        if task_id in user["done_tasks"]:
            continue
        if task_id in user["pending_tasks"]:
            continue

        found = True
        kb.add(types.InlineKeyboardButton(
            f"✅ Задание #{task_id} — {format_gmp(task['reward'])} GMP",
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

    if task_id in user["pending_tasks"]:
        return bot.answer_callback_query(call.id, "Задание уже на проверке.")

    task = data["tasks"].get(task_id)
    if not task or not task.get("active", True):
        return bot.answer_callback_query(call.id, "Задание не найдено.")

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔗 Перейти", url=task["link"]))
    kb.add(types.InlineKeyboardButton("✅ Я выполнил", callback_data=f"done_{task_id}"))

    bot.send_message(
        call.message.chat.id,
        f"✅ <b>Задание #{task_id}</b>\n\n"
        f"{task['text']}\n\n"
        f"💰 Награда: <b>{format_gmp(task['reward'])} GMP</b>\n\n"
        f"После выполнения нажми ✅ Я выполнил и отправь скрин.",
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("done_"))
def done_task(call):
    data = load_data()
    user = get_user(data, call.from_user.id)
    task_id = call.data.split("_")[1]

    if task_id in user["done_tasks"]:
        return bot.answer_callback_query(call.id, "Ты уже сделал это задание.")

    if task_id in user["pending_tasks"]:
        return bot.answer_callback_query(call.id, "Задание уже на проверке.")

    if task_id not in data["tasks"]:
        return bot.answer_callback_query(call.id, "Задание не найдено.")

    user["waiting_task"] = task_id
    user["withdraw_step"] = None
    user["withdraw_to"] = None
    save_data(data)

    bot.send_message(call.message.chat.id, "📸 Отправь скриншот выполнения задания.")


@bot.message_handler(content_types=["photo"])
def photo(message):
    data = load_data()
    user = get_user(data, message.from_user.id)

    task_id = user.get("waiting_task")
    if not task_id:
        return bot.send_message(message.chat.id, "❌ Сначала выбери задание и нажми ✅ Я выполнил.")

    if task_id in user["done_tasks"]:
        user["waiting_task"] = None
        save_data(data)
        return bot.send_message(message.chat.id, "✅ Ты уже выполнил это задание.")

    if task_id in user["pending_tasks"]:
        user["waiting_task"] = None
        save_data(data)
        return bot.send_message(message.chat.id, "⏳ Это задание уже на проверке.")

    task = data["tasks"].get(task_id)
    if not task:
        user["waiting_task"] = None
        save_data(data)
        return bot.send_message(message.chat.id, "❌ Задание не найдено.")

    data["last_submit_id"] += 1
    sid = str(data["last_submit_id"])

    user["waiting_task"] = None
    user["pending_tasks"].append(task_id)

    data["submits"][sid] = {
        "user_id": message.from_user.id,
        "username": message.from_user.username or "",
        "task_id": task_id,
        "reward": float(task["reward"]),
        "status": "wait",
        "photo_file_id": message.photo[-1].file_id,
        "time": int(time.time())
    }

    save_data(data)

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"yes_{sid}"),
        types.InlineKeyboardButton("❌ Отказать", callback_data=f"no_{sid}")
    )

    bot.send_message(message.chat.id, "✅ Скрин отправлен админу на проверку.\n⏳ Задание временно скрыто из списка.")

    bot.send_photo(
        ADMIN_ID,
        message.photo[-1].file_id,
        caption=
        f"📨 <b>Новая заявка #{sid}</b>\n\n"
        f"✅ Задание: #{task_id}\n"
        f"💰 Награда: {format_gmp(task['reward'])} GMP\n"
        f"👤 Пользователь: @{message.from_user.username or 'нет username'}\n"
        f"🆔 ID: <code>{message.from_user.id}</code>",
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("yes_") or c.data.startswith("no_"))
def check_request(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "Нет доступа.")

    data = load_data()
    action, sid = call.data.split("_", 1)
    submit = data["submits"].get(sid)

    if not submit or submit.get("status") != "wait":
        return bot.answer_callback_query(call.id, "Заявка уже обработана.")

    user_id = str(submit["user_id"])
    task_id = str(submit["task_id"])
    reward = float(submit["reward"])
    user = get_user(data, user_id)

    if task_id in user["pending_tasks"]:
        user["pending_tasks"].remove(task_id)

    if action == "yes":
        if task_id not in user["done_tasks"]:
            user["balance"] = round(float(user["balance"]) + reward, 2)
            user["total_earned"] = round(float(user.get("total_earned", 0)) + reward, 2)
            user["completed_tasks"] = int(user.get("completed_tasks", len(user.get("done_tasks", [])))) + 1
            user["done_tasks"].append(task_id)

        del data["submits"][sid]
        save_data(data)

        bot.send_message(user_id, f"🎉 Задание #{task_id} одобрено!\n💰 Начислено: <b>{format_gmp(reward)} GMP</b>")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id, caption=f"✅ Заявка #{sid} одобрена.")
    else:
        del data["submits"][sid]
        save_data(data)

        bot.send_message(
            user_id,
            f"❌ <b>Задание #{task_id} отклонено</b>\n\n"
            "Проверьте, что вы выполнили задание до конца и отправили правильный скриншот.\n\n"
            "После проверки можете попробовать снова ✅"
        )
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id, caption=f"❌ Заявка #{sid} отклонена.")


@bot.message_handler(func=lambda m: m.text == "💸 Вывод")
def withdraw(message):
    data = load_data()
    user = get_user(data, message.from_user.id)

    if float(user["balance"]) < 0:
        return bot.send_message(
            message.chat.id,
            f"❌ Вывод недоступен.\n\nУ тебя долг: <b>{format_gmp(abs(float(user['balance'])))} GMP</b>\nСначала погаси долг заданиями."
        )

    if float(user["balance"]) <= 0:
        return bot.send_message(message.chat.id, "❌ У тебя нет GMP для вывода.")


    user["withdraw_step"] = "username"
    user["withdraw_to"] = None
    user["waiting_task"] = None
    save_data(data)

    bot.send_message(
        message.chat.id,
        "💎 <b>Куда вывести GMP</b>\n\n"
        "Пример <code>@username</code>"
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("payyes_") or c.data.startswith("payno_"))
def pay_check(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "Нет доступа.")

    data = load_data()
    action, wid = call.data.split("_", 1)
    w = data["withdraws"].get(wid)

    if not w or w.get("status") != "wait":
        return bot.answer_callback_query(call.id, "Заявка уже обработана.")

    user = get_user(data, w["user_id"])
    amount = float(w["amount"])

    if action == "payyes":
        user["withdraw_pending"] = False
        user["withdraw_count"] = int(user.get("withdraw_count", 0)) + 1
        user["withdrawn_total"] = round(float(user.get("withdrawn_total", 0)) + amount, 2)
        del data["withdraws"][wid]
        save_data(data)

        bot.send_message(
            w["user_id"],
            f"✅ <b>Заказ #{wid} выполнен!</b>\n\n"
            "💎 GMP успешно выданы 💜\n\n"
            "Спасибо за использование «Заработок GMP» ✨"
        )
        bot.edit_message_text("✅ Выплата подтверждена.", call.message.chat.id, call.message.message_id)

    else:
        user["balance"] = round(float(user["balance"]) + amount, 2)
        user["withdraw_pending"] = False
        del data["withdraws"][wid]
        save_data(data)

        bot.send_message(w["user_id"], f"❌ Заявка на вывод #{wid} отклонена.\n💰 <b>{format_gmp(amount)} GMP</b> возвращены на баланс.")
        bot.edit_message_text("❌ Выплата отклонена. GMP возвращены.", call.message.chat.id, call.message.message_id)


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
        reward = float(reward.replace(",", "."))
    except Exception:
        return bot.send_message(message.chat.id, "❌ Награда должна быть числом.")

    if reward <= 0:
        return bot.send_message(message.chat.id, "❌ Награда должна быть больше 0.")

    if not link.startswith("http://") and not link.startswith("https://"):
        return bot.send_message(message.chat.id, "❌ Ссылка должна начинаться с https://")

    data = load_data()
    data["last_task_id"] += 1
    task_id = str(data["last_task_id"])

    data["tasks"][task_id] = {
        "text": task_text,
        "link": link,
        "reward": reward,
        "active": True
    }

    save_data(data)

    bot.send_message(
        message.chat.id,
        f"✅ <b>Задание #{task_id} создано</b>\n\n"
        f"🔗 Ссылка:\n{link}\n\n"
        f"{task_text}\n\n"
        f"💰 Награда: {format_gmp(reward)} GMP"
    )


@bot.message_handler(commands=["edittext"])
def edit_task_text(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        return bot.send_message(message.chat.id, "❌ Формат:\n/edittext 39 Новый текст задания")

    task_id, new_text = parts[1], parts[2]
    data = load_data()

    if task_id not in data["tasks"]:
        return bot.send_message(message.chat.id, "❌ Задание не найдено.")

    data["tasks"][task_id]["text"] = new_text
    save_data(data)
    bot.send_message(message.chat.id, f"✅ Текст задания #{task_id} изменён.")


@bot.message_handler(commands=["editlink"])
def edit_task_link(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        return bot.send_message(message.chat.id, "❌ Формат:\n/editlink 39 https://t.me/example")

    task_id, new_link = parts[1], parts[2].strip()
    if not new_link.startswith("http://") and not new_link.startswith("https://"):
        return bot.send_message(message.chat.id, "❌ Ссылка должна начинаться с https://")

    data = load_data()
    if task_id not in data["tasks"]:
        return bot.send_message(message.chat.id, "❌ Задание не найдено.")

    data["tasks"][task_id]["link"] = new_link
    save_data(data)
    bot.send_message(message.chat.id, f"✅ Ссылка задания #{task_id} изменена.")


@bot.message_handler(commands=["editreward"])
def edit_task_reward(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        return bot.send_message(message.chat.id, "❌ Формат:\n/editreward 39 2")

    task_id, reward_text = parts[1], parts[2].strip()

    try:
        reward = float(reward_text.replace(",", "."))
    except Exception:
        return bot.send_message(message.chat.id, "❌ Награда должна быть числом.")

    if reward <= 0:
        return bot.send_message(message.chat.id, "❌ Награда должна быть больше 0.")

    data = load_data()
    if task_id not in data["tasks"]:
        return bot.send_message(message.chat.id, "❌ Задание не найдено.")

    data["tasks"][task_id]["reward"] = reward
    save_data(data)
    bot.send_message(message.chat.id, f"✅ Награда задания #{task_id} изменена на {format_gmp(reward)} GMP.")


@bot.message_handler(commands=["deletetask"])
def delete_task(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "❌ Формат:\n/deletetask 39")

    task_id = parts[1].strip()
    data = load_data()

    if task_id not in data["tasks"]:
        return bot.send_message(message.chat.id, "❌ Задание не найдено.")

    del data["tasks"][task_id]
    save_data(data)
    bot.send_message(message.chat.id, f"✅ Задание #{task_id} удалено.")


@bot.message_handler(commands=["setstart"])
def set_start_text(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    new_text = message.text.replace("/setstart", "", 1).strip()
    if not new_text:
        return bot.send_message(
            message.chat.id,
            "❌ Формат:\n/setstart Новый стартовый текст\n\nМожно использовать HTML: <b>жирный</b>"
        )

    data = load_data()
    data["start_text"] = new_text
    save_data(data)
    bot.send_message(message.chat.id, "✅ Стартовый текст изменён.")


@bot.message_handler(commands=["give"])
def give_gmp(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split()
    if len(parts) != 3:
        return bot.send_message(message.chat.id, "❌ Формат:\n/give user_id сумма")

    user_id = parts[1]
    try:
        amount = float(parts[2].replace(",", "."))
    except Exception:
        return bot.send_message(message.chat.id, "❌ Сумма должна быть числом.")

    data = load_data()
    user = get_user(data, user_id)
    user["balance"] = round(float(user["balance"]) + amount, 2)
    user["total_earned"] = round(float(user.get("total_earned", 0)) + amount, 2)
    save_data(data)

    bot.send_message(message.chat.id, f"✅ Пользователю {user_id} начислено {format_gmp(amount)} GMP.")
    try:
        bot.send_message(user_id, f"🎁 Админ начислил тебе <b>{format_gmp(amount)} GMP</b>.")
    except Exception:
        pass



@bot.message_handler(commands=["fine"])
def fine_user(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=3)
    if len(parts) < 3:
        return bot.send_message(
            message.chat.id,
            "❌ Формат:\n<code>/fine user_id сумма причина</code>\n\n"
            "Пример:\n<code>/fine 7837011810 3 Спам заявками</code>"
        )

    user_id = parts[1].strip()
    try:
        amount = float(parts[2].replace(",", "."))
    except Exception:
        return bot.send_message(message.chat.id, "❌ Сумма должна быть числом.")

    if amount <= 0:
        return bot.send_message(message.chat.id, "❌ Сумма штрафа должна быть больше 0.")

    reason = parts[3].strip() if len(parts) >= 4 else "Нарушение правил"

    data = load_data()
    user = get_user(data, user_id)

    old_balance = float(user.get("balance", 0))
    user["balance"] = round(old_balance - amount, 2)
    user["fines_total"] = round(float(user.get("fines_total", 0)) + amount, 2)

    save_data(data)

    bot.send_message(
        message.chat.id,
        f"✅ Штраф выдан\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💸 Штраф: <b>{format_gmp(amount)} GMP</b>\n"
        f"💰 Баланс: <b>{format_gmp(old_balance)} → {format_gmp(user['balance'])} GMP</b>\n"
        f"📌 Причина: {reason}"
    )

    safe_send(
        user_id,
        f"⚠️ <b>Вам выдан штраф</b>\n\n"
        f"💸 Списано: <b>{format_gmp(amount)} GMP</b>\n"
        f"📌 Причина: {reason}\n\n"
        f"💰 Баланс: <b>{format_gmp(user['balance'])} GMP</b>\n\n"
        "Если баланс стал минусовым, выполните задания, чтобы погасить долг."
    )


@bot.message_handler(func=lambda m: m.text == "➕ Добавить задание")
def add_task_info(message):
    if message.from_user.id != ADMIN_ID:
        return

    bot.send_message(
        message.chat.id,
        "➕ <b>Добавление задания:</b>\n\n"
        "<code>/addtask Текст задания | ссылка | награда</code>\n\n"
        "Пример:\n"
        "<code>/addtask Зайти в бота и подписаться | https://t.me/example | 1</code>\n\n"
        "✏️ <b>Редактирование:</b>\n"
        "<code>/edittext 39 Новый текст</code>\n"
        "<code>/editlink 39 https://t.me/example</code>\n"
        "<code>/editreward 39 2</code>\n"
        "<code>/deletetask 39</code>\n"
        "<code>/setstart Новый текст старта</code>\n"
        "<code>/profile user_id</code> — профиль игрока\n"
        "<code>/fine user_id сумма причина</code> — штраф"
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
        status = "✅" if task.get("active", True) else "❌"
        text += f"{status} #{task_id} — {format_gmp(task['reward'])} GMP\n{task['text']}\n\n"

    bot.send_message(message.chat.id, text)


def send_withdraw_request(chat_id, wid, w):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Выплачено", callback_data=f"payyes_{wid}"),
        types.InlineKeyboardButton("❌ Отказать", callback_data=f"payno_{wid}")
    )
    bot.send_message(
        chat_id,
        f"💸 <b>Заявка на вывод #{wid}</b>\n\n"
        f"👤 Пользователь: @{w.get('username') or 'нет username'}\n"
        f"🆔 ID: <code>{w.get('user_id')}</code>\n"
        f"📤 Куда вывести: <b>{w.get('to', 'не указано')}</b>\n"
        f"💰 Сумма: <b>{format_gmp(float(w.get('amount', 0)))} GMP</b>",
        reply_markup=kb
    )


def send_submit_request(chat_id, sid, submit):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"yes_{sid}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"no_{sid}")
    )
    caption = (
        f"📸 <b>Заявка на задание #{sid}</b>\n\n"
        f"✅ Задание: #{submit.get('task_id')}\n"
        f"💰 Награда: {format_gmp(float(submit.get('reward', 0)))} GMP\n"
        f"👤 Пользователь: @{submit.get('username') or 'нет username'}\n"
        f"🆔 ID: <code>{submit.get('user_id')}</code>"
    )
    photo_id = submit.get("photo_file_id")
    if photo_id:
        bot.send_photo(chat_id, photo_id, caption=caption, reply_markup=kb)
    else:
        bot.send_message(chat_id, caption + "\n\n⚠️ Фото не сохранено в старой заявке.", reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == "📨 Заявки")
@bot.message_handler(commands=["requests", "zayavki"])
def requests_msg(message):
    if message.from_user.id != ADMIN_ID:
        return

    data = load_data()
    submits = data.get("submits", {})
    withdraws = data.get("withdraws", {})

    if not submits and not withdraws:
        return bot.send_message(message.chat.id, "✅ Активных заявок нет.")

    bot.send_message(
        message.chat.id,
        f"📨 <b>Активные заявки:</b>\n\n"
        f"📸 Задания: {len(submits)}\n"
        f"💸 Выводы: {len(withdraws)}\n\n"
        "Ниже отправляю заявки с кнопками 👇"
    )

    for sid, submit in list(submits.items()):
        if submit.get("status") == "wait":
            send_submit_request(message.chat.id, sid, submit)

    for wid, w in list(withdraws.items()):
        if w.get("status") == "wait":
            send_withdraw_request(message.chat.id, wid, w)


@bot.message_handler(commands=["send"])
def admin_broadcast(message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        return bot.reply_to(message, "Использование: /send текст")

    broadcast_text = parts[1].strip()
    data = load_data()

    sent = 0
    errors = 0

    for uid in list(data.get("users", {}).keys()):
        try:
            bot.send_message(int(uid), broadcast_text)
            sent += 1
            time.sleep(0.05)
        except Exception:
            errors += 1

    bot.reply_to(
        message,
        f"✅ Рассылка завершена\n\n📨 Отправлено: {sent}\n❌ Ошибок: {errors}"
    )


@bot.message_handler(func=lambda m: True)
def text_router(message):
    data = load_data()
    user = get_user(data, message.from_user.id)

    text = (message.text or "").strip()

    if text.lower() in ["отмена", "cancel", "назад"]:
        cancel_user_states(user)
        save_data(data)
        return bot.send_message(message.chat.id, "✅ Действие отменено.", reply_markup=main_menu())

    if user.get("withdraw_step") == "username":
        withdraw_to = text

        if len(withdraw_to) < 3:
            return bot.send_message(message.chat.id, "❌ Напиши нормальный username/ID для вывода.")

        user["withdraw_to"] = withdraw_to
        user["withdraw_step"] = "amount"
        save_data(data)

        return bot.send_message(
            message.chat.id,
            "💰 <b>Сколько GMP вывести?</b>\n\n"
            "Напишите сумму GMP для вывода."
        )

    if user.get("withdraw_step") == "amount":
        amount_text = text.lower()

        if amount_text in ["все", "all", "всё"]:
            amount = float(user["balance"])
        else:
            try:
                amount = float(amount_text.replace(",", "."))
            except Exception:
                return bot.send_message(message.chat.id, "❌ Напиши число. Например: <code>10</code>")

        if amount <= 0:
            return bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0.")

        if amount < 1:
            return bot.send_message(message.chat.id, "❌ Минимальная сумма для вывода: 1 GMP.")

        if amount > float(user["balance"]):
            return bot.send_message(
                message.chat.id,
                f"❌ Недостаточно GMP.\nТвой баланс: <b>{format_gmp(user['balance'])} GMP</b>"
            )

        data["last_withdraw_id"] += 1
        wid = str(data["last_withdraw_id"])
        withdraw_to = user.get("withdraw_to") or "не указано"

        user["balance"] = round(float(user["balance"]) - amount, 2)
        user["withdraw_step"] = None
        user["withdraw_to"] = None

        data["withdraws"][wid] = {
            "user_id": message.from_user.id,
            "username": message.from_user.username or "",
            "to": withdraw_to,
            "amount": amount,
            "status": "wait",
            "time": int(time.time())
        }

        save_data(data)

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ Выплачено", callback_data=f"payyes_{wid}"),
            types.InlineKeyboardButton("❌ Отказать", callback_data=f"payno_{wid}")
        )

        bot.send_message(
            message.chat.id,
            f"✅ <b>Заявка на вывод #{wid} создана</b>\n\n"
            f"👤 Куда: <b>{withdraw_to}</b>\n"
            f"💰 Сумма: <b>{format_gmp(amount)} GMP</b>\n\n"
            "⏳ Заявка отправлена на проверку.\n"
            "Ожидайте выплату до 24 часов."
        )

        return bot.send_message(
            ADMIN_ID,
            f"💸 <b>Новая заявка на вывод #{wid}</b>\n\n"
            f"👤 Пользователь: @{message.from_user.username or 'нет username'}\n"
            f"🆔 ID: <code>{message.from_user.id}</code>\n"
            f"📤 Куда вывести: <b>{withdraw_to}</b>\n"
            f"💰 Сумма: <b>{format_gmp(amount)} GMP</b>",
            reply_markup=kb
        )

    bot.send_message(message.chat.id, "👇 Выбери кнопку в меню.", reply_markup=main_menu())



if __name__ == "__main__":
    if not TOKEN:
        print("❌ BOT_TOKEN не найден.")
        exit()

    threading.Thread(target=run_site, daemon=True).start()
    threading.Thread(target=auto_ping, daemon=True).start()
    print("✅ Bot started")

    while True:
        try:
            bot.remove_webhook()
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:
            print("Polling error:", e)
            time.sleep(10)

