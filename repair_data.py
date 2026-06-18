import json
import shutil

DATA_FILE = "data.json"
BACKUP_FILE = "data_backup.json"

# Бэкап
shutil.copy(DATA_FILE, BACKUP_FILE)

with open(DATA_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

# Чистка admin_sent старше несуществующих заявок
for sid in list(data.get("admin_sent", {}).get("submits", {})):
    if sid not in data.get("submits", {}):
        data["admin_sent"]["submits"].pop(sid, None)

for wid in list(data.get("admin_sent", {}).get("withdraws", {})):
    if wid not in data.get("withdraws", {}):
        data["admin_sent"]["withdraws"].pop(wid, None)

# Исправление пользователей
for user in data.get("users", {}).values():

    if user.get("waiting_task") and user.get("waiting_task") not in user.get("pending_tasks", []):
        user["waiting_task"] = None

    if user.get("withdraw_pending"):
        active = False

        for w in data.get("withdraws", {}).values():
            if (
                str(w.get("user_id")) == str(user.get("id", ""))
                and w.get("status") == "wait"
            ):
                active = True
                break

        if not active:
            user["withdraw_pending"] = False

with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("✅ repair_data.py завершил проверку")
