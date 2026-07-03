import os
import json
import base64
import threading
import time
import re
import html
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import RLock
import requests
from flask import Flask
from telebot import TeleBot, types

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

GITHUB_TOKEN = (os.getenv("GITHUB_TOKEN") or "").strip()
GITHUB_REPO = (os.getenv("GITHUB_REPO") or "").strip()  # пример: GMPZADA/GmpZadanik
GITHUB_BRANCH = (os.getenv("GITHUB_BRANCH") or "main").strip()
GITHUB_FILE = "data.json"
GITHUB_REQUESTS_FILE = "requests.json"

LOCAL_DATA_FILE = "data.json"
LOCAL_REQUESTS_FILE = "requests.json"
BONUS_AMOUNT = 0.5
BONUS_COOLDOWN = 24 * 60 * 60
BONUS_REQUIRED_BIO = "@GmpEarnBot лучший бот для заработка GMP"

# Keep Alive: пингует сайт каждые 5 минут
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL", "https://gmpzadanik.onrender.com/")
KEEPALIVE_INTERVAL = 5 * 60

bot = TeleBot(TOKEN, parse_mode="HTML")
app = Flask(__name__)
DATA_LOCK = RLock()
WITHDRAW_CREATE_LOCK = RLock()
SUBMIT_CREATE_LOCK = RLock()

# Быстрый кэш: бот не читает data.json/GitHub заново на каждый клик
DATA_CACHE = None
REQUESTS_CACHE = None
CACHE_LOCK = RLock()

# GitHub сохраняем в фоне, чтобы пользователь не ждал 3-5 секунд ответа
GITHUB_SAVE_LOCK = RLock()
GITHUB_REQUESTS_SAVE_LOCK = RLock()

# Быстрое сохранение без лагов:
# в GitHub отправляется только последняя версия файла, старые фоновые потоки пропускаются.
GITHUB_SAVE_VERSION = {}
GITHUB_LAST_HASH = {}
GITHUB_VERSION_LOCK = RLock()


def clone_json(obj):
    """Быстрая безопасная копия dict/list, чтобы разные обработчики не портили один объект."""
    return json.loads(json.dumps(obj, ensure_ascii=False))


def save_json_atomic(filename, obj):
    """
    Быстро и безопасно сохраняет JSON.
    Если файл не изменился — не перезаписывает его, чтобы не грузить диск/Render.
    """
    content = json.dumps(obj, ensure_ascii=False, indent=2)
    try:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as old_f:
                if old_f.read() == content:
                    return
    except Exception:
        pass

    tmp_file = filename + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_file, filename)


def upload_json_to_github(filename, obj, message, save_version=None):
    """Медленная отправка в GitHub. Запускается в фоне и не тормозит пользователя."""
    if not (GITHUB_TOKEN and GITHUB_REPO):
        print(f"GitHub save skipped for {filename}: проверь GITHUB_TOKEN и GITHUB_REPO в Render")
        return

    content = json.dumps(obj, ensure_ascii=False, indent=2)
    content_hash = str(hash(content))

    with GITHUB_VERSION_LOCK:
        if save_version is not None and GITHUB_SAVE_VERSION.get(filename) != save_version:
            return
        if GITHUB_LAST_HASH.get(filename) == content_hash:
            return

    lock = GITHUB_REQUESTS_SAVE_LOCK if filename == GITHUB_REQUESTS_FILE else GITHUB_SAVE_LOCK

    with lock:
        with GITHUB_VERSION_LOCK:
            if save_version is not None and GITHUB_SAVE_VERSION.get(filename) != save_version:
                return
        try:
            sha = None
            r = requests.get(
                github_url(filename),
                headers=github_headers(),
                params={"ref": GITHUB_BRANCH},
                timeout=10
            )
            if r.status_code == 200:
                sha = r.json().get("sha")

            payload = {
                "message": message,
                "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
                "branch": GITHUB_BRANCH
            }
            if sha:
                payload["sha"] = sha

            pr = requests.put(github_url(filename), headers=github_headers(), json=payload, timeout=10)

            if pr.status_code == 409:
                r2 = requests.get(
                    github_url(filename),
                    headers=github_headers(),
                    params={"ref": GITHUB_BRANCH},
                    timeout=10
                )
                if r2.status_code == 200:
                    payload["sha"] = r2.json().get("sha")
                    pr = requests.put(github_url(filename), headers=github_headers(), json=payload, timeout=10)

            if pr.status_code not in (200, 201):
                print(f"GitHub save error {filename}:", pr.status_code, pr.text)
            else:
                with GITHUB_VERSION_LOCK:
                    GITHUB_LAST_HASH[filename] = content_hash
                print(f"GitHub save OK {filename}")
        except Exception as e:
            print(f"GitHub save exception {filename}:", e)


def save_to_github_background(filename, obj, message):
    """
    Не тормозит ответ бота: GitHub обновится в фоне.
    Если за секунду было несколько изменений — старые фоновые сохранения сами пропустятся,
    и в GitHub уйдёт только самая свежая версия.
    """
    try:
        with GITHUB_VERSION_LOCK:
            version = GITHUB_SAVE_VERSION.get(filename, 0) + 1
            GITHUB_SAVE_VERSION[filename] = version

        threading.Thread(
            target=upload_json_to_github,
            args=(filename, clone_json(obj), message, version),
            daemon=True
        ).start()
    except Exception as e:
        print("GitHub background thread error:", e)



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



def h(value):
    """Безопасный вывод текста в HTML-сообщениях Telegram."""
    return html.escape(str(value or ""), quote=False)


def short_text(value, limit=3500):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def safe_username(username):
    username = str(username or "").strip().replace("@", "")
    return username if username else "нет username"


def backup_bad_data_file():
    """Если data.json сломался, сохраняем копию, чтобы не потерять данные полностью."""
    try:
        if os.path.exists(LOCAL_DATA_FILE):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(LOCAL_DATA_FILE, f"{LOCAL_DATA_FILE}.broken_{ts}.bak")
    except Exception as e:
        print("backup bad data error:", e)



def empty_data():
    return {
        "users": {},
        "tasks": {},
        "deleted_tasks": {},
        "required_tasks": [],
        "submits": {},
        "withdraws": {},
        "withdraw_blocks": {},
        "promocodes": {},
        "processed_requests": {"submits": {}, "withdraws": {}},
        "admin_sent": {"submits": {}, "withdraws": {}},
        "balance_logs": [],
        "total_paid": 0,
        "total_withdrawals": 0,
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

    # Глобальное включение/выключение выплат.
    # Защита: старые/битые data.json иногда хранили withdraw_enabled=false без признака,
    # что админ реально нажимал /withdrawoff. В таком случае выплаты не должны
    # сами оставаться закрытыми после перезапуска.
    data.setdefault("withdraw_disabled_by_admin", False)
    if data.get("withdraw_enabled") is False and not data.get("withdraw_disabled_by_admin", False):
        data["withdraw_enabled"] = True
    else:
        data.setdefault("withdraw_enabled", True)

    data.setdefault("withdraw_blocks", {})
    if not isinstance(data.get("withdraw_blocks"), dict):
        data["withdraw_blocks"] = {}

    # Память удалённых заданий. Нужна, чтобы старый local/GitHub data.json
    # не вернул задание после перезапуска Render.
    data.setdefault("deleted_tasks", {})
    if isinstance(data.get("deleted_tasks"), list):
        data["deleted_tasks"] = {str(tid): {"time": 0} for tid in data.get("deleted_tasks", [])}
    if not isinstance(data.get("deleted_tasks"), dict):
        data["deleted_tasks"] = {}

    # Общая статистика выплат хранится отдельно, чтобы не пропадала,
    # если какой-то пользователь не записался/старый data.json вернулся после redeploy.
    data.setdefault("total_paid", 0)
    data.setdefault("total_withdrawals", 0)
    try:
        data["total_paid"] = round(float(data.get("total_paid", 0)), 2)
    except Exception:
        data["total_paid"] = 0
    try:
        data["total_withdrawals"] = int(data.get("total_withdrawals", 0))
    except Exception:
        data["total_withdrawals"] = 0
    data.setdefault("required_tasks", [])
    if not isinstance(data.get("required_tasks"), list):
        data["required_tasks"] = []

    # Чистим удалённые задания: если задание удалено админом — оно не должно вернуться.
    deleted_task_ids = {str(tid) for tid in data.get("deleted_tasks", {}).keys()}
    for tid in list(data.get("tasks", {}).keys()):
        if str(tid) in deleted_task_ids:
            data["tasks"].pop(tid, None)

    # Чистим обязательные задания: если админ удалил задание, оно больше не блокирует вывод.
    existing_task_ids = {str(tid) for tid in data.get("tasks", {}).keys()}
    data["required_tasks"] = list(dict.fromkeys(
        str(tid) for tid in data.get("required_tasks", []) if str(tid) in existing_task_ids
    ))

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
        user.setdefault("banned", False)
        user.setdefault("ban_reason", "")

    for task_id, task in data.get("tasks", {}).items():
        task.setdefault("active", True)
        task.setdefault("required", str(task_id) in data.get("required_tasks", []))
        if task.get("required") and str(task_id) not in data.get("required_tasks", []):
            data.setdefault("required_tasks", []).append(str(task_id))

    # Защита от повторной обработки заявок/выводов
    data.setdefault("processed_requests", {})
    if not isinstance(data.get("processed_requests"), dict):
        data["processed_requests"] = {}
    data["processed_requests"].setdefault("submits", {})
    data["processed_requests"].setdefault("withdraws", {})

    # Чтобы /requests не отправлял одни и те же заявки по 2-3 раза админу
    data.setdefault("admin_sent", {})
    if not isinstance(data.get("admin_sent"), dict):
        data["admin_sent"] = {}
    data["admin_sent"].setdefault("submits", {})
    data["admin_sent"].setdefault("withdraws", {})

    # Логи баланса
    data.setdefault("balance_logs", [])
    if not isinstance(data.get("balance_logs"), list):
        data["balance_logs"] = []

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
        # Убираем зависшие pending_tasks, если заявки уже нет или задание удалили.
        user["pending_tasks"] = [
            str(tid) for tid in user.get("pending_tasks", [])
            if str(tid) in existing_task_ids and (str(uid), str(tid)) in active_submit_pairs
        ]
        # Убираем дубли выполненных заданий и мусор от удалённых заданий.
        user["done_tasks"] = list(dict.fromkeys(
            str(tid) for tid in user.get("done_tasks", []) if str(tid) in existing_task_ids
        ))
        if str(user.get("waiting_task")) not in existing_task_ids:
            user["waiting_task"] = None


    # Авто-очистка мусора, чтобы data.json не раздувался бесконечно.
    now = int(time.time())

    # Старые обработанные заявки храним ограниченно.
    for kind in ["submits", "withdraws"]:
        processed = data.setdefault("processed_requests", {}).setdefault(kind, {})
        if isinstance(processed, dict) and len(processed) > 500:
            old_keys = sorted(processed.keys(), key=lambda k: processed[k].get("time", 0))[:-500]
            for k in old_keys:
                processed.pop(k, None)

        sent = data.setdefault("admin_sent", {}).setdefault(kind, {})
        if isinstance(sent, dict) and len(sent) > 500:
            old_keys = sorted(sent.keys(), key=lambda k: sent[k].get("time", 0))[:-500]
            for k in old_keys:
                sent.pop(k, None)

    # Логи баланса оставляем последние 1000 записей.
    if len(data.get("balance_logs", [])) > 1000:
        data["balance_logs"] = data["balance_logs"][-1000:]

    # Удаляем старые зависшие заявки старше 7 дней и возвращаем GMP за старый вывод.
    for sid, submit in list(data.get("submits", {}).items()):
        if submit.get("status") == "wait" and now - int(submit.get("time", now) or now) > 7 * 24 * 60 * 60:
            uid = str(submit.get("user_id"))
            task_id = str(submit.get("task_id"))
            user = get_user(data, uid)
            if task_id in user.get("pending_tasks", []):
                user["pending_tasks"].remove(task_id)
            remove_submit_from_data(data, sid, uid, task_id)
            mark_request_processed(data, "submits", sid, "expired", 0, uid, f"task #{task_id}")

    for wid, w in list(data.get("withdraws", {}).items()):
        if w.get("status") == "wait" and now - int(w.get("time", now) or now) > 7 * 24 * 60 * 60:
            uid = str(w.get("user_id"))
            amount = float(w.get("amount", 0) or 0)
            if amount > 0:
                add_balance_to_user(data, uid, amount, reason="withdraw_expired_return", request_id=wid)
            data["withdraws"].pop(wid, None)
            mark_request_processed(data, "withdraws", wid, "expired", 0, uid, f"amount {amount}")

    cleanup_closed_requests(data)
    return data


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }


def github_url(file_name=GITHUB_FILE):
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_name}"


def empty_requests():
    """Временный файл: тут живут только активные заявки и состояния."""
    return {
        "submits": {},
        "withdraws": {},
        "admin_sent": {"submits": {}, "withdraws": {}},
        "user_states": {},
        # старые названия оставлены для понятности, бот ими не пользуется напрямую
        "withdraw_requests": {},
        "photo_requests": {},
        "pending_tasks": {}
    }


def fix_requests(req):
    base = empty_requests()
    if not isinstance(req, dict):
        req = {}
    for key, value in base.items():
        if key not in req:
            req[key] = value
    if not isinstance(req.get("submits"), dict):
        req["submits"] = {}
    if not isinstance(req.get("withdraws"), dict):
        req["withdraws"] = {}
    if not isinstance(req.get("admin_sent"), dict):
        req["admin_sent"] = {"submits": {}, "withdraws": {}}
    req["admin_sent"].setdefault("submits", {})
    req["admin_sent"].setdefault("withdraws", {})
    return req


def strip_legacy_requests_from_data(data):
    """
    ВАЖНО: data.json больше НЕ является источником заявок.
    Старые submits/withdraws/admin_sent из data.json очищаются при чтении,
    чтобы requests.json не подтягивал старый мусор из data.json.
    Активные заявки теперь живут только в requests.json.
    """
    data = fix_data(data or empty_data())
    data["submits"] = {}
    data["withdraws"] = {}
    data.setdefault("admin_sent", {"submits": {}, "withdraws": {}})
    data["admin_sent"]["submits"] = {}
    data["admin_sent"]["withdraws"] = {}
    return data


def read_local_requests_raw():
    if not os.path.exists(LOCAL_REQUESTS_FILE):
        return empty_requests()
    try:
        with open(LOCAL_REQUESTS_FILE, "r", encoding="utf-8") as f:
            return fix_requests(json.load(f))
    except Exception as e:
        print("Read local requests error:", e)
        return empty_requests()


def read_github_requests_raw():
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return empty_requests()
    try:
        r = requests.get(
            github_url(GITHUB_REQUESTS_FILE),
            headers=github_headers(),
            params={"ref": GITHUB_BRANCH},
            timeout=10
        )
        if r.status_code != 200:
            if r.status_code != 404:
                print("Read GitHub requests error:", r.status_code, r.text)
            return empty_requests()
        content = r.json()["content"]
        file_text = base64.b64decode(content).decode("utf-8")
        return fix_requests(json.loads(file_text))
    except Exception as e:
        print("Read GitHub requests exception:", e)
        return empty_requests()


def merge_requests(primary, secondary):
    primary = fix_requests(primary or empty_requests())
    secondary = fix_requests(secondary or empty_requests())
    result = fix_requests(json.loads(json.dumps(secondary, ensure_ascii=False)))
    for key in ["submits", "withdraws"]:
        result.setdefault(key, {})
        for rid, value in primary.get(key, {}).items():
            result[key][str(rid)] = value
    result.setdefault("admin_sent", {"submits": {}, "withdraws": {}})
    for kind in ["submits", "withdraws"]:
        result["admin_sent"].setdefault(kind, {})
        for rid, value in primary.get("admin_sent", {}).get(kind, {}).items():
            result["admin_sent"][kind][str(rid)] = value
    return fix_requests(result)


def load_requests():
    global REQUESTS_CACHE

    with CACHE_LOCK:
        if REQUESTS_CACHE is not None:
            return clone_json(REQUESTS_CACHE)

    local_req = read_local_requests_raw()
    github_req = read_github_requests_raw()
    req = merge_requests(local_req, github_req)

    with CACHE_LOCK:
        REQUESTS_CACHE = clone_json(req)

    return clone_json(req)

def attach_requests_to_data(data):
    """Подклеивает временные заявки из requests.json к data, чтобы остальной bot.py работал как раньше."""
    data = fix_data(data or empty_data())
    req = load_requests()
    data["submits"] = req.get("submits", {})
    data["withdraws"] = req.get("withdraws", {})
    data.setdefault("admin_sent", {"submits": {}, "withdraws": {}})
    for kind in ["submits", "withdraws"]:
        data["admin_sent"].setdefault(kind, {})
        data["admin_sent"][kind].update(req.get("admin_sent", {}).get(kind, {}))
    cleanup_closed_requests(data)
    return fix_data(data)


def split_requests_from_data(data):
    """Достаёт активные заявки в requests.json, а data.json оставляет чистым."""
    data = fix_data(data or empty_data())
    req = empty_requests()
    req["submits"] = {
        str(k): v for k, v in data.get("submits", {}).items()
        if isinstance(v, dict) and v.get("status") == "wait"
    }
    req["withdraws"] = {
        str(k): v for k, v in data.get("withdraws", {}).items()
        if isinstance(v, dict) and v.get("status") == "wait"
    }
    req["admin_sent"] = data.get("admin_sent", {"submits": {}, "withdraws": {}})
    req = fix_requests(req)

    clean = json.loads(json.dumps(data, ensure_ascii=False))
    clean["submits"] = {}
    clean["withdraws"] = {}
    clean.setdefault("admin_sent", {"submits": {}, "withdraws": {}})
    clean["admin_sent"]["submits"] = {}
    clean["admin_sent"]["withdraws"] = {}
    return clean, req


def save_requests(req):
    global REQUESTS_CACHE

    req = fix_requests(req)

    with CACHE_LOCK:
        REQUESTS_CACHE = clone_json(req)

    try:
        save_json_atomic(LOCAL_REQUESTS_FILE, req)
    except Exception as e:
        print("Local requests save exception:", e)

    # ВАЖНО: requests.json сохраняем НЕ в фоне, а сразу.
    # Иначе старый пустой поток мог позже перезаписать файл,
    # поэтому админ видел заявку, а на GitHub requests.json оставался пустой.
    upload_json_to_github(GITHUB_REQUESTS_FILE, req, "update bot requests")


def read_local_data_raw():
    """Читает локальный data.json без вызова load_data, чтобы не было перезаписи по кругу."""
    if not os.path.exists(LOCAL_DATA_FILE):
        return None
    try:
        with open(LOCAL_DATA_FILE, "r", encoding="utf-8") as f:
            return strip_legacy_requests_from_data(json.load(f))
    except Exception as e:
        print("Read local raw error:", e)
        backup_bad_data_file()
        return None


def read_github_data_raw():
    """Читает data.json с GitHub без сохранения локально."""
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
            if r.status_code != 404:
                print("Read GitHub raw error:", r.status_code, r.text)
            return None
        content = r.json()["content"]
        file_text = base64.b64decode(content).decode("utf-8")
        return strip_legacy_requests_from_data(json.loads(file_text))
    except Exception as e:
        print("Read GitHub raw exception:", e)
        return None


def merge_data(primary, secondary):
    """
    Склеивает две базы, чтобы при старом локальном/GitHub файле не пропадали пользователи.
    primary важнее: его новые значения остаются, secondary только добавляет то, чего не хватает.
    """
    primary = fix_data(primary or empty_data())
    secondary = fix_data(secondary or empty_data())

    result = json.loads(json.dumps(secondary, ensure_ascii=False))
    result = fix_data(result)

    # Память удалённых заданий объединяем из двух файлов.
    # Это защита: если в одном файле задание удалили, второй старый файл не вернёт его назад.
    result.setdefault("deleted_tasks", {})
    for tid, info in primary.get("deleted_tasks", {}).items():
        result["deleted_tasks"][str(tid)] = info if isinstance(info, dict) else {"time": 0}
    deleted_task_ids = {str(tid) for tid in result.get("deleted_tasks", {}).keys()}

    # Пользователи: никого не удаляем, а существующего пользователя обновляем свежими данными из primary.
    result_users = result.setdefault("users", {})
    for uid, user in primary.get("users", {}).items():
        uid = str(uid)
        if uid not in result_users or not isinstance(result_users.get(uid), dict):
            result_users[uid] = user
        else:
            merged_user = dict(result_users[uid])
            merged_user.update(user)
            result_users[uid] = merged_user

    # Задания не теряем, но удалённые задания НЕ возвращаем.
    result_tasks = result.setdefault("tasks", {})
    for tid in list(result_tasks.keys()):
        if str(tid) in deleted_task_ids:
            result_tasks.pop(tid, None)
    for tid, task in primary.get("tasks", {}).items():
        if str(tid) in deleted_task_ids:
            result_tasks.pop(str(tid), None)
            continue
        result_tasks[str(tid)] = task

    # Заявки/промо/тех.словари — объединяем.
    # Важно: обработанные заявки НЕ возвращаем обратно из старого локального/GitHub файла.
    processed_submits = {str(x).strip().replace("#", "") for x in primary.get("processed_requests", {}).get("submits", {}).keys()} | {str(x).strip().replace("#", "") for x in result.get("processed_requests", {}).get("submits", {}).keys()}
    processed_withdraws = {str(x).strip().replace("#", "") for x in primary.get("processed_requests", {}).get("withdraws", {}).keys()} | {str(x).strip().replace("#", "") for x in result.get("processed_requests", {}).get("withdraws", {}).keys()}

    # Постоянные словари объединяем как раньше.
    for key in ["withdraw_blocks", "promocodes"]:
        result.setdefault(key, {})
        for k, v in primary.get(key, {}).items():
            result[key][str(k)] = v

    # ВАЖНО: data.json больше НЕ источник заявок.
    # Старые submits/withdraws из secondary/data.json не переносим.
    # Оставляем только свежие активные заявки из primary, которые появились в текущей работе бота.
    result["submits"] = {}
    for k, v in primary.get("submits", {}).items():
        clean_k = str(k).strip().replace("#", "")
        if isinstance(v, dict) and v.get("status") == "wait" and clean_k not in processed_submits:
            result["submits"][str(k)] = v

    result["withdraws"] = {}
    for k, v in primary.get("withdraws", {}).items():
        clean_k = str(k).strip().replace("#", "")
        if isinstance(v, dict) and v.get("status") == "wait" and clean_k not in processed_withdraws:
            result["withdraws"][str(k)] = v

    # processed_requests — постоянная защита от повторной обработки.
    result.setdefault("processed_requests", {})
    for kind, values in primary.get("processed_requests", {}).items():
        result["processed_requests"].setdefault(kind, {})
        if isinstance(values, dict):
            for k, v in values.items():
                result["processed_requests"][kind][str(k)] = v

    # admin_sent — временное, берём только из текущей памяти, старый data.json не подтягиваем.
    result["admin_sent"] = {"submits": {}, "withdraws": {}}
    for kind, values in primary.get("admin_sent", {}).items():
        if isinstance(values, dict):
            for k, v in values.items():
                result["admin_sent"].setdefault(kind, {})[str(k)] = v

    # Обязательные задания: объединяем и оставляем только существующие.
    req = [str(x) for x in result.get("required_tasks", [])] + [str(x) for x in primary.get("required_tasks", [])]
    existing_task_ids = {str(tid) for tid in result.get("tasks", {}).keys()}
    result["required_tasks"] = list(dict.fromkeys([x for x in req if x in existing_task_ids and x not in deleted_task_ids]))

    # Убираем следы удалённых заданий у игроков и из активных заявок,
    # но баланс/заработок игрока НЕ трогаем.
    for sid, submit in list(result.get("submits", {}).items()):
        if str(submit.get("task_id")) in deleted_task_ids:
            remove_submit_from_data(result, sid, submit.get("user_id", ""), submit.get("task_id", ""))
            mark_request_processed(result, "submits", sid, "deleted_task", 0, submit.get("user_id", ""), f"task #{submit.get('task_id')}")
    for u in result.get("users", {}).values():
        u["done_tasks"] = [str(tid) for tid in u.get("done_tasks", []) if str(tid) not in deleted_task_ids]
        u["pending_tasks"] = [str(tid) for tid in u.get("pending_tasks", []) if str(tid) not in deleted_task_ids]
        if str(u.get("waiting_task")) in deleted_task_ids:
            u["waiting_task"] = None

    # Счётчики не должны откатываться назад.
    for key in ["last_task_id", "last_submit_id", "last_withdraw_id", "total_withdrawals"]:
        try:
            result[key] = max(int(result.get(key, 0)), int(primary.get(key, 0)))
        except Exception:
            result[key] = primary.get(key, result.get(key, 0))

    try:
        result["total_paid"] = max(float(result.get("total_paid", 0)), float(primary.get("total_paid", 0)))
        result["total_paid"] = round(result["total_paid"], 2)
    except Exception:
        result["total_paid"] = primary.get("total_paid", result.get("total_paid", 0))

    # Важные настройки берём из primary.
    for key in ["start_text", "withdraw_enabled", "withdraw_disabled_by_admin", "withdraw_disabled_at", "withdraw_disabled_reason"]:
        if key in primary:
            result[key] = primary[key]

    # Логи не раздуваем: оставляем последние 500 записей.
    logs = result.get("balance_logs", [])
    if not isinstance(logs, list):
        logs = []
    p_logs = primary.get("balance_logs", [])
    if isinstance(p_logs, list):
        logs = logs + [x for x in p_logs if x not in logs]
    result["balance_logs"] = logs[-500:]

    cleanup_closed_requests(result)
    return fix_data(result)


def load_data():
    global DATA_CACHE

    # После первого запуска берём данные из памяти.
    # Так бот не читает data.json и GitHub заново на каждое сообщение/кнопку.
    with CACHE_LOCK:
        if DATA_CACHE is not None:
            return attach_requests_to_data(clone_json(DATA_CACHE))

    # Первый запуск: читаем локальный файл и GitHub, потом склеиваем.
    local_data = read_local_data_raw()
    github_data = read_github_data_raw()

    if local_data is not None and github_data is not None:
        data = merge_data(local_data, github_data)
    elif local_data is not None:
        data = local_data
    elif github_data is not None:
        data = github_data
    else:
        data = empty_data()

    data = strip_legacy_requests_from_data(fix_data(data))

    # Обновляем локальный кэш уже склеенной базой.
    try:
        save_json_atomic(LOCAL_DATA_FILE, data)
    except Exception as e:
        print("Local cache save after load error:", e)

    with CACHE_LOCK:
        DATA_CACHE = clone_json(data)

    return attach_requests_to_data(clone_json(data))

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
        return attach_requests_to_data(strip_legacy_requests_from_data(json.loads(file_text)))
    except Exception as e:
        print("GitHub fresh load exception:", e)
        return None




def load_data_for_admin_action():
    """
    Для админ-кнопок берём свежий GitHub, но НЕ затираем локальных пользователей.
    """
    github_data = load_data_from_github_only()
    local_data = read_local_data_raw()

    if github_data is not None and local_data is not None:
        data = merge_data(github_data, local_data)  # для кнопок GitHub важнее, но локальные пользователи сохраняются
    elif github_data is not None:
        data = github_data
    elif local_data is not None:
        data = local_data
    else:
        data = empty_data()

    try:
        with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("fresh cache save error:", e)

    return attach_requests_to_data(data)


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




def sync_withdraw_stats(data):
    """
    Подстраховка для статистики выплат.
    Если из-за лага/GitHub общий счётчик не обновился, считаем минимум по профилям пользователей.
    """
    try:
        users_paid = round(sum(float(u.get("withdrawn_total", 0) or 0) for u in data.get("users", {}).values() if isinstance(u, dict)), 2)
        users_count = sum(int(u.get("withdraw_count", 0) or 0) for u in data.get("users", {}).values() if isinstance(u, dict))
        data["total_paid"] = round(max(float(data.get("total_paid", 0) or 0), users_paid), 2)
        data["total_withdrawals"] = max(int(data.get("total_withdrawals", 0) or 0), users_count)
    except Exception as e:
        print("sync withdraw stats error:", e)
    return data


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






def remove_submit_from_data(data, submit_id, user_id=None, task_id=None):
    """
    Полностью убирает активную заявку на задание/фото из data.json.

    После Одобрить/Отклонить/Удалить в data остаётся только нужное:
    - users[user_id]["balance"] — новый баланс после начисления, если заявка одобрена;
    - users[user_id]["done_tasks"] — задание добавлено только при одобрении;
    - короткая метка processed_requests, чтобы старая копия GitHub/local
      не вернула эту же закрытую заявку назад.
    Сам объект data["submits"][id] удаляется, pending_tasks/waiting_task очищаются.
    """
    sid = str(submit_id).strip().replace("#", "")
    data.setdefault("submits", {})

    found_user_id = str(user_id or "")
    found_task_id = str(task_id or "")

    keys_to_delete = {sid, f"#{sid}", sid.lstrip("0") or "0"}
    for key in list(data.get("submits", {}).keys()):
        clean_key = str(key).strip().replace("#", "")
        if clean_key == sid or clean_key.lstrip("0") == sid.lstrip("0") or str(key) in keys_to_delete:
            submit = data["submits"].pop(key, {}) or {}
            if not found_user_id:
                found_user_id = str(submit.get("user_id", ""))
            if not found_task_id:
                found_task_id = str(submit.get("task_id", ""))

    # Метки отправки админу больше не нужны после закрытия заявки.
    sent = data.setdefault("admin_sent", {}).setdefault("submits", {})
    for key in list(sent.keys()):
        clean_key = str(key).strip().replace("#", "")
        if clean_key == sid or clean_key.lstrip("0") == sid.lstrip("0"):
            sent.pop(key, None)

    if found_user_id:
        user = get_user(data, found_user_id)
        if found_task_id and found_task_id in user.get("pending_tasks", []):
            user["pending_tasks"].remove(found_task_id)
        if found_task_id and str(user.get("waiting_task")) == found_task_id:
            user["waiting_task"] = None

    return data

def remove_withdraw_from_data(data, withdraw_id, user_id=None):
    """
    Полностью убирает активную заявку на вывод из data.json.

    После одобрения/отказа/удаления в data остаётся только нужное:
    - users[user_id]["balance"] — текущий баланс;
    - статистика выплат/возвратов;
    - короткая защита processed_requests, чтобы старая копия GitHub/local
      не вернула закрытую заявку назад.
    Сам объект data["withdraws"][id] удаляется.
    """
    wid = str(withdraw_id).strip().replace("#", "")
    data.setdefault("withdraws", {})

    keys_to_delete = {wid, f"#{wid}", wid.lstrip("0") or "0"}
    for key in list(data.get("withdraws", {}).keys()):
        clean_key = str(key).strip().replace("#", "")
        if clean_key == wid or clean_key.lstrip("0") == wid.lstrip("0") or str(key) in keys_to_delete:
            data["withdraws"].pop(key, None)

    # Метки отправки админу больше не нужны для закрытой заявки.
    sent = data.setdefault("admin_sent", {}).setdefault("withdraws", {})
    for key in list(sent.keys()):
        clean_key = str(key).strip().replace("#", "")
        if clean_key == wid or clean_key.lstrip("0") == wid.lstrip("0"):
            sent.pop(key, None)

    if user_id is not None:
        uid = str(user_id)
        user = get_user(data, uid)
        user["withdraw_pending"] = any(
            str(x.get("user_id")) == uid and x.get("status") == "wait"
            for x in data.get("withdraws", {}).values()
        )
        user["withdraw_step"] = None
        user["withdraw_to"] = None

    return data

def cleanup_closed_requests(data):
    """
    Убирает из активных списков заявки, которые уже были обработаны.
    Главное исправление: после Одобрить/Отказать/Удалить заявка больше
    не может вернуться в data.json при склейке локального файла и GitHub.
    """
    data.setdefault("processed_requests", {}).setdefault("submits", {})
    data.setdefault("processed_requests", {}).setdefault("withdraws", {})

    processed_submits = {str(x).strip().replace("#", "") for x in data["processed_requests"].get("submits", {}).keys()}
    processed_withdraws = {str(x).strip().replace("#", "") for x in data["processed_requests"].get("withdraws", {}).keys()}

    for sid, submit in list(data.get("submits", {}).items()):
        clean_sid = str(sid).strip().replace("#", "")
        if clean_sid in processed_submits or submit.get("status") in ["approved", "rejected", "deleted", "paid", "expired", "processing_done"]:
            uid = str(submit.get("user_id", ""))
            task_id = str(submit.get("task_id", ""))
            if uid:
                user = get_user(data, uid)
                if task_id in user.get("pending_tasks", []):
                    user["pending_tasks"].remove(task_id)
                if str(user.get("waiting_task")) == task_id:
                    user["waiting_task"] = None
            remove_submit_from_data(data, sid, uid, task_id)

    for wid, w in list(data.get("withdraws", {}).items()):
        clean_wid = str(wid).strip().replace("#", "")
        if clean_wid in processed_withdraws or w.get("status") in ["paid", "rejected", "deleted", "expired", "processing_done"]:
            uid = str(w.get("user_id", ""))
            data.setdefault("withdraws", {}).pop(wid, None)
            if uid:
                user = get_user(data, uid)
                user["withdraw_pending"] = any(
                    str(x.get("user_id")) == uid and x.get("status") == "wait"
                    for x in data.get("withdraws", {}).values()
                )

    return data

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


def find_duplicate_withdraw_by_message(data, user_id, source_message_id):
    """
    Дубль вывода считаем только если Telegram повторно прислал ТО ЖЕ самое сообщение
    с той же суммой (одинаковый message_id). Это не мешает пользователю создавать
    много новых выводов подряд, если баланс позволяет.
    """
    if source_message_id is None:
        return None
    for wid, withdraw in data.get("withdraws", {}).items():
        if withdraw.get("status") != "wait":
            continue
        if str(withdraw.get("user_id")) != str(user_id):
            continue
        if str(withdraw.get("source_message_id", "")) == str(source_message_id):
            return str(wid)
    return None


def admin_request_was_sent(data, kind, request_id):
    """
    Железная проверка: заявка уже отправлялась админу.
    Проверяем сразу 2 места:
    1) общий список data["admin_sent"];
    2) поле admin_sent внутри самой заявки.
    Поэтому /requests не сможет продублировать заявку даже при лаге/перезапуске.
    """
    rid = str(request_id).strip().replace("#", "")

    if rid in data.setdefault("admin_sent", {}).setdefault(kind, {}):
        return True

    real_id, req = find_request_by_id(data.get(kind, {}), rid)
    if req and req.get("admin_sent"):
        return True

    return False


def mark_admin_request_sent(data, kind, request_id, message_id=None):
    """
    Ставит метку ДО отправки сообщения админу.
    Это важно: если админ в этот момент нажмёт /requests, дубль не улетит.
    """
    rid = str(request_id).strip().replace("#", "")
    data.setdefault("admin_sent", {}).setdefault(kind, {})[rid] = {
        "message_id": message_id,
        "time": int(time.time())
    }

    real_id, req = find_request_by_id(data.get(kind, {}), rid)
    if req is not None:
        req["admin_sent"] = True
        req["admin_sent_time"] = int(time.time())
        if message_id is not None:
            req["admin_message_id"] = message_id

    # Не даём data.json раздуваться
    sent = data.setdefault("admin_sent", {}).setdefault(kind, {})
    if len(sent) > 300:
        old_keys = sorted(sent.keys(), key=lambda k: sent[k].get("time", 0))[:-300]
        for k in old_keys:
            sent.pop(k, None)


def find_active_submit_by_photo(data, user_id, task_id, photo_file_id):
    """Защита от дубля: тот же пользователь, то же задание, тот же скрин."""
    for sid, submit in data.get("submits", {}).items():
        if (
            str(submit.get("user_id")) == str(user_id)
            and str(submit.get("task_id")) == str(task_id)
            and str(submit.get("photo_file_id")) == str(photo_file_id)
            and submit.get("status") == "wait"
        ):
            return str(sid)
    return None



# Поля пользователя, которые НЕ нужно постоянно хранить в data.json,
# если они пустые. При загрузке bot.py сам создаст их обратно через fix_data().
TEMP_USER_KEYS_DEFAULTS = {
    "waiting_task": None,
    "last_open_task": None,
    "withdraw_pending": False,
    "withdraw_step": None,
    "withdraw_to": None,
    "pending_tasks": [],
    # Чтобы data.json не раздувался: у незаблокированных эти поля не храним.
    "banned": False,
    "ban_reason": "",
    "ban_until": 0,
    "ban_created": 0,
}

def compact_data_for_save(data):
    """
    Уменьшает data.json перед сохранением.
    ВАЖНО: не трогает баланс, пользователей, задания, статистику.
    Удаляет только пустые временные поля, которые bot.py умеет восстановить сам.
    Активные значения НЕ удаляются.
    """
    clean = json.loads(json.dumps(data, ensure_ascii=False))

    # заявки живут в requests.json, в data.json они не нужны
    clean["submits"] = {}
    clean["withdraws"] = {}
    clean.setdefault("admin_sent", {"submits": {}, "withdraws": {}})
    clean["admin_sent"] = {"submits": {}, "withdraws": {}}

    for uid, user in clean.get("users", {}).items():
        if not isinstance(user, dict):
            continue
        for key, default_value in TEMP_USER_KEYS_DEFAULTS.items():
            if user.get(key) == default_value:
                user.pop(key, None)

    # Логи баланса сильно раздувают файл. Оставляем последние 100,
    # этого хватает для проверки последних действий. Балансы и статистику не трогаем.
    if isinstance(clean.get("balance_logs"), list) and len(clean["balance_logs"]) > 100:
        clean["balance_logs"] = clean["balance_logs"][-100:]

    return clean

def save_data(data):
    global DATA_CACHE

    data = fix_data(data)
    cleanup_expired_account_bans(data, save=False)

    # Защита от потери пользователей без медленного чтения GitHub на каждый клик:
    # склеиваем с тем, что уже лежит в памяти.
    try:
        with CACHE_LOCK:
            cached = clone_json(DATA_CACHE) if DATA_CACHE is not None else None
        if cached is not None:
            data = merge_data(data, cached)
        else:
            local_old = read_local_data_raw()
            if local_old is not None:
                data = merge_data(data, local_old)
    except Exception as e:
        print("Merge before save error:", e)

    sync_withdraw_stats(data)

    # Активные заявки сохраняем отдельно в requests.json,
    # а data.json оставляем только для важных постоянных данных.
    data, req_to_save = split_requests_from_data(data)
    save_requests(req_to_save)

    # Перед сохранением убираем пустые временные поля из data.json.
    data = compact_data_for_save(data)

    with CACHE_LOCK:
        DATA_CACHE = clone_json(data)

    # Быстро сохраняем локально.
    try:
        save_json_atomic(LOCAL_DATA_FILE, data)
    except Exception as e:
        print("Local save exception:", e)

    # Медленный GitHub — в фоне, чтобы бот быстрее отвечал пользователю.
    save_to_github_background(GITHUB_FILE, data, "update bot data")



def get_user(data, user_id):
    uid = str(user_id)
    if uid not in data["users"]:
        data["users"][uid] = {
            "balance": 0,
            "done_tasks": [],
            "pending_tasks": [],
            "waiting_task": None,
            "last_open_task": None,
            "withdraw_pending": False,
            "withdraw_step": None,
            "withdraw_to": None,
            "last_bonus": 0,
            "completed_tasks": 0,
            "total_earned": 0,
            "withdraw_count": 0,
            "withdrawn_total": 0,
            "fines_total": 0,
            "banned": False,
            "ban_reason": "",
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "user_id": int(user_id) if str(user_id).isdigit() else str(user_id),
            "username": "",
            "first_name": "",
            "last_name": "",
            "language_code": ""
        }

    user = data["users"][uid]
    user.setdefault("balance", 0)
    user.setdefault("done_tasks", [])
    user.setdefault("pending_tasks", [])
    user.setdefault("waiting_task", None)
    user.setdefault("last_open_task", None)
    user.setdefault("withdraw_pending", False)
    user.setdefault("withdraw_step", None)
    user.setdefault("withdraw_to", None)
    user.setdefault("last_bonus", 0)
    user.setdefault("completed_tasks", len(user.get("done_tasks", [])))
    user.setdefault("total_earned", float(user.get("balance", 0)) if float(user.get("balance", 0)) > 0 else 0)
    user.setdefault("withdraw_count", 0)
    user.setdefault("withdrawn_total", 0)
    user.setdefault("fines_total", 0)
    user.setdefault("created_at", int(time.time()))
    user.setdefault("updated_at", int(time.time()))
    user.setdefault("user_id", int(user_id) if str(user_id).isdigit() else str(user_id))
    user.setdefault("username", "")
    user.setdefault("first_name", "")
    user.setdefault("last_name", "")
    user.setdefault("language_code", "")
    user.setdefault("banned", False)
    user.setdefault("ban_reason", "")
    user.setdefault("ban_until", 0)
    user.setdefault("ban_created", 0)
    return user


def cancel_user_states(user):
    # waiting_task НЕ сбрасываем: пользователь может нажать /start/Меню и потом отправить скрин.
    # Сбрасываем только вывод, чтобы не мешать заявке на задание.
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

UA_TZ = ZoneInfo("Europe/Kyiv")


def format_block_time(ts):
    try:
        return datetime.fromtimestamp(int(ts), UA_TZ).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "не указано"


def parse_withdraw_block_until(text):
    """
    Поддержка сроков:
    2h / 2ч / 2 часа, 30m / 30м, 6d / 6д / 6 дней
    или точная дата: 01.07.2026 17:12
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return None

    m = re.fullmatch(r"(\d+)\s*(m|min|мин|м)", raw)
    if m:
        return int(time.time()) + int(m.group(1)) * 60

    m = re.fullmatch(r"(\d+)\s*(h|hour|hours|ч|час|часа|часов)", raw)
    if m:
        return int(time.time()) + int(m.group(1)) * 60 * 60

    m = re.fullmatch(r"(\d+)\s*(d|day|days|д|день|дня|дней)", raw)
    if m:
        return int(time.time()) + int(m.group(1)) * 24 * 60 * 60

    try:
        dt = datetime.strptime(raw, "%d.%m.%Y %H:%M")
        return int(dt.replace(tzinfo=UA_TZ).timestamp())
    except Exception:
        return None




def parse_account_ban_until(text):
    """Срок бана аккаунта: 30m, 2h, 1d или дата 03.07.2026 20:36."""
    return parse_withdraw_block_until(text)


def cleanup_expired_account_bans(data=None, save=True):
    """
    Снимает просроченные баны и удаляет лишние поля, чтобы data.json не засорялся.
    Возвращает список ID, у которых бан закончился.
    """
    own_data = data is None
    if own_data:
        data = load_data()

    now = int(time.time())
    expired = []
    for uid, user in list(data.get("users", {}).items()):
        if not isinstance(user, dict):
            continue

        until = int(user.get("ban_until", 0) or 0)
        if bool(user.get("banned", False)) and until and until <= now:
            user["banned"] = False
            user["ban_reason"] = ""
            user["ban_until"] = 0
            user["ban_created"] = 0
            expired.append(str(uid))

        # Старые пустые значения тоже чистим при сохранении через compact_data_for_save.
        if not bool(user.get("banned", False)):
            user["ban_reason"] = ""
            user["ban_until"] = 0
            user["ban_created"] = 0

    if expired and save:
        save_data(data)
    return expired


def get_active_account_ban(data, user_id):
    user = get_user(data, user_id)
    if not bool(user.get("banned", False)):
        return None

    until = int(user.get("ban_until", 0) or 0)
    if until and until <= int(time.time()):
        user["banned"] = False
        user["ban_reason"] = ""
        user["ban_until"] = 0
        user["ban_created"] = 0
        save_data(data)
        return None

    return {
        "reason": user.get("ban_reason") or "не указана",
        "until": until,
    }


def account_ban_user_text(ban):
    reason = ban.get("reason") or "не указана"
    until = int(ban.get("until", 0) or 0)
    until_text = format_block_time(until) if until else "навсегда"
    return (
        "⛔ <b>Ваш аккаунт заблокирован</b>\n\n"
        f"⏳ До: <b>{until_text}</b>\n"
        f"📌 Причина: {h(reason)}\n\n"
        "Для уточнения обратитесь к администрации."
    )


def format_banned_users_report(data, limit=40):
    cleanup_expired_account_bans(data, save=False)
    rows = []
    for uid, user in data.get("users", {}).items():
        if not isinstance(user, dict) or not bool(user.get("banned", False)):
            continue
        username = safe_username(user.get("username"))
        reason = user.get("ban_reason") or "не указана"
        until = int(user.get("ban_until", 0) or 0)
        until_text = format_block_time(until) if until else "навсегда"
        rows.append((until or 9999999999, str(uid), username, reason, until_text))

    rows.sort(key=lambda x: x[0])
    total = len(rows)
    if total == 0:
        return "✅ <b>Заблокированных пользователей нет.</b>\n\n🧹 Просроченные баны очищаются автоматически."

    lines = [f"🚫 <b>Заблокированные пользователи:</b> {total}\n"]
    for _, uid, username, reason, until_text in rows[:limit]:
        lines.append(
            f"\n🆔 <code>{uid}</code> | @{h(username)}\n"
            f"⏳ До: <b>{h(until_text)}</b>\n"
            f"📌 {h(short_text(reason, 160))}"
        )
    if total > limit:
        lines.append(f"\n\n…и ещё {total - limit} пользователей.")
    lines.append("\n\nКоманды: <code>/unban ID</code> или <code>/profile ID</code>")
    return "".join(lines)

def get_active_withdraw_block(data, user_id):
    blocks = data.setdefault("withdraw_blocks", {})
    block = blocks.get(str(user_id))
    if not block:
        return None

    until = int(block.get("until", 0) or 0)
    if until <= int(time.time()):
        blocks.pop(str(user_id), None)
        user = get_user(data, user_id)
        user["withdraw_step"] = None
        user["withdraw_to"] = None
        return None

    return block


def withdraw_block_text(block):
    reason = block.get("reason") or "не указана"
    until_text = format_block_time(block.get("until", 0))
    return (
        f"🚫 <b>Ваш вывод заблокирован до {until_text}</b>\n\n"
        f"📌 <b>Причина:</b> {h(reason)}\n\n"
        "Вы можете пользоваться ботом, выполнять задания и копить GMP, "
        "но создать вывод получится только после окончания блокировки."
    )


def cleanup_expired_withdraw_blocks(notify_admin=False):
    with DATA_LOCK:
        data = load_data()
        blocks = data.setdefault("withdraw_blocks", {})
        now = int(time.time())
        expired = []

        for uid, block in list(blocks.items()):
            if int(block.get("until", 0) or 0) <= now:
                expired.append((str(uid), block))
                blocks.pop(str(uid), None)
                user = get_user(data, uid)
                user["withdraw_step"] = None
                user["withdraw_to"] = None

        if expired:
            save_data(data)

    if notify_admin:
        for uid, block in expired:
            uname = block.get("username") or get_user(load_data(), uid).get("username") or "нет username"
            safe_send(
                ADMIN_ID,
                f"✅ <b>Время блокировки вывода вышло</b>\n\n"
                f"🆔 ID: <code>{uid}</code>\n"
                f"👤 Username: @{str(uname).replace('@', '') if uname else 'нет username'}\n"
                f"⏰ Было до: <b>{format_block_time(block.get('until', 0))}</b>"
            )


def auto_withdraw_unblock_loop():
    time.sleep(20)
    while True:
        try:
            cleanup_expired_withdraw_blocks(notify_admin=True)
        except Exception as e:
            print("withdraw unblock loop error:", e)
        time.sleep(60)


def get_required_missing_tasks(data, user):
    """Возвращает обязательные активные задания, которые пользователь ещё не выполнил."""
    missing = []
    done = {str(tid) for tid in user.get("done_tasks", [])}
    required_ids = list(dict.fromkeys(str(tid) for tid in data.get("required_tasks", [])))

    for task_id in required_ids:
        task = data.get("tasks", {}).get(str(task_id))
        if not task:
            continue
        if not task.get("active", True):
            continue
        if str(task_id) in done:
            continue
        missing.append(str(task_id))

    return missing


def required_tasks_block_text(missing):
    if not missing:
        return ""
    if len(missing) == 1:
        return (
            f"❌ <b>Вывод пока недоступен.</b>\n\n"
            f"Сначала выполни обязательное задание <b>#{missing[0]}</b>.\n"
            f"После одобрения задания ты сможешь вывести GMP."
        )
    nums = ", ".join(f"#{x}" for x in missing[:10])
    return (
        f"❌ <b>Вывод пока недоступен.</b>\n\n"
        f"Сначала выполни обязательные задания: <b>{nums}</b>.\n"
        f"После одобрения заданий ты сможешь вывести GMP."
    )


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


def log_balance_change(data, user_id, old_balance, amount, new_balance, reason="", request_id=""):
    """Короткий лог баланса, чтобы потом понять, что и почему изменилось."""
    data.setdefault("balance_logs", [])
    data["balance_logs"].append({
        "time": int(time.time()),
        "user_id": str(user_id),
        "old": round(float(old_balance), 2),
        "amount": round(float(amount), 2),
        "new": round(float(new_balance), 2),
        "reason": str(reason),
        "request_id": str(request_id)
    })
    # Чтобы data.json не раздувался
    data["balance_logs"] = data["balance_logs"][-500:]


def add_balance_to_user(data, user_id, amount, reason="add", request_id=""):
    """
    Единая функция изменения баланса.

    Примеры:
    - было -2, начислили +1 -> стало -1
    - было -2, начислили +5 -> стало 3
    - списание делается amount отрицательным: -4

    В data.json старые балансы не копятся — поле balance просто перезаписывается.
    """
    user = get_user(data, user_id)
    amount = round(float(amount), 2)
    old_balance = round(float(user.get("balance", 0)), 2)
    new_balance = round(old_balance + amount, 2)

    user["balance"] = new_balance

    # Возврат вывода — это НЕ новый заработок, а возврат уже списанных GMP.
    # Поэтому total_earned не увеличиваем при rejected/deleted/expired/clear_return.
    is_withdraw_return = str(reason or "").startswith("withdraw_") and str(reason or "").endswith("_return")

    if amount > 0 and not is_withdraw_return:
        user["total_earned"] = round(float(user.get("total_earned", 0)) + amount, 2)

        # Статистика погашения долга: только для админа/проверки, на баланс не влияет.
        if old_balance < 0:
            paid_debt = min(abs(old_balance), amount)
            user["debt_paid_total"] = round(float(user.get("debt_paid_total", 0)) + paid_debt, 2)

    log_balance_change(data, user_id, old_balance, amount, new_balance, reason, request_id)
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
    users_total_paid = round(
        sum(float(u.get("withdrawn_total", 0)) for u in data.get("users", {}).values()),
        2
    )
    users_total_withdraws = sum(
        int(u.get("withdraw_count", 0)) for u in data.get("users", {}).values()
    )
    total_paid = max(float(data.get("total_paid", 0)), users_total_paid)
    total_withdraws = max(int(data.get("total_withdrawals", 0)), users_total_withdraws)

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
        "<code>/delpromo КОД</code> — удалить промокод\n"
        "<code>/addrequired Текст | ссылка | награда</code> — обязательное задание\n"
        "<code>/required номер</code> — сделать задание обязательным\n"
        "<code>/unrequired номер</code> — убрать обязательность"
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
    except Exception as e:
        print(f"safe_send error to {user_id}:", e)
        return False


def remember_telegram_user(data, tg_user):
    """
    Автоматически записывает/обновляет пользователя в data.json.
    Вызывать при /start, любом сообщении и любой inline-кнопке.
    """
    uid = str(tg_user.id)
    is_new = uid not in data.get("users", {})
    user = get_user(data, uid)

    now = int(time.time())
    user.setdefault("created_at", now)
    user["updated_at"] = now
    user["user_id"] = int(tg_user.id)
    user["username"] = tg_user.username or user.get("username", "")
    user["first_name"] = tg_user.first_name or user.get("first_name", "")
    user["last_name"] = tg_user.last_name or user.get("last_name", "")
    user["language_code"] = getattr(tg_user, "language_code", None) or user.get("language_code", "")

    return user, is_new


def auto_save_user_from_message(message):
    """Запись пользователя при любом обычном сообщении/кнопке клавиатуры."""
    with DATA_LOCK:
        data = load_fresh_data_for_ban_check()
        user, is_new = remember_telegram_user(data, message.from_user)
        save_data(data)
    return data, user, is_new


def auto_save_user_from_call(call):
    """Запись пользователя при любой inline-кнопке."""
    with DATA_LOCK:
        data = load_fresh_data_for_ban_check()
        user, is_new = remember_telegram_user(data, call.from_user)
        save_data(data)
    return data, user, is_new



def load_fresh_data_for_ban_check():
    # Для бана/разбана читаем свежую базу, но не затираем локальных пользователей.
    github_data = read_github_data_raw()
    local_data = read_local_data_raw()

    if github_data is not None and local_data is not None:
        data = merge_data(github_data, local_data)
    elif github_data is not None:
        data = github_data
    elif local_data is not None:
        data = local_data
    else:
        data = empty_data()

    try:
        with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return fix_data(data)


def is_banned_user(message):
    data, user, _ = auto_save_user_from_message(message)

    if message.from_user.id == ADMIN_ID:
        return False

    if bool(user.get("banned", False)):
        reason = user.get("ban_reason") or "не указана"
        bot.send_message(
            message.chat.id,
            f"⛔ <b>Ваш аккаунт заблокирован</b>\n\n"
            f"📌 Причина: {reason}\n\n"
            "Для уточнения обратитесь к администрации."
        )
        return True

    return False


def is_banned_call(call):
    data, user, _ = auto_save_user_from_call(call)

    if call.from_user.id == ADMIN_ID:
        return False

    if bool(user.get("banned", False)):
        reason = user.get("ban_reason") or "не указана"
        try:
            bot.answer_callback_query(call.id, "Аккаунт заблокирован.", show_alert=True)
        except Exception:
            pass
        bot.send_message(
            call.message.chat.id,
            f"⛔ <b>Ваш аккаунт заблокирован</b>\n\n"
            f"📌 Причина: {reason}\n\n"
            "Для уточнения обратитесь к администрации."
        )
        return True

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
    with DATA_LOCK:
        data = load_data()
        user, is_new = remember_telegram_user(data, message.from_user)
        cancel_user_states(user)
        save_data(data)

    bot.send_message(message.chat.id, data["start_text"], reply_markup=main_menu())



@bot.message_handler(commands=["withdrawoff"])
def withdraw_off(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    data = load_data()
    data["withdraw_enabled"] = False
    data["withdraw_disabled_by_admin"] = True
    data["withdraw_disabled_at"] = int(time.time())
    data["withdraw_disabled_by"] = int(message.from_user.id)

    # Если кто-то уже начал вводить вывод — сбрасываем шаги, чтобы заявка не создалась после отключения.
    for u in data.get("users", {}).values():
        u["withdraw_step"] = None
        u["withdraw_to"] = None

    save_data(data)
    bot.send_message(
        message.chat.id,
        "✅ Выплаты отключены.\\n\\n"
        "Пользователи могут выполнять задания и зарабатывать GMP, но вывод временно закрыт."
    )


@bot.message_handler(commands=["withdrawon"])
def withdraw_on(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    data = load_data()
    data["withdraw_enabled"] = True
    data["withdraw_disabled_by_admin"] = False
    data.pop("withdraw_disabled_at", None)
    data.pop("withdraw_disabled_by", None)
    save_data(data)
    bot.send_message(message.chat.id, "✅ Выплаты включены. Пользователи снова могут создавать заявки на вывод.")

@bot.message_handler(commands=["withdrawstatus", "wstatus"])
def withdraw_status(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    data = load_data()
    enabled = data.get("withdraw_enabled", True)
    disabled_by_admin = data.get("withdraw_disabled_by_admin", False)
    blocks_count = len(data.get("withdraw_blocks", {}))

    if enabled:
        state = "включены ✅"
    elif disabled_by_admin:
        state = "отключены админом ❌"
    else:
        state = "были выключены старым data.json, но код теперь будет чинить это ✅"

    bot.send_message(
        message.chat.id,
        "💸 <b>Статус выплат</b>\n\n"
        f"Состояние: <b>{state}</b>\n"
        f"Индивидуальных блокировок: <b>{blocks_count}</b>\n\n"
        "Открыть выплаты: <code>/withdrawon</code>\n"
        "Закрыть выплаты вручную: <code>/withdrawoff</code>"
    )

@bot.message_handler(commands=["admin"])
def admin(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")
    bot.send_message(message.chat.id, "🔐 <b>Админ-панель</b>", reply_markup=admin_menu())


@bot.message_handler(func=lambda m: m.text == "🏠 Меню")
def menu(message):
    if is_banned_user(message):
        return

    data = load_data()
    user = get_user(data, message.from_user.id)
    cancel_user_states(user)
    save_data(data)
    bot.send_message(message.chat.id, "🏠 Главное меню", reply_markup=main_menu())


@bot.message_handler(func=lambda m: m.text == "💬 Общение")
def chat_message(message):
    if is_banned_user(message):
        return

    chat_button(message)


@bot.message_handler(func=lambda m: m.text == "ℹ️ Помощь")
def help_message(message):
    if is_banned_user(message):
        return

    bot.send_message(
        message.chat.id,
        "ℹ️ <b>Как работает бот:</b>\n\n"
        "1. Нажми 📋 Задания\n"
        "2. Открой задание и выполни его\n"
        "3. Нажми ✅ Я выполнил\n"
        "4. Отправь скрин\n"
        "5. После проверки получишь GMP\n\n"
        "🎉 Бонус 0.5 GMP можно получать 1 раз в 24 часа, если в био стоит нужная надпись.\n"
        "💸 Для вывода нажми «Вывод», укажи куда вывести и сумму."
    )


@bot.message_handler(func=lambda m: m.text == "💰 Баланс")
def balance(message):
    if is_banned_user(message):
        return

    data = load_data()
    user = get_user(data, message.from_user.id)
    save_data(data)

    my_withdraws = [w for w in data.get("withdraws", {}).values() if str(w.get("user_id")) == str(message.from_user.id) and w.get("status") == "wait"]
    pending = f"\n⏳ Заявок на вывод: {len(my_withdraws)}" if my_withdraws else ""
    bot.send_message(message.chat.id, f"💰 <b>Твой баланс:</b> {format_gmp(user['balance'])} GMP{pending}")



@bot.message_handler(func=lambda m: m.text in ["🖥 Профиль", "👤 Профиль"])
def my_profile(message):
    if is_banned_user(message):
        return

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



@bot.message_handler(commands=["richest"])
def richest_command(message):
    data = load_data()

    users = []
    for uid, user in data.get("users", {}).items():
        try:
            balance = float(user.get("balance", 0))
        except:
            balance = 0

        username = user.get("username")
        if username:
            username = "@" + username
        else:
            username = f"ID {uid}"

        users.append((balance, username))

    users.sort(key=lambda x: x[0], reverse=True)

    text_msg = "🏆 Топ богатых игроков\n\n"

    medals = ["🥇", "🥈", "🥉"]

    for i, (balance, username) in enumerate(users[:10], start=1):
        prefix = medals[i-1] if i <= 3 else f"{i}."
        text_msg += f"{prefix} {username} — {balance} GMP\n"

    bot.send_message(message.chat.id, text_msg)

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


def user_has_required_bonus_bio(user_id):
    """Проверяет био пользователя. В data.json не пишет лишнюю историю бонусов."""
    try:
        chat = bot.get_chat(user_id)
        bio = (getattr(chat, "bio", "") or getattr(chat, "description", "") or "").strip()
        return BONUS_REQUIRED_BIO.lower() in bio.lower(), bio
    except Exception as e:
        print("bonus bio check error:", e)
        return False, ""


@bot.message_handler(func=lambda m: m.text == "🎉 Бонус")
def daily_bonus(message):
    if is_banned_user(message):
        return

    data = load_data()
    user = get_user(data, message.from_user.id)

    ok_bio, _bio = user_has_required_bonus_bio(message.from_user.id)
    if not ok_bio:
        save_data(data)
        return bot.send_message(
            message.chat.id,
            "🎉 <b>Ежедневный бонус 0.5 GMP</b>\n\n"
            "Чтобы получать бонус, поставь в Telegram в био/о себе эту надпись:\n"
            f"<code>{BONUS_REQUIRED_BIO}</code>\n\n"
            "После этого снова нажми кнопку 🎉 Бонус.\n"
            "Если уберёшь эту надпись — бонус снова не будет выдаваться."
        )

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

    old_balance = round(float(user.get("balance", 0)), 2)
    new_balance = round(old_balance + BONUS_AMOUNT, 2)
    user["balance"] = new_balance
    user["total_earned"] = round(float(user.get("total_earned", 0)) + BONUS_AMOUNT, 2)
    user["last_bonus"] = now
    user["updated_at"] = now
    save_data(data)

    bot.send_message(
        message.chat.id,
        "🎉 <b>Ежедневный бонус получен!</b>\n\n"
        f"💰 Начислено: <b>{format_gmp(BONUS_AMOUNT)} GMP</b>\n"
        f"💎 Баланс: <b>{format_gmp(old_balance)} → {format_gmp(new_balance)} GMP</b>"
    )



def get_available_task_ids(data, user):
    """Задания, которые пользователь ещё может выполнить. Одобренные/ожидающие/удалённые скрываются."""
    done = {str(x) for x in user.get("done_tasks", [])}
    pending = {str(x) for x in user.get("pending_tasks", [])}
    waiting = str(user.get("waiting_task") or "")

    def sort_key(tid):
        try:
            return int(tid)
        except Exception:
            return 10**12

    ids = []
    for task_id, task in data.get("tasks", {}).items():
        task_id = str(task_id)
        if not isinstance(task, dict):
            continue
        if not task.get("active", True):
            continue
        if task_id in done or task_id in pending or task_id == waiting:
            continue
        ids.append(task_id)
    return sorted(ids, key=sort_key)


def task_screen_keyboard(task_id, index, total, task):
    kb = types.InlineKeyboardMarkup()
    link = str(task.get("link", "")).strip()
    if link:
        kb.add(types.InlineKeyboardButton("🔗 Выполнить", url=link))
    kb.add(types.InlineKeyboardButton("📸 Отправить скрин", callback_data=f"tasksubmit_{task_id}"))

    nav = []
    nav.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"taskprev_{index}"))
    if index < total - 1:
        nav.append(types.InlineKeyboardButton("➡️ Далее", callback_data=f"tasknext_{index}"))
    kb.row(*nav)
    kb.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="taskhome"))
    return kb


def task_screen_text(task_id, task, index, total):
    return (
        f"📋 <b>Задание {index + 1} из {total}</b>\n\n"
        f"🆔 ID задания: <b>#{h(task_id)}</b>\n"
        f"💎 Награда: <b>{format_gmp(task.get('reward', 0))} GMP</b>\n\n"
        f"📝 {h(task.get('text', 'Описание задания'))}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ После полного выполнения нажмите «📸 Отправить скрин»."
    )


def show_task_screen(chat_id, user_id, index=0, call=None):
    data = load_data()
    user = get_user(data, user_id)
    task_ids = get_available_task_ids(data, user)

    if not task_ids:
        text = (
            "✅ <b>Новых заданий сейчас нет.</b>\n\n"
            "Ты уже выполнил доступные задания или они сейчас на проверке."
        )
        if call:
            try:
                bot.edit_message_text(text, call.message.chat.id, call.message.message_id)
            except Exception:
                bot.send_message(chat_id, text, reply_markup=main_menu())
        else:
            bot.send_message(chat_id, text, reply_markup=main_menu())
        return

    if index < 0:
        index = 0
    if index >= len(task_ids):
        index = len(task_ids) - 1

    task_id = task_ids[index]
    task = data.get("tasks", {}).get(task_id, {})
    user["last_open_task"] = task_id
    save_data(data)

    text = task_screen_text(task_id, task, index + 1, len(task_ids))
    kb = task_screen_keyboard(task_id, index, len(task_ids), task)

    if call:
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
            return
        except Exception as e:
            print("edit task screen error:", e)
    bot.send_message(chat_id, text, reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == "📋 Задания")
def tasks(message):
    if is_banned_user(message):
        return
    show_task_screen(message.chat.id, message.from_user.id, 0)


@bot.callback_query_handler(func=lambda c: c.data.startswith("tasknext_") or c.data.startswith("taskprev_"))
def task_navigation(call):
    if is_banned_call(call):
        return

    try:
        action, idx_text = call.data.split("_", 1)
        index = int(idx_text)
    except Exception:
        index = 0

    data = load_data()
    user = get_user(data, call.from_user.id)
    total = len(get_available_task_ids(data, user))

    if action == "taskprev":
        if index <= 0:
            return safe_answer_callback(call, "Это первое задание.")
        new_index = index - 1
    else:
        if index >= total - 1:
            return safe_answer_callback(call, "Это последнее задание.")
        new_index = index + 1

    safe_answer_callback(call)
    show_task_screen(call.message.chat.id, call.from_user.id, new_index, call=call)


@bot.callback_query_handler(func=lambda c: c.data == "taskhome")
def task_home(call):
    if is_banned_call(call):
        return
    safe_answer_callback(call)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.send_message(call.message.chat.id, "🏠 Главное меню", reply_markup=main_menu())


@bot.callback_query_handler(func=lambda c: c.data.startswith("tasksubmit_"))
def task_submit_from_screen(call):
    if is_banned_call(call):
        return

    data = load_data()
    user = get_user(data, call.from_user.id)
    task_id = call.data.split("_", 1)[1]

    if task_id in user.get("done_tasks", []):
        return safe_answer_callback(call, "Ты уже сделал это задание.", show_alert=True)

    old_sid = has_active_submit(data, call.from_user.id, task_id)
    if task_id in user.get("pending_tasks", []) or old_sid:
        if old_sid and task_id not in user.get("pending_tasks", []):
            user.setdefault("pending_tasks", []).append(task_id)
            save_data(data)
        return safe_answer_callback(call, "Задание уже на проверке.", show_alert=True)

    task = data.get("tasks", {}).get(task_id)
    if not task or not task.get("active", True):
        return safe_answer_callback(call, "Задание удалено или недоступно.", show_alert=True)

    user["waiting_task"] = task_id
    user["last_open_task"] = task_id
    user["withdraw_step"] = None
    user["withdraw_to"] = None
    save_data(data)

    safe_answer_callback(call, "Отправь скриншот.")
    bot.send_message(
        call.message.chat.id,
        f"📸 <b>Отправьте скриншот выполнения задания #{task_id}.</b>\n\n"
        "После проверки задание пропадёт из раздела заданий навсегда, если админ одобрит заявку."
    )


# Старые callback-кнопки оставлены для заявок/старых сообщений: task_ и done_ больше не используются в новом интерфейсе,
# но если у кого-то осталось старое сообщение, оно всё равно откроется.
@bot.callback_query_handler(func=lambda c: c.data.startswith("task_"))
def open_task(call):
    if is_banned_call(call):
        return

    data = load_data()
    user = get_user(data, call.from_user.id)
    task_id = call.data.split("_")[1]

    task_ids = get_available_task_ids(data, user)
    if task_id in task_ids:
        return show_task_screen(call.message.chat.id, call.from_user.id, task_ids.index(task_id), call=call)

    return safe_answer_callback(call, "Задание уже выполнено, на проверке или удалено.", show_alert=True)


@bot.callback_query_handler(func=lambda c: c.data.startswith("done_"))
def done_task(call):
    if is_banned_call(call):
        return

    # Поддержка старой кнопки ✅ Я выполнил
    data = load_data()
    user = get_user(data, call.from_user.id)
    task_id = call.data.split("_")[1]

    if task_id in user.get("done_tasks", []):
        return safe_answer_callback(call, "Ты уже сделал это задание.", show_alert=True)

    old_sid = has_active_submit(data, call.from_user.id, task_id)
    if task_id in user.get("pending_tasks", []) or old_sid:
        if old_sid and task_id not in user.get("pending_tasks", []):
            user.setdefault("pending_tasks", []).append(task_id)
            save_data(data)
        return safe_answer_callback(call, "Задание уже на проверке.", show_alert=True)

    if task_id not in data.get("tasks", {}):
        return safe_answer_callback(call, "Задание не найдено.", show_alert=True)

    user["waiting_task"] = task_id
    user["last_open_task"] = task_id
    user["withdraw_step"] = None
    user["withdraw_to"] = None
    save_data(data)

    safe_answer_callback(call, "Отправь скриншот.")
    bot.send_message(call.message.chat.id, f"📸 Отправь скриншот выполнения задания #{task_id}.")


@bot.message_handler(content_types=["photo", "document"])
def photo(message):
    if is_banned_user(message):
        return

    """
    Железная логика заявки на задание:
    - один пользователь + одно задание = только одна активная заявка;
    - можно отправить скрин как фото или как файл-картинку;
    - если Telegram/интернет прислал фото повторно — новый номер не создаётся;
    - /requests не будет повторно кидать админу одно и то же фото.
    """
    is_document_image = False

    if message.content_type == "photo":
        photo_file_id = message.photo[-1].file_id if message.photo else ""
    elif message.content_type == "document":
        mime_type = (getattr(message.document, "mime_type", "") or "").lower()
        file_name = (getattr(message.document, "file_name", "") or "").lower()
        image_ext = file_name.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"))

        if not (mime_type.startswith("image/") or image_ext):
            return bot.send_message(message.chat.id, "📸 Отправь именно скриншот: фото или файл-картинку.")

        is_document_image = True
        photo_file_id = message.document.file_id
    else:
        return

    with SUBMIT_CREATE_LOCK:
        data = load_data()
        user = get_user(data, message.from_user.id)

        task_id = user.get("waiting_task")

        # Если waiting_task почему-то сбросился, берём последнее открытое задание.
        # Это исправляет баг, когда после нажатия «✅ Я выполнил» скрин приходит,
        # а бот пишет «Сначала выбери задание».
        if not task_id:
            last_task_id = str(user.get("last_open_task") or "")
            task_exists = last_task_id in data.get("tasks", {}) and data["tasks"].get(last_task_id, {}).get("active", True)
            already_done = last_task_id in user.get("done_tasks", [])
            already_pending = last_task_id in user.get("pending_tasks", []) or has_active_submit(data, message.from_user.id, last_task_id)
            if last_task_id and task_exists and not already_done and not already_pending:
                task_id = last_task_id
                user["waiting_task"] = task_id
            else:
                return bot.send_message(message.chat.id, "❌ Сначала выбери задание и нажми ✅ Я выполнил.")

        task_id = str(task_id)

        if task_id in user.get("done_tasks", []):
            user["waiting_task"] = None
            if str(user.get("last_open_task")) == str(task_id):
                user["last_open_task"] = None
            save_data(data)
            return bot.send_message(message.chat.id, "✅ Ты уже выполнил это задание.")

        old_sid = has_active_submit(data, message.from_user.id, task_id)
        same_photo_sid = find_active_submit_by_photo(data, message.from_user.id, task_id, photo_file_id)
        if task_id in user.get("pending_tasks", []) or old_sid or same_photo_sid:
            sid = old_sid or same_photo_sid
            if task_id not in user.get("pending_tasks", []):
                user.setdefault("pending_tasks", []).append(task_id)
            user["waiting_task"] = None
            save_data(data)
            return bot.send_message(
                message.chat.id,
                f"⏳ Это задание уже на проверке.\nНомер заявки: <b>#{sid or 'уже создана'}</b>"
            )

        task = data.get("tasks", {}).get(task_id)
        if not task:
            user["waiting_task"] = None
            if str(user.get("last_open_task")) == str(task_id):
                user["last_open_task"] = None
            save_data(data)
            return bot.send_message(message.chat.id, "❌ Задание не найдено.")

        data["last_submit_id"] = int(data.get("last_submit_id", 0)) + 1
        sid = str(data["last_submit_id"])

        user["waiting_task"] = None
        user["last_open_task"] = None
        user.setdefault("pending_tasks", []).append(task_id)

        data.setdefault("submits", {})[sid] = {
            "user_id": message.from_user.id,
            "username": message.from_user.username or "",
            "task_id": task_id,
            "reward": float(task.get("reward", 0)),
            "status": "wait",
            "photo_file_id": photo_file_id,
            "file_type": "document" if is_document_image else "photo",
            "time": int(time.time())
        }

        # СНАЧАЛА ставим метку, что заявка уже отправляется админу.
        # Это убирает баг: если админ сразу нажмёт /requests, бот НЕ продублирует эту же заявку.
        mark_admin_request_sent(data, "submits", sid, "sending")
        save_data(data)

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Одобрить", callback_data=f"yes_{sid}"))
    kb.add(types.InlineKeyboardButton("❌ Отказать", callback_data=f"no_{sid}"))
    kb.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"del_{sid}"))

    bot.send_message(message.chat.id, "✅ Скрин отправлен админу на проверку.\n⏳ Задание временно скрыто из списка.")

    try:
        caption = (
            f"📨 <b>Новая заявка #{sid}</b>\n\n"
            f"✅ Задание: #{task_id}\n"
            f"💰 Награда: {format_gmp(task.get('reward', 0))} GMP\n"
            f"👤 Пользователь: @{safe_username(message.from_user.username)}\n"
            f"🆔 ID: <code>{message.from_user.id}</code>"
        )

        if is_document_image:
            admin_msg = bot.send_document(
                ADMIN_ID,
                photo_file_id,
                caption=caption,
                reply_markup=kb
            )
        else:
            admin_msg = bot.send_photo(
                ADMIN_ID,
                photo_file_id,
                caption=caption,
                reply_markup=kb
            )

        with DATA_LOCK:
            data = load_data()
            mark_admin_request_sent(data, "submits", sid, getattr(admin_msg, "message_id", None))
            save_data(data)
    except Exception as e:
        print("send submit to admin error:", e)
        with DATA_LOCK:
            data = load_data()
            data.setdefault("admin_sent", {}).setdefault("submits", {}).pop(str(sid), None)
            submit = data.get("submits", {}).get(str(sid))
            if submit:
                submit.pop("admin_sent", None)
                submit.pop("admin_sent_time", None)
                submit.pop("admin_message_id", None)
            save_data(data)

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
        return True
    except Exception as e:
        print("edit admin message error:", e)
        try:
            bot.send_message(call.message.chat.id, text)
            return True
        except Exception as e2:
            print("send admin fallback error:", e2)
        return False
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception as e2:
            print("remove reply markup error:", e2)
        try:
            bot.send_message(call.message.chat.id, text)
        except Exception as e3:
            print("send admin status error:", e3)


def safe_close_old_buttons(call, callback_text="⚠️ Заявка уже закрыта"):
    """
    Для старых/повторных кнопок.
    НЕ отправляет новое сообщение в чат, чтобы не было спама:
    просто показывает маленькое уведомление и пытается убрать кнопки.
    """
    safe_answer_callback(call, callback_text, show_alert=False)
    try:
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None
        )
    except Exception as e:
        print("safe_close_old_buttons error:", e)


@bot.callback_query_handler(func=lambda c: c.data.startswith("del_"))
def delete_submit_request(call):
    if call.from_user.id != ADMIN_ID:
        return safe_answer_callback(call, "❌ Нет доступа.", show_alert=True)

    safe_answer_callback(call, "⏳ Удаляю заявку...")

    try:
        sid = call.data.split("_", 1)[1].strip().replace("#", "")
        user_id = None
        task_id = None
        real_sid = sid
        already_text = None

        with DATA_LOCK:
            data = load_data_for_admin_action()
            if request_already_processed(data, "submits", sid):
                already_text = f"⚠️ Заявка #{sid} уже обработана."
            else:
                real_sid, submit = find_request_by_id(data.get("submits", {}), sid)
                if not submit or submit.get("status") != "wait":
                    fb_sid, fb_submit = parse_admin_submit_message(call.message)
                    if fb_sid and str(fb_sid) == str(sid):
                        real_sid, submit = fb_sid, fb_submit
                    else:
                        already_text = f"⚠️ Заявка #{sid} уже обработана или не найдена."

                if not already_text:
                    real_sid = str(real_sid or sid)
                    submit["status"] = "processing"
                    user_id = str(submit.get("user_id"))
                    task_id = str(submit.get("task_id"))
                    user = get_user(data, user_id)
                    if task_id in user.get("pending_tasks", []):
                        user["pending_tasks"].remove(task_id)
                    if str(user.get("waiting_task")) == task_id:
                        user["waiting_task"] = None
                    remove_submit_from_data(data, real_sid, user_id, task_id)
                    if real_sid != sid:
                        remove_submit_from_data(data, sid, user_id, task_id)
                    mark_request_processed(data, "submits", real_sid, "deleted", call.from_user.id, user_id, f"task #{task_id}")
                    if real_sid != sid:
                        mark_request_processed(data, "submits", sid, "deleted", call.from_user.id, user_id, f"task #{task_id}")
                    save_data(data)

        if already_text:
            safe_answer_callback(call, "⚠️ Уже обработано.", show_alert=True)
            safe_close_old_buttons(call, "⚠️ Уже обработано")
            return

        sent_ok = safe_send(
            user_id,
            f"🗑 <b>Ваша заявка по заданию #{task_id} удалена администратором.</b>\n\n"
            "Можно отправить новый скриншот, если задание выполнено правильно."
        )
        notify_line = "📩 Пользователю отправлено уведомление." if sent_ok else "⚠️ Telegram не дал отправить уведомление пользователю."
        safe_edit_admin_message(
            call,
            f"🗑 <b>Заявка #{real_sid} удалена администратором.</b>\n\n"
            f"✅ Задание: #{task_id}\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"{notify_line}"
        )

    except Exception as e:
        print("delete submit callback error:", e)
        safe_edit_admin_message(call, "❌ Ошибка удаления заявки. Проверь логи Render.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("yes_") or c.data.startswith("no_"))
def check_request(call):
    if call.from_user.id != ADMIN_ID:
        return safe_answer_callback(call, "❌ Нет доступа.", show_alert=True)

    safe_answer_callback(call, "⏳ Обрабатываю заявку...")

    try:
        action, sid = call.data.split("_", 1)
        sid = str(sid).strip().replace("#", "")

        user_id = None
        task_id = None
        reward = 0
        credited_amount = 0
        new_balance = None
        result_status = None
        real_sid = sid
        already_text = None

        with DATA_LOCK:
            data = load_data_for_admin_action()
            processed = data.setdefault("processed_requests", {}).setdefault("submits", {}).get(str(sid))
            if processed:
                already_text = f"⚠️ <b>Заявка #{sid} уже обработана.</b>\n\nПовторное действие заблокировано."
            else:
                real_sid, submit = find_request_by_id(data.get("submits", {}), sid)

                # Запасной вариант: если requests.json лагнул/очистился, берём данные из сообщения с заявкой.
                if not submit or submit.get("status") != "wait":
                    fb_sid, fb_submit = parse_admin_submit_message(call.message)
                    if fb_sid and str(fb_sid) == str(sid):
                        real_sid, submit = fb_sid, fb_submit
                    else:
                        already_text = f"⚠️ <b>Заявка #{sid} уже обработана или не найдена.</b>\n\nПовторное действие заблокировано."

                if not already_text:
                    real_sid = str(real_sid or sid)
                    submit["status"] = "processing"
                    user_id = str(submit.get("user_id"))
                    task_id = str(submit.get("task_id"))
                    reward = float(submit.get("reward", 0) or 0)
                    user = get_user(data, user_id)

                    if task_id in user.get("pending_tasks", []):
                        user["pending_tasks"].remove(task_id)
                    if str(user.get("waiting_task")) == task_id:
                        user["waiting_task"] = None

                    if action == "yes":
                        if task_id not in user.get("done_tasks", []):
                            add_balance_to_user(data, user_id, reward, reason="task_approved", request_id=real_sid)
                            user["completed_tasks"] = int(user.get("completed_tasks", len(user.get("done_tasks", [])))) + 1
                            user["done_tasks"].append(task_id)
                            credited_amount = reward
                        else:
                            credited_amount = 0
                        new_balance = float(user.get("balance", 0))
                        result_status = "approved"
                    else:
                        result_status = "rejected"

                    remove_submit_from_data(data, real_sid, user_id, task_id)
                    if real_sid != sid:
                        remove_submit_from_data(data, sid, user_id, task_id)
                    mark_request_processed(data, "submits", real_sid, result_status, call.from_user.id, user_id, f"task #{task_id}")
                    if real_sid != sid:
                        mark_request_processed(data, "submits", sid, result_status, call.from_user.id, user_id, f"task #{task_id}")
                    save_data(data)

        if already_text:
            safe_answer_callback(call, "⚠️ Уже обработано.", show_alert=True)
            safe_close_old_buttons(call, "⚠️ Уже обработано")
            return

        if result_status == "approved":
            if credited_amount > 0:
                user_text = (
                    f"🎉 <b>Задание #{task_id} одобрено!</b>\n\n"
                    f"💰 Начислено: <b>{format_gmp(credited_amount)} GMP</b>\n"
                    f"💎 Баланс: <b>{format_gmp(new_balance)} GMP</b>"
                )
                admin_credit_text = f"💰 Начислено: {format_gmp(credited_amount)} GMP\n"
            else:
                user_text = (
                    f"✅ <b>Задание #{task_id} уже было засчитано ранее.</b>\n\n"
                    f"💎 Баланс: <b>{format_gmp(new_balance)} GMP</b>"
                )
                admin_credit_text = "⚠️ Повтор: GMP не начислялись второй раз\n"

            sent_ok = safe_send(user_id, user_text)
            notify_line = "📩 Пользователю отправлено уведомление." if sent_ok else "⚠️ Telegram не дал отправить уведомление пользователю."
            safe_edit_admin_message(
                call,
                f"✅ <b>Заявка #{real_sid} одобрена.</b>\n\n"
                f"✅ Задание: #{task_id}\n"
                f"{admin_credit_text}"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"{notify_line}"
            )
            return

        if result_status == "rejected":
            sent_ok = safe_send(
                user_id,
                f"❌ <b>Задание #{task_id} отклонено</b>\n\n"
                "Проверьте, что вы выполнили задание до конца и отправили правильный скриншот.\n\n"
                "После исправления можно попробовать снова ✅"
            )
            notify_line = "📩 Пользователю отправлено уведомление." if sent_ok else "⚠️ Telegram не дал отправить уведомление пользователю."
            safe_edit_admin_message(
                call,
                f"❌ <b>Заявка #{real_sid} отклонена.</b>\n\n"
                f"✅ Задание: #{task_id}\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"{notify_line}"
            )
            return

    except Exception as e:
        print("task approve/reject callback error:", e)
        safe_edit_admin_message(call, "❌ Ошибка обработки заявки. Проверь логи Render.")


@bot.message_handler(func=lambda m: m.text == "💸 Вывод")
def withdraw(message):
    if is_banned_user(message):
        return

    data = load_data()
    user = get_user(data, message.from_user.id)

    block = get_active_withdraw_block(data, message.from_user.id)
    if block:
        user["withdraw_step"] = None
        user["withdraw_to"] = None
        save_data(data)
        return bot.send_message(message.chat.id, withdraw_block_text(block))
    save_data(data)

    if not data.get("withdraw_enabled", True):
        user["withdraw_step"] = None
        user["withdraw_to"] = None
        save_data(data)
        return bot.send_message(
            message.chat.id,
            "⚠️ <b>Выплаты временно недоступны.</b>\n\n"
            "🔄 Пожалуйста, попробуйте позже.\n"
            "📢 О возобновлении выплат будет сообщено в боте."
        )

    # ВАЖНО: несколько выводов разрешены.
    # Если у пользователя уже есть заявка на вывод, мы НЕ блокируем новый вывод.
    # Защита от случайных дублей стоит ниже при создании заявки по сумме.

    if float(user["balance"]) < 0:
        return bot.send_message(
            message.chat.id,
            f"❌ Вывод недоступен.\n\nУ тебя долг: <b>{format_gmp(abs(float(user['balance'])))} GMP</b>\nСначала погаси долг заданиями."
        )

    if float(user["balance"]) <= 0:
        return bot.send_message(message.chat.id, "❌ У тебя нет GMP для вывода.")

    missing_required = get_required_missing_tasks(data, user)
    if missing_required:
        user["withdraw_step"] = None
        user["withdraw_to"] = None
        save_data(data)
        return bot.send_message(message.chat.id, required_tasks_block_text(missing_required))


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




def parse_admin_submit_message(message):
    """Берёт данные заявки на задание прямо из сообщения админа.
    Нужен как запасной вариант, если requests.json/GitHub лагнул или уже почистился.
    """
    text = (getattr(message, "caption", None) or getattr(message, "text", None) or "")
    sid_m = re.search(r"(?:Новая заявка|Заявка на задание)\s*#(\d+)", text, re.IGNORECASE)
    task_m = re.search(r"Задание:\s*#?(\d+)", text, re.IGNORECASE)
    reward_m = re.search(r"(?:Награда|Начислено):\s*([0-9]+(?:[\.,][0-9]+)?)", text, re.IGNORECASE)
    user_m = re.search(r"ID:\s*<?code>?\s*(\d+)", text, re.IGNORECASE)
    username_m = re.search(r"Пользователь:\s*@?([A-Za-z0-9_]+)", text, re.IGNORECASE)
    if not (sid_m and task_m and user_m):
        return None, None
    reward = 0
    if reward_m:
        try:
            reward = float(reward_m.group(1).replace(',', '.'))
        except Exception:
            reward = 0
    sid = sid_m.group(1)
    submit = {
        "status": "wait",
        "task_id": task_m.group(1),
        "reward": reward,
        "user_id": user_m.group(1),
        "username": username_m.group(1) if username_m else "",
        "time": int(time.time()),
        "fallback_from_admin_message": True,
    }
    return sid, submit


def parse_admin_withdraw_message(message):
    """Берёт данные заявки на вывод прямо из сообщения админа.
    Это спасает, если requests.json не успел сохраниться, но заявка в чате есть.
    """
    text = (getattr(message, "caption", None) or getattr(message, "text", None) or "")
    wid_m = re.search(r"(?:Новая заявка на вывод|Заявка на вывод)\s*#(\d+)", text, re.IGNORECASE)
    user_m = re.search(r"ID:\s*<?code>?\s*(\d+)", text, re.IGNORECASE)
    amount_m = re.search(r"Сумма:\s*([0-9]+(?:[\.,][0-9]+)?)", text, re.IGNORECASE)
    to_m = re.search(r"Куда вывести:\s*([^\n]+)", text, re.IGNORECASE)
    username_m = re.search(r"Пользователь:\s*@?([A-Za-z0-9_]+)", text, re.IGNORECASE)
    if not (wid_m and user_m and amount_m):
        return None, None
    try:
        amount = float(amount_m.group(1).replace(',', '.'))
    except Exception:
        amount = 0
    wid = wid_m.group(1)
    withdraw = {
        "status": "wait",
        "user_id": user_m.group(1),
        "username": username_m.group(1) if username_m else "",
        "to": (to_m.group(1).strip() if to_m else "не указано"),
        "amount": amount,
        "time": int(time.time()),
        "fallback_from_admin_message": True,
    }
    return wid, withdraw


def status_time_text():
    try:
        return datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return datetime.now().strftime("%d.%m.%Y %H:%M")


@bot.callback_query_handler(func=lambda c: c.data.startswith("payyes_") or c.data.startswith("payno_") or c.data.startswith("paydel_"))
def pay_check(call):
    if call.from_user.id != ADMIN_ID:
        return safe_answer_callback(call, "❌ Нет доступа.", show_alert=True)

    safe_answer_callback(call, "⏳ Обрабатываю выплату...")

    try:
        action, wid = call.data.split("_", 1)
        wid = str(wid).strip().replace("#", "")

        user_id = None
        amount = 0
        result_status = None
        real_wid = wid
        already_text = None

        with DATA_LOCK:
            data = load_data_for_admin_action()
            processed = data.setdefault("processed_requests", {}).setdefault("withdraws", {}).get(str(wid))
            if processed:
                already_text = f"⚠️ <b>Заявка на вывод #{wid} уже обработана.</b>\n\nПовторное действие заблокировано."
            else:
                real_wid, w = find_request_by_id(data.get("withdraws", {}), wid)

                # Если в requests.json заявки уже нет, но сообщение админа есть — берём данные из сообщения.
                # Так кнопка всё равно сработает, пользователь получит смс, статистика обновится.
                if not w or w.get("status") != "wait":
                    fb_wid, fb_w = parse_admin_withdraw_message(call.message)
                    if fb_wid and str(fb_wid) == str(wid):
                        real_wid, w = fb_wid, fb_w
                    else:
                        already_text = f"⚠️ <b>Заявка на вывод #{wid} уже закрыта или не найдена.</b>\n\nПовторное действие заблокировано."

                if not already_text:
                    real_wid = str(real_wid or wid)
                    w["status"] = "processing"
                    user_id = str(w.get("user_id"))
                    amount = float(w.get("amount", 0) or 0)
                    user = get_user(data, user_id)

                    if action == "payyes":
                        user["withdraw_count"] = int(user.get("withdraw_count", 0)) + 1
                        user["withdrawn_total"] = round(float(user.get("withdrawn_total", 0)) + amount, 2)
                        data["total_paid"] = round(float(data.get("total_paid", 0)) + amount, 2)
                        data["total_withdrawals"] = int(data.get("total_withdrawals", 0)) + 1
                        sync_withdraw_stats(data)
                        result_status = "paid"
                    elif action == "payno":
                        add_balance_to_user(data, user_id, amount, reason="withdraw_rejected_return", request_id=real_wid)
                        result_status = "rejected"
                    else:
                        add_balance_to_user(data, user_id, amount, reason="withdraw_deleted_return", request_id=real_wid)
                        result_status = "deleted"

                    remove_withdraw_from_data(data, real_wid, user_id)
                    if real_wid != wid:
                        remove_withdraw_from_data(data, wid, user_id)
                    mark_request_processed(data, "withdraws", real_wid, result_status, call.from_user.id, user_id, f"amount {amount}")
                    if real_wid != wid:
                        mark_request_processed(data, "withdraws", wid, result_status, call.from_user.id, user_id, f"amount {amount}")
                    save_data(data)

        if already_text:
            safe_answer_callback(call, "⚠️ Уже обработано.", show_alert=True)
            safe_close_old_buttons(call, "⚠️ Уже обработано")
            return

        if result_status == "paid":
            sent_ok = safe_send(
                user_id,
                f"✅ <b>Заявка на вывод #{real_wid} одобрена!</b>\n\n"
                f"💎 Выплачено: <b>{format_gmp(amount)} GMP</b>\n\n"
                "Спасибо за использование GMP от Artemwe 💜"
            )
            notify_line = "📩 Пользователю отправлено уведомление." if sent_ok else "⚠️ Telegram не дал отправить уведомление пользователю."
            safe_edit_admin_message(
                call,
                f"✅ <b>Выплата #{real_wid} подтверждена.</b>\n\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"💰 Сумма: <b>{format_gmp(amount)} GMP</b>\n"
                f"🕒 Обработано: <b>{status_time_text()}</b>\n"
                f"{notify_line}\n"
                f"📊 Статистика выплат обновлена."
            )
            return

        if result_status == "rejected":
            sent_ok = safe_send(
                user_id,
                f"❌ <b>Заявка на вывод #{real_wid} отклонена.</b>\n\n"
                f"💰 <b>{format_gmp(amount)} GMP</b> возвращены на баланс."
            )
            notify_line = "📩 Пользователю отправлено уведомление." if sent_ok else "⚠️ Telegram не дал отправить уведомление пользователю."
            safe_edit_admin_message(
                call,
                f"❌ <b>Выплата #{real_wid} отклонена.</b>\n\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"💰 Возвращено: <b>{format_gmp(amount)} GMP</b>\n"
                f"🕒 Обработано: <b>{status_time_text()}</b>\n"
                f"{notify_line}"
            )
            return

        if result_status == "deleted":
            sent_ok = safe_send(
                user_id,
                f"🗑 <b>Заявка на вывод #{real_wid} удалена администратором.</b>\n\n"
                f"💰 <b>{format_gmp(amount)} GMP</b> возвращены на баланс."
            )
            notify_line = "📩 Пользователю отправлено уведомление." if sent_ok else "⚠️ Telegram не дал отправить уведомление пользователю."
            safe_edit_admin_message(
                call,
                f"🗑 <b>Выплата #{real_wid} удалена.</b>\n\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"💰 Возвращено: <b>{format_gmp(amount)} GMP</b>\n"
                f"🕒 Обработано: <b>{status_time_text()}</b>\n"
                f"{notify_line}"
            )
            return

    except Exception as e:
        print("withdraw callback error:", e)
        safe_edit_admin_message(call, "❌ Ошибка обработки выплаты. Проверь логи Render.")



def get_request_from_admin_reply(message):
    """
    Берёт номер заявки именно из сообщения, на которое админ ответил.
    Так не важно, если 2 человека отправили одно и то же задание:
    /ok в ответ на нужную заявку обработает именно её.
    """
    if not getattr(message, "reply_to_message", None):
        return None, None

    replied = message.reply_to_message
    text = (getattr(replied, "caption", None) or getattr(replied, "text", None) or "")

    # Заявка на задание/фото
    m = re.search(r"(?:Новая заявка|Заявка на задание)\s*#(\d+)", text, re.IGNORECASE)
    if m:
        return "submits", m.group(1)

    # Заявка на вывод
    m = re.search(r"(?:Новая заявка на вывод|Заявка на вывод)\s*#(\d+)", text, re.IGNORECASE)
    if m:
        return "withdraws", m.group(1)

    return None, None


def remove_replied_buttons(message):
    """Пробуем убрать кнопки под заявкой, на которую ответил админ."""
    try:
        if getattr(message, "reply_to_message", None):
            bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=message.reply_to_message.message_id,
                reply_markup=None
            )
    except Exception as e:
        print("remove replied buttons error:", e)


def admin_reply_submit_action(message, sid, action):
    """Обработка заявки на задание через ответ командой /ok, /no, /delreq."""
    sid = str(sid).strip().replace("#", "")
    reason = message.text.split(maxsplit=1)[1].strip() if action == "reject" and len(message.text.split(maxsplit=1)) > 1 else ""

    with DATA_LOCK:
        data = load_data_for_admin_action()

        processed = data.setdefault("processed_requests", {}).setdefault("submits", {}).get(sid)
        if processed:
            return bot.reply_to(message, f"⚠️ Заявка #{sid} уже обработана. Повторное действие заблокировано.")

        real_sid, submit = find_request_by_id(data.get("submits", {}), sid)
        if not submit or submit.get("status") != "wait":
            return bot.reply_to(message, f"⚠️ Заявка #{sid} не найдена или уже закрыта.")

        real_sid = str(real_sid or sid)
        submit["status"] = "processing"
        user_id = str(submit.get("user_id"))
        task_id = str(submit.get("task_id"))
        reward = float(submit.get("reward", 0) or 0)
        user = get_user(data, user_id)

        if task_id in user.get("pending_tasks", []):
            user["pending_tasks"].remove(task_id)
        if str(user.get("waiting_task")) == task_id:
            user["waiting_task"] = None

        credited = 0
        new_balance = float(user.get("balance", 0) or 0)

        if action == "approve":
            if task_id not in user.get("done_tasks", []):
                add_balance_to_user(data, user_id, reward, reason="task_approved", request_id=real_sid)
                user["completed_tasks"] = int(user.get("completed_tasks", len(user.get("done_tasks", [])))) + 1
                user["done_tasks"].append(task_id)
                credited = reward
            new_balance = float(user.get("balance", 0) or 0)
            status = "approved"
        elif action == "reject":
            status = "rejected"
        else:
            status = "deleted"

        remove_submit_from_data(data, real_sid, user_id, task_id)
        if real_sid != sid:
            remove_submit_from_data(data, sid, user_id, task_id)
        mark_request_processed(data, "submits", real_sid, status, message.from_user.id, user_id, f"task #{task_id}")
        if real_sid != sid:
            mark_request_processed(data, "submits", sid, status, message.from_user.id, user_id, f"task #{task_id}")
        save_data(data)

    remove_replied_buttons(message)

    if action == "approve":
        if credited > 0:
            safe_send(user_id, f"🎉 <b>Задание #{task_id} одобрено!</b>\n\n💰 Начислено: <b>{format_gmp(credited)} GMP</b>\n💎 Баланс: <b>{format_gmp(new_balance)} GMP</b>")
        else:
            safe_send(user_id, f"✅ <b>Задание #{task_id} уже было засчитано ранее.</b>\n\n💎 Баланс: <b>{format_gmp(new_balance)} GMP</b>")
        return bot.reply_to(message, f"✅ Заявка #{real_sid} одобрена. Начислено: {format_gmp(credited)} GMP")

    if action == "reject":
        extra = f"\n\nПричина: {h(reason)}" if reason else ""
        safe_send(user_id, f"❌ <b>Задание #{task_id} отклонено</b>{extra}\n\nМожно попробовать снова ✅")
        return bot.reply_to(message, f"❌ Заявка #{real_sid} отклонена.")

    safe_send(user_id, f"🗑 <b>Ваша заявка по заданию #{task_id} удалена администратором.</b>\n\nМожно отправить новый скриншот, если задание выполнено правильно.")
    return bot.reply_to(message, f"🗑 Заявка #{real_sid} удалена.")


def admin_reply_withdraw_action(message, wid, action):
    """Обработка вывода через ответ командой /ok, /no, /delreq."""
    wid = str(wid).strip().replace("#", "")

    with DATA_LOCK:
        data = load_data_for_admin_action()

        processed = data.setdefault("processed_requests", {}).setdefault("withdraws", {}).get(wid)
        if processed:
            return bot.reply_to(message, f"⚠️ Вывод #{wid} уже обработан. Повторное действие заблокировано.")

        real_wid, w = find_request_by_id(data.get("withdraws", {}), wid)
        if not w or w.get("status") != "wait":
            return bot.reply_to(message, f"⚠️ Вывод #{wid} не найден или уже закрыт.")

        real_wid = str(real_wid or wid)
        w["status"] = "processing"
        user_id = str(w.get("user_id"))
        amount = float(w.get("amount", 0) or 0)
        user = get_user(data, user_id)

        if action == "approve":
            user["withdraw_count"] = int(user.get("withdraw_count", 0)) + 1
            user["withdrawn_total"] = round(float(user.get("withdrawn_total", 0)) + amount, 2)
            data["total_paid"] = round(float(data.get("total_paid", 0)) + amount, 2)
            data["total_withdrawals"] = int(data.get("total_withdrawals", 0)) + 1
            sync_withdraw_stats(data)
            status = "paid"
        elif action == "reject":
            add_balance_to_user(data, user_id, amount, reason="withdraw_rejected_return", request_id=real_wid)
            status = "rejected"
        else:
            add_balance_to_user(data, user_id, amount, reason="withdraw_deleted_return", request_id=real_wid)
            status = "deleted"

        remove_withdraw_from_data(data, real_wid, user_id)
        if real_wid != wid:
            remove_withdraw_from_data(data, wid, user_id)
        mark_request_processed(data, "withdraws", real_wid, status, message.from_user.id, user_id, f"amount {amount}")
        if real_wid != wid:
            mark_request_processed(data, "withdraws", wid, status, message.from_user.id, user_id, f"amount {amount}")
        save_data(data)

    remove_replied_buttons(message)

    if action == "approve":
        sent_ok = safe_send(user_id, f"✅ <b>Заказ #{real_wid} выполнен!</b>\n\n💎 GMP успешно выданы 💜")
        notify_line = "Сообщение пользователю отправлено." if sent_ok else "Сообщение пользователю не отправилось, но вывод закрыт."
        return bot.reply_to(message, f"✅ Вывод #{real_wid} отмечен как выплаченный. {notify_line}")

    if action == "reject":
        sent_ok = safe_send(user_id, f"❌ <b>Заявка на вывод #{real_wid} отклонена.</b>\n\n💰 <b>{format_gmp(amount)} GMP</b> возвращены на баланс.")
        notify_line = "Сообщение пользователю отправлено." if sent_ok else "Сообщение пользователю не отправилось, но вывод закрыт."
        return bot.reply_to(message, f"❌ Вывод #{real_wid} отклонён, GMP возвращены. {notify_line}")

    sent_ok = safe_send(user_id, f"🗑 <b>Заявка на вывод #{real_wid} удалена.</b>\n\n💰 <b>{format_gmp(amount)} GMP</b> возвращены на баланс.")
    notify_line = "Сообщение пользователю отправлено." if sent_ok else "Сообщение пользователю не отправилось, но вывод закрыт."
    return bot.reply_to(message, f"🗑 Вывод #{real_wid} удалён, GMP возвращены. {notify_line}")


@bot.message_handler(commands=["ok", "no", "delreq"])
def admin_reply_request_commands(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    cmd = message.text.split()[0].lower().replace("/", "").split("@", 1)[0]
    kind, request_id = get_request_from_admin_reply(message)

    if not kind or not request_id:
        return bot.reply_to(
            message,
            "❌ Нужно ответить командой на сообщение заявки.\n\n"
            "Пример:\n"
            "1) Открой заявку админа\n"
            "2) Нажми Ответить\n"
            "3) Напиши /ok или /no или /delreq"
        )

    action = "approve" if cmd == "ok" else ("reject" if cmd == "no" else "delete")

    if kind == "submits":
        return admin_reply_submit_action(message, request_id, action)
    return admin_reply_withdraw_action(message, request_id, action)


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
        "active": True,
        "required": False
    }

    save_data(data)

    bot.send_message(
        message.chat.id,
        f"✅ <b>Задание #{task_id} создано</b>\n\n"
        f"🔗 Ссылка:\n{link}\n\n"
        f"{task_text}\n\n"
        f"💰 Награда: {format_gmp(reward)} GMP"
    )


@bot.message_handler(commands=["addrequired", "addreq"])
def add_required_task(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return bot.send_message(message.chat.id, "❌ Формат:\n/addrequired Текст | ссылка | награда")

    parts = [p.strip() for p in text[1].split("|")]
    if len(parts) != 3:
        return bot.send_message(message.chat.id, "❌ Формат:\n/addrequired Текст | ссылка | награда")

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

    with DATA_LOCK:
        data = load_data()
        data["last_task_id"] += 1
        task_id = str(data["last_task_id"])

        data.setdefault("tasks", {})[task_id] = {
            "text": task_text,
            "link": link,
            "reward": reward,
            "active": True,
            "required": True
        }
        data.setdefault("required_tasks", [])
        if task_id not in data["required_tasks"]:
            data["required_tasks"].append(task_id)
        save_data(data)

    bot.send_message(
        message.chat.id,
        f"🔥 <b>Обязательное задание #{task_id} создано</b>\n\n"
        f"Теперь пользователи не смогут вывести GMP, пока не выполнят это задание.\n\n"
        f"🔗 Ссылка:\n{link}\n\n"
        f"{task_text}\n\n"
        f"💰 Награда: {format_gmp(reward)} GMP"
    )


@bot.message_handler(commands=["required", "reqtask"])
def make_task_required(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "❌ Формат:\n/required номер_задания")

    task_id = parts[1].strip().replace("#", "")
    with DATA_LOCK:
        data = load_data()
        task = data.get("tasks", {}).get(task_id)
        if not task:
            return bot.send_message(message.chat.id, "❌ Задание не найдено.")

        task["required"] = True
        task["active"] = True
        data.setdefault("required_tasks", [])
        if task_id not in data["required_tasks"]:
            data["required_tasks"].append(task_id)
        save_data(data)

    bot.send_message(message.chat.id, f"🔥 Задание #{task_id} теперь обязательное для вывода GMP.")


@bot.message_handler(commands=["unrequired", "unreqtask"])
def remove_task_required(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "❌ Формат:\n/unrequired номер_задания")

    task_id = parts[1].strip().replace("#", "")
    with DATA_LOCK:
        data = load_data()
        if task_id in data.get("tasks", {}):
            data["tasks"][task_id]["required"] = False
        data["required_tasks"] = [str(tid) for tid in data.get("required_tasks", []) if str(tid) != task_id]
        save_data(data)

    bot.send_message(message.chat.id, f"✅ Задание #{task_id} больше не обязательное. Вывод не будет блокироваться этим заданием.")


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

    # Удаляем задание и ставим "память удаления".
    # Поэтому старый data.json с GitHub/Render уже не сможет вернуть это задание назад.
    old_task = data["tasks"].pop(task_id, {})
    data.setdefault("deleted_tasks", {})[str(task_id)] = {
        "time": int(time.time()),
        "by": int(message.from_user.id),
        "text": str(old_task.get("text", ""))[:200]
    }
    data["required_tasks"] = [str(tid) for tid in data.get("required_tasks", []) if str(tid) != str(task_id)]

    removed_submits = 0
    for sid, submit in list(data.get("submits", {}).items()):
        if str(submit.get("task_id")) == str(task_id):
            uid = str(submit.get("user_id", ""))
            remove_submit_from_data(data, sid, uid, task_id)
            mark_request_processed(data, "submits", sid, "deleted_task", message.from_user.id, uid, f"task #{task_id}")
            removed_submits += 1

    for u in data.get("users", {}).values():
        u["done_tasks"] = [str(tid) for tid in u.get("done_tasks", []) if str(tid) != str(task_id)]
        u["pending_tasks"] = [str(tid) for tid in u.get("pending_tasks", []) if str(tid) != str(task_id)]
        if str(u.get("waiting_task")) == str(task_id):
            u["waiting_task"] = None

    save_data(data)
    extra = f"\n🧹 Очищено заявок по нему: {removed_submits}" if removed_submits else ""
    bot.send_message(message.chat.id, f"✅ Задание #{task_id} удалено. Оно больше не блокирует вывод.{extra}")


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

        user, old_balance, new_balance = add_balance_to_user(data, user_id, amount, reason="admin_give", request_id="manual")
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

    with DATA_LOCK:
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

        user, old_balance, new_balance = add_balance_to_user(data, user_id, -amount, reason="admin_take", request_id="manual")
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

    with DATA_LOCK:
        data = load_data()
        user, old_balance, new_balance = add_balance_to_user(data, user_id, -amount, reason=f"admin_fine: {reason}", request_id="manual")
        user["fines_total"] = round(float(user.get("fines_total", 0)) + amount, 2)
        save_data(data)

    bot.send_message(
        message.chat.id,
        f"✅ Штраф выдан\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💸 Штраф: <b>{format_gmp(amount)} GMP</b>\n"
        f"💰 Баланс: <b>{format_gmp(old_balance)} → {format_gmp(user['balance'])} GMP</b>\n"
        f"📌 Причина: {h(reason)}"
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
        status = "🔥" if task.get("required") else ("✅" if task.get("active", True) else "❌")
        text += f"{status} #{task_id} — {format_gmp(task['reward'])} GMP\n{task['text']}\n\n"

    bot.send_message(message.chat.id, text)


def send_withdraw_request(chat_id, wid, w):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Выплачено", callback_data=f"payyes_{wid}"),
        types.InlineKeyboardButton("❌ Отказать", callback_data=f"payno_{wid}")
    )
    kb.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"paydel_{wid}"))
    return bot.send_message(
        chat_id,
        f"💸 <b>Заявка на вывод #{wid}</b>\n\n"
        f"👤 Пользователь: @{safe_username(w.get('username'))}\n"
        f"🆔 ID: <code>{w.get('user_id')}</code>\n"
        f"📤 Куда вывести: <b>{h(w.get('to', 'не указано'))}</b>\n"
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
        f"👤 Пользователь: @{safe_username(submit.get('username'))}\n"
        f"🆔 ID: <code>{submit.get('user_id')}</code>"
    )
    photo_id = submit.get("photo_file_id")
    if photo_id:
        return bot.send_photo(chat_id, photo_id, caption=caption, reply_markup=kb)
    return bot.send_message(chat_id, caption + "\n\n⚠️ Фото не сохранено в старой заявке.", reply_markup=kb)


@bot.message_handler(commands=["cleanrequests", "fixrequests"])
def clean_requests_command(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    with DATA_LOCK:
        data = load_data_for_admin_action()
        before_s = sum(1 for s in data.get("submits", {}).values() if s.get("status") == "wait")
        before_w = sum(1 for w in data.get("withdraws", {}).values() if w.get("status") == "wait")
        cleanup_closed_requests(data)
        save_data(data)
        after_s = sum(1 for s in data.get("submits", {}).values() if s.get("status") == "wait")
        after_w = sum(1 for w in data.get("withdraws", {}).values() if w.get("status") == "wait")

    bot.send_message(
        message.chat.id,
        "🧹 <b>Проверка заявок выполнена</b>\n\n"
        f"📸 Задания: <b>{before_s} → {after_s}</b>\n"
        f"💸 Выводы: <b>{before_w} → {after_w}</b>\n\n"
        "Если какие-то старые заявки всё равно остались активными, используй /clearrequests — он удалит ВСЕ активные заявки и вернёт GMP за выводы."
    )


@bot.message_handler(commands=["clearrequests"])
def clear_requests_command(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    with DATA_LOCK:
        data = load_data_for_admin_action()
        removed_s = 0
        removed_w = 0
        returned = 0.0

        for sid, submit in list(data.get("submits", {}).items()):
            if submit.get("status") == "wait":
                uid = str(submit.get("user_id"))
                task_id = str(submit.get("task_id"))
                user = get_user(data, uid)
                if task_id in user.get("pending_tasks", []):
                    user["pending_tasks"].remove(task_id)
                if str(user.get("waiting_task")) == task_id:
                    user["waiting_task"] = None
                remove_submit_from_data(data, sid, uid, task_id)
                mark_request_processed(data, "submits", sid, "deleted", message.from_user.id, uid, "clearrequests")
                removed_s += 1

        for wid, w in list(data.get("withdraws", {}).items()):
            if w.get("status") == "wait":
                uid = str(w.get("user_id"))
                amount = float(w.get("amount", 0) or 0)
                if amount > 0:
                    add_balance_to_user(data, uid, amount, reason="withdraw_clear_return", request_id=wid)
                    returned += amount
                data["withdraws"].pop(wid, None)
                user = get_user(data, uid)
                user["withdraw_pending"] = any(
                    str(x.get("user_id")) == uid and x.get("status") == "wait"
                    for x in data.get("withdraws", {}).values()
                )
                mark_request_processed(data, "withdraws", wid, "deleted", message.from_user.id, uid, "clearrequests")
                removed_w += 1

        save_data(data)

    bot.send_message(
        message.chat.id,
        "✅ <b>Все активные заявки очищены</b>\n\n"
        f"📸 Удалено заявок заданий: <b>{removed_s}</b>\n"
        f"💸 Удалено выводов: <b>{removed_w}</b>\n"
        f"💰 Возвращено за выводы: <b>{format_gmp(returned)} GMP</b>"
    )


@bot.message_handler(func=lambda m: m.text == "📨 Заявки")
@bot.message_handler(commands=["requests", "zayavki"])
def requests_msg(message):
    if message.from_user.id != ADMIN_ID:
        return

    # /requests теперь работает как архив активных заявок:
    # даже если ты удалил сообщение админа, команда снова отправит все активные заявки с кнопками.
    with DATA_LOCK:
        data = load_data_for_admin_action()
        wait_submits = {str(sid): dict(s) for sid, s in data.get("submits", {}).items() if s.get("status") == "wait"}
        wait_withdraws = {str(wid): dict(w) for wid, w in data.get("withdraws", {}).items() if w.get("status") == "wait"}
        save_data(data)

    sent_submit = 0
    sent_withdraw = 0

    for sid, submit in wait_submits.items():
        try:
            msg = send_submit_request(message.chat.id, sid, submit)
            sent_submit += 1
            with DATA_LOCK:
                data = load_data_for_admin_action()
                mark_admin_request_sent(data, "submits", sid, getattr(msg, "message_id", None))
                save_data(data)
        except Exception as e:
            print("/requests send submit error:", e)

    for wid, w in wait_withdraws.items():
        try:
            msg = send_withdraw_request(message.chat.id, wid, w)
            sent_withdraw += 1
            with DATA_LOCK:
                data = load_data_for_admin_action()
                mark_admin_request_sent(data, "withdraws", wid, getattr(msg, "message_id", None))
                save_data(data)
        except Exception as e:
            print("/requests send withdraw error:", e)

    if sent_submit or sent_withdraw:
        text = (
            "📨 <b>Активные заявки:</b>\n\n"
            f"📸 Задания: <b>{len(wait_submits)}</b>\n"
            f"💸 Выводы: <b>{len(wait_withdraws)}</b>\n\n"
            f"✅ Снова отправил с кнопками: <b>{sent_submit + sent_withdraw}</b>"
        )
    else:
        text = "✅ Активных заявок нет."

    bot.send_message(message.chat.id, text)


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




@bot.message_handler(commands=["ban"])
def ban_user(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 4:
        return bot.send_message(
            message.chat.id,
            "❌ Формат:\n"
            "<code>/ban ID 1d причина</code>\n"
            "<code>/ban @username 2h причина</code>\n\n"
            "Пример:\n"
            "<code>/ban 5241714648 1d Не ознакомились с условиями задания</code>\n\n"
            "Сроки: <b>30m</b>, <b>2h</b>, <b>1d</b>, <b>7d</b>."
        )

    target = parts[1].strip()
    until_raw = parts[2].strip()
    reason = parts[3].strip() or "не указана"
    until_ts = parse_account_ban_until(until_raw)

    if not until_ts:
        return bot.send_message(message.chat.id, "❌ Не понял срок. Пример: <code>1d</code>, <code>2h</code>, <code>30m</code>.")
    if until_ts <= int(time.time()):
        return bot.send_message(message.chat.id, "❌ Это время уже прошло. Укажи будущий срок.")

    with DATA_LOCK:
        data = load_fresh_data_for_ban_check()
        user_id, err = resolve_user_id(data, target)
        if err:
            return bot.send_message(message.chat.id, f"❌ {err}")

        user = get_user(data, user_id)
        user["banned"] = True
        user["ban_until"] = int(until_ts)
        user["ban_reason"] = reason
        user["ban_created"] = int(time.time())
        user["withdraw_step"] = None
        user["withdraw_to"] = None
        save_data(data)

    until_text = format_block_time(until_ts)
    bot.send_message(
        message.chat.id,
        f"✅ <b>Пользователь заблокирован</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"⏳ До: <b>{until_text}</b>\n"
        f"📌 Причина: {h(reason)}"
    )

    safe_send(
        user_id,
        f"⛔ <b>Ваш аккаунт заблокирован до {until_text}</b>\n\n"
        f"📌 Причина: {h(reason)}\n\n"
        "После окончания срока блокировка снимется автоматически."
    )


@bot.message_handler(commands=["blockwithdraw", "wblock"])
def block_withdraw_user(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = (message.text or "").split()
    if len(parts) < 3:
        return bot.send_message(
            message.chat.id,
            "❌ Формат:\n"
            "<code>/blockwithdraw ID 2h причина</code>\n"
            "<code>/blockwithdraw @username 6d причина</code>\n"
            "<code>/blockwithdraw ID 01.07.2026 17:12 причина</code>\n\n"
            "Можно писать: <b>30m</b>, <b>2h</b>, <b>6d</b>, <b>2ч</b>, <b>6д</b>."
        )

    target = parts[1].strip()

    # Если срок задан датой и временем — забираем 2 слова: 01.07.2026 17:12
    if len(parts) >= 4 and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", parts[2]) and re.fullmatch(r"\d{1,2}:\d{2}", parts[3]):
        until_raw = parts[2] + " " + parts[3]
        reason = " ".join(parts[4:]).strip() or "не указана"
    else:
        until_raw = parts[2]
        reason = " ".join(parts[3:]).strip() or "не указана"

    until_ts = parse_withdraw_block_until(until_raw)
    if not until_ts:
        return bot.send_message(message.chat.id, "❌ Не понял срок. Пример: <code>2h</code>, <code>6d</code> или <code>01.07.2026 17:12</code>")

    if until_ts <= int(time.time()):
        return bot.send_message(message.chat.id, "❌ Это время уже прошло. Укажи будущую дату/срок.")

    with DATA_LOCK:
        data = load_fresh_data_for_ban_check()
        user_id, err = resolve_user_id(data, target)
        if err:
            return bot.send_message(message.chat.id, f"❌ {err}")

        user = get_user(data, user_id)
        username = user.get("username", "")
        user["withdraw_step"] = None
        user["withdraw_to"] = None

        data.setdefault("withdraw_blocks", {})[str(user_id)] = {
            "until": int(until_ts),
            "reason": reason,
            "created": int(time.time()),
            "by": int(message.from_user.id),
            "username": username
        }
        save_data(data)

    until_text = format_block_time(until_ts)
    bot.send_message(
        message.chat.id,
        f"✅ <b>Вывод заблокирован</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"👤 Username: @{safe_username(username)}\n"
        f"⏰ До: <b>{until_text}</b>\n"
        f"📌 Причина: {h(reason)}"
    )

    safe_send(
        user_id,
        f"🚫 <b>Ваш вывод заблокирован до {until_text}</b>\n\n"
        f"📌 <b>Причина:</b> {h(reason)}\n\n"
        "Ботом можно пользоваться дальше, но вывод временно недоступен."
    )


@bot.message_handler(commands=["unblockwithdraw", "wunblock"])
def unblock_withdraw_user(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "❌ Формат:\n<code>/unblockwithdraw ID</code> или <code>/unblockwithdraw @username</code>")

    with DATA_LOCK:
        data = load_fresh_data_for_ban_check()
        user_id, err = resolve_user_id(data, parts[1].strip())
        if err:
            return bot.send_message(message.chat.id, f"❌ {err}")

        block = data.setdefault("withdraw_blocks", {}).pop(str(user_id), None)
        user = get_user(data, user_id)
        user["withdraw_step"] = None
        user["withdraw_to"] = None
        save_data(data)

    bot.send_message(
        message.chat.id,
        f"✅ <b>Блокировка вывода снята</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📌 Блокировка была: {'да' if block else 'нет'}"
    )
    safe_send(user_id, "✅ <b>Блокировка вывода снята</b>\n\nТеперь вы снова можете создавать заявки на вывод.")


@bot.message_handler(commands=["withdrawblocks", "wblocks"])
def withdraw_blocks_list(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    cleanup_expired_withdraw_blocks(notify_admin=False)
    data = load_data()
    blocks = data.get("withdraw_blocks", {})
    if not blocks:
        return bot.send_message(message.chat.id, "✅ Активных блокировок вывода нет.")

    lines = ["🚫 <b>Активные блокировки вывода:</b>\n"]
    for uid, block in list(blocks.items())[:30]:
        uname = block.get("username") or get_user(data, uid).get("username") or "нет username"
        reason = block.get("reason") or "не указана"
        lines.append(
            f"\n🆔 <code>{uid}</code> | @{str(uname).replace('@', '')}\n"
            f"⏰ До: <b>{format_block_time(block.get('until', 0))}</b>\n"
            f"📌 {reason}"
        )

    bot.send_message(message.chat.id, "".join(lines))


@bot.message_handler(commands=["unban"])
def unban_user(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.send_message(
            message.chat.id,
            "❌ Формат:\n<code>/unban user_id</code>\n\nПример:\n<code>/unban 8823804307</code>"
        )

    target = parts[1].strip()

    with DATA_LOCK:
        data = load_fresh_data_for_ban_check()
        user_id, err = resolve_user_id(data, target)
        if err:
            return bot.send_message(message.chat.id, f"❌ {err}")

        user = get_user(data, user_id)
        was_banned = bool(user.get("banned", False))

        user["banned"] = False
        user["ban_reason"] = ""
        user["ban_until"] = 0
        user["ban_created"] = 0

        save_data(data)

        # Сразу обновляем локальную копию, чтобы бот не видел старый бан
        try:
            with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("Local unban save error:", e)

    bot.send_message(
        message.chat.id,
        f"✅ Пользователь разблокирован\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📌 Был заблокирован: {'да' if was_banned else 'нет'}"
    )

    safe_send(
        user_id,
        "✅ <b>Ваш аккаунт разблокирован</b>\n\n"
        "Теперь вы снова можете пользоваться ботом."
    )


@bot.message_handler(commands=["status"])
def status_command(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")
    data = load_data()
    active_submits = sum(1 for s in data.get("submits", {}).values() if s.get("status") == "wait")
    active_withdraws = sum(1 for w in data.get("withdraws", {}).values() if w.get("status") == "wait")
    users_count = len(data.get("users", {}))
    tasks_count = sum(1 for t in data.get("tasks", {}).values() if t.get("active", True))
    blocks_count = len(data.get("withdraw_blocks", {}))
    withdraw_state = "включены ✅" if data.get("withdraw_enabled", True) else "отключены ❌"
    bot.send_message(
        message.chat.id,
        "📊 <b>Статус бота</b>\n\n"
        f"👥 Пользователей: <b>{users_count}</b>\n"
        f"📋 Активных заданий: <b>{tasks_count}</b>\n"
        f"📸 Заявок заданий: <b>{active_submits}</b>\n"
        f"💸 Заявок вывода: <b>{active_withdraws}</b>\n"
        f"🚫 Блокировок вывода: <b>{blocks_count}</b>\n"
        f"💳 Выплаты: <b>{withdraw_state}</b>"
    )




@bot.message_handler(commands=["findid"])
def findid_command(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    parts = message.text.split()
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "Использование: /findid user_id")

    uid = str(parts[1]).strip()
    data = load_data()
    exists = uid in data.get("users", {})
    bot.send_message(message.chat.id, f"🔎 ID <code>{uid}</code> в data.json: {'✅ есть' if exists else '❌ нет'}\n👥 Пользователей сейчас: <b>{len(data.get('users', {}))}</b>")


@bot.message_handler(commands=["githubstatus"])
def githubstatus_command(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")

    data = load_data()
    local_ok = os.path.exists(LOCAL_DATA_FILE)
    gh_ok = False
    gh_users = "нет"
    gh_error = ""

    try:
        gh = read_github_data_raw()
        if gh is not None:
            gh_ok = True
            gh_users = str(len(gh.get("users", {})))
        else:
            gh_error = "GitHub не прочитался. Проверь GITHUB_TOKEN, GITHUB_REPO, ветку main и права токена."
    except Exception as e:
        gh_error = str(e)

    bot.send_message(
        message.chat.id,
        "🧪 <b>Проверка сохранения</b>\n\n"
        f"📁 Локальный data.json: {'✅ есть' if local_ok else '❌ нет'}\n"
        f"👥 Пользователей локально/после load: <b>{len(data.get('users', {}))}</b>\n"
        f"🌐 GitHub data.json: {'✅ читается' if gh_ok else '❌ не читается'}\n"
        f"👥 Пользователей в GitHub: <b>{gh_users}</b>\n"
        f"⚙️ GITHUB_REPO: <code>{h(GITHUB_REPO or 'не задан')}</code>\n"
        f"🌿 GITHUB_BRANCH: <code>{h(GITHUB_BRANCH)}</code>\n"
        f"📄 GITHUB_FILE: <code>{h(GITHUB_FILE)}</code>\n"
        f"⚠️ {h(gh_error)}"
    )



@bot.message_handler(commands=["banned", "bans", "blocked"])
def banned_users_command(message):
    if message.from_user.id != ADMIN_ID:
        return bot.send_message(message.chat.id, "❌ Нет доступа.")
    with DATA_LOCK:
        data = load_fresh_data_for_ban_check()
        expired = cleanup_expired_account_bans(data, save=False)
        if expired:
            save_data(data)
    bot.send_message(message.chat.id, format_banned_users_report(data))


@bot.message_handler(func=lambda m: (m.from_user.id == ADMIN_ID and (m.text or "").strip().lower() in ["заблокированные", "заблокированые", "заблокируваные", "заблокированные люди", "баны"]))
def banned_users_text(message):
    with DATA_LOCK:
        data = load_fresh_data_for_ban_check()
        expired = cleanup_expired_account_bans(data, save=False)
        if expired:
            save_data(data)
    bot.send_message(message.chat.id, format_banned_users_report(data))


@bot.message_handler(commands=["forceaddme"])
def forceaddme_command(message):
    data = load_data()
    user = get_user(data, message.from_user.id)
    user["username"] = message.from_user.username or user.get("username", "")
    save_data(data)
    bot.send_message(message.chat.id, f"✅ Я принудительно записал этот аккаунт.\n🆔 ID: <code>{message.from_user.id}</code>\nПроверь: /findid {message.from_user.id}")


@bot.message_handler(func=lambda m: True)
def text_router(message):
    if is_banned_user(message):
        return

    data = load_data()
    user = get_user(data, message.from_user.id)
    user["username"] = message.from_user.username or user.get("username", "")

    text = (message.text or "").strip()

    if user.get("withdraw_step"):
        block = get_active_withdraw_block(data, message.from_user.id)
        if block:
            user["withdraw_step"] = None
            user["withdraw_to"] = None
            save_data(data)
            return bot.send_message(message.chat.id, withdraw_block_text(block))
        save_data(data)

    if user.get("withdraw_step") and not data.get("withdraw_enabled", True):
        user["withdraw_step"] = None
        user["withdraw_to"] = None
        save_data(data)
        return bot.send_message(
            message.chat.id,
            "⚠️ <b>Выплаты временно недоступны.</b>\n\n"
            "🔄 Пожалуйста, попробуйте позже.\n"
            "📢 О возобновлении выплат будет сообщено в боте."
        )

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
                amount = float(amount_text.replace(" ", "").replace(",", "."))
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

            block = get_active_withdraw_block(data, message.from_user.id)
            if block:
                user["withdraw_step"] = None
                user["withdraw_to"] = None
                save_data(data)
                return bot.send_message(message.chat.id, withdraw_block_text(block))

            if not data.get("withdraw_enabled", True):
                user["withdraw_step"] = None
                user["withdraw_to"] = None
                save_data(data)
                return bot.send_message(
                    message.chat.id,
                    "⚠️ <b>Выплаты временно недоступны.</b>\n\n"
                    "🔄 Пожалуйста, попробуйте позже.\n"
                    "📢 О возобновлении выплат будет сообщено в боте."
                )

            missing_required = get_required_missing_tasks(data, user)
            if missing_required:
                user["withdraw_step"] = None
                user["withdraw_to"] = None
                save_data(data)
                return bot.send_message(message.chat.id, required_tasks_block_text(missing_required))

            withdraw_to = user.get("withdraw_to") or "не указано"

            # Разрешаем много активных выводов, если хватает баланса.
            # Дублем считаем ТОЛЬКО повтор того же самого сообщения Telegram
            # (одинаковый message_id). Поэтому пользователь может сразу создать
            # ещё один вывод на ту же сумму, если отправит новое сообщение.
            duplicate_wid = find_duplicate_withdraw_by_message(
                data,
                message.from_user.id,
                getattr(message, "message_id", None)
            )
            if duplicate_wid:
                user["withdraw_step"] = None
                user["withdraw_to"] = None
                save_data(data)
                return bot.send_message(
                    message.chat.id,
                    f"✅ Заявка на вывод уже принята.\n"
                    f"Номер заявки: <b>#{duplicate_wid}</b>"
                )

            if amount > float(user.get("balance", 0)):
                return bot.send_message(
                    message.chat.id,
                    f"❌ Недостаточно GMP.\nТвой баланс: <b>{format_gmp(user.get('balance', 0))} GMP</b>"
                )

            data["last_withdraw_id"] += 1
            wid = str(data["last_withdraw_id"])

            add_balance_to_user(data, message.from_user.id, -amount, reason="withdraw_created", request_id=wid)
            user["withdraw_pending"] = True
            user["withdraw_step"] = None
            user["withdraw_to"] = None

            data.setdefault("withdraws", {})[wid] = {
                "user_id": message.from_user.id,
                "username": message.from_user.username or "",
                "to": withdraw_to,
                "amount": amount,
                "status": "wait",
                "time": int(time.time()),
                "source_message_id": getattr(message, "message_id", None)
            }

            # СНАЧАЛА ставим метку, что заявка уже отправляется админу.
            # Это убирает баг: если админ сразу нажмёт /requests, бот НЕ продублирует эту же заявку.
            mark_admin_request_sent(data, "withdraws", wid, "sending")
            save_data(data)

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ Выплачено", callback_data=f"payyes_{wid}"),
            types.InlineKeyboardButton("❌ Отказать", callback_data=f"payno_{wid}")
        )
        kb.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"paydel_{wid}"))

        bot.send_message(
            message.chat.id,
            f"✅ <b>Заявка на вывод #{wid} создана</b>\n\n"
            f"👤 Куда: <b>{h(withdraw_to)}</b>\n"
            f"💰 Сумма: <b>{format_gmp(amount)} GMP</b>\n\n"
            "⏳ Заявка отправлена на проверку.\n"
            "Ожидайте выплату до 24 часов."
        )

        try:
            admin_msg = bot.send_message(
                ADMIN_ID,
                f"💸 <b>Новая заявка на вывод #{wid}</b>\n\n"
                f"👤 Пользователь: @{safe_username(message.from_user.username)}\n"
                f"🆔 ID: <code>{message.from_user.id}</code>\n"
                f"📤 Куда вывести: <b>{h(withdraw_to)}</b>\n"
                f"💰 Сумма: <b>{format_gmp(amount)} GMP</b>",
                reply_markup=kb
            )

            with DATA_LOCK:
                data = load_data()
                mark_admin_request_sent(data, "withdraws", wid, getattr(admin_msg, "message_id", None))
                save_data(data)
        except Exception as e:
            print("send withdraw to admin error:", e)
            with DATA_LOCK:
                data = load_data()
                data.setdefault("admin_sent", {}).setdefault("withdraws", {}).pop(str(wid), None)
                w = data.get("withdraws", {}).get(str(wid))
                if w:
                    w.pop("admin_sent", None)
                    w.pop("admin_sent_time", None)
                    w.pop("admin_message_id", None)
                save_data(data)
        return

    # Если сюда дошло обычное сообщение без активного шага — показываем меню.
    bot.send_message(message.chat.id, "👇 Выбери кнопку в меню.", reply_markup=main_menu())




if __name__ == "__main__":
    if not TOKEN:
        print("❌ BOT_TOKEN не найден.")
        exit()

    threading.Thread(target=run_site, daemon=True).start()
    threading.Thread(target=auto_ping, daemon=True).start()
    threading.Thread(target=auto_withdraw_unblock_loop, daemon=True).start()
    print("✅ Bot started")

    while True:
        try:
            bot.remove_webhook()
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:
            print("Polling error:", e)
            time.sleep(10)

