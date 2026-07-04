# admin.py — все админ-команды вынесены из bot.py
# В bot.py должно быть подключение:
# from admin import register_admin_handlers
# register_admin_handlers(bot, globals())

import time
import re
import html
import shutil


def register_admin_handlers(bot, ctx):
    # Берём все функции и переменные из bot.py: load_data, save_data, ADMIN_ID, types и т.д.
    globals().update(ctx)

    def admin_menu():
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.row("➕ Добавить задание", "📋 Все задания")
        kb.row("💎 Начислить баланс", "📨 Заявки")
        kb.row("🏠 Меню")
        return kb

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

    def admin(message):
        if message.from_user.id != ADMIN_ID:
            return bot.send_message(message.chat.id, "❌ Нет доступа.")
        bot.send_message(message.chat.id, "🔐 <b>Админ-панель</b>", reply_markup=admin_menu())

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

    def repair_command(message):
        if message.from_user.id != ADMIN_ID:
            return bot.send_message(message.chat.id, "❌ Нет доступа.")

        with DATA_LOCK:
            data = load_data_for_admin_action()

            before_submits = len(data.get("submits", {}))
            before_withdraws = len(data.get("withdraws", {}))
            before_processed_submits = len(data.get("processed_requests", {}).get("submits", {}))
            before_processed_withdraws = len(data.get("processed_requests", {}).get("withdraws", {}))

            expired_bans = cleanup_expired_account_bans(data, save=False)

            # Удаляем заявки, которые уже обработаны или не ждут проверки.
            cleanup_processed_requests(data)

            active_task_ids = {str(tid) for tid, t in data.get("tasks", {}).items() if isinstance(t, dict) and t.get("active", True)}
            active_submit_tasks = {str(s.get("task_id")) for s in data.get("submits", {}).values() if isinstance(s, dict) and s.get("status") == "wait"}
            active_withdraw_users = {str(w.get("user_id")) for w in data.get("withdraws", {}).values() if isinstance(w, dict) and w.get("status") == "wait"}

            cleaned_users = 0
            for uid, user in data.get("users", {}).items():
                if not isinstance(user, dict):
                    continue
                old_pending = list(user.get("pending_tasks", []))
                user["pending_tasks"] = [str(tid) for tid in old_pending if str(tid) in active_task_ids and str(tid) in active_submit_tasks]
                if str(user.get("waiting_task") or "") not in active_task_ids:
                    user["waiting_task"] = None
                if str(user.get("last_open_task") or "") not in active_task_ids:
                    user["last_open_task"] = None
                user["withdraw_pending"] = str(uid) in active_withdraw_users
                if old_pending != user.get("pending_tasks", []):
                    cleaned_users += 1

            # Чистим список обязательных заданий от удалённых.
            old_required = list(data.get("required_tasks", []))
            data["required_tasks"] = [str(tid) for tid in old_required if str(tid) in active_task_ids]

            sync_withdraw_stats(data)
            data["start_text"] = DEFAULT_START_TEXT
            save_data(data)

            after_submits = len(data.get("submits", {}))
            after_withdraws = len(data.get("withdraws", {}))
            after_processed_submits = len(data.get("processed_requests", {}).get("submits", {}))
            after_processed_withdraws = len(data.get("processed_requests", {}).get("withdraws", {}))

        bot.send_message(
            message.chat.id,
            "🛠 <b>Проверка завершена</b>\n\n"
            f"🗑 Заявки на задания: {before_submits} → {after_submits}\n"
            f"💸 Заявки на вывод: {before_withdraws} → {after_withdraws}\n"
            f"🚫 Просроченных банов снято: {len(expired_bans)}\n"
            f"👤 Профилей очищено: {cleaned_users}\n"
            f"📌 Метки обработанных заявок: {before_processed_submits + before_processed_withdraws} → {after_processed_submits + after_processed_withdraws}\n"
            "✅ data.json и requests.json очищены от лишнего."
        )

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

    def banned_users_command(message):
        if message.from_user.id != ADMIN_ID:
            return bot.send_message(message.chat.id, "❌ Нет доступа.")
        with DATA_LOCK:
            data = load_fresh_data_for_ban_check()
            expired = cleanup_expired_account_bans(data, save=False)
            if expired:
                save_data(data)
        bot.send_message(message.chat.id, format_banned_users_report(data))

    def banned_users_text(message):
        with DATA_LOCK:
            data = load_fresh_data_for_ban_check()
            expired = cleanup_expired_account_bans(data, save=False)
            if expired:
                save_data(data)
        bot.send_message(message.chat.id, format_banned_users_report(data))

