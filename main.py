import os
import sqlite3
import json
from dotenv import load_dotenv
import telebot
from telebot import types
from typing import Optional, List, Any, Dict
from datetime import datetime
from flask import Flask
from threading import Thread

# =====================
# Загрузка переменных окружения
# =====================
load_dotenv()
TOKEN = os.getenv("TOKEN")
OWNER_ID = os.getenv("OWNER_ID")

if TOKEN is None:
    raise ValueError("TOKEN не задан в .env")
if OWNER_ID is None:
    raise ValueError("OWNER_ID должен быть целым числом.")

try:
    OWNER_ID = int(OWNER_ID)
except ValueError:
    raise ValueError("OWNER_ID должен быть целым числом.")

# =====================
# Инициализация бота
# =====================
# Теперь мы будем использовать MarkdownV2 для ссылок, чтобы избежать конфликтов
bot = telebot.TeleBot(str(TOKEN), parse_mode="HTML")

# =====================
# База данных и Миграция
# =====================
DB_PATH = os.getenv("DB_PATH", "skezzy_support.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

def init_db():
    print(">>> Инициализация базы данных...")

    # Создание таблицы tickets с правильным столбцом admin_id
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        category TEXT,
        nick TEXT,
        description TEXT,
        proofs TEXT,
        status TEXT DEFAULT 'open',
        admin_id INTEGER, 
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Создание других таблиц
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        tg_id INTEGER PRIMARY KEY,
        level INTEGER DEFAULT 1
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_chats (
        user_id INTEGER PRIMARY KEY,
        admin_id INTEGER
    )
    """)

    # АВТОМАТИЧЕСКАЯ МИГРАЦИЯ (резервная проверка)
    try:
        cur.execute("SELECT admin_id FROM tickets LIMIT 1")
    except sqlite3.OperationalError:
        print(">>> [MIGRATION] Столбец 'admin_id' отсутствует. Добавляем его...")
        cur.execute("ALTER TABLE tickets ADD COLUMN admin_id INTEGER")
        print(">>> [MIGRATION] Столбец 'admin_id' успешно добавлен.")

    conn.commit()
    print(">>> Инициализация завершена.")

init_db()
# Добавляем владельца как главного администратора при первом запуске
cur.execute("INSERT OR IGNORE INTO admins(tg_id, level) VALUES(?,?)", (OWNER_ID, 3))
conn.commit()

# =====================
# Состояния пользователей
# =====================
user_states: Dict[int, Dict[str, Any]] = {}

# =====================
# Утилиты для работы с БД 
# =====================
def is_admin(tg_id: int) -> bool:
    cur.execute("SELECT level FROM admins WHERE tg_id=?", (tg_id,))
    return cur.fetchone() is not None

def get_admin_username(tg_id: int) -> str:
    cur.execute("SELECT username FROM users WHERE tg_id=?", (tg_id,))
    row = cur.fetchone()
    return f"@{row[0]}" if row and row[0] else f"Admin ID {tg_id}"

def get_admins() -> List[int]:
    cur.execute("SELECT tg_id FROM admins")
    return [r[0] for r in cur.fetchall()]

def register_user(tg_id: int, username: Optional[str]):
    cur.execute("INSERT OR IGNORE INTO users(tg_id, username) VALUES(?,?)", (tg_id, username))
    conn.commit()

def assign_admin_chat(user_id: int, admin_id: int):
    cur.execute("INSERT OR REPLACE INTO admin_chats(user_id, admin_id) VALUES(?,?)", (user_id, admin_id))
    conn.commit()

def get_assigned_admin(user_id: int) -> Optional[int]:
    cur.execute("SELECT admin_id FROM admin_chats WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return row[0] if row else None

def remove_assigned_chat(user_id: int):
    cur.execute("DELETE FROM admin_chats WHERE user_id=?", (user_id,))
    conn.commit()

def create_ticket(user_id: int, username: str, category: str, nick: str, description: str, proofs: Optional[List]) -> Optional[int]:
    proofs_json = json.dumps(proofs or [])
    try:
        cur.execute("""INSERT INTO tickets(user_id, username, category, nick, description, proofs, status)
                        VALUES(?,?,?,?,?,?,'open')""",
                    (user_id, username, category, nick, description, proofs_json))
        conn.commit()
        ticket_id = cur.lastrowid

        if ticket_id is not None:
            notify_admins(int(ticket_id), username, category, nick, description)
            return int(ticket_id)

    except sqlite3.Error as e:
        print(f"DB Error creating ticket: {e}")
        return None

    return None

def notify_admins(ticket_id: int, username: str, category: str, nick: str, description: str):
    message_text = (
        f"🆕 **НОВЫЙ ТИКЕТ** (ID: {ticket_id})\n"
        f"Игрок: @{username}\n"
        f"Категория: **{category}**\n"
        f"Ник: {nick if nick != '-' else '—'}\n"
        f"Описание: _{description[:100]}..._"
    )
    for a in get_admins():
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("👁️ Просмотреть и взять в работу", callback_data=f"view_ticket_{ticket_id}"))
        try:
            bot.send_message(a, message_text, reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            print(f"Error notifying admin {a}: {e}")

def get_ticket(ticket_id: int) -> Optional[Dict]:
    cur.execute("SELECT id,user_id,username,category,nick,description,proofs,status,admin_id FROM tickets WHERE id=?", (ticket_id,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "user_id": row[1], "username": row[2], "category": row[3],
        "nick": row[4], "description": row[5], "proofs": json.loads(row[6]) if row[6] else [],
        "status": row[7], "admin_id": row[8]
    }

def get_open_tickets() -> List:
    cur.execute("SELECT id, user_id, category, status, admin_id, created_at FROM tickets WHERE status IN ('open', 'in_progress') ORDER BY created_at DESC")
    return cur.fetchall()

def take_ticket(ticket_id: int, admin_id: int) -> bool:
    cur.execute("UPDATE tickets SET status='in_progress', admin_id=? WHERE id=? AND status='open'", (admin_id, ticket_id))
    conn.commit()
    return cur.rowcount > 0

def close_ticket(ticket_id: int, admin_id: int):
    cur.execute("UPDATE tickets SET status='closed', admin_id=? WHERE id=?", (admin_id, ticket_id))
    conn.commit()
    ticket = get_ticket(ticket_id)
    if ticket and ticket["user_id"]:
        remove_assigned_chat(ticket["user_id"])
        try:
            bot.send_message(ticket["user_id"], f"✅ Ваш тикет ID **{ticket_id}** закрыт администратором.", parse_mode="Markdown")
        except Exception:
            pass

def list_users() -> List:
    cur.execute("SELECT tg_id, username FROM users")
    return cur.fetchall()

def add_admin(tg_id: int, level: int = 1):
    cur.execute("INSERT OR REPLACE INTO admins(tg_id, level) VALUES(?,?)", (tg_id, level))
    conn.commit()

# =====================
# Меню (ReplyKeyboardMarkup)
# =====================
def admin_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    # ДОБАВЛЕНИЕ: Кнопка "📢 Рассылка"
    kb.row(types.KeyboardButton("📄 Список тикетов"), types.KeyboardButton("📢 Рассылка"))
    kb.row(types.KeyboardButton("👥 Список пользователей"), types.KeyboardButton("➕ Добавить админа"))
    kb.row(types.KeyboardButton("❌ Завершить чат"), types.KeyboardButton("🚪 В меню игрока"))
    return kb

def main_menu(user_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)

    if is_admin(user_id):
        kb.row(types.KeyboardButton("🛠 Админ-панель"))

    kb.row(types.KeyboardButton("📜 Правила"), types.KeyboardButton("💰 Донат"))
    kb.row(types.KeyboardButton("🆘 Вызвать админа"), types.KeyboardButton("⚙️ Тех. вопросы"))
    kb.row(types.KeyboardButton("🎁 Возврат имущества"), types.KeyboardButton("🐞 Нашёл баг"))
    kb.row(types.KeyboardButton("ℹ️ Информация"))

    return kb


# =====================
# Функции Админ-панели
# =====================
def show_tickets_list(cid: int, message_id: Optional[int] = None):
    tickets = get_open_tickets()

    if not tickets:
        message = "✅ Открытых или взятых в работу тикетов нет."
        if message_id:
            try:
                bot.edit_message_text(message, cid, message_id, reply_markup=types.InlineKeyboardMarkup()) 
            except Exception:
                bot.send_message(cid, message) 
        else:
            bot.send_message(cid, message)
        return

    message_text = "📄 **Активные тикеты:**\n\n"
    kb = types.InlineKeyboardMarkup()

    for tid, uid, category, status, admin_id, created_at in tickets:
        status_emoji = "🟢 Открыт" if status == 'open' else "🟠 В работе"
        admin_info = f" ({get_admin_username(admin_id)})" if admin_id else ""
        try:
            date_str = datetime.strptime(created_at.split('.')[0], "%Y-%m-%d %H:%M:%S").strftime("%H:%M %d.%m")
        except ValueError:
            date_str = "Неизвестная дата"

        message_text += f"🔹 ID **{tid}** | {status_emoji}{admin_info} | {category} ({date_str})\n"

        kb.row(
            types.InlineKeyboardButton(f"👁️ Просмотр {tid}", callback_data=f"view_ticket_{tid}"),
            types.InlineKeyboardButton(f"🔒 Закрыть {tid}", callback_data=f"close_ticket_list_{tid}")
        )

    kb.add(types.InlineKeyboardButton("🔄 Обновить список", callback_data="tickets_list"))

    if message_id:
        try:
            bot.edit_message_text(message_text, cid, message_id, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            bot.send_message(cid, message_text, reply_markup=kb, parse_mode="Markdown")
    else:
        bot.send_message(cid, message_text, reply_markup=kb, parse_mode="Markdown")

def get_ticket_details_markup(ticket: Dict, current_admin_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()

    if ticket['status'] == 'open':
        kb.add(types.InlineKeyboardButton("🔨 Взять в работу", callback_data=f"take_ticket_{ticket['id']}"))
    elif ticket['status'] == 'in_progress':
        if ticket['admin_id'] == current_admin_id:
            kb.add(types.InlineKeyboardButton("🔒 Закрыть тикет", callback_data=f"close_ticket_{ticket['id']}"))

    kb.add(types.InlineKeyboardButton("💬 Ответить игроку", callback_data=f"reply_ticket_{ticket['id']}"))

    user_id_for_chat = ticket['user_id']

    if not get_assigned_admin(user_id_for_chat):
        kb.add(types.InlineKeyboardButton("🔗 Инициировать чат", callback_data=f"connect_{user_id_for_chat}"))
    else:
         kb.add(types.InlineKeyboardButton("💬 Чат уже активен", callback_data=f"connect_{user_id_for_chat}")) 

    kb.add(types.InlineKeyboardButton("🔙 Назад к списку", callback_data="tickets_list"))
    return kb


# =====================
# ВЕБ-СЕРВЕР ДЛЯ ПОДДЕРЖАНИЯ АКТИВНОСТИ (24/7)
# =====================
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run_flask_server():
    app.run(host='0.0.0.0', port=8080) 

# =====================
# Обработчики Telegram
# =====================
@bot.message_handler(commands=["start","help"])
def start_handler(msg):
    register_user(msg.from_user.id, getattr(msg.from_user, "username", None))
    bot.send_message(
        msg.chat.id,
        "👋 Привет! Я бот поддержки <b>SKEZZY ONLINE</b>!\nВыберите раздел меню ⬇️",
        reply_markup=main_menu(msg.from_user.id)
    )

@bot.message_handler(func=lambda m: True, content_types=['text', 'photo'])
def message_handler(msg):
    cid = msg.chat.id
    text = msg.text

    username_raw: Optional[str] = getattr(msg.from_user, "username", None)
    username: str = username_raw if username_raw else f"user_{cid}" 

    register_user(cid, username_raw)

    # ------------------
    # 1. ОБРАБОТКА СОСТОЯНИЙ
    # ------------------
    if cid in user_states:
        state = user_states[cid]
        step = state.get("step")
        data = state.get("data", {})

        if not text and msg.content_type != 'photo' and step not in ["return_item", "bug_report", "waiting_for_broadcast_message"]:
            return

        # 1.1. Администратор: Ввод ID для добавления
        if step == "waiting_for_admin_id" and is_admin(cid):
            if not text:
                bot.send_message(cid, "❌ Ожидался ввод Telegram ID.", reply_markup=admin_menu())
                user_states.pop(cid)
                return
            try:
                new_admin_id = int(text.strip())
                add_admin(new_admin_id)
                bot.send_message(cid, f"✅ Пользователь с ID **{new_admin_id}** теперь администратор (уровень 1).", parse_mode="Markdown", reply_markup=admin_menu())
                try:
                    bot.send_message(new_admin_id, "🥳 Поздравляем! Вы получили права администратора на **SKEZZY ONLINE**.", reply_markup=main_menu(new_admin_id))
                except Exception:
                    pass 
            except ValueError:
                bot.send_message(cid, "❌ Некорректный ID. Введите числовой Telegram ID пользователя.", parse_mode="Markdown", reply_markup=admin_menu()) 
            finally:
                user_states.pop(cid)
            return

        # 1.2. Администратор: Ожидание быстрого ответа на тикет
        if step == "waiting_for_ticket_response":

            if text == "Отмена":
                bot.send_message(cid, "❌ Ответ на тикет отменен.", reply_markup=admin_menu())
                user_states.pop(cid)
                return

            if not text or msg.content_type != 'text':
                bot.send_message(cid, "❗ Введите ответ текстом. Отправка фото не поддерживается в режиме быстрого ответа.", reply_markup=types.ReplyKeyboardRemove()) 
                return

            user_id = data["user_id"]
            ticket_id = data["ticket_id"]
            admin_name = data["admin_name"]

            response_text = (
                f"✉️ **Ответ администратора {admin_name} по тикету ID {ticket_id}:**\n\n"
                f"_{text}_"
            )

            try:
                bot.send_message(user_id, response_text, parse_mode="Markdown")
                bot.send_message(cid, f"✅ Ответ по тикету ID **{ticket_id}** успешно отправлен игроку.", parse_mode="Markdown", reply_markup=admin_menu())
            except Exception as e:
                print(f"Error sending reply to user {user_id}: {e}")
                bot.send_message(cid, f"❌ Ошибка отправки: не удалось отправить ответ игроку ID **{user_id}**.", parse_mode="Markdown", reply_markup=admin_menu())

            user_states.pop(cid)
            return

        # 1.3. Администратор: Ожидание поста для рассылки
        if step == "waiting_for_broadcast_message" and is_admin(cid):
            if text == "Отмена":
                bot.send_message(cid, "❌ Рассылка отменена.", reply_markup=admin_menu())
                user_states.pop(cid)
                return

            all_users = list_users()
            sent_count = 0

            # Отправка рассылки
            for user_id, _ in all_users:
                # Пропускаем самого админа, чтобы он не получил свою же рассылку
                if user_id == cid:
                    continue

                try:
                    if msg.content_type == 'text':
                        bot.send_message(user_id, text, parse_mode="Markdown")
                    elif msg.content_type == 'photo':
                        # Отправляем фото с подписью (текстом сообщения, если есть)
                        caption = msg.caption if msg.caption else ""
                        bot.send_photo(user_id, msg.photo[-1].file_id, caption=caption, parse_mode="Markdown")
                    sent_count += 1
                except Exception as e:
                    # Обработка ошибки, например, если пользователь заблокировал бота
                    print(f"Error sending broadcast to user {user_id}: {e}")

            bot.send_message(
                cid, 
                f"✅ **Рассылка завершена!**\n\n"
                f"Отправлено сообщений: **{sent_count}**",
                parse_mode="Markdown",
                reply_markup=admin_menu()
            )
            user_states.pop(cid)
            return

        # 1.4. ЛОГИКА СОЗДАНИЯ ТИКЕТОВ С НЕСКОЛЬКИМИ ШАГАМИ (return_item, bug_report, tech_question)
        current_step_name = step

        if current_step_name == "return_item":
            if "nick" not in data:
                if not text:
                    bot.send_message(cid, "❗ Введите ник персонажа текстом.", reply_markup=main_menu(cid))
                    return
                data["nick"] = text
                bot.send_message(cid,"Введите описание имущества:", reply_markup=main_menu(cid))
            elif "description" not in data:
                if not text:
                    bot.send_message(cid, "❗ Введите описание имущества текстом.", reply_markup=main_menu(cid))
                    return
                data["description"] = text
                bot.send_message(cid,"Прикрепите доказательства (фото) или отправьте любой текст для завершения.", reply_markup=main_menu(cid))
            else: 
                proofs_list = data.get("proofs", [])
                if msg.content_type == 'photo':
                    proofs_list.append(msg.photo[-1].file_id)
                    data["proofs"] = proofs_list
                    bot.send_message(cid, f"✅ Фото добавлено! Всего: {len(proofs_list)}.\nПришлите ещё или отправьте любой текст, чтобы завершить.", reply_markup=main_menu(cid))
                    user_states[cid]["data"] = data
                    return

                if text or len(proofs_list) > 0:
                    ticket_id = create_ticket(cid, username, "Возврат имущества", data["nick"], data["description"], proofs_list)
                    msg_text = f"✅ Тикет на возврат имущества создан! ID: **{ticket_id}**" if ticket_id else "❌ Произошла ошибка при создании тикета."
                    bot.send_message(cid, msg_text, parse_mode="Markdown", reply_markup=main_menu(cid))
                    user_states.pop(cid)
                    return

        if current_step_name == "bug_report":
            if "description" not in data:
                if not text:
                    bot.send_message(cid, "❗ Пожалуйста, опишите баг текстом.", reply_markup=main_menu(cid))
                    return
                data["description"] = text
                bot.send_message(cid,"Прикрепите доказательства (фото) или отправьте любой текст для завершения.", reply_markup=main_menu(cid))
            else: 
                proofs_list = data.get("proofs", [])
                if msg.content_type == 'photo':
                    proofs_list.append(msg.photo[-1].file_id)
                    data["proofs"] = proofs_list
                    bot.send_message(cid, f"✅ Фото добавлено! Всего: {len(proofs_list)}.\nПришлите ещё или отправьте любой текст, чтобы завершить.", reply_markup=main_menu(cid))
                    user_states[cid]["data"] = data
                    return

                if text or len(proofs_list) > 0:
                    ticket_id = create_ticket(cid, username, "Баг-репорт", "-", data["description"], proofs_list)
                    msg_text = f"✅ Тикет создан! ID: **{ticket_id}**" if ticket_id else "❌ Произошла ошибка при создании тикета."
                    bot.send_message(cid, msg_text, parse_mode="Markdown", reply_markup=main_menu(cid))
                    user_states.pop(cid)
                    return

        if current_step_name == "tech_question":
            if not text:
                bot.send_message(cid, "❗ Пожалуйста, опишите проблему текстом.", reply_markup=main_menu(cid))
                return
            ticket_id = create_ticket(cid, username, "Тех. вопросы", "-", text, None)
            msg_text = f"✅ Тикет создан! ID: **{ticket_id}**" if ticket_id else "❌ Произошла ошибка при создании тикета."
            bot.send_message(cid, msg_text, parse_mode="Markdown", reply_markup=main_menu(cid))
            user_states.pop(cid)
            return

        user_states[cid]["data"] = data
        return

    # ------------------
    # 2. ОБРАБОТКА КНОПОК МЕНЮ
    # ------------------

    if text == "📜 Правила":
        rules_url = "http://forum.skezzy-rp.ru/index.php?forums/%D0%9F%D1%80%D0%B0%D0%B2%D0%B8%D0%BB%D0%B0.54/"
        kb_inline = types.InlineKeyboardMarkup()
        kb_inline.add(types.InlineKeyboardButton("📜 Открыть Правила", url=rules_url))
        bot.send_message(cid, "**Правила проекта SKEZZY ONLINE**.\nНажмите на кнопку ниже, чтобы ознакомиться:", reply_markup=kb_inline, parse_mode="Markdown")
        return

    if text == "💰 Донат":
        bot.send_message(cid, "💰 Донат SKEZZY ONLINE\nПо вопросам доната пишите: @stardxx\nПриобрести можно на сайте: skezzy-rp.ru", reply_markup=main_menu(cid))
        return

    # ФИНАЛЬНЫЙ ОБРАБОТЧИК ДЛЯ КНОПКИ "ℹ️ Информация"
    if text == "ℹ️ Информация": 
        # Обратите внимание, что здесь используется Markdown для кликабельных ссылок
        message_text = (
            "🌐 **Наши соц сети:**\n"
            f"📱 [TikTok](https://www.tiktok.com/@skezzy_rp?_r=1)\n"
            f"💬 [Telegram](https://t.me/skezzyrpp)\n"
            f"🌐 [VK](https://vk.me/join/GjVUZI52NqVfL4sb3nPMvRVDVBpEDisQaYk=)\n"
            f"🗣 [Discord](https://discord.gg/RBeQrqrgZN)\n\n"
            "➖➖➖➖➖➖➖➖➖➖\n"
            "👋 **Перенос имущества (для новых игроков):**\n"
            "Ты только перешел на наш проект? У нас есть **перенос имущества**!\n"
            "Для этого нажми кнопку **\"🎁 Возврат имущества\"** и следуй инструкциям.\n"
            "По всем вопросам: **@Seko116**" 
        )
        bot.send_message(cid, message_text, reply_markup=main_menu(cid), parse_mode="Markdown")
        return

    if text == "⚙️ Тех. вопросы":
        user_states[cid] = {"step":"tech_question"}
        bot.send_message(cid,"⚙️ Опишите проблему:", reply_markup=main_menu(cid))
        return

    if text == "🎁 Возврат имущества":
        user_states[cid] = {"step":"return_item", "data":{}}
        bot.send_message(cid,"Введите ник персонажа:", reply_markup=main_menu(cid))
        return

    if text == "🐞 Нашёл баг":
        user_states[cid] = {"step":"bug_report", "data":{}}
        bot.send_message(cid,"Опишите баг:", reply_markup=main_menu(cid))
        return

    if text == "🆘 Вызвать админа":
        if get_assigned_admin(cid):
            bot.send_message(cid,"❗ Вы уже подключены к администратору.", reply_markup=main_menu(cid))
            return
        for a in get_admins():
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🔗 Подключиться", callback_data=f"connect_{cid}"))
            try:
                bot.send_message(a,f"🆘 Игрок @{username} ({cid}) вызвал админа.", reply_markup=kb)
            except Exception:
                pass
        bot.send_message(cid,"🆘 Ваш вызов отправлен администраторам. Ожидайте подключения.", reply_markup=main_menu(cid))
        return

    # --- КНОПКИ АДМИН-ПАНЕЛИ (ReplyKeyboardMarkup) ---
    if text == "🛠 Админ-панель" and is_admin(cid):
        bot.send_message(cid,"🛠 Добро пожаловать в Админ-панель. Выберите действие:", reply_markup=admin_menu())
        return

    if text == "🚪 В меню игрока" and is_admin(cid):
        bot.send_message(cid, "👋 Вы вернулись в меню игрока.", reply_markup=main_menu(cid))
        return

    if text == "📄 Список тикетов" and is_admin(cid):
        show_tickets_list(cid) 
        return

    # ДОБАВЛЕНИЕ: Вход в режим рассылки
    if text == "📢 Рассылка" and is_admin(cid):
        user_states[cid] = {"step": "waiting_for_broadcast_message"}
        kb_cancel = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb_cancel.add(types.KeyboardButton("Отмена"))
        bot.send_message(
            cid, 
            "📢 **Режим рассылки.**\n\n"
            "Отправьте мне сообщение (текст или фото с подписью), которое нужно разослать всем пользователям.\n"
            "*(Поддерживается форматирование Markdown)*", 
            parse_mode="Markdown", 
            reply_markup=kb_cancel
        )
        return

    if text == "👥 Список пользователей" and is_admin(cid):
        users = list_users()
        message_text = "👥 **Список пользователей:**\n"
        for tg_id, username_user in users: 
            message_text += f"ID: `{tg_id}` | @{username_user if username_user else 'Нет юзернейма'}\n"
        bot.send_message(cid, message_text, parse_mode="Markdown", reply_markup=admin_menu())
        return

    if text == "➕ Добавить админа" and is_admin(cid):
        user_states[cid] = {"step": "waiting_for_admin_id"}
        bot.send_message(cid, "➕ **Добавление администратора**.\nВведите **Telegram ID** пользователя, которого хотите назначить администратором:", parse_mode="Markdown", reply_markup=admin_menu())
        return

    if text == "❌ Завершить чат":
        admin_id = get_assigned_admin(cid)
        if is_admin(cid):
            removed_chats = 0
            for uid, aid in cur.execute("SELECT user_id, admin_id FROM admin_chats WHERE admin_id=?", (cid,)).fetchall():
                try:
                    bot.send_message(uid,"❌ Админ завершил чат.", reply_markup=main_menu(uid))
                except Exception:
                    pass
                remove_assigned_chat(uid)
                removed_chats += 1
            if removed_chats > 0:
                bot.send_message(cid, f"❌ Вы завершили {removed_chats} активных чатов.", reply_markup=admin_menu())
            else:
                bot.send_message(cid, "❌ Активных чатов для завершения не найдено.", reply_markup=admin_menu())
        else:
            if admin_id:
                try:
                    bot.send_message(admin_id,f"❌ Игрок @{username} завершил чат.", reply_markup=admin_menu())
                except Exception:
                    pass
                remove_assigned_chat(cid)
            bot.send_message(cid,"❌ Вы завершили чат.", reply_markup=main_menu(cid))
        return

    # ------------------
    # 3. ПЕРЕПИСКА ЧАТ АДМИН/ИГРОК
    # ------------------
    if msg.content_type in ['text', 'photo']:

        admin_id_assigned = get_assigned_admin(cid)
        if admin_id_assigned:
            if msg.content_type == 'text':
                bot.send_message(admin_id_assigned, f"💬 Игрок @{username}: {text}")
            else:
                bot.send_message(admin_id_assigned, f"💬 Игрок @{username} отправил фото:")
                bot.forward_message(admin_id_assigned, cid, msg.message_id)
            return

        if is_admin(cid):
            cur.execute("SELECT user_id FROM admin_chats WHERE admin_id=?", (cid,))
            rows = cur.fetchall()
            for row in rows:
                user_id = row[0]
                if msg.content_type == 'text':
                    bot.send_message(user_id, f"💬 Админ: {text}")
                else:
                    bot.send_message(user_id, "💬 Админ отправил фото:")
                    bot.forward_message(user_id, cid, msg.message_id)
            if rows:
                return

    # ------------------
    # 4. ДЕФОЛТНЫЙ ОТВЕТ
    # ------------------
    bot.send_message(cid, "❓ Неизвестная команда. Выберите опцию из меню.", reply_markup=main_menu(cid))


# =====================
# Inline кнопки
# =====================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    data = call.data
    cid = call.from_user.id
    bot.answer_callback_query(call.id)

    if not is_admin(cid):
        bot.send_message(cid, "⛔ У вас нет прав для этого действия.", reply_markup=main_menu(cid))
        return

    # 1. Обработка подключения к чату с игроком
    if data.startswith("connect_"):
        uid = int(data.split("_")[1])

        current_admin_id = get_assigned_admin(uid)

        kb_chat = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb_chat.row(types.KeyboardButton("❌ Завершить чат"))

        if current_admin_id:
            if current_admin_id == cid:
                bot.send_message(cid, f"Вы уже подключены к чату с пользователем ID **{uid}**. Начните писать сообщение.", parse_mode="Markdown", reply_markup=kb_chat)
            else:
                bot.send_message(cid, f"❌ Чат уже занят другим администратором ({get_admin_username(current_admin_id)}).", parse_mode="Markdown", reply_markup=admin_menu())
            return

        assign_admin_chat(uid, cid)

        bot.send_message(cid, f"✅ Вы подключились к чату с игроком **{uid}**.", parse_mode="Markdown", reply_markup=kb_chat)

        try:
             bot.send_message(uid,"🆘 Админ подключился к чату. Теперь можно писать сообщения.", reply_markup=kb_chat)
        except Exception:
             bot.send_message(cid, f"❌ Не удалось уведомить пользователя ID {uid} о подключении.", reply_markup=admin_menu())
        return

    # 2. Обновление списка тикетов
    if data == "tickets_list":
        show_tickets_list(cid, call.message.message_id) 
        return

    # 3. CALLBACK: Подготовка к быстрому ответу
    if data.startswith("reply_ticket_"):
        ticket_id = int(data.split("_")[2])
        ticket = get_ticket(ticket_id)

        if not ticket:
             bot.send_message(cid, f"❌ Тикет ID **{ticket_id}** не найден.", parse_mode="Markdown", reply_markup=admin_menu())
             return

        user_states[cid] = {
            "step": "waiting_for_ticket_response",
            "data": {
                "ticket_id": ticket_id,
                "user_id": ticket["user_id"],
                "admin_name": get_admin_username(cid)
            }
        }

        kb_cancel = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb_cancel.add(types.KeyboardButton("Отмена"))

        bot.send_message(
            cid, 
            f"✍️ Вы отвечаете на **Тикет ID {ticket_id}** игроку @{ticket['username']}.\nВведите ваш ответ:",
            parse_mode="Markdown",
            reply_markup=kb_cancel 
        )
        return


    # 4. Обработка просмотра конкретного тикета
    if data.startswith("view_ticket_"):
        ticket_id = int(data.split("_")[2])
        ticket = get_ticket(ticket_id)

        if not ticket:
            bot.send_message(cid, f"❌ Тикет ID **{ticket_id}** не найден.", parse_mode="Markdown", reply_markup=admin_menu())
            return

        status_text = {
            'open': '🟢 Открыт',
            'in_progress': f'🟠 В работе (Админ: {get_admin_username(ticket["admin_id"])})',
            'closed': '🔴 Закрыт'
        }.get(ticket['status'], 'Неизвестен')

        message_text = (
            f"📄 **Тикет ID: {ticket['id']}**\n"
            f"Игрок: @{ticket['username']} ({ticket['user_id']})\n"
            f"Категория: **{ticket['category']}**\n"
            f"Ник в игре: {ticket['nick'] if ticket['nick'] != '-' else 'Не указан'}\n"
            f"Статус: **{status_text}**\n"
            f"\n**Описание:**\n_{ticket['description']}_"
        )

        proofs = ticket['proofs']
        if proofs:
             message_text += f"\n\n📎 **Доказательства:** ({len(proofs)} шт.)"

        kb = get_ticket_details_markup(ticket, cid)

        try:
            bot.edit_message_text(message_text, cid, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            bot.send_message(cid, message_text, reply_markup=kb, parse_mode="Markdown")

        if proofs:
            for file_id in proofs:
                try:
                    bot.send_photo(cid, file_id)
                except Exception as e:
                    print(f"Error sending proof: {e}")
                    bot.send_message(cid, f"❌ Не удалось отправить доказательство. ID: `{file_id}`")

        return

    # 5. Обработка взятия тикета в работу
    if data.startswith("take_ticket_"):
        ticket_id = int(data.split("_")[2])
        if take_ticket(ticket_id, cid):
            bot.send_message(cid, f"✅ Вы взяли тикет ID **{ticket_id}** в работу. Можете начать чат с игроком или ответить.", parse_mode="Markdown", reply_markup=admin_menu())
            ticket = get_ticket(ticket_id)
            if ticket:
                kb = get_ticket_details_markup(ticket, cid)
                try:
                    bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=kb)
                except Exception:
                    pass
        else:
            ticket = get_ticket(ticket_id)
            if ticket and ticket['admin_id']:
                admin_name = get_admin_username(ticket['admin_id'])
                bot.send_message(cid, f"❌ Тикет ID **{ticket_id}** уже взят в работу администратором {admin_name}.", parse_mode="Markdown", reply_markup=admin_menu())
            else:
                bot.send_message(cid, f"❌ Тикет ID **{ticket_id}** уже не 'open'.", parse_mode="Markdown", reply_menu=admin_menu())
        return

    # 6. Обработка закрытия тикета прямо из списка
    if data.startswith("close_ticket_list_"):
        ticket_id = int(data.split("_")[3])

        close_ticket(ticket_id, cid) 

        show_tickets_list(cid, call.message.message_id) 

        return

    # 7. Обработка закрытия тикета (из меню просмотра)
    if data.startswith("close_ticket_"):
        ticket_id = int(data.split("_")[2])
        close_ticket(ticket_id, cid)

        try:
             bot.edit_message_text(f"✅ Тикет ID **{ticket_id}** закрыт администратором.", cid, call.message.message_id, parse_mode="Markdown", reply_markup=types.InlineKeyboardMarkup())
        except Exception:
             bot.send_message(cid, f"✅ Тикет ID **{ticket_id}** закрыт администратором.", parse_mode="Markdown", reply_markup=admin_menu())
        return

# =====================
# Запуск бота
# =====================
if __name__ == "__main__":
    # 1. Запуск веб-сервера в отдельном потоке (для Replit 24/7)
    t = Thread(target=run_flask_server)
    t.start()

    # 2. Запуск Telegram-бота
    print("Bot started...")
    # !!! ИСПРАВЛЕННАЯ СТРОКА: Удалил clean_up_old_updates !!!
    bot.infinity_polling(skip_pending=True)