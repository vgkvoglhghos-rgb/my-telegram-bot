import asyncio
import os
import sqlite3
import logging
import time
import shutil
import threading
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from collections import defaultdict

from telethon import TelegramClient, events, Button
from telethon.tl.functions.messages import ReportRequest, ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.stories import (
    ReportRequest as StoryReportRequest,
    GetStoriesByIDRequest,
)
from telethon.tl.types import (
    ReportResultReported,
    ReportResultChooseOption,
    ReportResultAddComment,
    InputPeerChannel,
    InputPeerUser,
    MessageEntityCustomEmoji,
)
from telethon.errors import (
    FloodWaitError,
    MessageNotModifiedError,
    SessionPasswordNeededError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
    AuthKeyUnregisteredError,
    AuthKeyInvalidError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    InterdcCallErrorError,
    PeerIdInvalidError,
)
from telethon.sessions import StringSession

BOT_TOKEN = "8970679243:AAGgSp1ArXUHIQsRUmifcM-sHOeZOZnxNQw"
API_ID = 37775409
API_HASH = "f9f95dc4b1a9ff5fbbe20a34c18fa26f"
DEVELOPER_ID = 6426911761
MAX_REPORTS = 1000
MAX_RETRY_ATTEMPTS = 3
CONNECTION_TIMEOUT = 30
MAX_ACCOUNTS = 50
SUBSCRIPTION_CHANNEL = "@xxmxsx"
SLEEP_BETWEEN_REPORTS = 6
MAX_MESSAGE_LINKS = 100
MAX_COMMENT_LENGTH = 500
MAX_SESSION_LENGTH = 10000


def get_iraq_time():
    return datetime.utcnow() + timedelta(hours=3)


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

os.makedirs("sessions", exist_ok=True)
os.makedirs("backups", exist_ok=True)


PREMIUM_EMOJIS = {
    "BACK": 5210952531676504517,
    "OK": 5206607081334906820,
    "ERR": 5420323339723881652,
    "HELLO1": 5244707710447924388,
    "HELLO2": 5247161953545106438,
    "LINK": 5305265301917549162,
    "PERCENT": 5341498088408234504,
    "CHANNEL": 5424818078833715060,
}

FALLBACKS = {
    "BACK": "🔙", "OK": "✅", "ERR": "⚠️", "HELLO1": "👋", "HELLO2": "🎉",
    "LINK": "📎", "PERCENT": "💯", "CHANNEL": "📢",
}


def utf16_len(s: str) -> int:
    return len(s.encode('utf-16-le')) // 2


def build_entities(text: str):
    entities = []
    for key, emoji_id in PREMIUM_EMOJIS.items():
        fallback = FALLBACKS[key]
        start = 0
        while True:
            idx = text.find(fallback, start)
            if idx == -1:
                break
            entities.append(
                MessageEntityCustomEmoji(
                    offset=utf16_len(text[:idx]),
                    length=utf16_len(fallback),
                    document_id=emoji_id
                )
            )
            start = idx + len(fallback)
    return entities


def premium(text: str):
    return text, build_entities(text)


async def send_premium(client, chat_id, text, buttons=None, **kwargs):
    t, e = premium(text)
    return await client.send_message(chat_id, t, buttons=buttons, formatting_entities=e, **kwargs)


async def respond_premium(event, text, buttons=None, **kwargs):
    t, e = premium(text)
    return await event.respond(t, buttons=buttons, formatting_entities=e, **kwargs)


async def edit_premium(event, text, buttons=None, **kwargs):
    t, e = premium(text)
    try:
        await event.edit(t, buttons=buttons, formatting_entities=e, **kwargs)
    except MessageNotModifiedError:
        pass


class DatabasePool:
    def __init__(self):
        self.db_file = "bot_database.db"
        self._local = threading.local()
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_file, timeout=60)
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT,
            is_subscribed BOOLEAN DEFAULT 0, subscription_end TEXT,
            joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            phone_number TEXT, session_string TEXT,
            is_active BOOLEAN DEFAULT 1, account_name TEXT,
            proxy_index INTEGER DEFAULT -1,
            added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY, report_count INTEGER DEFAULT 5,
            target_chat TEXT, message_links TEXT,
            report_path TEXT DEFAULT "", report_text TEXT DEFAULT "",
            sleep_seconds INTEGER DEFAULT 6
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS story_settings (
            user_id INTEGER PRIMARY KEY,
            story_link TEXT,
            story_path TEXT DEFAULT "",
            story_text TEXT DEFAULT "",
            story_count INTEGER DEFAULT 5,
            story_sleep INTEGER DEFAULT 6
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS active_reports (
            user_id INTEGER PRIMARY KEY, is_reporting BOOLEAN DEFAULT 0
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS active_story_reports (
            user_id INTEGER PRIMARY KEY, is_reporting BOOLEAN DEFAULT 0
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY, value TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY, added_by INTEGER,
            added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        cursor.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('free_mode', '0')")
        cursor.execute("UPDATE users SET is_subscribed=0 WHERE is_subscribed=1 AND (subscription_end IS NULL OR subscription_end='')")
        cursor.execute("PRAGMA table_info(settings)")
        cols = [c[1] for c in cursor.fetchall()]
        if 'report_path' not in cols:
            cursor.execute("ALTER TABLE settings ADD COLUMN report_path TEXT DEFAULT ''")
        if 'report_text' not in cols:
            cursor.execute("ALTER TABLE settings ADD COLUMN report_text TEXT DEFAULT ''")
        if 'sleep_seconds' not in cols:
            cursor.execute("ALTER TABLE settings ADD COLUMN sleep_seconds INTEGER DEFAULT 6")
        cursor.execute("PRAGMA table_info(accounts)")
        acc_cols = [c[1] for c in cursor.fetchall()]
        if 'proxy_index' not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN proxy_index INTEGER DEFAULT -1")
        cursor.execute("PRAGMA table_info(story_settings)")
        story_cols = [c[1] for c in cursor.fetchall()]
        if 'story_sleep' not in story_cols:
            cursor.execute("ALTER TABLE story_settings ADD COLUMN story_sleep INTEGER DEFAULT 6")
        conn.commit()
        conn.close()
        logger.info("Database initialized")

    def _get_connection(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_file, timeout=60, check_same_thread=False, isolation_level=None)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=60000")
        return self._local.conn

    @contextmanager
    def get_connection(self):
        conn = self._get_connection()
        try:
            yield conn
        except Exception as e:
            logger.error(f"Connection error: {e}")
            raise

    def execute_query(self, query, params=None, fetch_one=False, fetch_all=False):
        max_retries = 10
        retry_count = 0
        while retry_count < max_retries:
            try:
                conn = self._get_connection()
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                if fetch_one:
                    r = cursor.fetchone(); conn.commit(); return r
                elif fetch_all:
                    r = cursor.fetchall(); conn.commit(); return r
                conn.commit(); return True
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    retry_count += 1
                    time.sleep(0.5 * (2 ** retry_count))
                    if hasattr(self._local, 'conn'):
                        try:
                            self._local.conn.close()
                        except:
                            pass
                        delattr(self._local, 'conn')
                    continue
                return None
            except Exception as e:
                logger.error(f"Query error: {e}")
                return None
        return None


db = DatabasePool()


def fix_chat_id(chat_id):
    s = str(chat_id)
    if s.startswith("-100"): return int(s)
    if s.startswith("-"): return int("-100" + s[1:])
    if s.isdigit(): return int("-100" + s)
    try:
        return int(s)
    except:
        return chat_id


def hide_phone(phone):
    if not phone: return "غير معروف"
    phone = str(phone)
    if len(phone) <= 6: return phone
    return f"{phone[:3]}*****{phone[-2:]}"


def is_admin(uid):
    if uid == DEVELOPER_ID: return True
    return db.execute_query("SELECT user_id FROM admins WHERE user_id=?", (uid,), fetch_one=True) is not None


def add_admin(uid, by):
    db.execute_query("INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?,?)", (uid, by))


def remove_admin(uid):
    db.execute_query("DELETE FROM admins WHERE user_id=?", (uid,))


def get_admins():
    return db.execute_query("SELECT user_id FROM admins", fetch_all=True) or []


def is_free_mode():
    r = db.execute_query("SELECT value FROM bot_settings WHERE key='free_mode'", fetch_one=True)
    return r and r[0] == '1'


def set_free_mode(enabled):
    db.execute_query("INSERT OR REPLACE INTO bot_settings (key,value) VALUES ('free_mode',?)", ('1' if enabled else '0',))


def is_subscribed(uid):
    if is_free_mode(): return True
    if is_admin(uid): return True
    r = db.execute_query("SELECT is_subscribed, subscription_end FROM users WHERE user_id=?", (uid,), fetch_one=True)
    if not r or r[0] != 1: return False
    if not r[1] or r[1] == "" or r[1] is None:
        db.execute_query("UPDATE users SET is_subscribed=0 WHERE user_id=?", (uid,))
        return False
    try:
        end = datetime.strptime(r[1], "%Y-%m-%d %H:%M:%S")
        if get_iraq_time() > end:
            db.execute_query("UPDATE users SET is_subscribed=0 WHERE user_id=?", (uid,))
            return False
        return True
    except:
        try:
            end = datetime.strptime(r[1], "%Y-%m-%d")
            if get_iraq_time() > end:
                db.execute_query("UPDATE users SET is_subscribed=0 WHERE user_id=?", (uid,))
                return False
            return True
        except:
            db.execute_query("UPDATE users SET is_subscribed=0 WHERE user_id=?", (uid,))
            return False


def get_all_accounts(uid):
    return db.execute_query("SELECT id, account_name, is_active, phone_number, proxy_index FROM accounts WHERE user_id=?", (uid,), fetch_all=True) or []


def get_settings(uid):
    return db.execute_query("SELECT * FROM settings WHERE user_id=?", (uid,), fetch_one=True)


def get_sleep_seconds(uid):
    s = get_settings(uid)
    if s and len(s) > 6 and s[6] is not None:
        try:
            return max(3, min(10, int(s[6])))
        except:
            return 6
    return 6


def validate_message_links(text):
    valid = []
    if not text: return []
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line: continue
        if 't.me/c/' in line:
            line = line.split('?')[0]
            parts = line.split('/')
            mid = parts[-1] if parts else ""
        elif 't.me/' in line:
            line = line.split('?')[0]
            parts = line.split('/')
            mid = parts[-1] if parts else ""
        elif line.isdigit():
            mid = line
        else:
            continue
        if mid.isdigit() and int(mid) > 0:
            if int(mid) not in valid:
                valid.append(int(mid))
        if len(valid) >= MAX_MESSAGE_LINKS: break
    return valid


def hide_username(u):
    if not u: return "غير محدد"
    if "t.me/+" in u or "t.me/joinchat/" in u: return "خاص (مخفي)"
    if "t.me/" in u:
        p = u.replace("https://", "").replace("http://", "").split("/")
        if len(p) >= 2: return f"@ {p[1].split('?')[0]}"
    if u.startswith("@"): return f"@ {u[1:]}"
    if u.startswith("-100"): return "خاص (مخفي)"
    return u


def save_report_path(user_id, path):
    path_list = [list(p) for p in path]
    path_json = json.dumps(path_list)
    s = get_settings(user_id)
    if s:
        db.execute_query("UPDATE settings SET report_path=? WHERE user_id=?", (path_json, user_id))
    else:
        db.execute_query("INSERT INTO settings (user_id, report_path) VALUES (?,?)", (user_id, path_json))


def load_report_path(user_id):
    s = get_settings(user_id)
    if not s or not s[4]:
        return []
    try:
        path_list = json.loads(s[4])
        return [bytes(p) for p in path_list]
    except:
        return []


def get_story_settings(uid):
    r = db.execute_query("SELECT * FROM story_settings WHERE user_id=?", (uid,), fetch_one=True)
    if not r:
        db.execute_query("INSERT INTO story_settings (user_id) VALUES (?)", (uid,))
        r = db.execute_query("SELECT * FROM story_settings WHERE user_id=?", (uid,), fetch_one=True)
    return r


def save_story_path(user_id, path):
    path_json = json.dumps([list(p) for p in path])
    get_story_settings(user_id)
    db.execute_query("UPDATE story_settings SET story_path=? WHERE user_id=?", (path_json, user_id))


def load_story_path(user_id):
    s = get_story_settings(user_id)
    if not s or not s[2]:
        return []
    try:
        return [bytes(p) for p in json.loads(s[2])]
    except:
        return []


def get_story_sleep(uid):
    s = get_story_settings(uid)
    if s and len(s) > 5 and s[5] is not None:
        try:
            return max(3, min(10, int(s[5])))
        except:
            return 6
    return 6


def parse_story_link(link):
    link = str(link).strip().split("?")[0]
    m = re.search(r't\.me/(?:c/)?([^/]+)/s/(\d+)', link)
    if m:
        return m.group(1), int(m.group(2))
    m = re.search(r't\.me/(?:c/)?([^/]+)/(\d+)', link)
    if m:
        return m.group(1), int(m.group(2))
    if link.isdigit():
        return None, int(link)
    return None, None


def parse_multiple_story_links(text):
    """يستخرج قائمة (link, peer_id, story_id) من نص متعدد الأسطر."""
    results = []
    if not text:
        return results
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        peer_id, story_id = parse_story_link(line)
        if story_id:
            results.append((line, peer_id, story_id))
    return results


async def resolve_story_peer(client, peer_identifier):
    if peer_identifier is None:
        return None, "لم يتم تحديد الناشر"
    peer_str = str(peer_identifier).strip()
    if peer_str.startswith("c/") or peer_str.isdigit():
        try:
            channel_id = int(peer_str.replace("c/", ""))
            if not str(channel_id).startswith("-100"):
                channel_id = int("-100" + str(channel_id))
            entity = await asyncio.wait_for(client.get_entity(channel_id), timeout=15)
            return entity, None
        except Exception as e:
            return None, f"فشل الوصول بـ ID: {str(e)[:60]}"
    if not peer_str.startswith("@"):
        peer_str = "@" + peer_str
    try:
        entity = await asyncio.wait_for(client.get_entity(peer_str), timeout=15)
        return entity, None
    except Exception as e:
        return None, f"الناشر محذوف أو غير موجود: {str(e)[:60]}"


def to_input_peer(entity):
    if hasattr(entity, 'access_hash'):
        if hasattr(entity, 'broadcast') or hasattr(entity, 'megagroup'):
            return InputPeerChannel(entity.id, entity.access_hash)
        else:
            return InputPeerUser(entity.id, entity.access_hash)
    return entity


async def safe_edit_message(msg, text, markup=None):
    try:
        if len(text) > 4000: text = text[:3990] + "..."
        t, e = premium(text)
        await msg.edit(t, buttons=markup, formatting_entities=e)
    except MessageNotModifiedError:
        pass
    except Exception as e:
        logger.warning(f"Edit error: {e}")


async def safe_stop_client(c):
    if c:
        try:
            if c.is_connected(): await c.disconnect()
        except:
            pass


async def connect_client_with_retry(client, max_retries=3):
    for attempt in range(max_retries):
        try:
            if client.is_connected(): return True
            await asyncio.wait_for(client.connect(), timeout=CONNECTION_TIMEOUT)
            return True
        except:
            if attempt == max_retries - 1: return False
            await asyncio.sleep(2 ** attempt)
    return False


def create_no_proxy_client(session_string):
    try:
        return TelegramClient(StringSession(session_string), API_ID, API_HASH, lang_code="en", system_lang_code="en")
    except:
        return None


async def verify_account_sync(session_string):
    c = None
    try:
        c = create_no_proxy_client(session_string)
        if not c: return False, "فشل إنشاء العميل"
        if not await connect_client_with_retry(c):
            await safe_stop_client(c); return False, "فشل الاتصال"
        me = await asyncio.wait_for(c.get_me(), timeout=15)
        if not me:
            await safe_stop_client(c); return False, "الجلسة غير صالحة"
        name = getattr(me, 'first_name', 'غير معروف')
        await safe_stop_client(c)
        return True, f"نشط ({name})"
    except AuthKeyUnregisteredError:
        await safe_stop_client(c); return False, "معطل - الجلسة غير مسجلة"
    except AuthKeyInvalidError:
        await safe_stop_client(c); return False, "معطل - مفتاح غير صالح"
    except SessionRevokedError:
        await safe_stop_client(c); return False, "معطل - الجلسة ملغية"
    except UserDeactivatedBanError:
        await safe_stop_client(c); return False, "محظور"
    except UserDeactivatedError:
        await safe_stop_client(c); return False, "معطل"
    except FloodWaitError as e:
        await safe_stop_client(c); return False, f"فلود - {e.seconds} ثانية"
    except Exception as e:
        await safe_stop_client(c); return False, f"خطأ - {str(e)[:50]}"


async def join_chat_via_link(client, invite_link):
    try:
        if not client.is_connected():
            if not await connect_client_with_retry(client): return None, "فشل الاتصال"
        if "t.me/+" in invite_link:
            h = invite_link.split("t.me/+")[1].split("?")[0]
        elif "t.me/joinchat/" in invite_link:
            h = invite_link.split("t.me/joinchat/")[1].split("?")[0]
        elif "t.me/" in invite_link:
            parts = invite_link.replace("https://", "").replace("http://", "").split("/")
            if len(parts) >= 2:
                username = parts[1].split('?')[0]
                entity = await client.get_entity(f"@{username}")
                return entity, None
        else:
            return None, "رابط غير صالح"
        try:
            result = await client(ImportChatInviteRequest(h))
            if hasattr(result, 'chats') and result.chats:
                return result.chats[0], None
            return None, "تم الانضمام"
        except UserAlreadyParticipantError:
            try:
                chat_info = await client(CheckChatInviteRequest(h))
                if hasattr(chat_info, 'chat'):
                    return chat_info.chat, None
            except:
                pass
            try:
                result = await client(ImportChatInviteRequest(h))
                if hasattr(result, 'chats') and result.chats:
                    return result.chats[0], None
            except:
                pass
            return None, "عضو بالفعل"
        except InviteHashExpiredError:
            return None, "الرابط منتهي"
        except InviteHashInvalidError:
            return None, "الرابط غير صالح"
        except Exception as e:
            return None, str(e)
    except Exception as e:
        return None, str(e)


async def get_messages_batch(client, chat_id, message_ids):
    try:
        if not client.is_connected():
            if not await connect_client_with_retry(client):
                return []
        chat_id = fix_chat_id(chat_id)
        for attempt in range(3):
            try:
                msgs = await asyncio.wait_for(
                    client.get_messages(chat_id, ids=message_ids),
                    timeout=CONNECTION_TIMEOUT
                )
                if msgs:
                    valid = [m for m in msgs if m and m.id and not getattr(m, 'deleted', False)]
                    if valid:
                        return valid
                    return []
                await asyncio.sleep(3)
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 60))
            except Exception as e:
                logger.warning(f"جلب الرسائل محاولة {attempt+1} فشل: {e}")
                await asyncio.sleep(2)
        return []
    except Exception as e:
        logger.error(f"Get messages error: {e}")
        return []


async def fetch_report_options(client, peer, message_ids, option=b'', comment=''):
    try:
        if not client.is_connected():
            if not await connect_client_with_retry(client):
                return {'type': 'error', 'message': 'فشل الاتصال'}
        if not message_ids:
            return {'type': 'error', 'message': 'لا توجد رسائل'}
        result = await client(ReportRequest(peer=peer, id=message_ids, option=option, message=comment))
        if isinstance(result, ReportResultReported):
            return {'type': 'reported'}
        elif isinstance(result, ReportResultChooseOption):
            opts = [{'text': getattr(o, 'text', '?'), 'option': getattr(o, 'option', b'')} for o in result.options]
            return {'type': 'choose', 'options': opts}
        elif isinstance(result, ReportResultAddComment):
            return {'type': 'comment', 'optional': getattr(result, 'optional', False)}
        return {'type': 'unknown'}
    except FloodWaitError as e:
        return {'type': 'error', 'message': f'فلود: {e.seconds} ثانية'}
    except Exception as e:
        return {'type': 'error', 'message': str(e)[:50]}


async def fetch_story_options(client, peer, story_id, option=b'', comment=''):
    try:
        if not client.is_connected():
            if not await connect_client_with_retry(client):
                return {'type': 'error', 'message': 'فشل الاتصال'}
        result = await client(StoryReportRequest(peer=peer, id=[story_id], option=option, message=comment))
        if isinstance(result, ReportResultReported):
            return {'type': 'reported'}
        elif isinstance(result, ReportResultChooseOption):
            opts = [{'text': getattr(o, 'text', '?'), 'option': getattr(o, 'option', b'')} for o in result.options]
            return {'type': 'choose', 'options': opts}
        elif isinstance(result, ReportResultAddComment):
            return {'type': 'comment', 'optional': getattr(result, 'optional', False)}
        return {'type': 'unknown'}
    except FloodWaitError as e:
        return {'type': 'error', 'message': f'فلود: {e.seconds} ثانية'}
    except PeerIdInvalidError:
        return {'type': 'error', 'message': 'الناشر محذوف أو غير موجود'}
    except Exception as e:
        return {'type': 'error', 'message': str(e)[:50]}


async def check_story_exists(client, peer, story_id):
    """
    يتحقق إذا الستوري لا يزال موجوداً.
    يرجع True إذا موجود، False إذا محذوف/منتهي.
    """
    try:
        result = await asyncio.wait_for(
            client(GetStoriesByIDRequest(peer=peer, id=[story_id])),
            timeout=15
        )
        if hasattr(result, 'stories') and result.stories:
            return True
        return False
    except FloodWaitError as e:
        await asyncio.sleep(min(e.seconds, 60))
        return True
    except PeerIdInvalidError:
        return False
    except Exception as e:
        return False


async def send_report_with_path(client, peer, msg_id, path, report_text):
    """."""
    async def _send_request(option, comment=""):
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                result = await client(ReportRequest(
                    peer=peer,
                    id=[msg_id],
                    option=option,
                    message=comment
                ))
                return result
            except (InterdcCallErrorError, ValueError) as e:
                if attempt == MAX_RETRY_ATTEMPTS - 1:
                    raise
                logger.warning(f"محاولة {attempt+1} فشلت ({e}). إعادة المحاولة بعد 5 ثوانٍ...")
                await asyncio.sleep(5)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                if attempt == MAX_RETRY_ATTEMPTS - 1:
                    raise
        return None

    try:
        result = await _send_request(b'')
        if isinstance(result, ReportResultReported):
            return True, "نجاح"
        if isinstance(result, ReportResultAddComment):
            await _send_request(result.option, report_text)
            return True, "نجاح"

        current_options = result.options if hasattr(result, 'options') else []
        for idx, option in enumerate(path):
            selected = next((o for o in current_options if o.option == option), None)
            if not selected:
                return False, f"الخيار {idx+1} غير موجود"
            result = await _send_request(option, "")
            if isinstance(result, ReportResultReported):
                return True, "نجاح"
            if isinstance(result, ReportResultAddComment):
                await _send_request(result.option, report_text)
                return True, "نجاح"
            if hasattr(result, 'options') and result.options:
                current_options = result.options
            else:
                await _send_request(option, report_text)
                return True, "نجاح"

        await _send_request(path[-1], report_text)
        return True, "نجاح"
    except Exception as e:
        logger.error(f"خطأ غير متوقع في الإبلاغ: {e}")
        return False, str(e)[:50]


async def send_story_report_with_path(client, peer, story_id, path, report_text):
    async def _send_request(option=b'', comment=""):
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                result = await client(StoryReportRequest(
                    peer=peer, id=[story_id], option=option, message=comment
                ))
                return result
            except (InterdcCallErrorError, ValueError) as e:
                if attempt == MAX_RETRY_ATTEMPTS - 1:
                    raise
                logger.warning(f"محاولة {attempt+1} فشلت ({e}). إعادة بعد 5 ثوانٍ...")
                await asyncio.sleep(5)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                if attempt == MAX_RETRY_ATTEMPTS - 1:
                    raise
        return None

    try:
        result = await _send_request(b'', "")
        if isinstance(result, ReportResultReported):
            return True, "نجاح"
        if isinstance(result, ReportResultAddComment):
            if not getattr(result, 'optional', False) and not report_text:
                return False, "التعليق إلزامي"
            await _send_request(result.option, report_text)
            return True, "نجاح"

        current_options = result.options if hasattr(result, 'options') else []
        for idx, option in enumerate(path):
            selected = next((o for o in current_options if o.option == option), None)
            if not selected:
                return False, f"الخيار {idx+1} غير موجود"
            result = await _send_request(option, "")
            if isinstance(result, ReportResultReported):
                return True, "نجاح"
            if isinstance(result, ReportResultAddComment):
                if not getattr(result, 'optional', False) and not report_text:
                    return False, "التعليق إلزامي"
                await _send_request(result.option, report_text)
                return True, "نجاح"
            if hasattr(result, 'options') and result.options:
                current_options = result.options
            else:
                await _send_request(option, report_text)
                return True, "نجاح"

        await _send_request(path[-1] if path else b'', report_text)
        return True, "نجاح"
    except PeerIdInvalidError:
        return False, "الناشر محذوف أو غير موجود"
    except Exception as e:
        logger.error(f"خطأ غير متوقع في إبلاغ الستوري: {e}")
        return False, str(e)[:50]


def get_main_keyboard():
    kb = [
        [Button.inline("【 اضافة حساب 】", b"add_account", style="success"), Button.inline("【 حساباتي 】", b"accounts_info", style="primary")],
        [Button.inline("【 فحص الحسابات 】", b"verify_accounts", style="success"), Button.inline("【 حذف حساب 】", b"delete_account_menu", style="danger")],
        [Button.inline("【 ضبط الرفع 】", b"report_settings", style="primary")],
        [Button.inline("【 عرض معلوماتي 】", b"show_info", style="success"), Button.inline("【 حذف معلوماتي 】", b"delete_info", style="danger")],
        [Button.inline("【 بدء الرفع 】", b"start_report", style="success"), Button.inline("【 ايقاف 】", b"stop_report", style="danger")],
        [Button.inline("【 إبلاغ ستوري 】", b"story_panel", style="primary")],
        [Button.url("【 قناة التحديثات 】", "https://t.me/xxmxsx", style="primary")],
        [Button.url("【 المطور 】", "https://t.me/xxjxrx", style="primary")],
    ]
    return kb


def get_story_keyboard(uid):
    s = get_story_settings(uid)
    raw_link = s[1] if s and s[1] else ""
    stories = parse_multiple_story_links(raw_link) if raw_link else []
    if stories:
        link_display = f"{len(stories)} رابط"
    else:
        link_display = "غير محدد"
    count = s[4] if s and s[4] else 5
    has_path = bool(s[2]) if s else False
    sleep_val = get_story_sleep(uid)
    kb = [
        [Button.inline(f"【 روابط الستوري: {link_display} 】", b"story_set_link", style="primary")],
        [Button.inline(f"【 عدد الإبلاغات: {count} 】", b"story_set_count", style="primary")],
        [Button.inline(f"【 السليب: {sleep_val} ثانية 】", b"story_set_sleep", style="primary")],
        [Button.inline("【 اختيار نوع البلاغ 】", b"story_choose_type", style="success")],
    ]
    if has_path:
        kb.append([Button.inline("【 تحديد الكليشة 】", b"story_set_text", style="primary")])
    kb.append([Button.inline("【 بدء الإبلاغ 】", b"story_start", style="success")])
    kb.append([Button.inline("【 رجوع 】", b"back_main", style="danger")])
    return kb


def get_story_report_control_keyboard(s=0, f=0, current_msg=0, total_msg=0):
    kb = [
        [Button.inline(f"【 نجح: {s} 】", b"none", style="success"),
         Button.inline(f"【 فشل: {f} 】", b"none", style="danger")],
    ]
    if current_msg and total_msg:
        kb.append([Button.inline(f"【 المحاولة: {current_msg}/{total_msg} 】", b"none", style="primary")])
    kb.append([Button.inline("【 STOP 】", b"story_stop_btn", style="danger")])
    return kb


def get_story_retry_keyboard():
    return [[Button.inline("【 اعادة المحاولة 】", b"story_retry", style="success"),
             Button.inline("【 القائمة الرئيسية 】", b"back_main", style="primary")]]

def get_admin_keyboard():
    ft = "【 الغاء المجاني 】" if is_free_mode() else "【 تفعيل مجاني 】"
    return [
        [Button.inline("【 اضافة مشترك 】", b"add_sub", style="success"), Button.inline("【 حذف مشترك 】", b"del_sub", style="danger")],
        [Button.inline("【 المشتركين 】", b"subs_count", style="primary"), Button.inline("【 احصائيات 】", b"stats", style="primary")],
        [Button.inline(ft, b"free_mode", style="success"), Button.inline("【 نسخ احتياطي 】", b"backup_db", style="primary")],
        [Button.inline("【 النسخ الاحتياطية 】", b"list_backups", style="primary")],
        [Button.inline("【 اضافة مطور 】", b"add_admin", style="success"), Button.inline("【 حذف مطور 】", b"remove_admin", style="danger")],
        [Button.inline("【 المطورين 】", b"list_admins", style="primary")],
    ]


def get_report_control_keyboard(s=0, f=0, current_msg=0, total_msg=0):
    kb = [
        [Button.inline(f"【 نجح: {s} 】", b"none", style="success"),
         Button.inline(f"【 فشل: {f} 】", b"none", style="danger")],
    ]
    if current_msg and total_msg:
        kb.append([Button.inline(f"【 الرسالة: {current_msg}/{total_msg} 】", b"none", style="primary")])
    kb.append([Button.inline("【 STOP 】", b"stop_report_btn", style="danger")])
    return kb


def get_retry_keyboard():
    return [[Button.inline("【 اعادة المحاولة 】", b"retry_report", style="success"), Button.inline("【 القائمة الرئيسية 】", b"back_main", style="primary")]]


def get_report_settings_keyboard(user_id):
    s = get_settings(user_id)
    count = s[1] if s else 5
    target = s[2] if s else None
    ht = hide_username(target) if target else "غير محدد"
    has_path = bool(s[4]) if s else False
    sleep_val = get_sleep_seconds(user_id)
    kb = [
        [Button.inline(f"【 الهدف: {ht} 】", b"set_target", style="primary")],
        [Button.inline("【 تعيين الروابط 】", b"set_messages", style="primary")],
        [Button.inline(f"【 عدد البلاغات: {count} 】", b"set_count", style="primary")],
        [Button.inline(f"【 السليب: {sleep_val} ثانية 】", b"set_sleep", style="primary")],
        [Button.inline("【 اختيار نوع البلاغ 】", b"choose_report_type", style="success")],
    ]
    if has_path:
        kb.append([Button.inline("【 تحديد الكليشة 】", b"set_text", style="primary")])
    kb.append([Button.inline("【 رجوع 】", b"back_main", style="danger")])
    return kb


async def start_reporting_process(user_id, bot_client):
    working_clients = []
    try:
        s = get_settings(user_id)
        accounts = get_all_accounts(user_id)
        if not s or not accounts:
            await send_premium(bot_client, user_id, "⚠️ يرجى اعداد الاعدادات والحسابات اولا")
            return

        count = s[1] or 5
        text = s[5] or ""
        target = s[2]
        links = s[3]
        report_path = load_report_path(user_id)

        if not target or not links or not report_path:
            await send_premium(bot_client, user_id, "⚠️ يرجى اكمال جميع الاعدادات")
            return

        if count > MAX_REPORTS: count = MAX_REPORTS
        mids = validate_message_links(links)
        if not mids:
            await send_premium(bot_client, user_id, "⚠️ لا توجد روابط صالحة")
            return

        status = await send_premium(
            bot_client, user_id, "✅ يتم الرفع...",
            buttons=get_report_control_keyboard(0, 0, 1, len(mids))
        )

        active = [a for a in accounts if a[2]]
        if not active:
            await safe_edit_message(status, "⚠️ لا توجد حسابات نشطة", get_report_control_keyboard())
            return

        is_invite_link = "t.me/+" in target or "t.me/joinchat/" in target
        wc = []
        failed_accounts = []

        for idx, acc in enumerate(active):
            sd = db.execute_query("SELECT session_string FROM accounts WHERE id=?", (acc[0],), fetch_one=True)
            if not sd: continue

            uc = create_no_proxy_client(sd[0])
            if not uc or not await connect_client_with_retry(uc):
                failed_accounts.append(f"{acc[1]}: فشل الاتصال")
                await safe_stop_client(uc)
                continue
            working_clients.append(uc)

            try:
                await safe_edit_message(status, f"✅ جاري تجهيز الحساب {idx+1}/{len(active)}...", get_report_control_keyboard())

                if is_invite_link:
                    chat_info, join_msg = await join_chat_via_link(uc, target)
                    if not chat_info:
                        failed_accounts.append(f"{acc[1]}: {join_msg}")
                        await safe_stop_client(uc); continue
                    ent = chat_info
                    cid = ent.id if hasattr(ent, 'id') else fix_chat_id(target)
                else:
                    cid = fix_chat_id(target)
                    ent = await asyncio.wait_for(uc.get_entity(cid), timeout=15)
                    try:
                        await uc(JoinChannelRequest(ent))
                        await asyncio.sleep(2)
                    except:
                        pass

                msgs = await get_messages_batch(uc, cid, mids)
                if not msgs:
                    failed_accounts.append(f"{acc[1]}: القناة مغلقة أو الرسائل محذوفة")
                    await safe_stop_client(uc); continue

                avail_messages = [m.id for m in msgs if m and m.id]
                peer = InputPeerChannel(ent.id, ent.access_hash)
                wc.append({
                    'client': uc, 'peer': peer, 'chat_id': cid,
                    'account_id': acc[0], 'messages': avail_messages
                })
                logger.info(f"الحساب {acc[1]} جاهز - {len(avail_messages)} رسالة")

            except Exception as e:
                failed_accounts.append(f"{acc[1]}: {str(e)[:50]}")
                logger.error(f"Account {acc[1]} error: {e}")
                await safe_stop_client(uc)

        if not wc:
            await safe_edit_message(status, "⚠️ لا توجد حسابات قادرة للوصول", get_report_control_keyboard())
            return

        if failed_accounts:
            skipped_msg = f"⚠️ فشل {len(failed_accounts)} من أصل {len(active)} حسابات\n✅ الحسابات الجاهزة: {len(wc)}"
            await send_premium(bot_client, user_id, skipped_msg)

        fids = wc[0]['messages'] if wc[0].get('messages') else mids

        await safe_edit_message(status, "✅ يتم الرفع...", get_report_control_keyboard(0, 0, 1, len(fids)))

        successful = 0
        failed = 0
        msg_index = 0
        acc_index = 0
        notified_deleted = set()
        sleep_val = get_sleep_seconds(user_id)

        for i in range(count):
            chk = db.execute_query("SELECT is_reporting FROM active_reports WHERE user_id=?", (user_id,), fetch_one=True)
            if not chk or not chk[0]:
                await safe_edit_message(status, "🔙 تم الايقاف", get_report_control_keyboard(successful, failed))
                break

            msg_id = fids[msg_index % len(fids)]
            msg_index += 1
            ci = wc[acc_index % len(wc)]
            acc_index += 1

            try:
                if not ci['client'].is_connected():
                    if not await connect_client_with_retry(ci['client']):
                        failed += 1; continue

                check_msgs = await get_messages_batch(ci['client'], ci['chat_id'], [msg_id])
                if not check_msgs:
                    if msg_id not in notified_deleted:
                        notified_deleted.add(msg_id)
                        await send_premium(bot_client, user_id, f"⚠️ تنبيه: تم حذف الرسالة {msg_id}")
                    if len(notified_deleted) >= len(fids):
                        await send_premium(bot_client, user_id, "⚠️ الهدف انبعص او بلع انذارات! تم ايقاف الرفع")
                        break
                    continue

                success, msg = await send_report_with_path(ci['client'], ci['peer'], msg_id, report_path, text)
                if success: successful += 1
                else: failed += 1

                msg_num = (msg_index % len(fids)) + 1
                await safe_edit_message(status, "✅ يتم الرفع...",
                                         get_report_control_keyboard(successful, failed, msg_num, len(fids)))
                await asyncio.sleep(sleep_val)

            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 120))
                failed += 1
            except Exception as e:
                failed += 1
                logger.error(f"Report {i+1}: {e}")

        for ci in wc:
            await safe_stop_client(ci['client'])

        db.execute_query("DELETE FROM active_reports WHERE user_id=?", (user_id,))
        total = successful + failed
        rate = round((successful / total * 100), 1) if total > 0 else 0
        await safe_edit_message(
            status,
            f"✅ اكتمل الرفع!\n\n✅ ناجح: {successful}\n⚠️ فاشل: {failed}\n💯 المجموع: {total}\n💯 نسبة النجاح: {rate}%",
            get_retry_keyboard()
        )
    except Exception as e:
        logger.error(f"Process error: {e}")
        db.execute_query("DELETE FROM active_reports WHERE user_id=?", (user_id,))
    finally:
        for client in working_clients:
            try:
                if client.is_connected(): await client.disconnect()
            except:
                pass


async def start_story_reporting_process(user_id, bot_client):
    working_clients = []
    try:
        s = get_story_settings(user_id)
        accounts = get_all_accounts(user_id)
        if not s or not accounts:
            await send_premium(bot_client, user_id, "⚠️ يرجى اعداد الاعدادات والحسابات اولا")
            return

        raw_links = s[1] or ""
        total_count = s[4] or 5
        text = s[3] or ""

        stories_list = parse_multiple_story_links(raw_links)
        if not stories_list:
            await send_premium(bot_client, user_id, "⚠️ لا توجد روابط صالحة")
            return

        path = load_story_path(user_id)
        if not path:
            await send_premium(bot_client, user_id, "⚠️ يرجى اختيار نوع البلاغ")
            return

        if total_count > MAX_REPORTS:
            total_count = MAX_REPORTS

        status = await send_premium(
            bot_client, user_id,
            f"✅ يتم الرفع...\n({total_count} إبلاغ موزعة على {len(stories_list)} ستوري)",
            buttons=get_story_report_control_keyboard(0, 0, 0, total_count)
        )

        active = [a for a in accounts if a[2]]
        if not active:
            await safe_edit_message(status, "⚠️ لا توجد حسابات نشطة", get_story_report_control_keyboard())
            return

        wc = []
        failed_accounts = []

        for idx, acc in enumerate(active):
            sd = db.execute_query("SELECT session_string FROM accounts WHERE id=?", (acc[0],), fetch_one=True)
            if not sd:
                continue

            uc = create_no_proxy_client(sd[0])
            if not uc or not await connect_client_with_retry(uc):
                failed_accounts.append(f"{acc[1]}: فشل الاتصال")
                await safe_stop_client(uc)
                continue
            working_clients.append(uc)

            try:
                await safe_edit_message(
                    status,
                    f"✅ جاري تجهيز الحساب {idx+1}/{len(active)}...",
                    get_story_report_control_keyboard()
                )
                wc.append({
                    'client': uc,
                    'account_id': acc[0],
                    'account_name': acc[1],
                    'peer_cache': {}
                })
            except Exception as e:
                failed_accounts.append(f"{acc[1]}: {str(e)[:50]}")
                await safe_stop_client(uc)

        if not wc:
            await safe_edit_message(status, "⚠️ لا توجد حسابات قادرة للاتصال", get_story_report_control_keyboard())
            return

        if failed_accounts:
            msg = f"⚠️ فشل {len(failed_accounts)} من أصل {len(active)} حسابات\n✅ الحسابات الجاهزة: {len(wc)}"
            await send_premium(bot_client, user_id, msg)

        # ========== ✅ فحص كل الستوريات قبل البدء ==========
        await safe_edit_message(
            status,
            f"⏳ جاري فحص {len(stories_list)} ستوري...",
            get_story_report_control_keyboard()
        )

        first_ci = wc[0]
        available_stories = []
        skipped_at_start = []

        for idx, (link, peer_id, story_id) in enumerate(stories_list):
            entity, error = await resolve_story_peer(first_ci['client'], peer_id)
            if not entity:
                skipped_at_start.append((idx + 1, story_id, "الناشر محذوف"))
                continue

            peer_input = to_input_peer(entity)
            exists = await check_story_exists(first_ci['client'], peer_input, story_id)
            if exists:
                available_stories.append((link, peer_id, story_id, peer_input))
            else:
                skipped_at_start.append((idx + 1, story_id, "محذوف"))

        if skipped_at_start:
            msg = "⚠️ تم استبعاد الستوريات التالية قبل البدء:\n"
            for (num, sid, reason) in skipped_at_start:
                msg += f"• الستوري رقم ({num}) — {reason}\n"
            await send_premium(bot_client, user_id, msg)

        if not available_stories:
            await safe_edit_message(
                status,
                "⚠️ تنبيه: الهدف انبعص او حذف ستوريات مالته.",
                get_story_report_control_keyboard()
            )
            for ci in wc:
                await safe_stop_client(ci['client'])
            db.execute_query("DELETE FROM active_story_reports WHERE user_id=?", (user_id,))
            return

        # ✅ الستوريات المتاحة فقط
        stories_list = [(link, peer_id, story_id) for (link, peer_id, story_id, _) in available_stories]
        total_stories = len(stories_list)

        await safe_edit_message(
            status,
            f"✅ يتم الرفع...\n({total_count} إبلاغ على {total_stories} ستوري)",
            get_story_report_control_keyboard(0, 0, 0, total_count)
        )

        # ========== ✅ الإبلاغ بالتناوب ==========
        successful = 0
        failed = 0
        not_found = 0
        global_index = 0
        story_idx = 0
        acc_idx = 0
        skipped_stories = set()
        sleep_val = get_story_sleep(user_id)

        for i in range(total_count):
            chk = db.execute_query(
                "SELECT is_reporting FROM active_story_reports WHERE user_id=?",
                (user_id,), fetch_one=True
            )
            if not chk or not chk[0]:
                await safe_edit_message(
                    status, "🔙 تم الايقاف",
                    get_story_report_control_keyboard(successful, failed, global_index, total_count)
                )
                break

            # ✅ إذا كل الستوريات محذوفة → توقف
            if len(skipped_stories) >= total_stories:
                await send_premium(bot_client, user_id, "⚠️ تنبيه: الهدف انبعص او حذف ستوريات مالته.")
                break

            # ✅ تخطي الستوريات المحذوفة
            attempts_to_find = 0
            while story_idx in skipped_stories and attempts_to_find < total_stories:
                story_idx = (story_idx + 1) % total_stories
                attempts_to_find += 1
            if attempts_to_find >= total_stories:
                break

            story_link, peer_id, story_id = stories_list[story_idx]

            # ✅ اختيار الحساب بالتناوب
            ci = wc[acc_idx % len(wc)]

            try:
                if not ci['client'].is_connected():
                    if not await connect_client_with_retry(ci['client']):
                        failed += 1
                        global_index += 1
                        story_idx = (story_idx + 1) % total_stories
                        acc_idx += 1
                        continue

                # ✅ cache الـ peer
                cache_key = f"{peer_id}:{story_id}"
                if cache_key not in ci['peer_cache']:
                    entity, error = await resolve_story_peer(ci['client'], peer_id)
                    ci['peer_cache'][cache_key] = to_input_peer(entity) if entity else None

                peer_input = ci['peer_cache'][cache_key]
                if not peer_input:
                    failed += 1
                    global_index += 1
                    story_idx = (story_idx + 1) % total_stories
                    acc_idx += 1
                    continue

                # ✅ فحص وجود الستوري قبل الإبلاغ
                exists = await check_story_exists(ci['client'], peer_input, story_id)
                if not exists:
                    not_found += 1
                    skipped_stories.add(story_idx)
                    await send_premium(
                        bot_client, user_id,
                        f"⚠️ تنبيه: تم حذف الستوري رقم ({story_idx+1})"
                    )
                    story_idx = (story_idx + 1) % total_stories
                    acc_idx += 1
                    continue

                # ✅ إرسال البلاغ
                success, msg = await send_story_report_with_path(
                    ci['client'], peer_input, story_id, path, text
                )

                if success:
                    successful += 1
                else:
                    msg_str = str(msg)
                    if "محذوف" in msg_str or "PEER_ID_INVALID" in msg_str:
                        not_found += 1
                        skipped_stories.add(story_idx)
                        await send_premium(
                            bot_client, user_id,
                            f"⚠️ تنبيه: تم حذف الستوري رقم ({story_idx+1})"
                        )
                    else:
                        failed += 1

                global_index += 1

                # ✅ تحديث رسالة الحالة
                await safe_edit_message(
                    status,
                    f"✅ يتم الرفع...\n📎 ستوري {story_idx+1}/{total_stories} | الحساب: {ci['account_name']}",
                    get_story_report_control_keyboard(successful, failed, global_index, total_count)
                )

                # ✅ التناوب
                story_idx = (story_idx + 1) % total_stories
                acc_idx += 1

                await asyncio.sleep(sleep_val)

            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 120))
                failed += 1
                global_index += 1
                story_idx = (story_idx + 1) % total_stories
                acc_idx += 1
            except Exception as e:
                failed += 1
                global_index += 1
                story_idx = (story_idx + 1) % total_stories
                acc_idx += 1
                logger.error(f"Story report {i+1}: {e}")

        for ci in wc:
            await safe_stop_client(ci['client'])

        db.execute_query("DELETE FROM active_story_reports WHERE user_id=?", (user_id,))

        total_attempts = successful + failed + not_found
        rate = round((successful / total_attempts * 100), 1) if total_attempts > 0 else 0
        await safe_edit_message(
            status,
            f"✅ اكتمل الرفع!\n\n✅ ناجح: {successful}\n⚠️ فاشل: {failed}\n❓ محذوف: {not_found}\n💯 المجموع: {total_attempts}\n💯 نسبة النجاح: {rate}%",
            get_story_retry_keyboard()
        )
    except Exception as e:
        logger.error(f"Story process error: {e}")
        db.execute_query("DELETE FROM active_story_reports WHERE user_id=?", (user_id,))
    finally:
        for client in working_clients:
            try:
                if client.is_connected():
                    await client.disconnect()
            except:
                pass


async def check_expired_subscriptions(bot_client):
    while True:
        try:
            now = get_iraq_time()
            expired = db.execute_query(
                "SELECT user_id, subscription_end FROM users WHERE is_subscribed=1 AND subscription_end IS NOT NULL AND subscription_end != ''",
                fetch_all=True
            ) or []
            for sub in expired:
                user_id = sub[0]
                end_date_str = sub[1]
                try:
                    end_date = datetime.strptime(end_date_str, "%Y-%m-%d %H:%M:%S")
                except:
                    try:
                        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
                    except:
                        continue
                if now > end_date:
                    db.execute_query("UPDATE users SET is_subscribed=0 WHERE user_id=?", (user_id,))
                    logger.info(f"تم حذف المشترك المنتهي: {user_id}")
        except Exception as e:
            logger.error(f"Expired check error: {e}")
        await asyncio.sleep(60)


async def is_user_in_channel(bot_client, user_id):
    try:
        entity = await bot_client.get_entity(SUBSCRIPTION_CHANNEL)
        participant = await bot_client.get_permissions(entity, user_id)
        return participant is not None
    except:
        return False


user_states = {}
processing_users = set()


async def start_command(event):
    uid = event.sender_id
    if uid in user_states: del user_states[uid]
    db.execute_query("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)", (uid, event.sender.username))
    if not is_admin(uid):
        if not await is_user_in_channel(event.client, uid):
            await respond_premium(event, "⚠️ يجب الاشتراك في القناة أولاً:\n" + SUBSCRIPTION_CHANNEL,
                                   buttons=[[Button.url("【 اشتراك 】", "https://t.me/xxmxsx", style="danger")],
                                            [Button.inline("【 تحقق 】", b"check_sub", style="success")]])
            return
    if not is_admin(uid) and not is_subscribed(uid):
        await respond_premium(event, "⚠️ لا يمكنك استخدام البوت.انتَ ما مشترك!",
                              buttons=[[Button.url("【 المطور 】", "https://t.me/xxjxrx", style="primary")]])
        return
    await respond_premium(event, "👋 اهلاً بك في بوت الرفع الداخلي 🎉", buttons=get_main_keyboard())


async def admin_command(event):
    if not is_admin(event.sender_id):
        await respond_premium(event, "⚠️ انجب فقير مو مطور انتَ")
        return
    await respond_premium(event, "✅ لوحة الادمن", buttons=get_admin_keyboard())


async def handle_callbacks(event):
    try:
        data = event.data.decode('utf-8') if isinstance(event.data, bytes) else str(event.data)
        uid = event.sender_id

        if not is_admin(uid):
            if not await is_user_in_channel(event.client, uid):
                await edit_premium(event, "⚠️ يجب الاشتراك في القناة أولاً:\n" + SUBSCRIPTION_CHANNEL,
                                   buttons=[[Button.url("【 اشتراك 】", "https://t.me/xxmxsx", style="danger")],
                                            [Button.inline("【 تحقق 】", b"check_sub", style="success")]])
                return

        if not is_admin(uid) and not is_subscribed(uid):
            await edit_premium(event, "⚠️ لا يمكنك استخدام البوت. انتَ ما مشترك!",
                              buttons=[[Button.url("【 المطور 】", "https://t.me/xxjxrx", style="primary")]]); return

        if data == "check_sub":
            if await is_user_in_channel(event.client, uid):
                if is_subscribed(uid) or is_admin(uid):
                    await edit_premium(event, "✅ اهلاً بك", buttons=get_main_keyboard())
                else:
                    await edit_premium(event, "⚠️ لا يمكنك استخدام البوت. انتَ ما مشترك!",
                                      buttons=[[Button.url("【 المطور 】", "https://t.me/xxjxrx", style="primary")]])
            else:
                await edit_premium(event, "⚠️ لم يتم الاشتراك",
                                  buttons=[[Button.url("【 اشتراك 】", "https://t.me/xxmxsx", style="danger")],
                                           [Button.inline("【 تحقق 】", b"check_sub", style="success")]])
            return

        # ========== قسم القناة ==========
        if data == "choose_report_type":
            s = get_settings(uid)
            accounts = get_all_accounts(uid)
            if not s or not s[2] or not s[3]:
                await send_premium(event.client, uid, "⚠️ يجب تحديد الهدف والروابط أولاً"); return
            if not accounts:
                await send_premium(event.client, uid, "⚠️ يجب اضافة حساب أولاً"); return

            session_data = db.execute_query("SELECT session_string FROM accounts WHERE id=? AND is_active=1", (accounts[0][0],), fetch_one=True)
            if not session_data:
                await send_premium(event.client, uid, "⚠️ لا توجد حسابات نشطة"); return

            temp_client = create_no_proxy_client(session_data[0])
            if not temp_client or not await connect_client_with_retry(temp_client):
                await send_premium(event.client, uid, "⚠️ فشل الاتصال")
                await safe_stop_client(temp_client); return

            try:
                chat_id_full = fix_chat_id(s[2])
                entity = await temp_client.get_entity(chat_id_full)
                peer = InputPeerChannel(entity.id, entity.access_hash)
                message_ids = validate_message_links(s[3])
                result = await fetch_report_options(temp_client, peer, message_ids)
                if result['type'] == 'choose':
                    kb = []
                    for opt in result['options']:
                        kb.append([Button.inline(opt['text'], f"rep_{json.dumps(list(opt['option']))}".encode())])
                    kb.append([Button.inline("【 رجوع 】", b"back_main", style="danger")])
                    await edit_premium(event, "✅ اختر نوع البلاغ:", buttons=kb)
                else:
                    await send_premium(event.client, uid, "⚠️ لا توجد خيارات")
            except Exception as e:
                await send_premium(event.client, uid, f"⚠️ خطأ: {str(e)[:50]}")
            finally:
                await safe_stop_client(temp_client)
            return

        if data.startswith("rep_"):
            option_json = data.replace("rep_", "")
            try: option_bytes = bytes(json.loads(option_json))
            except:
                await send_premium(event.client, uid, "⚠️ خطأ في الخيار"); return

            s = get_settings(uid)
            accounts = get_all_accounts(uid)
            session_data = db.execute_query("SELECT session_string FROM accounts WHERE id=? AND is_active=1", (accounts[0][0],), fetch_one=True)
            temp_client = create_no_proxy_client(session_data[0])
            if not temp_client or not await connect_client_with_retry(temp_client):
                await send_premium(event.client, uid, "⚠️ فشل الاتصال")
                await safe_stop_client(temp_client); return

            try:
                chat_id_full = fix_chat_id(s[2])
                entity = await temp_client.get_entity(chat_id_full)
                peer = InputPeerChannel(entity.id, entity.access_hash)
                message_ids = validate_message_links(s[3])
                result = await fetch_report_options(temp_client, peer, message_ids, option_bytes, "")

                if result['type'] == 'choose':
                    user_states[uid] = {"state": "choosing_sub_option", "path": [option_bytes],
                                        "client": temp_client, "peer": peer, "message_ids": message_ids}
                    kb = []
                    for opt in result['options']:
                        kb.append([Button.inline(opt['text'], f"sub_{json.dumps(list(opt['option']))}".encode())])
                    kb.append([Button.inline("【 حفظ المسار 】", b"save_path", style="success")])
                    await edit_premium(event, "✅ هذا الخيار له فروع:", buttons=kb)
                    return
                elif result['type'] == 'comment':
                    user_states[uid] = {"state": "waiting_report_text", "path": [option_bytes]}
                    await edit_premium(event, "✅ ارسل الكليشة:")
                    return
                elif result['type'] == 'reported':
                    save_report_path(uid, [option_bytes])
                    await edit_premium(event, "✅ تم اختيار نوع البلاغ", buttons=get_report_settings_keyboard(uid))
                    await safe_stop_client(temp_client); return
            except Exception as e:
                await send_premium(event.client, uid, f"⚠️ خطأ: {str(e)[:50]}")
            finally:
                if user_states.get(uid, {}).get('state') != 'choosing_sub_option':
                    await safe_stop_client(temp_client)
            return

        if data.startswith("sub_"):
            option_json = data.replace("sub_", "")
            try: option_bytes = bytes(json.loads(option_json))
            except:
                await send_premium(event.client, uid, "⚠️ خطأ في الخيار"); return

            state = user_states.get(uid, {})
            path = state.get('path', [])
            path.append(option_bytes)
            temp_client = state.get('client')
            peer = state.get('peer')
            message_ids = state.get('message_ids')
            result = await fetch_report_options(temp_client, peer, message_ids, option_bytes, "")

            if result['type'] == 'choose':
                kb = []
                for opt in result['options']:
                    kb.append([Button.inline(opt['text'], f"sub_{json.dumps(list(opt['option']))}".encode())])
                kb.append([Button.inline("【 حفظ المسار 】", b"save_path", style="success")])
                await edit_premium(event, "✅ اختر الخيار الفرعي:", buttons=kb)
                user_states[uid]['path'] = path
                return
            elif result['type'] == 'reported':
                save_report_path(uid, path)
                await safe_stop_client(temp_client)
                del user_states[uid]
                await edit_premium(event, "✅ تم حفظ المسار", buttons=get_report_settings_keyboard(uid))
                return
            elif result['type'] == 'comment':
                user_states[uid]['state'] = 'waiting_report_text'
                await edit_premium(event, "✅ ارسل نص الكليشة:")
                return

        if data == "save_path":
            state = user_states.get(uid, {})
            if state and 'path' in state:
                save_report_path(uid, state['path'])
                await safe_stop_client(state.get('client'))
                del user_states[uid]
                await edit_premium(event, "✅ تم حفظ المسار", buttons=get_report_settings_keyboard(uid))
            return

        if data == "back_main":
            await edit_premium(event, "🔙 القائمة الرئيسية", buttons=get_main_keyboard())
        elif data == "report_settings":
            await edit_premium(event, "✅ ضبط الرفع", buttons=get_report_settings_keyboard(uid))
        elif data == "set_target":
            await event.delete()
            await send_premium(event.client, uid, "✅ ارسل رابط القناة:")
            user_states[uid] = {"state": "waiting_chat"}
        elif data == "set_messages":
            await event.delete()
            await send_premium(event.client, uid, "📎 ارسل روابط الرسائل:")
            user_states[uid] = {"state": "waiting_messages"}
        elif data == "set_count":
            await event.delete()
            await send_premium(event.client, uid, f"💯 ارسل عدد البلاغات (1-{MAX_REPORTS}):")
            user_states[uid] = {"state": "waiting_count"}
        elif data == "set_sleep":
            await event.delete()
            kb = [
                [Button.inline("【 3 ثواني 】", b"sleep_3", style="success"), Button.inline("【 4 ثواني 】", b"sleep_4", style="primary")],
                [Button.inline("【 5 ثواني 】", b"sleep_5", style="primary"), Button.inline("【 6 ثواني 】", b"sleep_6", style="primary")],
                [Button.inline("【 7 ثواني 】", b"sleep_7", style="primary"), Button.inline("【 8 ثواني 】", b"sleep_8", style="primary")],
                [Button.inline("【 9 ثواني 】", b"sleep_9", style="primary"), Button.inline("【 10 ثواني 】", b"sleep_10", style="danger")],
                [Button.inline("【 رجوع 】", b"report_settings", style="danger")]
            ]
            await send_premium(event.client, uid, "⚠️ اختر مدة الانتظار (3-10 ثواني):", buttons=kb)
        elif data.startswith("sleep_") and not data.startswith("sleep_1"):
            try:
                val = int(data.replace("sleep_", ""))
                if val < 3: val = 3
                if val > 10: val = 10
                s = get_settings(uid)
                if s: db.execute_query("UPDATE settings SET sleep_seconds=? WHERE user_id=?", (val, uid))
                else: db.execute_query("INSERT INTO settings (user_id, sleep_seconds) VALUES (?,?)", (uid, val))
                await edit_premium(event, f"✅ تم حفظ السليب: {val} ثانية", buttons=get_report_settings_keyboard(uid))
            except Exception as e:
                logger.error(f"Sleep save error: {e}")
            return
        elif data == "set_text":
            await event.delete()
            await send_premium(event.client, uid, "✅ ارسل الكليشة:")
            user_states[uid] = {"state": "waiting_text"}
        elif data == "start_report":
            chk = db.execute_query("SELECT is_reporting FROM active_reports WHERE user_id=?", (uid,), fetch_one=True)
            if chk and chk[0]:
                await send_premium(event.client, uid, "⚠️ الرفع قيد التشغيل!"); return
            s = get_settings(uid)
            accs = get_all_accounts(uid)
            path = load_report_path(uid)
            if not s or not s[2] or not s[3] or not path:
                await send_premium(event.client, uid, "⚠️ اكمل الاعدادات"); return
            if not accs:
                await send_premium(event.client, uid, "🔙 لا توجد حسابات"); return
            db.execute_query("INSERT OR REPLACE INTO active_reports (user_id, is_reporting) VALUES (?,1)", (uid,))
            asyncio.create_task(start_reporting_process(uid, event.client))
            await send_premium(event.client, uid, "✅ بدأ الرفع")
        elif data == "stop_report":
            db.execute_query("DELETE FROM active_reports WHERE user_id=?", (uid,))
            await send_premium(event.client, uid, "🔙 تم الايقاف")
        elif data == "stop_report_btn":
            db.execute_query("DELETE FROM active_reports WHERE user_id=?", (uid,))
            await edit_premium(event, "🔙 تم ايقاف الرفع", buttons=get_main_keyboard())
        elif data == "retry_report":
            db.execute_query("INSERT OR REPLACE INTO active_reports (user_id, is_reporting) VALUES (?,1)", (uid,))
            asyncio.create_task(start_reporting_process(uid, event.client))
            await send_premium(event.client, uid, "✅ بدات اعادة المحاولة")
        elif data == "show_info":
            s = get_settings(uid)
            accs = get_all_accounts(uid)
            if not s:
                await send_premium(event.client, uid, "⚠️ لا توجد معلومات"); return
            path = load_report_path(uid)
            sleep_val = get_sleep_seconds(uid)
            txt = f"📎 الهدف: {hide_username(s[2])}\n💯 عدد البلاغات: {s[1]}\n⚠️ السليب: {sleep_val} ثانية\n✅ نوع البلاغ: {'محدد' if path else 'غير محدد'}\n📎 الحسابات: {len(accs)}/{MAX_ACCOUNTS}"
            await send_premium(event.client, uid, txt)
        elif data == "delete_info":
            db.execute_query("DELETE FROM settings WHERE user_id=?", (uid,))
            await send_premium(event.client, uid, "✅ تم حذف المعلومات")
        elif data == "add_account":
            await event.delete()
            kb = [[Button.inline("【 اضافة برقم الهاتف 】", b"add_phone", style="success")],
                  [Button.inline("【 اضافة بالسيشن 】", b"add_session", style="primary")],
                  [Button.inline("【 رجوع 】", b"back_main", style="danger")]]
            await send_premium(event.client, uid, "✅ اختر طريقة الاضافة:", buttons=kb)
        elif data == "add_phone":
            await event.delete()
            await send_premium(event.client, uid, "✅ ارسل رقم الهاتف:")
            user_states[uid] = {"state": "waiting_phone"}
        elif data == "add_session":
            await event.delete()
            await send_premium(event.client, uid, "✅ ارسل السيشن:")
            user_states[uid] = {"state": "waiting_session"}
        elif data == "accounts_info":
            accs = get_all_accounts(uid)
            if not accs:
                await send_premium(event.client, uid, "🔙 لا توجد حسابات"); return
            txt = f"📎 حساباتك ({len(accs)}/{MAX_ACCOUNTS}):\n\n"
            for a in accs:
                st = "✅ نشط" if a[2] else "⚠️ معطل"
                txt += f"• {a[1]} ({hide_phone(a[3])}): {st}\n"
            await send_premium(event.client, uid, txt)
        elif data == "verify_accounts":
            await send_premium(event.client, uid, "✅ جاري الفحص...")
            accs = get_all_accounts(uid)
            results = []
            for a in accs:
                sd = db.execute_query("SELECT session_string FROM accounts WHERE id=?", (a[0],), fetch_one=True)
                if sd:
                    ok, status = await verify_account_sync(sd[0])
                    db.execute_query("UPDATE accounts SET is_active=? WHERE id=?", (1 if ok else 0, a[0]))
                    results.append(f"{a[1]}: {status}")
            await send_premium(event.client, uid, "📎 نتيجة الفحص:\n\n" + "\n".join(results))
        elif data == "delete_account_menu":
            accs = get_all_accounts(uid)
            if not accs:
                await send_premium(event.client, uid, "🔙 لا توجد حسابات"); return
            kb = [[Button.inline(f"【 حذف {a[1]} 】", f"del_acc_{a[0]}".encode(), style="danger")] for a in accs]
            kb.append([Button.inline("【 حذف الكل 】", b"del_all_acc", style="danger")])
            kb.append([Button.inline("【 رجوع 】", b"back_main", style="primary")])
            await edit_premium(event, "🔙 اختر:", buttons=kb)
        elif data.startswith("del_acc_"):
            aid = int(data.replace("del_acc_", ""))
            db.execute_query("DELETE FROM accounts WHERE id=? AND user_id=?", (aid, uid))
            await event.delete()
            await send_premium(event.client, uid, "✅ تم الحذف")
        elif data == "del_all_acc":
            db.execute_query("DELETE FROM accounts WHERE user_id=?", (uid,))
            await event.delete()
            await send_premium(event.client, uid, "✅ تم حذف الكل")

        # ========== قسم الستوري ==========
        elif data == "story_panel":
            get_story_settings(uid)
            await edit_premium(event, "✅ إبلاغ ستوري", buttons=get_story_keyboard(uid))

        elif data == "story_set_link":
            await event.delete()
            await send_premium(
                event.client, uid,
                "📎 ارسل روابط الستوري:\n"
                "• رابط واحد في كل سطر"
            )
            user_states[uid] = {"state": "waiting_story_link"}

        elif data == "story_set_count":
            await event.delete()
            await send_premium(event.client, uid, f"💯 ارسل عدد الإبلاغات الإجمالي (1-{MAX_REPORTS}):")
            user_states[uid] = {"state": "waiting_story_count"}

        elif data == "story_set_sleep":
            await event.delete()
            kb = [
                [Button.inline("【 3 ثواني 】", b"story_sleep_3", style="success"), Button.inline("【 4 ثواني 】", b"story_sleep_4", style="primary")],
                [Button.inline("【 5 ثواني 】", b"story_sleep_5", style="primary"), Button.inline("【 6 ثواني 】", b"story_sleep_6", style="primary")],
                [Button.inline("【 7 ثواني 】", b"story_sleep_7", style="primary"), Button.inline("【 8 ثواني 】", b"story_sleep_8", style="primary")],
                [Button.inline("【 9 ثواني 】", b"story_sleep_9", style="primary"), Button.inline("【 10 ثواني 】", b"story_sleep_10", style="danger")],
                [Button.inline("【 رجوع 】", b"story_panel", style="danger")]
            ]
            await send_premium(event.client, uid, "⚠️ اختر مدة الانتظار (3-10 ثواني):", buttons=kb)

        elif data.startswith("story_sleep_"):
            try:
                val = int(data.replace("story_sleep_", ""))
                if val < 3: val = 3
                if val > 10: val = 10
                get_story_settings(uid)
                db.execute_query("UPDATE story_settings SET story_sleep=? WHERE user_id=?", (val, uid))
                await edit_premium(event, f"✅ تم حفظ السليب: {val} ثانية", buttons=get_story_keyboard(uid))
            except Exception as e:
                logger.error(f"Story sleep save error: {e}")

        elif data == "story_set_text":
            await event.delete()
            await send_premium(event.client, uid, "✅ ارسل الكليشة:")
            user_states[uid] = {"state": "waiting_story_text"}

        elif data == "story_choose_type":
            s = get_story_settings(uid)
            accounts = get_all_accounts(uid)
            if not s or not s[1]:
                await send_premium(event.client, uid, "⚠️ يجب تحديد الروابط أولاً"); return
            if not accounts:
                await send_premium(event.client, uid, "⚠️ يجب اضافة حساب أولاً"); return

            stories = parse_multiple_story_links(s[1])
            if not stories:
                await send_premium(event.client, uid, "⚠️ لا توجد روابط صالحة"); return

            first_link, peer_id, target_id = stories[0]

            session_data = db.execute_query("SELECT session_string FROM accounts WHERE id=? AND is_active=1", (accounts[0][0],), fetch_one=True)
            if not session_data:
                await send_premium(event.client, uid, "⚠️ لا توجد حسابات نشطة"); return

            temp_client = create_no_proxy_client(session_data[0])
            if not temp_client or not await connect_client_with_retry(temp_client):
                await send_premium(event.client, uid, "⚠️ فشل الاتصال")
                await safe_stop_client(temp_client); return

            try:
                entity, error = await resolve_story_peer(temp_client, peer_id)
                if not entity:
                    await send_premium(event.client, uid, f"⚠️ {error}")
                    await safe_stop_client(temp_client); return
                peer_input = to_input_peer(entity)
                result = await fetch_story_options(temp_client, peer_input, target_id)
                if result['type'] == 'choose':
                    kb = []
                    for opt in result['options']:
                        kb.append([Button.inline(opt['text'], f"repstory_{json.dumps(list(opt['option']))}".encode())])
                    kb.append([Button.inline("【 رجوع 】", b"story_panel", style="danger")])
                    await edit_premium(event, "✅ اختر نوع البلاغ:", buttons=kb)
                elif result['type'] == 'reported':
                    save_story_path(uid, [])
                    await send_premium(event.client, uid, "✅ البلاغ مباشر (لا يحتاج نوع)")
                elif result['type'] == 'comment':
                    user_states[uid] = {"state": "waiting_story_report_text", "path": []}
                    await edit_premium(event, "✅ ارسل الكليشة:")
                else:
                    await send_premium(event.client, uid, f"⚠️ {result.get('message', 'لا توجد خيارات')}")
            except Exception as e:
                await send_premium(event.client, uid, f"⚠️ خطأ: {str(e)[:50]}")
            finally:
                if user_states.get(uid, {}).get('state') != 'choosing_story_sub_option':
                    await safe_stop_client(temp_client)

        elif data.startswith("repstory_"):
            option_json = data.replace("repstory_", "")
            try: option_bytes = bytes(json.loads(option_json))
            except:
                await send_premium(event.client, uid, "⚠️ خطأ في الخيار"); return

            s = get_story_settings(uid)
            accounts = get_all_accounts(uid)
            session_data = db.execute_query("SELECT session_string FROM accounts WHERE id=? AND is_active=1", (accounts[0][0],), fetch_one=True)

            stories = parse_multiple_story_links(s[1])
            if not stories:
                await send_premium(event.client, uid, "⚠️ لا توجد روابط"); return
            first_link, peer_id, target_id = stories[0]

            temp_client = create_no_proxy_client(session_data[0])
            if not temp_client or not await connect_client_with_retry(temp_client):
                await send_premium(event.client, uid, "⚠️ فشل الاتصال")
                await safe_stop_client(temp_client); return

            try:
                entity, _ = await resolve_story_peer(temp_client, peer_id)
                if not entity:
                    await send_premium(event.client, uid, "⚠️ الناشر غير موجود")
                    await safe_stop_client(temp_client); return
                peer_input = to_input_peer(entity)
                result = await fetch_story_options(temp_client, peer_input, target_id, option_bytes, "")

                if result['type'] == 'choose':
                    user_states[uid] = {"state": "choosing_story_sub_option",
                                        "path": [option_bytes], "client": temp_client,
                                        "peer": peer_input, "target_id": target_id}
                    kb = []
                    for opt in result['options']:
                        kb.append([Button.inline(opt['text'], f"substory_{json.dumps(list(opt['option']))}".encode())])
                    kb.append([Button.inline("【 حفظ المسار 】", b"story_save_path", style="success")])
                    await edit_premium(event, "✅ هذا الخيار له فروع:", buttons=kb)
                    return
                elif result['type'] == 'comment':
                    user_states[uid] = {"state": "waiting_story_report_text", "path": [option_bytes]}
                    await edit_premium(event, "✅ ارسل الكليشة:")
                    return
                elif result['type'] == 'reported':
                    save_story_path(uid, [option_bytes])
                    await edit_premium(event, "✅ تم اختيار نوع البلاغ")
                    await safe_stop_client(temp_client); return
            except Exception as e:
                await send_premium(event.client, uid, f"⚠️ خطأ: {str(e)[:50]}")
            finally:
                if user_states.get(uid, {}).get('state') != 'choosing_story_sub_option':
                    await safe_stop_client(temp_client)

        elif data.startswith("substory_"):
            option_json = data.replace("substory_", "")
            try: option_bytes = bytes(json.loads(option_json))
            except:
                await send_premium(event.client, uid, "⚠️ خطأ في الخيار"); return

            state = user_states.get(uid, {})
            path = state.get('path', [])
            path.append(option_bytes)
            temp_client = state.get('client')
            peer_input = state.get('peer')
            target_id = state.get('target_id')

            result = await fetch_story_options(temp_client, peer_input, target_id, option_bytes, "")

            if result['type'] == 'choose':
                kb = []
                for opt in result['options']:
                    kb.append([Button.inline(opt['text'], f"substory_{json.dumps(list(opt['option']))}".encode())])
                kb.append([Button.inline("【 حفظ المسار 】", b"story_save_path", style="success")])
                await edit_premium(event, "✅ اختر الخيار الفرعي:", buttons=kb)
                user_states[uid]['path'] = path
                return
            elif result['type'] == 'reported':
                save_story_path(uid, path)
                await safe_stop_client(temp_client)
                del user_states[uid]
                await edit_premium(event, "✅ تم حفظ المسار", buttons=get_story_keyboard(uid))
                return
            elif result['type'] == 'comment':
                optional = result.get('optional', False)
                user_states[uid]['state'] = 'waiting_story_report_text'
                user_states[uid]['path'] = path
                if optional:
                    await edit_premium(event, "✅ ارسل الكليشة (اختياري — أرسل . للتخطي):")
                else:
                    await edit_premium(event, "✅ ارسل الكليشة (إلزامي):")
                return

        elif data == "story_save_path":
            state = user_states.get(uid, {})
            if state and 'path' in state:
                save_story_path(uid, state['path'])
                await safe_stop_client(state.get('client'))
                del user_states[uid]
                await edit_premium(event, "✅ تم حفظ المسار", buttons=get_story_keyboard(uid))

        elif data == "story_start":
            chk = db.execute_query("SELECT is_reporting FROM active_story_reports WHERE user_id=?", (uid,), fetch_one=True)
            if chk and chk[0]:
                await send_premium(event.client, uid, "⚠️ الرفع قيد التشغيل!"); return
            s = get_story_settings(uid)
            accs = get_all_accounts(uid)
            path = load_story_path(uid)
            if not s or not s[1] or not path:
                await send_premium(event.client, uid, "⚠️ اكمل الاعدادات"); return
            if not accs:
                await send_premium(event.client, uid, "🔙 لا توجد حسابات"); return
            db.execute_query("INSERT OR REPLACE INTO active_story_reports (user_id, is_reporting) VALUES (?,1)", (uid,))
            asyncio.create_task(start_story_reporting_process(uid, event.client))
            await send_premium(event.client, uid, "✅ بدأ الرفع")

        elif data == "story_stop_btn":
            db.execute_query("DELETE FROM active_story_reports WHERE user_id=?", (uid,))
            await edit_premium(event, "🔙 تم ايقاف الرفع", buttons=get_main_keyboard())

        elif data == "story_retry":
            db.execute_query("INSERT OR REPLACE INTO active_story_reports (user_id, is_reporting) VALUES (?,1)", (uid,))
            asyncio.create_task(start_story_reporting_process(uid, event.client))
            await send_premium(event.client, uid, "✅ بدات اعادة المحاولة")

        # ========== لوحة الأدمن ==========
        elif data == "add_sub" and is_admin(uid):
            await event.delete()
            await send_premium(event.client, uid, "✅ ارسل ايدي المستخدم:")
            user_states[uid] = {"state": "waiting_sub_id"}
        elif data == "del_sub" and is_admin(uid):
            subs = db.execute_query("SELECT user_id, username FROM users WHERE is_subscribed=1", fetch_all=True) or []
            if not subs:
                await send_premium(event.client, uid, "⚠️ لا يوجد مشتركين"); return
            kb = []
            for sub in subs:
                sub_id = sub[0]
                username = sub[1] if sub[1] else "بدون معرف"
                kb.append([Button.inline(f"【 حذف {sub_id} (@{username}) 】", f"delsub_{sub_id}".encode(), style="danger")])
            kb.append([Button.inline("【 حذف الكل 】", b"del_all_subs", style="danger")])
            kb.append([Button.inline("【 رجوع 】", b"admin_panel", style="primary")])
            await edit_premium(event, "🔙 اختر مشترك للحذف:", buttons=kb)
        elif data.startswith("delsub_"):
            sub_id = int(data.replace("delsub_", ""))
            db.execute_query("UPDATE users SET is_subscribed=0 WHERE user_id=?", (sub_id,))
            await send_premium(event.client, uid, f"✅ تم حذف اشتراك {sub_id}")
            await edit_premium(event, "✅ تم الحذف", buttons=get_admin_keyboard())
        elif data == "del_all_subs" and is_admin(uid):
            db.execute_query("UPDATE users SET is_subscribed=0 WHERE is_subscribed=1")
            await send_premium(event.client, uid, "✅ تم حذف جميع المشتركين")
            await edit_premium(event, "✅ تم حذف الكل", buttons=get_admin_keyboard())
        elif data == "admin_panel" and is_admin(uid):
            await edit_premium(event, "✅ لوحة الادمن", buttons=get_admin_keyboard())
        elif data == "subs_count" and is_admin(uid):
            subs = db.execute_query(
                "SELECT user_id, username, subscription_end FROM users WHERE is_subscribed=1 AND subscription_end IS NOT NULL AND subscription_end != ''",
                fetch_all=True) or []
            if not subs:
                await send_premium(event.client, uid, "⚠️ لا يوجد مشتركين"); return
            now = get_iraq_time()
            txt = f"📎 المشتركين ({len(subs)}):\n\n━━━━━━━━━━━━━━━━━━\n\n"
            for sub in subs:
                user_id = sub[0]
                username = sub[1] if sub[1] else "بدون معرف"
                end_date_str = sub[2]
                user_accounts = get_all_accounts(user_id)
                account_count = len(user_accounts)
                try:
                    end_date = datetime.strptime(end_date_str, "%Y-%m-%d %H:%M:%S")
                except:
                    try:
                        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
                    except:
                        txt += f"• {user_id} (@{username}) - غير محدد - حساباته: {account_count}\n"
                        continue
                remaining = end_date - now
                days_left = remaining.days
                hours_left = remaining.seconds // 3600
                if days_left > 0:
                    txt += f"• {user_id} (@{username})\n  └ المتبقي: {days_left} يوم و {hours_left} ساعة\n  └ حساباته: {account_count}\n\n"
                elif hours_left > 0:
                    txt += f"• {user_id} (@{username})\n  └ المتبقي: {hours_left} ساعة\n  └ حساباته: {account_count}\n\n"
                else:
                    txt += f"• {user_id} (@{username})\n  └ منتهي\n  └ حساباته: {account_count}\n\n"
            txt += "━━━━━━━━━━━━━━━━━━"
            if len(txt) > 4000:
                parts = [txt[i:i+3900] for i in range(0, len(txt), 3900)]
                for part in parts:
                    await send_premium(event.client, uid, part)
            else:
                await send_premium(event.client, uid, txt)
        elif data == "stats" and is_admin(uid):
            tu = db.execute_query("SELECT COUNT(*) FROM users", fetch_one=True)[0]
            ta = db.execute_query("SELECT COUNT(*) FROM accounts", fetch_one=True)[0]
            await send_premium(event.client, uid, f"📎 المستخدمين: {tu}\n📎 الحسابات: {ta}")
        elif data == "free_mode" and is_admin(uid):
            set_free_mode(not is_free_mode())
            await edit_premium(event, "✅ لوحة الادمن", buttons=get_admin_keyboard())
        elif data == "add_admin" and is_admin(uid):
            if uid != DEVELOPER_ID:
                await send_premium(event.client, uid, "⚠️ فقط المطور الرئيسي"); return
            await event.delete()
            await send_premium(event.client, uid, "✅ ارسل ايدي المطور:")
            user_states[uid] = {"state": "waiting_add_admin"}
        elif data == "remove_admin" and is_admin(uid):
            if uid != DEVELOPER_ID:
                await send_premium(event.client, uid, "⚠️ فقط المطور الرئيسي"); return
            admins = get_admins()
            if not admins:
                await send_premium(event.client, uid, "⚠️ لا يوجد مطورين"); return
            kb = [[Button.inline(f"【 حذف {a[0]} 】", f"del_admin_{a[0]}".encode(), style="danger")] for a in admins]
            kb.append([Button.inline("【 رجوع 】", b"admin_panel", style="primary")])
            await edit_premium(event, "🔙 اختر مطور للحذف:", buttons=kb)
        elif data.startswith("del_admin_"):
            admin_id = int(data.replace("del_admin_", ""))
            remove_admin(admin_id)
            await send_premium(event.client, uid, f"✅ تم حذف المطور {admin_id}")
            await edit_premium(event, "✅ تم الحذف", buttons=get_admin_keyboard())
        elif data == "list_admins" and is_admin(uid):
            admins = get_admins()
            txt = f"📎 المطور الرئيسي: {DEVELOPER_ID}\n\n📎 المطورين:\n"
            txt += "\n".join([str(a[0]) for a in admins]) if admins else "لا يوجد"
            await send_premium(event.client, uid, txt)
        elif data == "backup_db" and is_admin(uid):
            ts = get_iraq_time().strftime("%Y%m%d_%H%M%S")
            shutil.copy2("bot_database.db", f"backups/backup_{ts}.db")
            await send_premium(event.client, uid, "✅ تم النسخ الاحتياطي")
        elif data == "list_backups" and is_admin(uid):
            backups = [f for f in os.listdir("backups") if f.endswith(".db")]
            txt = "📎 النسخ الاحتياطية:\n\n" + "\n".join(backups[:10]) if backups else "لا توجد نسخ"
            await send_premium(event.client, uid, txt)

    except Exception as e:
        logger.error(f"Callback error: {e}")
        try:
            await send_premium(event.client, uid, "⚠️ حدث خطأ")
        except:
            pass


async def handle_messages(event):
    uid = event.sender_id
    state = user_states.get(uid, {})
    sn = state.get("state", "")
    if not sn: return
    if uid in processing_users: return

    processing_users.add(uid)
    try:
        if sn == "waiting_phone":
            accs_count = len(get_all_accounts(uid))
            if accs_count >= MAX_ACCOUNTS:
                await respond_premium(event, f"⚠️ وصلت للحد الأقصى ({MAX_ACCOUNTS} حساب)")
                del user_states[uid]; return
            phone = event.text.strip()
            tc = TelegramClient(StringSession(), API_ID, API_HASH)
            try:
                await tc.connect()
                sent = await tc.send_code_request(phone)
                user_states[uid] = {"state": "waiting_code", "phone": phone,
                                    "hash": sent.phone_code_hash, "client": tc}
                await respond_premium(event, "✅ تم ارسال الرمز:")
            except Exception as e:
                await respond_premium(event, f"⚠️ خطأ: {e}")
                await safe_stop_client(tc); del user_states[uid]

        elif sn == "waiting_code":
            accs_count = len(get_all_accounts(uid))
            if accs_count >= MAX_ACCOUNTS:
                await safe_stop_client(state.get("client"))
                await respond_premium(event, f"⚠️ وصلت للحد الأقصى ({MAX_ACCOUNTS} حساب)")
                del user_states[uid]; return
            code = event.text.strip()
            tc = state.get("client")
            if not tc: del user_states[uid]; return
            try:
                await tc.sign_in(phone=state["phone"], code=code, phone_code_hash=state["hash"])
                ss = tc.session.save(); me = await tc.get_me()
                db.execute_query("INSERT INTO accounts (user_id, phone_number, session_string, account_name, proxy_index) VALUES (?,?,?,?,?)",
                                 (uid, state["phone"], ss, me.first_name, -1))
                await respond_premium(event, f"✅ تم اضافة: {me.first_name}")
                await safe_stop_client(tc); del user_states[uid]
            except SessionPasswordNeededError:
                user_states[uid]["state"] = "waiting_password"
                await respond_premium(event, "✅ ارسل كلمة المرور:")
            except Exception as e:
                await respond_premium(event, f"⚠️ خطأ: {e}")
                await safe_stop_client(tc); del user_states[uid]

        elif sn == "waiting_password":
            pw = event.text.strip()
            tc = state.get("client")
            try:
                await tc.sign_in(password=pw)
                ss = tc.session.save(); me = await tc.get_me()
                db.execute_query("INSERT INTO accounts (user_id, phone_number, session_string, account_name, proxy_index) VALUES (?,?,?,?,?)",
                                 (uid, state["phone"], ss, me.first_name, -1))
                await respond_premium(event, f"✅ تم اضافة: {me.first_name}")
                await safe_stop_client(tc); del user_states[uid]
            except Exception as e:
                await respond_premium(event, f"⚠️ خطأ: {e}")
                await safe_stop_client(tc); del user_states[uid]

        elif sn == "waiting_session":
            accs_count = len(get_all_accounts(uid))
            if accs_count >= MAX_ACCOUNTS:
                await respond_premium(event, f"⚠️ وصلت للحد الأقصى ({MAX_ACCOUNTS} حساب)")
                del user_states[uid]; return
            ss = event.text.strip()
            sm = await respond_premium(event, "✅ جاري التحقق...")
            ok, status = await verify_account_sync(ss)
            if ok:
                db.execute_query("INSERT INTO accounts (user_id, session_string, account_name, is_active, proxy_index) VALUES (?,?,?,1,?)",
                                 (uid, ss, status.replace('نشط (', '').replace(')', ''), -1))
                t, e = premium(f"✅ تم اضافة: {status}")
                await sm.edit(t, formatting_entities=e)
            else:
                t, e = premium(f"⚠️ فشل: {status}")
                await sm.edit(t, formatting_entities=e)
            del user_states[uid]

        elif sn == "waiting_count":
            try:
                c = int(event.text)
                if c > MAX_REPORTS: c = MAX_REPORTS
                if c < 1: c = 1
                s = get_settings(uid)
                if s: db.execute_query("UPDATE settings SET report_count=? WHERE user_id=?", (c, uid))
                else: db.execute_query("INSERT INTO settings (user_id, report_count) VALUES (?,?)", (uid, c))
                await respond_premium(event, f"✅ تم حفظ: {c}")
                del user_states[uid]
            except:
                await respond_premium(event, "⚠️ رقم غير صحيح")

        elif sn == "waiting_text":
            t = event.text.strip()
            if not t:
                await respond_premium(event, "⚠️ النص فارغ. ارسل الكليشة:"); return
            s = get_settings(uid)
            if s: db.execute_query("UPDATE settings SET report_text=? WHERE user_id=?", (t, uid))
            else: db.execute_query("INSERT INTO settings (user_id, report_text) VALUES (?,?)", (uid, t))
            await respond_premium(event, "✅ تم حفظ الكليشة.", buttons=[[Button.inline("【 رجوع 】", b"back_main", style="danger")]])
            del user_states[uid]

        elif sn == "waiting_report_text":
            t = event.text.strip()
            path = state.get("path", [])
            if path:
                save_report_path(uid, path)
                s = get_settings(uid)
                if s: db.execute_query("UPDATE settings SET report_text=? WHERE user_id=?", (t, uid))
                else: db.execute_query("INSERT INTO settings (user_id, report_text) VALUES (?,?)", (uid, t))
                await respond_premium(event, "✅ تم حفظ الكليشة.")
                del user_states[uid]

        elif sn == "waiting_chat":
            chat = event.text.strip()
            s = get_settings(uid)
            if s: db.execute_query("UPDATE settings SET target_chat=? WHERE user_id=?", (chat, uid))
            else: db.execute_query("INSERT INTO settings (user_id, target_chat) VALUES (?,?)", (uid, chat))
            await respond_premium(event, "✅ تم حفظ الهدف\n📎 الان ارسل روابط الرسائل:")
            user_states[uid] = {"state": "waiting_messages"}

        elif sn == "waiting_messages":
            links = event.text.strip()
            vids = validate_message_links(links)
            lt = '\n'.join([str(i) for i in vids])
            s = get_settings(uid)
            if s: db.execute_query("UPDATE settings SET message_links=? WHERE user_id=?", (lt, uid))
            else: db.execute_query("INSERT INTO settings (user_id, message_links) VALUES (?,?)", (uid, lt))
            await respond_premium(event, f"✅ تم حفظ {len(vids)} رابط")
            del user_states[uid]

        # ========== رسائل الستوري ==========
        elif sn == "waiting_story_link":
            raw = event.text.strip()
            stories = parse_multiple_story_links(raw)
            if not stories:
                await respond_premium(event, "⚠️ لا توجد روابط صالحة"); return
            get_story_settings(uid)
            db.execute_query(
                "UPDATE story_settings SET story_link=?, story_path='', story_text='' WHERE user_id=?",
                (raw, uid)
            )
            await respond_premium(event, f"✅ تم حفظ {len(stories)} رابط")
            del user_states[uid]

        elif sn == "waiting_story_count":
            try:
                c = int(event.text.strip())
                if c > MAX_REPORTS: c = MAX_REPORTS
                if c < 1: c = 1
                get_story_settings(uid)
                db.execute_query("UPDATE story_settings SET story_count=? WHERE user_id=?", (c, uid))
                await respond_premium(event, f"✅ تم حفظ: {c}")
                del user_states[uid]
            except:
                await respond_premium(event, "⚠️ رقم غير صحيح")

        elif sn == "waiting_story_text":
            t = event.text.strip()
            if not t:
                await respond_premium(event, "⚠️ النص فارغ. ارسل الكليشة:"); return
            get_story_settings(uid)
            db.execute_query("UPDATE story_settings SET story_text=? WHERE user_id=?", (t, uid))
            await respond_premium(event, "✅ تم حفظ الكليشة.")
            del user_states[uid]

        elif sn == "waiting_story_report_text":
            t = event.text.strip()
            if t == ".":
                t = ""
            path = state.get("path", [])
            if path:
                save_story_path(uid, path)
            get_story_settings(uid)
            db.execute_query("UPDATE story_settings SET story_text=? WHERE user_id=?", (t, uid))
            await respond_premium(event, "✅ تم حفظ الكليشة.")
            del user_states[uid]

        # ========== رسائل الأدمن ==========
        elif sn == "waiting_sub_id" and is_admin(uid):
            try:
                sub_id = int(event.text.strip())
                user_states[uid] = {"state": "waiting_sub_days", "sub_id": sub_id}
                await respond_premium(event, f"✅ تم حفظ الايدي: {sub_id}\n✅ الان ارسل عدد الأيام:")
            except:
                await respond_premium(event, "⚠️ ايدي غير صحيح")
                del user_states[uid]

        elif sn == "waiting_sub_days" and is_admin(uid):
            try:
                days = int(event.text.strip())
                sub_id = state.get("sub_id")
                if sub_id:
                    ed = (get_iraq_time() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                    db.execute_query("INSERT OR REPLACE INTO users (user_id, is_subscribed, subscription_end) VALUES (?,1,?)", (sub_id, ed))
                    await respond_premium(event, f"✅ تم اضافة {sub_id} لمدة {days} يوم")
                    del user_states[uid]
            except:
                await respond_premium(event, "⚠️ رقم غير صحيح")

        elif sn == "waiting_add_admin" and is_admin(uid):
            try:
                admin_id = int(event.text.strip())
                add_admin(admin_id, uid)
                await respond_premium(event, f"✅ تم اضافة المطور {admin_id}")
                del user_states[uid]
            except:
                await respond_premium(event, "⚠️ ايدي غير صحيح")
    finally:
        processing_users.discard(uid)


async def main():
    logger.info("Starting bot...")
    bot = TelegramClient('bot_session', API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    asyncio.create_task(check_expired_subscriptions(bot))

    @bot.on(events.NewMessage(pattern='/start'))
    async def sh(event): await start_command(event)

    @bot.on(events.NewMessage(pattern='/admin'))
    async def ah(event): await admin_command(event)

    @bot.on(events.CallbackQuery())
    async def ch(event): await handle_callbacks(event)

    @bot.on(events.NewMessage())
    async def mh(event):
        if event.is_private and event.text: await handle_messages(event)

    logger.info("Bot is running!")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
    except Exception as e:
        logger.error(f"Fatal error: {e}")