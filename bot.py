import os
import json
import base64
import threading
import time
from threading import RLock
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
DATA_LOCK = RLock()
WITHDRAW_CREATE_LOCK = RLock()


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
        "promocodes": {},
        "processed_requests": {"submits": {}, "withdraws": {}},
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

    # Защита от повторной обработки заявок/выводов
    data.setdefault("processed_requests", {})
    if not isinstance(data.get("processed_requests"), dict):
        data["processed_requests"] = {}
    data["processed_requests"].setdefault("submits", {})
    data["processed_requests"].setdefault("withdraws", {})

    # Промокоды
    data.setdefault("promocodes", {})
    if not isinstance(data.get("promocodes"), dict):
        data["promocodes"] = {}

    for code, promo in list(data.get("promocodes", {}).items()):
        if not isinstance(promo, dict):
            data["promocodes"].pop(code, None)
            continue

        promo.setdefault("amount", 0)
        promo.setdefault("left", 0)
        promo.setdefault("created_by", 0)
        promo.setdefault("used_by", [])
        promo.setdefault("time", int(time.time()))

        try:
            promo["amount"] = round(float(promo.get("amount", 0)), 2)
            promo["left"] = int(promo.get("left", 0))
        except Exception:
            data["promocodes"].pop(code, None)
            continue

        if promo["amount"] <= 0 or promo["left"] <= 0:
            data["promocodes"].pop(code, None)

    # Авто-починка зависших статусов: если заявка есть — pending True, если нет — False.
    active_withdraw_users = {str(w.get("user_id")) for w in data.get("withdraws", {}).values() if w.get("status") == "wait"}
    active_submit_pairs = {(str(s.get("user_id")), str(s.get("task_id"))) for s in data.get("submits", {}).values() if s.get("status") == "wait"}

    for uid, user in data.get("users", {}).items():
        user["withdraw_pending"] = str(uid) in active_withdraw_users
        # Убираем зависшие pending_tasks, если заявки уже нет.
        user["pending_tasks"] = [str(tid) for tid in user.get("pending_tasks", []) if (str(uid), str(tid)) in active_submit_pairs]
        # Убираем дубли выполненных заданий.
        user["done_tasks"] = list(dict.fromkeys(str(tid) for tid in user.get("done_tasks", [])))

    return data


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }


def github_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"


def load_data():
    # ВАЖНО: сначала читаем локальный data.json.
    # Так быстрые шаги вывода (@username -> сумма) не теряются, пока GitHub обновляется.
    if os.path.exists(LOCAL_DATA_FILE):
        try:
            with open(LOCAL_DATA_FILE, "r", encoding="utf-8") as f:
                return fix_data(json.load(f))
        except Exception as e:
            print("Local load exception:", e)

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
                file_text = base64.b64decode(content).decode("utf-8")
                data = fix_data(json.loads(file_text))

                # сохраняем локально, чтобы следующие сообщения читали свежую базу
                try:
                    with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

                return data

            if r.status_code == 404:
                data = empty_data()
                save_data(data)
                return data

            print("GitHub load error:", r.status_code, r.text)
        except Exception as e:
            print("GitHub load exception:", e)

    return empty_data()


def load_data_from_github_only():
    """
    Читает data.json прямо с GitHub, не используя локальный файл.
    Нужно для админ-кнопок, если Render взял старый локальный файл
    или после перезапуска локальный data.json был пустой/старый.
    """
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return None

    try:
        r = requests.get(
            github_url(),
            headers=github_headers(),
            params={"ref": GITHUB_BRANCH},
            timeout=10
        )

        if r.status_code != 200:
            print("GitHub fresh load error:", r.status_code, r.text)
            return None

        content = r.json()["content"]
        file_text = base64.b64decode(content).decode("utf-8")
        return fix_data(json.loads(file_text))
    except Exception as e:
        print("GitHub fresh load exception:", e)
        return None


def find_request_by_id(requests_dict, request_id):
    """
    Безопасно ищет заявку по ID.
    Исправляет баги, когда ID в кнопке строкой '68',
    а в файле мог сохраниться как 68, '068' или '#68'.
    """
    rid = str(request_id).strip().replace("#", "")

    if not isinstance(requests_dict, dict):
        return None, None

    variants = [rid, f"#{rid}", rid.lstrip("0") or "0"]
    for key in variants:
        if key in requests_dict:
            return str(key), requests_dict[key]

    for key, value in requests_dict.items():
        clean_key = str(key).strip().replace("#", "")
        if clean_key == rid or clean_key.lstrip("0") == rid.lstrip("0"):
            return str(key), value

    return None, None




def request_already_processed(data, kind, request_id):
    rid = str(request_id).strip().replace("#", "")
    processed = data.setdefault("processed_requests", {}).setdefault(kind, {})
    return rid in processed


def mark_request_processed(data, kind, request_id, status, admin_id=0, user_id=0, note=""):
    rid = str(request_id).strip().replace("#", "")
    processed = data.setdefault("processed_requests", {}).setdefault(kind, {})
    processed[rid] = {
        "status": status,
        "admin_id": int(admin_id or 0),
        "user_id": str(user_id or ""),
        "note": note,
        "time": int(time.time())
    }

    # Чтобы data.json не раздувался: храним только последние 300 обработанных ID.
    if len(processed) > 300:
        old_keys = sorted(processed.keys(), key=lambda k: processed[k].get("time", 0))[:-300]
        for k in old_keys:
            processed.pop(k, None)


def has_active_submit(data, user_id, task_id):
    for sid, submit in data.get("submits", {}).items():
        if (
            str(submit.get("user_id")) == str(user_id)
            and str(submit.get("task_id")) == str(task_id)
            and submit.get("status") == "wait"
        ):
            return str(sid)
    return None


def has_active_withdraw(data, user_id):
    for wid, withdraw in data.get("withdraws", {}).items():
        if str(withdraw.get("user_id")) == str(user_id) and withdraw.get("status") == "wait":
            return str(wid)
    return None


def find_recent_duplicate_withdraw(data, user_id, withdraw_to, amount, window_seconds=120):
    """
    Ищет дубль вывода: тот же пользователь + та же сумма + тот же получатель
    за последние window_seconds секунд. Нужен, если Telegram/интернет случайно
    повторил один и тот же шаг, чтобы не создавать новый номер заказа.
    """
    now = int(time.time())
    for wid, withdraw in data.get("withdraws", {}).items():
        if withdraw.get("status") != "wait":
            continue
        if str(withdraw.get("user_id")) != str(user_id):
            continue
        if str(withdraw.get("to", "")).strip().lower() != str(withdraw_to).strip().lower():
            continue
        try:
            same_amount = float(withdraw.get("amount", 0)) == float(amount)
        except Exception:
            same_amount = False
        if not same_amount:
            continue
        try:
            created_time = int(withdraw.get("time", 0))
        except Exception:
            created_time = 0
        if now - created_time <= window_seconds:
            return str(wid)
    return None

def save_data(data):
    data = fix_data(data)

    # Сначала сохраняем локально атомарно, чтобы файл не ломался при перезапуске/ошибке.
    try:
        tmp_file = LOCAL_DATA_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, LOCAL_DATA_FILE)
    except Exception as e:
        print("Local save exception:", e)

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

    # локально уже сохранено выше


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
    try:
        amount = float(amount)
    except Exception:
        amount = 0
    if amount.is_integer():
        return str(int(amount))
    return str(round(amount, 2)).replace(".0", "")


def parse_gmp_amount(raw):
    try:
        amount = float(str(raw).replace(",", ".").strip())
    except Exception:
        return None
    if amount <= 0:
        return None
    return round(amount, 2)


def resolve_user_id(data, user_query):
    """
    Ищет пользователя по ID или @username.
    Важно: по username можно найти только того, кто уже нажимал /start в боте.
    """
    q = str(user_query).strip()
    if not q:
        return None, "Пользователь не указан."

    if q.startswith("@"):
        clean = q[1:].lower()
        matches = []
        for uid, u in data.get("users", {}).items():
            username = str(u.get("username", "")).replace("@", "").lower()
            if username == clean:
                matches.append(str(uid))

        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "Найдено несколько пользователей с таким username. Используй ID."
        return None, "Username не найден. Пусть человек нажмёт /start в боте или дай его ID."

    # ID должен быть числом, чтобы случайно не создать мусорного пользователя.
    if not q.isdigit():
        return None, "Неверный ID. Используй ID цифрами или @username."

    return q, None


def add_balance_to_user(data, user_id, amount):
    """
    Единая функция начисления GMP.

    Работает правильно и с минусовым балансом:
    было -2 GMP, начислили +1 GMP -> стало -1 GMP
    было -2 GMP, начислили +5 GMP -> стало 3 GMP

    В data.json НЕ создаётся никаких старых записей баланса.
    Просто перезаписывается поле balance на новое значение.
    """
    user = get_user(data, user_id)
    amount = round(float(amount), 2)
    old_balance = round(float(user.get("balance", 0)), 2)
    new_balance = round(old_balance + amount, 2)

    user["balance"] = new_balance
    user["total_earned"] = round(float(user.get("total_earned", 0)) + amount, 2)

    # Статистика погашения долга: только для админа/проверки, на баланс не влияет.
    if old_balance < 0 and amount > 0:
        paid_debt = min(abs(old_balance), amount)
        user["debt_paid_total"] = round(float(user.get("debt_paid_total", 0)) + paid_debt, 2)

    return user, old_balance, new_balance



def build_admin_profile_text(data, user_id, user, username=None):
    balance = float(user.get("balance", 0))
    uname = username or user.get("username") or "нет username"

    users_count = len(data.get("users", {}))
    tasks_count = len(data.get("tasks", {}))
    active_tasks_count = sum(1 for t in data.get("tasks", {}).values() if t.get("active", True))
    submits_wait = sum(1 for s in data.get("submits", {}).values() if s.get("status") == "wait")
    withdraws_wait = sum(1 for w in data.get("withdraws", {}).values() if w.get("status") == "wait")
    negative_balances = sum(1 for u in data.get("users", {}).values() if float(u.get("balance", 0)) < 0)

    # Дополнительная статистика только для админа
    total_balance = round(
        sum(float(u.get("balance", 0)) for u in data.get("users", {}).values()),
        2
    )
    total_paid = round(
        sum(float(u.get("withdrawn_total", 0)) for u in data.get("users", {}).values()),
        2
    )
    total_withdraws = sum(
        int(u.get("withdraw_count", 0)) for u in data.get("users", {}).values()
    )

    return (
        "👑 <b>Профиль администратора</b>\n\n"
        f"💰 <b>Твой баланс:</b> {format_gmp(balance)} GMP\n"
        f"🆔 <b>Твой ID:</b> <code>{user_id}</code>\n"
        f"👤 <b>Username:</b> <b>{uname}</b>\n\n"
        "📊 <b>Статистика бота</b>\n"
        f"├ 👥 Пользователей: <b>{users_count}</b>\n"
        f"├ 📋 Всего заданий: <b>{tasks_count}</b>\n"
        f"├ ✅ Активных заданий: <b>{active_tasks_count}</b>\n"
        f"├ 📨 Заявок пользователей: <b>{submits_wait}</b>\n"
        f"├ 💸 Заявок на вывод: <b>{withdraws_wait}</b>\n"
        f"├ ⚠️ Минусовых балансов: <b>{negative_balances}</b>\n"
        f"├ 💰 Общий баланс пользователей: <b>{format_gmp(total_balance)}</b> GMP\n"
        f"├ 🧾 Всего выплачено: <b>{format_gmp(total_paid)}</b> GMP\n"
        f"└ 🏦 Всего выводов: <b>{total_withdraws}</b>\n\n"
        "⚙️ <b>Админ-команды</b>\n"
        "<code>/profile user_id</code> — профиль игрока\n"
        "<code>/give user_id сумма</code> — начислить GMP\n"
        "<code>/take user_id сумма</code> — списать GMP\n"
        "<code>/requests</code> — активные заявки\n"
        "<code>/promo КОД сумма активации</code> — создать промокод\n"
        "<code>/promos</code> — список промокодов\n"
        "<code>/delpromo КОД</code> — удалить промокод"
    )


def build_profile_text(user_id, user, username=None, admin_view=False, data=None):
    balance = float(user.get("balance", 0))
    completed = int(user.get("completed_tasks", len(user.get("done_tasks", []))))
    withdrawn_total = float(user.get("withdrawn_total", 0))
    withdraw_count = int(user.get("withdraw_count", 0))
    pending_count = len(user.get("pending_tasks", []))
    fines_total = float(user.get("fines_total", 0))

    pending_withdraws = 0
    if data:
        for w in data.get("withdraws", {}).values():
            if str(w.get("user_id")) == str(user_id) and w.get("status") == "wait":
                pending_withdraws += 1

    uname = username or user.get("username") or "нет username"

    extra_text = ""
    if balance < 0:
        extra_text = (
            f"\n\n⚠️ <b>Долг:</b> {format_gmp(abs(balance))} GMP\n"
            "Сначала погасите долг заданиями, потом будет доступен вывод."
        )
    elif pending_withdraws > 0:
        extra_text = f"\n\n⏳ <b>Заявок на вывод на проверке:</b> {pending_withdraws}"

    title = "🖥 <b>Профиль игрока</b>" if admin_view else "🖥 <b>Мой Профиль</b>"

    return (
        f"{title}\n\n"
        f"💰 <b>Баланс</b>\n"
        f"{format_gmp(balance)} GMP\n\n"
        f"├ 🆔 Айди: <code>{user_id}</code>\n"
        f"├ 👤 Username: <b>{uname}</b>\n"
        f"├ ✅ Заданий сделал: <b>{completed}</b>\n"
        f"├ ⏳ На проверке: <b>{pending_count}</b>\n"
        f"├ 💸 Выводов ждёт: <b>{pending_withdraws}</b>\n"
        f"├ 📤 Выведено: <b>{format_gmp(withdrawn_total)} GMP</b>\n"
        f"├ 📦 Выплат: <b>{withdraw_count}</b>\n"
        f"└ ⚠️ Штрафов: <b>{format_gmp(fines_total)} GMP</b>"
        f"{extra_text}"
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
    kb.row("💎 Начислить баланс", "📨 Заявки")
    kb.row("🏠 Меню")
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
    if message.from_user.id == ADMIN_ID:
        text = build_admin_profile_text(data, message.from_user.id, user, username=username)
    else:
        text = build_profile_text(message.from_user.id, user, username=username, data=data)

    bot.send_message(message.chat.id, text)


@bot.message_handler(commands=["profile", "user", "whois"])
def admin_profile(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.send_message(
            message.chat.id,
            "❌ Формат:\n<code>/profile user_id</code>\n<code>/profile @username</code>\n\n"
            "Пример:\n<code>/profile 7837011810</code>"
        )

    query = parts[1].strip()
    data = load_data()

    found_id = None
    clean_query = query.replace("@", "").lower()

    if query.isdigit() and query in data.get("users", {}):
        found_id = query
    else:
        for uid, u in data.get("users", {}).items():
            uname = str(u.get("username", "")).replace("@", "").lower()
            if uname and uname == clean_query:
                found_id = uid
                break

    if not found_id:
        return bot.send_message(message.chat.id, "❌ Пользователь не найден в data.json.")

    user = get_user(data, found_id)
    username = user.get("username") or "нет username"
    if username and username != "нет username" and not username.startswith("@"):
        username = "@" + username

    save_data(data)
    bot.send_message(message.chat.id, build_profile_text(found_id, user, username=username, admin_view=True, data=data))


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

    add_balance_to_user(data, message.from_user.id, BONUS_AMOUNT)
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

    old_sid = has_active_submit(data, call.from_user.id, task_id)
    if task_id in user["pending_tasks"] or old_sid:
        if old_sid and task_id not in user.get("pending_tasks", []):
            user.setdefault("pending_tasks", []).append(task_id)
            save_data(data)
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

    old_sid = has_active_submit(data, message.from_user.id, task_id)
    if old_sid:
        user["waiting_task"] = None
        if task_id not in user.get("pending_tasks", []):
            user.setdefault("pending_tasks", []).append(task_id)
        save_data(data)
        return bot.send_message(
            message.chat.id,
            f"⚠️ Это задание уже отправлено на проверку.\nНомер заявки: <b>#{old_sid}</b>"
        )

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
    kb.add(types.InlineKeyboardButton("✅ Одобрить", callback_data=f"yes_{sid}"))
    kb.add(types.InlineKeyboardButton("❌ Отказать", callback_data=f"no_{sid}"))
    kb.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"del_{sid}"))

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




def safe_answer_callback(call, text=None, show_alert=False):
    try:
        bot.answer_callback_query(call.id, text or "✅ Готово", show_alert=show_alert)
    except Exception as e:
        print("answer_callback_query error:", e)


def safe_edit_admin_message(call, text):
    # Убираем кнопки и меняем сообщение админа.
    # Если это фото — меняем caption, если обычное сообщение — text.
    try:
        if getattr(call.message, "content_type", None) == "photo":
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption=text,
                reply_markup=None
            )
        else:
            bot.edit_message_text(
                text,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None
            )
    except Exception as e:
        print("edit admin message error:", e)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception as e2:
            print("remove reply markup error:", e2)
        try:
            bot.send_message(call.message.chat.id, text)
        except Exception as e3:
            print("send admin status error:", e3)


@bot.callback_query_handler(func=lambda c: c.data.startswith("del_"))
def delete_submit_request(call):
    if call.from_user.id != ADMIN_ID:
        return safe_answer_callback(call, "❌ Нет доступа.", show_alert=True)

    safe_answer_callback(call, "⏳ Удаляю заявку...")

    try:
        data = load_data()
        sid = call.data.split("_", 1)[1]
        submit = data.get("submits", {}).get(sid)

        if request_already_processed(data, "submits", sid):
            return safe_answer_callback(call, f"⚠️ Заявка #{sid} уже была обработана ранее.", show_alert=True)

        if not submit or submit.get("status") != "wait":
            return safe_answer_callback(call, f"⚠️ Заявка #{sid} уже обработана или не найдена.", show_alert=True)

        user_id = str(submit.get("user_id"))
        task_id = str(submit.get("task_id"))
        user = get_user(data, user_id)

        if task_id in user.get("pending_tasks", []):
            user["pending_tasks"].remove(task_id)

        if str(user.get("waiting_task")) == task_id:
            user["waiting_task"] = None

        data.get("submits", {}).pop(sid, None)
        mark_request_processed(data, "submits", sid, "deleted", call.from_user.id, user_id, f"task #{task_id}")
        save_data(data)

        try:
            bot.send_message(
                user_id,
                f"🗑 <b>Ваша заявка по заданию #{task_id} удалена администратором.</b>\n\n"
                "Вы можете отправить новый скриншот, если задание выполнено правильно."
            )
        except Exception as e:
            print("send delete submit message error:", e)

        safe_edit_admin_message(
            call,
            f"🗑 <b>Заявка #{sid} удалена администратором.</b>\n\n"
            f"✅ Задание: #{task_id}\n"
            f"🆔 ID: <code>{user_id}</code>"
        )

    except Exception as e:
        print("delete submit callback error:", e)
        safe_edit_admin_message(call, "❌ Ошибка удаления заявки. Проверь логи Render.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("yes_") or c.data.startswith("no_"))
def check_request(call):
    if call.from_user.id != ADMIN_ID:
        return safe_answer_callback(call, "❌ Нет доступа.", show_alert=True)

    # Сразу отвечаем Telegram, чтобы кнопка не крутилась и казалось, что она не нажимается.
    safe_answer_callback(call, "⏳ Обрабатываю заявку...")

    try:
        data = load_data()
        action, sid = call.data.split("_", 1)
        submit = data.get("submits", {}).get(sid)

        if request_already_processed(data, "submits", sid):
            return safe_answer_callback(call, f"⚠️ Заявка #{sid} уже была обработана ранее.", show_alert=True)

        if not submit or submit.get("status") != "wait":
            return safe_answer_callback(call, f"⚠️ Заявка #{sid} уже обработана или не найдена.", show_alert=True)

        user_id = str(submit["user_id"])
        task_id = str(submit["task_id"])
        reward = float(submit["reward"])
        user = get_user(data, user_id)

        if task_id in user.get("pending_tasks", []):
            user["pending_tasks"].remove(task_id)

        if action == "yes":
            if task_id not in user.get("done_tasks", []):
                add_balance_to_user(data, user_id, reward)
                user["completed_tasks"] = int(user.get("completed_tasks", len(user.get("done_tasks", [])))) + 1
                user["done_tasks"].append(task_id)

            data["submits"].pop(sid, None)
            mark_request_processed(data, "submits", sid, "approved", call.from_user.id, user_id, f"task #{task_id}")
            save_data(data)

            try:
                bot.send_message(
                    user_id,
                    f"🎉 <b>Задание #{task_id} одобрено!</b>\n\n"
                    f"💰 Начислено: <b>{format_gmp(reward)} GMP</b>\n"
                    f"💎 Баланс: <b>{format_gmp(user['balance'])} GMP</b>"
                )
            except Exception as e:
                print("send approve message error:", e)

            safe_edit_admin_message(
                call,
                f"✅ <b>Заявка #{sid} одобрена.</b>\n\n"
                f"✅ Задание: #{task_id}\n"
                f"💰 Начислено: {format_gmp(reward)} GMP\n"
                f"🆔 ID: <code>{user_id}</code>"
            )

        else:
            data["submits"].pop(sid, None)
            mark_request_processed(data, "submits", sid, "rejected", call.from_user.id, user_id, f"task #{task_id}")
            save_data(data)

            try:
                bot.send_message(
                    user_id,
                    f"❌ <b>Задание #{task_id} отклонено</b>\n\n"
                    "Проверьте, что вы выполнили задание до конца и отправили правильный скриншот.\n\n"
                    "После проверки можете попробовать снова ✅"
                )
            except Exception as e:
                print("send reject message error:", e)

            safe_edit_admin_message(
                call,
                f"❌ <b>Заявка #{sid} отклонена.</b>\n\n"
                f"✅ Задание: #{task_id}\n"
                f"🆔 ID: <code>{user_id}</code>"
            )

    except Exception as e:
        print("task approve/reject callback error:", e)
        safe_edit_admin_message(call, "❌ Ошибка обработки заявки. Проверь логи Render.")


@bot.message_handler(func=lambda m: m.text == "💸 Вывод")
def withdraw(message):
    data = load_data()
    user = get_user(data, message.from_user.id)

    active_wid = has_active_withdraw(data, message.from_user.id)
    if user.get("withdraw_pending") or active_wid:
        user["withdraw_pending"] = True
        save_data(data)
        return bot.send_message(
            message.chat.id,
            "⏳ У тебя уже есть заявка на вывод на проверке.\nДождись решения администратора."
        )

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
        "💎 <b>Куда вывести GMP?</b>\n\n"
        "Напишите @username куда вывести GMP.\n\n"
        "Пример:\n<code>@username</code>"
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("payyes_") or c.data.startswith("payno_"))
def pay_check(call):
    if call.from_user.id != ADMIN_ID:
        return safe_answer_callback(call, "❌ Нет доступа.", show_alert=True)

    # Сразу отвечаем Telegram, чтобы кнопка не крутилась.
    safe_answer_callback(call, "⏳ Обрабатываю выплату...")

    try:
        action, wid = call.data.split("_", 1)
        wid = str(wid).strip().replace("#", "")

        data = load_data()

        if request_already_processed(data, "withdraws", wid):
            safe_edit_admin_message(call, f"⚠️ <b>Заявка на вывод #{wid} уже была обработана ранее.</b>\n\nПовторная выплата заблокирована защитой.")
            return

        real_wid, w = find_request_by_id(data.get("withdraws", {}), wid)

        # Если локальный файл старый/пустой — пробуем взять свежую базу с GitHub.
        if (not w or w.get("status") != "wait") and GITHUB_TOKEN and GITHUB_REPO:
            fresh_data = load_data_from_github_only()
            if fresh_data:
                if request_already_processed(fresh_data, "withdraws", wid):
                    safe_edit_admin_message(call, f"⚠️ <b>Заявка на вывод #{wid} уже была обработана ранее.</b>\n\nПовторная выплата заблокирована защитой.")
                    return
                fresh_real_wid, fresh_w = find_request_by_id(fresh_data.get("withdraws", {}), wid)
                if fresh_w and fresh_w.get("status") == "wait":
                    data = fresh_data
                    real_wid = fresh_real_wid
                    w = fresh_w

        if not w:
            safe_edit_admin_message(
                call,
                f"ℹ️ <b>Заявка на вывод #{wid} уже закрыта или не найдена.</b>\n\n"
                "Возможные причины:\n"
                "• заявка уже была обработана ранее;\n"
                "• бот был перезапущен до сохранения заявки;\n"
                "• заявка была удалена из data.json.\n\n"
                "Проверьте активные заявки командой /requests."
            )
            return

        if w.get("status") != "wait":
            safe_edit_admin_message(call, f"ℹ️ Заявка на вывод #{wid} уже была обработана ранее.")
            return

        real_wid = str(real_wid or wid)
        user_id = str(w.get("user_id"))
        amount = float(w.get("amount", 0))
        user = get_user(data, user_id)

        # Удаляем заявку только после того, как все данные уже взяли.
        if action == "payyes":
            user["withdraw_count"] = int(user.get("withdraw_count", 0)) + 1
            user["withdrawn_total"] = round(float(user.get("withdrawn_total", 0)) + amount, 2)

            data.get("withdraws", {}).pop(real_wid, None)
            data.get("withdraws", {}).pop(wid, None)

            user["withdraw_pending"] = any(
                str(x.get("user_id")) == user_id and x.get("status") == "wait"
                for x in data.get("withdraws", {}).values()
            )
            mark_request_processed(data, "withdraws", wid, "paid", call.from_user.id, user_id, f"amount {amount}")
            save_data(data)

            safe_send(
                user_id,
                f"✅ <b>Заказ #{wid} выполнен!</b>\n\n"
                "💎 GMP успешно выданы 💜\n\n"
                "Спасибо за использование «Заработок GMP» ✨"
            )
            safe_edit_admin_message(
                call,
                f"✅ <b>Выплата #{wid} подтверждена.</b>\n\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"💰 Сумма: <b>{format_gmp(amount)} GMP</b>"
            )
            return

        # Отказ: возвращаем списанные GMP на баланс.
        old_balance = float(user.get("balance", 0))
        user["balance"] = round(old_balance + amount, 2)

        data.get("withdraws", {}).pop(real_wid, None)
        data.get("withdraws", {}).pop(wid, None)

        user["withdraw_pending"] = any(
            str(x.get("user_id")) == user_id and x.get("status") == "wait"
            for x in data.get("withdraws", {}).values()
        )
        mark_request_processed(data, "withdraws", wid, "rejected", call.from_user.id, user_id, f"amount {amount}")
        save_data(data)

        safe_send(
            user_id,
            f"❌ <b>Заявка на вывод #{wid} отклонена.</b>\n\n"
            f"💰 <b>{format_gmp(amount)} GMP</b> возвращены на баланс."
        )
        safe_edit_admin_message(
            call,
            f"❌ <b>Выплата #{wid} отклонена.</b>\n\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"💰 Возвращено: <b>{format_gmp(amount)} GMP</b>"
        )

    except Exception as e:
        print("withdraw callback error:", e)
        safe_edit_admin_message(call, "❌ Ошибка обработки выплаты. Проверь логи Render.")


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

    if link.startswith("t.me/"):
        link = "https://" + link
    if not link.startswith("http://") and not link.startswith("https://"):
        return bot.send_message(message.chat.id, "❌ Ссылка должна быть https:// или t.me/...")

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
    if new_link.startswith("t.me/"):
        new_link = "https://" + new_link
    if not new_link.startswith("http://") and not new_link.startswith("https://"):
        return bot.send_message(message.chat.id, "❌ Ссылка должна быть https:// или t.me/...")

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




@bot.message_handler(commands=["promo", "promocode"])
def promo_command(message):
    """
    Админ:
    /promo CODE сумма активации
    пример: /promo GMP 2 5

    Пользователь:
    /promo CODE
    пример: /promo GMP
    """
    data = load_data()
    user = get_user(data, message.from_user.id)
    user["username"] = message.from_user.username or user.get("username", "")

    args = message.text.split()

    if len(args) < 2:
        if message.from_user.id == ADMIN_ID:
            return bot.send_message(
                message.chat.id,
                "🎁 <b>Промокоды</b>\n\n"
                "Создать промокод:\n"
                "<code>/promo КОД сумма активации</code>\n\n"
                "Пример:\n"
                "<code>/promo GMP 2 5</code>\n\n"
                "Активировать промокод:\n"
                "<code>/promo КОД</code>"
            )

        return bot.send_message(
            message.chat.id,
            "🎁 <b>Активация промокода</b>\n\n"
            "Напиши промокод так:\n"
            "<code>/promo КОД</code>"
        )

    code = args[1].strip().upper()

    if len(code) < 2 or len(code) > 32:
        return bot.send_message(message.chat.id, "❌ Код должен быть от 2 до 32 символов.")

    # Админ создаёт промокод
    if message.from_user.id == ADMIN_ID and len(args) >= 4:
        try:
            amount = round(float(args[2].replace(",", ".")), 2)
            activations = int(args[3])
        except Exception:
            return bot.send_message(
                message.chat.id,
                "❌ Неверный формат.\n\n"
                "Используй так:\n"
                "<code>/promo КОД сумма активации</code>\n\n"
                "Пример:\n"
                "<code>/promo GMP 2 5</code>"
            )

        if amount <= 0:
            return bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0.")

        if activations <= 0:
            return bot.send_message(message.chat.id, "❌ Количество активаций должно быть больше 0.")

        data.setdefault("promocodes", {})
        data["promocodes"][code] = {
            "amount": amount,
            "left": activations,
            "created_by": message.from_user.id,
            "used_by": [],
            "time": int(time.time())
        }

        save_data(data)

        return bot.send_message(
            message.chat.id,
            "✅ <b>Промокод создан и активен!</b>\n\n"
            f"🎁 Код: <code>{code}</code>\n"
            f"💰 Награда: <b>{format_gmp(amount)} GMP</b>\n"
            f"🔢 Активаций: <b>{activations}</b>\n\n"
            "Пользователи могут активировать так:\n"
            f"<code>/promo {code}</code>"
        )

    # Если не админ пытается создать промокод
    if len(args) >= 4 and message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Создавать промокоды может только админ.")

    # Пользователь активирует промокод
    promo = data.get("promocodes", {}).get(code)

    if not promo:
        return bot.send_message(message.chat.id, "❌ Промокод не найден или уже закончился.")

    if int(promo.get("left", 0)) <= 0:
        data["promocodes"].pop(code, None)
        save_data(data)
        return bot.send_message(message.chat.id, "❌ Промокод уже закончился.")

    used_by = [str(x) for x in promo.get("used_by", [])]
    uid = str(message.from_user.id)

    if uid in used_by:
        return bot.send_message(message.chat.id, "⚠️ Ты уже активировал этот промокод.")

    amount = round(float(promo.get("amount", 0)), 2)
    if amount <= 0:
        data["promocodes"].pop(code, None)
        save_data(data)
        return bot.send_message(message.chat.id, "❌ Промокод повреждён и был удалён.")

    add_balance_to_user(data, message.from_user.id, amount)

    promo.setdefault("used_by", [])
    promo["used_by"].append(uid)
    promo["left"] = int(promo.get("left", 0)) - 1

    left = promo["left"]

    # Когда активации закончились — полностью удаляем промокод из data.json,
    # чтобы файл не забивался историей старых промокодов.
    if left <= 0:
        data["promocodes"].pop(code, None)

    save_data(data)

    text = (
        "🎉 <b>Промокод успешно активирован!</b>\n\n"
        f"💰 Вам выдано: <b>{format_gmp(amount)} GMP</b>\n"
        f"💎 Баланс: <b>{format_gmp(user['balance'])} GMP</b>"
    )

    if left > 0:
        text += f"\n\n🔢 Осталось активаций промокода: <b>{left}</b>"
    else:
        text += "\n\n✅ Промокод закончился и был удалён из базы."

    bot.send_message(message.chat.id, text)


@bot.message_handler(commands=["promos"])
def promos_command(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    data = load_data()
    promos = data.get("promocodes", {})

    if not promos:
        return bot.send_message(message.chat.id, "🎁 Активных промокодов нет.")

    text = "🎁 <b>Активные промокоды:</b>\n\n"
    for code, promo in promos.items():
        text += (
            f"• <code>{code}</code> — "
            f"{format_gmp(promo.get('amount', 0))} GMP | "
            f"осталось: <b>{promo.get('left', 0)}</b>\n"
        )

    bot.send_message(message.chat.id, text)


@bot.message_handler(commands=["delpromo"])
def delpromo_command(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    args = message.text.split()
    if len(args) < 2:
        return bot.send_message(message.chat.id, "❌ Используй: <code>/delpromo КОД</code>")

    code = args[1].strip().upper()
    data = load_data()

    if code not in data.get("promocodes", {}):
        return bot.send_message(message.chat.id, "❌ Такого активного промокода нет.")

    data["promocodes"].pop(code, None)
    save_data(data)

    bot.send_message(message.chat.id, f"✅ Промокод <code>{code}</code> удалён.")


@bot.message_handler(commands=["give", "addbalance", "addbal"])
def give_gmp(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        return bot.send_message(
            message.chat.id,
            "❌ Формат:\n"
            "<code>/give user_id сумма</code>\n"
            "<code>/give @username сумма</code>\n\n"
            "Пример:\n"
            "<code>/give 7837011810 2</code>\n"
            "<code>/give @Artemwesh 2</code>"
        )

    user_query = parts[1].strip()
    amount = parse_gmp_amount(parts[2])
    if amount is None:
        return bot.send_message(message.chat.id, "❌ Сумма должна быть числом больше 0.")

    with DATA_LOCK:
        data = load_data()
        user_id, error = resolve_user_id(data, user_query)
        if error:
            return bot.send_message(message.chat.id, f"❌ {error}")

        user, old_balance, new_balance = add_balance_to_user(data, user_id, amount)
        save_data(data)

    username = user.get("username")
    name_line = f"@{username}" if username else f"ID {user_id}"

    bot.send_message(
        message.chat.id,
        f"✅ <b>Баланс начислен</b>\n\n"
        f"👤 Пользователь: <b>{name_line}</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"➕ Начислено: <b>{format_gmp(amount)} GMP</b>\n"
        f"💰 Баланс: <b>{format_gmp(old_balance)} → {format_gmp(new_balance)} GMP</b>"
    )

    safe_send(
        user_id,
        f"🎁 <b>Админ начислил тебе {format_gmp(amount)} GMP</b>\n\n"
        f"💰 Твой баланс: <b>{format_gmp(new_balance)} GMP</b>"
    )



@bot.message_handler(commands=["take"])
def take_gmp(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        return bot.send_message(message.chat.id, "❌ Формат:\n/take user_id сумма")

    user_query = parts[1].strip()
    try:
        amount = float(parts[2].replace(",", "."))
    except Exception:
        return bot.send_message(message.chat.id, "❌ Сумма должна быть числом.")

    if amount <= 0:
        return bot.send_message(message.chat.id, "❌ Сумма должна быть больше 0.")

    data = load_data()
    user_id = user_query
    if user_query.startswith("@"):
        clean = user_query[1:].lower()
        found = None
        for uid, u in data.get("users", {}).items():
            if str(u.get("username", "")).replace("@", "").lower() == clean:
                found = uid
                break
        if not found:
            return bot.send_message(message.chat.id, "❌ Username не найден. Пусть человек нажмёт /start в боте или дай его ID.")
        user_id = found

    user = get_user(data, user_id)
    user["balance"] = round(float(user.get("balance", 0)) - amount, 2)
    user["fines_total"] = round(float(user.get("fines_total", 0)) + amount, 2)
    save_data(data)

    bot.send_message(message.chat.id, f"✅ У пользователя {user_id} списано {format_gmp(amount)} GMP. Баланс: {format_gmp(user['balance'])} GMP")
    safe_send(user_id, f"⚠️ Админ списал <b>{format_gmp(amount)} GMP</b>.\n💰 Баланс: <b>{format_gmp(user['balance'])} GMP</b>")


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


@bot.message_handler(func=lambda m: m.text == "💎 Начислить баланс")
def add_balance_info(message):
    if message.from_user.id != ADMIN_ID:
        return

    bot.send_message(
        message.chat.id,
        "💎 <b>Начислить GMP пользователю</b>\n\n"
        "Команды:\n"
        "<code>/give user_id сумма</code>\n"
        "<code>/give @username сумма</code>\n\n"
        "Примеры:\n"
        "<code>/give 7837011810 2</code>\n"
        "<code>/give @Artemwesh 2</code>\n\n"
        "⚠️ По @username бот найдёт только тех, кто уже нажимал /start."
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
        "<code>/give user_id сумма</code> — начислить GMP\n"
        "<code>/give @username сумма</code> — начислить по username\n"
        "<code>/take user_id сумма</code> — списать GMP\n"
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
    kb.add(types.InlineKeyboardButton("✅ Одобрить", callback_data=f"yes_{sid}"))
    kb.add(types.InlineKeyboardButton("❌ Отклонить", callback_data=f"no_{sid}"))
    kb.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"del_{sid}"))
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
    user["username"] = message.from_user.username or user.get("username", "")

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
            "Напишите сумму GMP для вывода.\n"
            "Можно написать <b>все</b>."
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
                f"❌ Недостаточно GMP.\nТвой баланс: <b>{format_gmp(user['balance'])} GMP</b>\n\n"
                "Можно вывести только сумму, которая сейчас есть на балансе."
            )

        # Критическая зона: создание вывода.
        # Если пользователь нажал кнопку/отправил сумму 2, 10 или 50 раз,
        # бот всё равно создаст только 1 заявку. Если Telegram случайно повторит
        # один и тот же запрос, повтор будет привязан к тому же номеру заказа.
        with WITHDRAW_CREATE_LOCK:
            data = load_data()
            user = get_user(data, message.from_user.id)
            withdraw_to = user.get("withdraw_to") or "не указано"

            old_wid = has_active_withdraw(data, message.from_user.id)
            if old_wid:
                user["withdraw_pending"] = True
                user["withdraw_step"] = None
                user["withdraw_to"] = None
                save_data(data)
                return bot.send_message(
                    message.chat.id,
                    f"⚠️ У тебя уже есть заявка на вывод на проверке.\nНомер заявки: <b>#{old_wid}</b>"
                )

            duplicate_wid = find_recent_duplicate_withdraw(
                data,
                message.from_user.id,
                withdraw_to,
                amount,
                window_seconds=120
            )
            if duplicate_wid:
                user["withdraw_pending"] = True
                user["withdraw_step"] = None
                user["withdraw_to"] = None
                save_data(data)
                return bot.send_message(
                    message.chat.id,
                    f"⚠️ Повторная заявка не создана.\n"
                    f"У тебя уже есть заявка на вывод с этим же номером: <b>#{duplicate_wid}</b>"
                )

            if amount > float(user.get("balance", 0)):
                return bot.send_message(
                    message.chat.id,
                    f"❌ Недостаточно GMP.\nТвой баланс: <b>{format_gmp(user.get('balance', 0))} GMP</b>"
                )

            data["last_withdraw_id"] += 1
            wid = str(data["last_withdraw_id"])

            user["balance"] = round(float(user["balance"]) - amount, 2)
            user["withdraw_pending"] = True
            user["withdraw_step"] = None
            user["withdraw_to"] = None

            data.setdefault("withdraws", {})[wid] = {
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

    # Если сюда дошло обычное сообщение без активного шага — показываем меню.
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

