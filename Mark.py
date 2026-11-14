import logging
import asyncio
import sqlite3
import os
import re
import random
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberUpdated
from aiogram.exceptions import TelegramBadRequest
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import (UserAlreadyParticipantError, InviteRequestSentError,
                             SessionPasswordNeededError, InviteHashExpiredError, FloodWaitError)

# ===== الإعدادات =====
BOT_TOKEN = '9202617560:AAGcqKRxmc00h_yilzfqNAL8wU1QrzmoF0U'
API_ID = 31684349
API_HASH = 'b4ac0120b531eaf94244c1b374139639'

DEV_NAME = "MaRk"
DEV_USERNAME = "xxjxrx"
DEV_CHANNEL = "@xxmxsx"

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_yozrat.db") 

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

clients = {}

def db_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_monitoring (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_chat_id INTEGER,
            monitor_link TEXT,
            monitor_chat_id INTEGER,
            group_name TEXT,
            UNIQUE(target_chat_id, monitor_chat_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_usernames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            target_chat_id INTEGER NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(username, target_chat_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            target_chat_id INTEGER PRIMARY KEY,
            photo_only INTEGER DEFAULT 0,
            vip_only INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_joins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_chat_id INTEGER NOT NULL,
            monitor_link TEXT,
            group_name TEXT,
            hash_str TEXT,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS assistants (
            phone TEXT PRIMARY KEY,
            session_string TEXT,
            is_active INTEGER DEFAULT 0,
            name TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

class BotStates(StatesGroup):
    waiting_for_monitor_link = State()
    waiting_for_assistant_phone = State()
    waiting_for_assistant_code = State()
    waiting_for_assistant_password = State()

async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=DEV_CHANNEL, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False

def main_menu_keyboard(target_chat_id):
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT photo_only, vip_only FROM settings WHERE target_chat_id = ?', (target_chat_id,))
    res = cursor.fetchone()
    conn.close()

    photo_only = res[0] if res else 0
    vip_only = res[1] if res and len(res) > 1 else 0

    if photo_only == 1:
        filter_text = "الفلترة: [بصورة شخصية فقط]"
    elif vip_only == 1:
        filter_text = "الفلترة: [يوزرات مميزة فقط ⭐]"
    else:
        filter_text = "الفلترة: [الجميع]"

    keyboard_rows = [
        [
            InlineKeyboardButton(text="الكروبات المخزنة والمجلدات", callback_data="section_storage", icon_custom_emoji_id="5944970130554359187"),
            InlineKeyboardButton(text="الإحصائيات", callback_data="section_stats", icon_custom_emoji_id="5328159848427189012")
        ],
        [InlineKeyboardButton(text="تفعيل المراقبة لهذا الكروب", callback_data="active_monitor", icon_custom_emoji_id="5776208830996159244")],
        [InlineKeyboardButton(text="📢 تاك للكل (بلشو خمط)", callback_data="tag_all_members", icon_custom_emoji_id="5776208830996159244")],
        [InlineKeyboardButton(text=filter_text, callback_data="toggle_filter", icon_custom_emoji_id="5287696755373124799")],
        [InlineKeyboardButton(text="إيقاف المراقبة (حذف نهائي)", callback_data="stop_monitoring", icon_custom_emoji_id="5251340630306612580")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

def is_vip_username(username: str) -> bool:
    if not username:
        return False
    if len(username) > 7:
        return False
    if re.search(r'(.)\1{2,}', username):
        return False
    if re.search(r'^(.)\1+$', username):
        return False
    return True

# ==========================================
# قسم المساعدين وإدارة قاعدة البيانات للمطور
# ==========================================

async def start_assistant_client(phone, session_string):
    client = None
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None

        clients[phone] = client
        await register_assistant_events(client)

        conn = db_conn()
        cursor = conn.cursor()
        cursor.execute('UPDATE assistants SET is_active = 1 WHERE phone = ?', (phone,))
        conn.commit()
        conn.close()
        return client
    except Exception as e:
        logging.exception(f"Error starting client {phone}: {e}")
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        return None

def get_assistants_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="تسجيل مساعد", callback_data="assistant_register"),
         InlineKeyboardButton(text="حذف مساعد", callback_data="assistant_delete")],
        [InlineKeyboardButton(text="حالة المساعدين", callback_data="assistant_status"),
         InlineKeyboardButton(text="ترسيخ المساعدين", callback_data="assistant_restart")],
        [InlineKeyboardButton(text="🗄️ إدارة قاعدة البيانات", callback_data="dev_db_management")],
        [InlineKeyboardButton(text="⬅️ رجوع", callback_data="back_to_main_private")]
    ])

def get_db_management_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 عرض إحصائيات الجداول", callback_data="db_stats")],
        [InlineKeyboardButton(text="🗑️ تفريغ سجل اليوزرات المرسلة", callback_data="db_clear_sent")],
        [InlineKeyboardButton(text="⚠️ تصفير قاعدة البيانات بالكامل", callback_data="db_full_reset")],
        [InlineKeyboardButton(text="⬅️ رجوع لقسم المساعد", callback_data="dev_panel")]
    ])

@dp.message(lambda message: message.text and message.text.strip() == 'قسم المساعد' and message.chat.type == 'private')
async def show_assistant_section(message: types.Message):
    if not (message.from_user.username and message.from_user.username.lower() == DEV_USERNAME.lower()):
        return

    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM assistants WHERE is_active = 1')
    active_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM assistants')
    total_count = cursor.fetchone()[0]
    conn.close()

    text = f"قسم المساعد وإدارة البوت\n\n• مساعدك النشط: `{active_count}/{total_count}`\n• يمكنك من هنا التحكم الكامل بالحسابات وقاعدة البيانات."
    await message.reply(text, reply_markup=get_assistants_keyboard(), parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == 'dev_db_management')
async def dev_db_management_menu(callback_query: types.CallbackQuery):
    if not (callback_query.from_user.username and callback_query.from_user.username.lower() == DEV_USERNAME.lower()):
        await callback_query.answer("❌ للمطور فقط!", show_alert=True)
        return
    await callback_query.answer()
    db_size = os.path.getsize(DB_PATH) / 1024 if os.path.exists(DB_PATH) else 0
    text = f"🗄️ **لوحة التحكم بقاعدة البيانات (SQLite)**\n\n حجم الملف الحالي: `{db_size:.2f} KB`\nاختر الإجراء المطلوب أدناه:"
    await callback_query.message.edit_text(text, reply_markup=get_db_management_keyboard(), parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == 'db_stats')
async def db_stats_action(callback_query: types.CallbackQuery):
    if not (callback_query.from_user.username and callback_query.from_user.username.lower() == DEV_USERNAME.lower()):
        return
    await callback_query.answer()
    conn = db_conn()
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) FROM group_monitoring')
    groups_count = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM sent_usernames')
    sent_count = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM bot_users')
    users_count = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM assistants')
    assistants_count = cursor.fetchone()[0]
    
    conn.close()
    
    text = (
        f"📊 **إحصائيات قاعدة البيانات التفصيلية:**\n\n"
        f"📁 الكروبات المخزنة والمراقبّة: `{groups_count}`\n"
        f"💎 اليوزرات المسجلة والمرسلة: `{sent_count}`\n"
        f"👤 مستخدمو البوت: `{users_count}`\n"
        f"📱 الحسابات المساعدة المسجلة: `{assistants_count}`"
    )
    await callback_query.message.edit_text(text, reply_markup=get_db_management_keyboard(), parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == 'db_clear_sent')
async def db_clear_sent_action(callback_query: types.CallbackQuery):
    if not (callback_query.from_user.username and callback_query.from_user.username.lower() == DEV_USERNAME.lower()):
        return
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM sent_usernames')
    conn.commit()
    conn.close()
    await callback_query.answer("✅ تم تفريغ سجل اليوزرات المرسلة بنجاح!", show_alert=True)
    await callback_query.message.edit_text("🗄️ **لوحة التحكم بقاعدة البيانات**\n\nتم مسح سجل اليوزرات المرسلة بنجاح.", reply_markup=get_db_management_keyboard(), parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == 'db_full_reset')
async def db_full_reset_action(callback_query: types.CallbackQuery):
    if not (callback_query.from_user.username and callback_query.from_user.username.lower() == DEV_USERNAME.lower()):
        return
    conn = db_conn()
    cursor = conn.cursor()
    for table in ['group_monitoring', 'sent_usernames', 'settings', 'pending_joins', 'bot_users', 'assistants']:
        cursor.execute(f'DELETE FROM {table}')
    try:
        cursor.execute("DELETE FROM sqlite_sequence")
    except Exception:
        pass
    conn.commit()
    conn.close()
    await callback_query.answer("⚠️ تم تصفير قاعدة البيانات بالكامل!", show_alert=True)
    await callback_query.message.edit_text("🗄️ **لوحة التحكم بقاعدة البيانات**\n\n⚠️ تم تصفير كافة الجداول بنجاح.", reply_markup=get_db_management_keyboard(), parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == 'assistant_register')
async def assistant_register_start(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await callback_query.message.edit_text(
        "تسجيل مساعد\n\n- ارسل رقم المساعد الان:-\n- او يمكن ارسال جلسه بايروجرام جاهزه.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="( إلغاء )", callback_data="assistant_cancel")]])
    )
    await state.set_state(BotStates.waiting_for_assistant_phone)

@dp.message(BotStates.waiting_for_assistant_phone)
async def process_assistant_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()

    if len(phone) > 100:
        client = await start_assistant_client(f"session_{random.randint(1000, 9999)}", phone)
        if client:
            try:
                me = await client.get_me()
                key = f"session_{me.id}"
                clients[key] = client
                conn = db_conn()
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT OR REPLACE INTO assistants (phone, session_string, is_active, name) VALUES (?, ?, 1, ?)',
                    (key, phone, f"{me.first_name} {me.last_name or ''}".strip())
                )
                conn.commit()
                conn.close()
                await message.reply("✅ : تم إضافة المساعد للبوت بنجاح .", reply_markup=get_assistants_keyboard())
            except Exception as e:
                logging.exception("session parse error")
                await message.reply(f"⚠️ خطأ: {e}")
            await state.clear()
        else:
            await message.reply("⚠️ الجلسة غير صالحة.")
        return

    if phone.startswith('+') and phone[1:].isdigit():
        await state.update_data(phone=phone)
        await state.set_state(BotStates.waiting_for_assistant_code)

        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            sent_code = await client.send_code_request(phone)
            await state.update_data(phone_code_hash=sent_code.phone_code_hash)
            await state.update_data(client_session=client.session.save())
            await client.disconnect()
            
            cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="( إلغاء )", callback_data="assistant_cancel")]])
            await message.reply(
                f"{phone}\n\n: تم ارسال الكود لتطبيق تيليجرام (بالمحادثة الرسمية من Telegram) على جهاز مسجل بيه الحساب ، مو SMS\n"
                f": ارسله بمسافات بين الارقام ، مثل: 1 2 3 4 5",
                reply_markup=cancel_kb
            )
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            await message.reply(f"⚠️ خطأ في إرسال الكود: {e}")
            await state.clear()
    else:
        await message.reply("⚠️ رقم غير صالح. يرجى إرسال الرقم مع رمز الدولة (مثل +964...).")

@dp.message(BotStates.waiting_for_assistant_code)
async def process_assistant_code(message: types.Message, state: FSMContext):
    code = message.text.replace(" ", "")
    data = await state.get_data()
    phone = data.get('phone')

    client = TelegramClient(StringSession(data.get('client_session')), API_ID, API_HASH)
    try:
        await client.connect()
        await client.sign_in(phone, code, phone_code_hash=data.get('phone_code_hash'))
        session_string = client.session.save()
        me = await client.get_me()
        await client.disconnect()

        conn = db_conn()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO assistants (phone, session_string, is_active, name) VALUES (?, ?, 1, ?)',
            (phone, session_string, f"{me.first_name} {me.last_name or ''}".strip())
        )
        conn.commit()
        conn.close()

        cl = await start_assistant_client(phone, session_string)
        if cl:
            await message.reply("✅ : تم إضافة المساعد للبوت بنجاح .", reply_markup=get_assistants_keyboard())
        else:
            await message.reply("⚠️ تم الحفظ لكن فشل الاتصال، جرب 'ترسيخ المساعدين'.")
        await state.clear()
        
    except SessionPasswordNeededError as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        hint = getattr(e, 'password_hint', None)
        hint_text = f"\n: التلميح : {hint}" if hint else ""
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="( إلغاء )", callback_data="assistant_cancel")]])
        await message.reply(f"🔐 : الحساب عليه تحقق بخطوتين .\n: ارسل كلمة السر .{hint_text}", reply_markup=cancel_kb)
        await state.set_state(BotStates.waiting_for_assistant_password)
        
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        await message.reply(f"⚠️ خطأ في الكود: {e}")
        await state.clear()

@dp.message(BotStates.waiting_for_assistant_password)
async def process_assistant_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    phone = data.get('phone')

    client = TelegramClient(StringSession(data.get('client_session')), API_ID, API_HASH)
    try:
        await client.connect()
        await client.sign_in(password=password)
        session_string = client.session.save()
        me = await client.get_me()
        await client.disconnect()

        conn = db_conn()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO assistants (phone, session_string, is_active, name) VALUES (?, ?, 1, ?)',
            (phone, session_string, f"{me.first_name} {me.last_name or ''}".strip())
        )
        conn.commit()
        conn.close()

        cl = await start_assistant_client(phone, session_string)
        if cl:
            await message.reply("✅ : تم إضافة المساعد للبوت بنجاح .", reply_markup=get_assistants_keyboard())
        else:
            await message.reply("⚠️ تم الحفظ لكن فشل الاتصال.")
        await state.clear()
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="( إلغاء )", callback_data="assistant_cancel")]])
        await message.reply(f"⚠️ كلمة سر خاطئة: {e}\n: أعد المحاولة أو اضغط إلغاء.", reply_markup=cancel_kb)

@dp.callback_query(lambda c: c.data == 'assistant_delete')
async def assistant_delete_menu(callback_query: types.CallbackQuery):
    await callback_query.answer()
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT phone, name FROM assistants')
    assistants = cursor.fetchall()
    conn.close()

    if not assistants:
        await callback_query.message.edit_text("لا يوجد مساعدين مسجلين لحذفهم.", reply_markup=get_assistants_keyboard())
        return

    buttons = [[InlineKeyboardButton(text=f"🗑️ {name} ({phone})", callback_data=f"del_ass_{phone}")] for phone, name in assistants]
    buttons.append([InlineKeyboardButton(text="⬅️ رجوع", callback_data="dev_panel")])
    await callback_query.message.edit_text("اختر المساعد المراد حذفه:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(lambda c: c.data.startswith('del_ass_'))
async def assistant_delete_execute(callback_query: types.CallbackQuery):
    await callback_query.answer()
    phone = callback_query.data.replace('del_ass_', '')

    if phone in clients:
        try:
            await clients[phone].disconnect()
        except Exception:
            pass
        del clients[phone]

    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM assistants WHERE phone = ?', (phone,))
    conn.commit()
    conn.close()

    await callback_query.message.edit_text("✅ تم حذف المساعد بنجاح.", reply_markup=get_assistants_keyboard())

@dp.callback_query(lambda c: c.data == 'assistant_status')
async def assistant_status(callback_query: types.CallbackQuery):
    await callback_query.answer()
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT phone, name, is_active FROM assistants')
    assistants = cursor.fetchall()
    conn.close()

    if not assistants:
        text = "لا يوجد مساعدين مسجلين حالياً."
    else:
        text = "حالة المساعدين:\n\n"
        for phone, name, is_active in assistants:
            status_icon = "🟢 متصل" if phone in clients else "🔴 غير متصل"
            text += f"👤 {name}\n📱 {phone}\nالحالة: {status_icon}\n\n"

    await callback_query.message.edit_text(text, reply_markup=get_assistants_keyboard())

@dp.callback_query(lambda c: c.data == 'assistant_restart')
async def assistant_restart(callback_query: types.CallbackQuery):
    await callback_query.answer("⏳ جاري ترسيخ المساعدين...", show_alert=True)
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT phone, session_string FROM assistants')
    assistants = cursor.fetchall()
    conn.close()

    success = 0
    for phone, session_string in assistants:
        if phone not in clients:
            cl = await start_assistant_client(phone, session_string)
            if cl:
                success += 1

    await callback_query.message.edit_text(f"✅ تم ترسيخ ({success}) مساعد بنجاح.", reply_markup=get_assistants_keyboard())

@dp.callback_query(lambda c: c.data == 'assistant_cancel')
async def assistant_cancel(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.clear()
    await callback_query.message.edit_text("تم الإلغاء.", reply_markup=get_assistants_keyboard())

# ==========================================
# أوامر المطور
# ==========================================

@dp.message(lambda message: message.text and message.text.strip() == 'تصفير' and message.chat.type == 'private')
async def dev_reset_database(message: types.Message):
    if not (message.from_user.username and message.from_user.username.lower() == DEV_USERNAME.lower()):
        return
    conn = db_conn()
    cursor = conn.cursor()
    for table in ['group_monitoring', 'sent_usernames', 'settings', 'pending_joins', 'bot_users', 'assistants']:
        cursor.execute(f'DELETE FROM {table}')
    try:
        cursor.execute("DELETE FROM sqlite_sequence")
    except Exception:
        pass
    conn.commit()
    conn.close()
    await message.reply("✅ **تم تصفير قاعدة البيانات بالكامل بنجاح!**")

@dp.message(lambda message: message.text and message.text.strip() == 'ايقاف' and message.chat.type == 'private')
async def dev_stop_assistants(message: types.Message):
    if not (message.from_user.username and message.from_user.username.lower() == DEV_USERNAME.lower()):
        return
    count = len(clients)
    for cl in list(clients.values()):
        try:
            await cl.disconnect()
        except Exception:
            pass
    clients.clear()
    await message.reply(f"🛑 **تم إيقاف وفصل جميع الحسابات المساعدة ({count} حساب).**")

@dp.message(lambda message: message.text and message.text.strip() == "المطور")
async def dev_info_command(message: types.Message):
    try:
        me = await bot.get_me()
        photos = await bot.get_user_profile_photos(me.id)
        caption = f"👑 **مطور البوت الأساسي**\n\n👤 الاسم: {DEV_NAME}\n🔗 المعرف: @{DEV_USERNAME}\n💎 القناة الرسمية: {DEV_CHANNEL}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="تواصل مع المطور 👨‍💻", url=f"https://t.me/{DEV_USERNAME}")]])
        if photos.total_count > 0:
            await message.reply_photo(photo=photos.photos[0][-1].file_id, caption=caption, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await message.reply(caption, reply_markup=keyboard, parse_mode="Markdown")
    except Exception:
        await message.reply(f"👑 المطور: {DEV_NAME}\n🔗 المعرف: @{DEV_USERNAME}")

@dp.my_chat_member()
async def bot_added_to_group(event: ChatMemberUpdated):
    if event.new_chat_member.status in ['member', 'administrator'] and event.old_chat_member.status in ['left', 'kicked', 'restricted']:
        chat_id = event.chat.id
        try:
            link = await bot.create_chat_invite_link(chat_id=chat_id, member_limit=1)
            invite_link = link.invite_link
            if '+' in invite_link:
                hash_str = invite_link.split('+')[-1]
                for cl in clients.values():
                    try:
                        await cl(ImportChatInviteRequest(hash_str))
                        break
                    except FloodWaitError:
                        continue
                    except Exception:
                        continue
            await bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=invite_link)
            await bot.send_message(chat_id, "✅ **تم إضافة حساب المساعد تلقائياً** لتسهيل التاك والمراقبة!")
        except Exception as e:
            logging.exception(f"Auto-join error: {e}")

@dp.message(lambda message: message.text and message.text.strip() == '/start' and message.chat.type == 'private')
async def start_private_command(message: types.Message):
    user_id = message.from_user.id
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO bot_users (user_id, first_name, username) VALUES (?, ?, ?)', (user_id, message.from_user.first_name, message.from_user.username))
    conn.commit()
    conn.close()

    if not await check_subscription(user_id):
        sub_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="اشتري / اشترك بالقناة 🔔", url=f"https://t.me/{DEV_CHANNEL.replace('@', '')}")],
            [InlineKeyboardButton(text="تحقق من الاشتراك ✅", callback_data="check_sub")]
        ])
        await message.reply(f"⚠️ **عذراً عزيزي!**\n\nيجب عليك الاشتراك في قناة البوت الرسمية أولاً:\n🔗 {DEV_CHANNEL}", reply_markup=sub_keyboard)
        return

    bot_info = await bot.get_me()
    keyboard_rows = [[InlineKeyboardButton(text="اضف البوت الى مقرك وبلش خمط", url=f"https://t.me/{bot_info.username}?startgroup=true", icon_custom_emoji_id="5776012417846745906")]]
    if message.from_user.username and message.from_user.username.lower() == DEV_USERNAME.lower():
        keyboard_rows.append([InlineKeyboardButton(text="👤 قسم المساعد وإدارة البوت", callback_data="dev_panel")])
    await message.reply(f"أهلاً بك يا عزيزي في بوت يوزرات الاحترافي.\n\nالمطور: {DEV_NAME} (@{DEV_USERNAME})", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows))

@dp.callback_query(lambda c: c.data == 'dev_panel')
async def dev_panel_callback(callback_query: types.CallbackQuery):
    if not (callback_query.from_user.username and callback_query.from_user.username.lower() == DEV_USERNAME.lower()):
        await callback_query.answer("❌ للمطور فقط!", show_alert=True)
        return
    await callback_query.answer()
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM assistants WHERE is_active = 1')
    active_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM assistants')
    total_count = cursor.fetchone()[0]
    conn.close()
    text = f"قسم المساعد وإدارة البوت\n\n• مساعدك النشط: `{active_count}/{total_count}`\n• يمكنك من هنا التحكم الكامل بالحسابات وقاعدة البيانات."
    await callback_query.message.edit_text(text, reply_markup=get_assistants_keyboard(), parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == 'check_sub')
async def verify_subscription(callback_query: types.CallbackQuery):
    if not await check_subscription(callback_query.from_user.id):
        await callback_query.answer("❌ لم تقم بالاشتراك في القناة بعد!", show_alert=True)
        return
    await callback_query.answer("✅ تم التحقق من اشتراكك بنجاح!", show_alert=True)
    bot_info = await bot.get_me()
    keyboard_rows = [[InlineKeyboardButton(text="اضف البوت الى مقرك وبلش خمط", url=f"https://t.me/{bot_info.username}?startgroup=true", icon_custom_emoji_id="5776012417846745906")]]
    if callback_query.from_user.username and callback_query.from_user.username.lower() == DEV_USERNAME.lower():
        keyboard_rows.append([InlineKeyboardButton(text="👤 قسم المساعد وإدارة البوت", callback_data="dev_panel")])
    await callback_query.message.edit_text(text="أهلاً بك يا عزيزي في بوت يوزرات.", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows))

@dp.message(lambda message: message.text and message.text.strip() == 'يوزرات')
async def send_menu_on_keyword(message: types.Message):
    if message.chat.type == 'private': return
    if not await check_subscription(message.from_user.id):
        await message.reply(f"⚠️ عزيزي `{message.from_user.first_name}`، يجب عليك الاشتراك في قناة البوت ({DEV_CHANNEL}) أولاً.")
        return
    try:
        bot_member = await bot.get_chat_member(message.chat.id, (await bot.get_me()).id)
        if bot_member.status not in ['administrator', 'creator']:
            await message.reply("⚠️ **يجب أن يكون البوت مشرفاً (أدمن)** لكي تعمل لوحة التحكم والخمط بشكل صحيح!")
            return
    except Exception:
        pass
    await message.reply("⚡ **لوحة تحكم بوت اليوزرات الاحترافي**", reply_markup=main_menu_keyboard(message.chat.id))

@dp.callback_query(lambda c: c.data == 'tag_all_members')
async def process_tag_all(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    await callback_query.answer("⏳ جاري جمع الأعضاء وتاك الكل...", show_alert=False)
    try:
        participants = []
        for cl in clients.values():
            try:
                entity = await cl.get_entity(chat_id)
                participants = await cl.get_participants(entity)
                if participants: break
            except FloodWaitError:
                continue
            except Exception:
                continue
        if not participants:
            await callback_query.message.reply("❌ لم يتم العثور على أعضاء عبر الحسابات المساعدة.")
            return
        mentions = [f"<a href='tg://user?id={user.id}'>{(user.first_name or 'عضو').replace('<', '').replace('>', '')}</a>" for user in participants if not user.bot]
        for i in range(0, len(mentions), 35):
            chunk = mentions[i:i + 35]
            try:
                await bot.send_message(chat_id, "🔥 **بلشو خمط يا شباب!** 🚀\n\n" + " ".join(chunk), parse_mode="HTML")
            except TelegramBadRequest:
                pass
            await asyncio.sleep(1.5)
    except Exception:
        await callback_query.message.reply("⚠️ تأكد من أن حساب المساعد متواجد داخل المجموعة.")

@dp.callback_query(lambda c: c.data == 'toggle_filter')
async def toggle_filter(callback_query: types.CallbackQuery):
    target_chat_id = callback_query.message.chat.id
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT photo_only, vip_only FROM settings WHERE target_chat_id = ?', (target_chat_id,))
    res = cursor.fetchone()
    photo_only = res[0] if res else 0
    vip_only = res[1] if res and len(res) > 1 else 0

    if photo_only == 0 and vip_only == 0:
        new_photo, new_vip, status_msg = 1, 0, "🖼️ التفعيل: بصورة شخصية فقط!"
    elif photo_only == 1:
        new_photo, new_vip, status_msg = 0, 1, "⭐ التفعيل: يوزرات مميزة فقط (نظيفة)!"
    else:
        new_photo, new_vip, status_msg = 0, 0, "👥 التفعيل: الجميع!"

    cursor.execute('INSERT OR IGNORE INTO settings (target_chat_id, photo_only, vip_only) VALUES (?, ?, ?)', (target_chat_id, new_photo, new_vip))
    cursor.execute('UPDATE settings SET photo_only = ?, vip_only = ? WHERE target_chat_id = ?', (new_photo, new_vip, target_chat_id))
    conn.commit()
    conn.close()
    await callback_query.answer(status_msg, show_alert=True)
    await callback_query.message.edit_reply_markup(reply_markup=main_menu_keyboard(target_chat_id))

@dp.callback_query(lambda c: c.data == 'section_stats')
async def process_stats_section(callback_query: types.CallbackQuery):
    await callback_query.answer()
    target_chat_id = callback_query.message.chat.id
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM sent_usernames WHERE target_chat_id = ?', (target_chat_id,))
    total = cursor.fetchone()[0]
    conn.close()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ رجوع للقائمة", callback_data="back_to_main", icon_custom_emoji_id="5774029646194547127")]])
    await callback_query.message.edit_text(text=f"📊 **إحصائيات الخمط لهذه المجموعة:**\n\n💎 إجمالي اليوزرات المسجلة: `{total}`", reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == 'section_storage')
async def process_storage_section(callback_query: types.CallbackQuery):
    await callback_query.answer()
    target_chat_id = callback_query.message.chat.id
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT monitor_chat_id, group_name FROM group_monitoring WHERE target_chat_id = ?', (target_chat_id,))
    rows = cursor.fetchall()
    conn.close()

    keyboard_buttons = []
    text = "📁 **قسم المجلدات والكروبات المخزنة:**\n\nاختر الكروب أدناه:\n"
    if not rows:
        text += "❌ لا توجد كروبات مراقبة مرتبطة بهذا الكروب حالياً."
    else:
        for (m_chat_id, g_name) in rows:
            keyboard_buttons.append([InlineKeyboardButton(text=f"📁 {g_name or 'كروب بدون اسم'}", callback_data=f"folder_group_{m_chat_id}", icon_custom_emoji_id="5944970130554359187")])
    keyboard_buttons.append([InlineKeyboardButton(text="⬅️ رجوع للقائمة", callback_data="back_to_main", icon_custom_emoji_id="5774029646194547127")])
    await callback_query.message.edit_text(text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons), disable_web_page_preview=True)

@dp.callback_query(lambda c: c.data and c.data.startswith('folder_group_'))
async def process_folder_group_click(callback_query: types.CallbackQuery):
    await callback_query.answer()
    target_chat_id = callback_query.message.chat.id
    try:
        m_chat_id = int(callback_query.data.split('_')[2])
    except Exception:
        return
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT group_name FROM group_monitoring WHERE target_chat_id = ? AND monitor_chat_id = ?', (target_chat_id, m_chat_id))
    group_info = cursor.fetchone()
    conn.close()
    if not group_info:
        await callback_query.answer("⚠️ عذراً، لم يتم العثور على بيانات هذا الكروب.", show_alert=True)
        return
    text = f"📂 **مجلد الكروب: {group_info[0]}**\n\n✅ الخمط شغال حالياً."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ رجوع لقائمة المجلدات", callback_data="section_storage", icon_custom_emoji_id="5774029646194547127")]])
    await callback_query.message.edit_text(text=text, reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == 'active_monitor')
async def ask_for_monitor_link(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="إلغاء", callback_data="back_to_main", icon_custom_emoji_id="5774029646194547127")]])
    await callback_query.message.edit_text(text="👁️ **تفعيل المراقبة**\n\nأرسل رابط الكروب المراد مراقبته (رابط خاص أو عام):", reply_markup=keyboard)
    await state.set_state(BotStates.waiting_for_monitor_link)

# زر إيقاف المراقبة وحذف البيانات (النهائي)
@dp.callback_query(lambda c: c.data == 'stop_monitoring')
async def stop_monitoring_group(callback_query: types.CallbackQuery, state: FSMContext):
    target_chat_id = callback_query.message.chat.id
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM group_monitoring WHERE target_chat_id = ?', (target_chat_id,))
    cursor.execute('DELETE FROM sent_usernames WHERE target_chat_id = ?', (target_chat_id,))
    cursor.execute('DELETE FROM pending_joins WHERE target_chat_id = ?', (target_chat_id,))
    cursor.execute('DELETE FROM settings WHERE target_chat_id = ?', (target_chat_id,))
    conn.commit()
    conn.close()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="القائمة الرئيسية", callback_data="back_to_main", icon_custom_emoji_id="5774029646194547127")]])
    await callback_query.answer("🛑 تم حذف وإيقاف المراقبة نهائياً!", show_alert=True)
    await callback_query.message.edit_text(text="🛑 **تم إيقاف المراقبة وحذف كافة البيانات المرتبطة.**", reply_markup=keyboard)
    await state.clear()

async def _activate_monitoring(target_chat_id, monitor_link, entity, group_name):
    try:
        peer_id_val = utils.get_peer_id(entity)
        conn = db_conn()
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO group_monitoring (target_chat_id, monitor_link, monitor_chat_id, group_name) VALUES (?, ?, ?, ?)', (target_chat_id, monitor_link, peer_id_val, group_name))
        conn.commit()
        conn.close()
        keyboard_stop = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="إيقاف المراقبة", callback_data="stop_monitoring", icon_custom_emoji_id="5251340630306612580")]])
        try:
            await bot.send_message(target_chat_id, f"✅ **تمت الموافقة على طلب الانضمام!**\n\n🔥 بدأ الخمط الآن في: **{group_name}**", reply_markup=keyboard_stop)
        except TelegramBadRequest:
            pass
    except Exception as e:
        logging.exception(f"_activate_monitoring error: {e}")

async def poll_pending_joins():
    while True:
        await asyncio.sleep(3)
        try:
            conn = db_conn()
            cursor = conn.cursor()
            cursor.execute('SELECT id, target_chat_id, monitor_link, hash_str, username, group_name FROM pending_joins')
            rows = cursor.fetchall()
            conn.close()

            for row in rows:
                row_id, target_chat_id, monitor_link, hash_str, username, pending_name = row
                entity = None
                group_name = pending_name or "كروب مراقبة"

                for cl in clients.values():
                    try:
                        if hash_str:
                            try:
                                updates = await cl(ImportChatInviteRequest(hash_str))
                                if updates.chats:
                                    entity = updates.chats[0]
                                    group_name = getattr(entity, 'title', group_name)
                                    break
                            except UserAlreadyParticipantError:
                                res = await cl(CheckChatInviteRequest(hash_str))
                                chat_obj = getattr(res, 'chat', None)
                                if chat_obj:
                                    entity = chat_obj
                                    group_name = getattr(entity, 'title', group_name)
                                    break
                        elif username:
                            try:
                                await cl(JoinChannelRequest(username))
                            except UserAlreadyParticipantError:
                                pass
                            entity = await cl.get_entity(username)
                            group_name = getattr(entity, 'title', group_name)
                            break
                    except FloodWaitError:
                        continue
                    except Exception:
                        continue

                if entity:
                    conn2 = db_conn()
                    cur2 = conn2.cursor()
                    cur2.execute('DELETE FROM pending_joins WHERE id = ?', (row_id,))
                    conn2.commit()
                    conn2.close()
                    await _activate_monitoring(target_chat_id, monitor_link, entity, group_name)
        except Exception as e:
            logging.exception(f"poll_pending_joins error: {e}")

async def register_assistant_events(cl: TelegramClient):
    @cl.on(events.NewMessage(incoming=True))
    async def monitor_groups(event):
        if not (event.is_group or event.is_channel): return
        sender = await event.get_sender()
        if not sender or not getattr(sender, 'username', None) or getattr(sender, 'bot', False): return

        username = sender.username
        profile_link = f"https://t.me/{username}"
        cache_key = username.lower()

        conn = db_conn()
        cursor = conn.cursor()
        try:
            chat_id_val = event.chat_id
            peer_id_val = utils.get_peer_id(event.chat) if hasattr(event, 'chat') and event.chat else chat_id_val
            possible_ids = list({chat_id_val, -chat_id_val, peer_id_val, -peer_id_val})
            if chat_id_val > 0:
                possible_ids.append(-int(f"-100{chat_id_val}" if not str(chat_id_val).startswith("100") else chat_id_val))

            placeholders = ','.join(['?'] * len(possible_ids))
            cursor.execute(f'SELECT target_chat_id FROM group_monitoring WHERE monitor_chat_id IN ({placeholders})', tuple(possible_ids))
            rows = cursor.fetchall()

            for row in rows:
                target_chat_id = row[0]
                cursor.execute('SELECT photo_only, vip_only FROM settings WHERE target_chat_id = ?', (target_chat_id,))
                sett = cursor.fetchone()
                photo_only = sett[0] if sett else 0
                vip_only = sett[1] if sett and len(sett) > 1 else 0

                if photo_only == 1 and not getattr(sender, 'photo', None): continue
                if vip_only == 1 and not is_vip_username(username): continue

                cursor.execute('SELECT 1 FROM sent_usernames WHERE username = ? AND target_chat_id = ? LIMIT 1', (cache_key, target_chat_id))
                if cursor.fetchone(): 
                    continue

                try:
                    cursor.execute('INSERT INTO sent_usernames (username, target_chat_id) VALUES (?, ?)', (cache_key, target_chat_id))
                    conn.commit()
                except sqlite3.IntegrityError:
                    continue

                try:
                    await bot.send_message(chat_id=target_chat_id, text=profile_link)
                except TelegramBadRequest as e:
                    if "chat not found" in str(e):
                        cursor.execute('DELETE FROM group_monitoring WHERE target_chat_id = ?', (target_chat_id,))
                        conn.commit()
        finally:
            conn.close()

@dp.message(BotStates.waiting_for_monitor_link)
async def join_and_save_monitor_group(message: types.Message, state: FSMContext):
    link = message.text.strip() if message.text else ""
    target_chat_id = message.chat.id
    await message.reply("⏳ **جاري فحص الرابط وتفعيل المراقبة عبر الحسابات المساعدة...**")

    try:
        entity = None
        group_name = "كروب تليجرام"
        hash_str = None
        username_part = None
        join_requested = False
        error_msg = None

        if '+' in link or 'joinchat' in link:
            parts = link.split('/')
            for p in parts:
                if '+' in p:
                    hash_str = p.replace('+', '').split('?')[0]
                    break
            if not hash_str:
                hash_str = link.split('+')[-1].split('/')[0].split('?')[0]

            if not clients:
                await message.reply("⚠️ لا يوجد حساب مساعد متصل حالياً.")
                await state.clear()
                return

            for cl in clients.values():
                try:
                    updates = await cl(ImportChatInviteRequest(hash_str))
                    if updates.chats:
                        entity = updates.chats[0]
                        group_name = getattr(entity, 'title', 'كروب مراقبة')
                        break
                except UserAlreadyParticipantError:
                    try:
                        res = await cl(CheckChatInviteRequest(hash_str))
                        if hasattr(res, 'chat') and res.chat:
                            entity = res.chat
                            group_name = getattr(entity, 'title', 'كروب مراقبة')
                            break
                        else:
                            error_msg = "الحساب داخل الكروب، لكن تيليجرام رفض إعطاء بيانات الكروب."
                    except FloodWaitError as e:
                        error_msg = f"FloodWait: {e.seconds} ثانية"
                        continue
                    except Exception as e:
                        error_msg = f"خطأ في جلب البيانات: {e}"
                        continue
                except InviteRequestSentError:
                    join_requested = True
                    break
                except FloodWaitError as e:
                    error_msg = f"FloodWait: {e.seconds} ثانية"
                    continue
                except Exception as e:
                    error_msg = str(e)
                    continue
        else:
            if 't.me/' in link:
                username_part = link.split('t.me/')[-1].replace('@', '').split('/')[0].split('?')[0]
            else:
                username_part = link.replace('@', '').strip()
            group_name = f"@{username_part}"
            
            for cl in clients.values():
                try:
                    await cl(JoinChannelRequest(username_part))
                    entity = await cl.get_entity(username_part)
                    group_name = getattr(entity, 'title', group_name)
                    break
                except UserAlreadyParticipantError:
                    entity = await cl.get_entity(username_part)
                    group_name = getattr(entity, 'title', group_name)
                    break
                except InviteRequestSentError:
                    join_requested = True
                    break
                except FloodWaitError as e:
                    error_msg = f"FloodWait: {e.seconds} ثانية"
                    continue
                except Exception as e:
                    error_msg = str(e)
                    continue

        if join_requested:
            conn = db_conn()
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM pending_joins WHERE target_chat_id = ? AND monitor_link = ?', (target_chat_id, link))
            if not cursor.fetchone():
                cursor.execute('INSERT INTO pending_joins (target_chat_id, monitor_link, group_name, hash_str, username) VALUES (?, ?, ?, ?, ?)', (target_chat_id, link, group_name, hash_str, username_part))
                conn.commit()
            conn.close()

            keyboard_stop = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="إيقاف المراقبة", callback_data="stop_monitoring", icon_custom_emoji_id="5251340630306612580")]])
            await message.reply(f"📨 **تم إرسال طلب انضمام للحساب المساعد إلى ({group_name})**\n\n⏳ سيبدأ الخمط تلقائياً بمجرد موافقة إدارة الكروب.", reply_markup=keyboard_stop)
            await state.clear()
            return

        if entity is None:
            err_text = f"\n\n**السبب:** {error_msg}" if error_msg else ""
            await message.reply(f"⚠️ لم يتم التعرف على الكروب أو واجهت الحسابات قيود مؤقتة (FloodWait). تأكد أن الرابط صحيح.{err_text}")
            await state.clear()
            return

        monitor_chat_id = utils.get_peer_id(entity)
        conn = db_conn()
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO group_monitoring (target_chat_id, monitor_link, monitor_chat_id, group_name) VALUES (?, ?, ?, ?)', (target_chat_id, link, monitor_chat_id, group_name))
        conn.commit()
        conn.close()

        keyboard_stop = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="إيقاف المراقبة", callback_data="stop_monitoring", icon_custom_emoji_id="5251340630306612580")]])
        await message.reply(f"🔥 **تم تفعيل المراقبة بنجاح لـ ({group_name}) وبدأ الخمط فوراً!**", reply_markup=keyboard_stop)
        await state.clear()

    except Exception as e:
        await message.reply(f"⚠️ تعذر تفعيل المراقبة بسبب خطأ تقني: {str(e)[:150]}")
        await state.clear()

@dp.callback_query(lambda c: c.data == 'back_to_main')
async def back_to_main(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.clear()
    await callback_query.message.edit_text(text=f"⚡ **لوحة تحكم بوت اليوزرات الاحترافي**\n\nالمطور: {DEV_NAME} (@{DEV_USERNAME})", reply_markup=main_menu_keyboard(callback_query.message.chat.id))

@dp.callback_query(lambda c: c.data == 'back_to_main_private')
async def back_to_main_private(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.clear()
    bot_info = await bot.get_me()
    keyboard_rows = [[InlineKeyboardButton(text="اضف البوت الى مقرك وبلش خمط", url=f"https://t.me/{bot_info.username}?startgroup=true", icon_custom_emoji_id="5776012417846745906")]]
    if callback_query.from_user.username and callback_query.from_user.username.lower() == DEV_USERNAME.lower():
        keyboard_rows.append([InlineKeyboardButton(text="👤 قسم المساعد وإدارة البوت", callback_data="dev_panel")])
    await callback_query.message.edit_text(text=f"أهلاً بك يا عزيزي في بوت يوزرات الاحترافي.\n\nالمطور: {DEV_NAME} (@{DEV_USERNAME})", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows))

async def main():
    conn = db_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT phone, session_string FROM assistants WHERE is_active = 1')
    saved_assistants = cursor.fetchall()
    conn.close()

    for phone, session_string in saved_assistants:
        try:
            await start_assistant_client(phone, session_string)
        except Exception as e:
            logging.exception(f"Failed to start {phone}: {e}")

    asyncio.create_task(poll_pending_joins())
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == '__main__':
    asyncio.run(main())
