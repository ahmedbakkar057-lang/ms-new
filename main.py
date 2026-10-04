import logging
import requests
import re
import sqlite3
import json
import os
import fcntl
import tempfile
import random
import hashlib
import secrets
import shutil
import zipfile
import threading
import io
import asyncio
from html import escape as html_escape
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ApplicationHandlerStop
)
from werkzeug.security import generate_password_hash

# ─── مسارات البانل (لربط البوت مباشرة بملفات وإعدادات الموقع) ──────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
PANEL_DATA = (os.environ.get("BASE_PATH") or os.path.join(BASE_DIR, "panel_data")).strip()
USERS_FILE     = os.path.join(PANEL_DATA, "users.json")
PROCESSES_FILE = os.path.join(PANEL_DATA, "processes.json")
OWNER_CFG_FILE = os.path.join(PANEL_DATA, "owner_config.json")
IPS_FILE       = os.path.join(PANEL_DATA, "ips_pool.json")
BOT_DB         = os.path.join(PANEL_DATA, "bot_data.db")
os.makedirs(PANEL_DATA, exist_ok=True)

def _load_json(path, default=None):

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}

def _save_json(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".json-write-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass

_CUSTOM_EMOJI_TAG_RE = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", re.DOTALL | re.IGNORECASE)
_STANDARD_EMOJI_RE = re.compile(
    r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF"
    r"\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)

def strip_standard_emojis(value):
    """يحذف الإيموجي العادي مع إبقاء وسوم Telegram للإيموجي المميز كما هي."""
    if not isinstance(value, str):
        return value
    protected = []

    def protect(match):
        protected.append(match.group(0))
        return f"\x00CUSTOM_EMOJI_{len(protected) - 1}\x00"

    cleaned = _CUSTOM_EMOJI_TAG_RE.sub(protect, value)
    cleaned = _STANDARD_EMOJI_RE.sub("", cleaned)
    for index, tag in enumerate(protected):
        cleaned = cleaned.replace(f"\x00CUSTOM_EMOJI_{index}\x00", tag)
    return cleaned

def _clean_telegram_value(value):
    if isinstance(value, dict):
        return {key: _clean_telegram_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_telegram_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clean_telegram_value(item) for item in value)
    return strip_standard_emojis(value)

_requests_post = requests.post

def _telegram_safe_post(url, *args, **kwargs):
    """يمرر كل رسائل Telegram عبر فلتر الإيموجي العادي."""
    if "api.telegram.org/bot" in str(url) and "json" in kwargs:
        kwargs["json"] = _clean_telegram_value(kwargs["json"])
    return _requests_post(url, *args, **kwargs)

requests.post = _telegram_safe_post

# يُقرأ سر البوت من متغير البيئة فقط. لا تضع التوكن داخل الملف أو قاعدة البيانات.
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8695666087:AAGDsTgpn7Xhd32fJFxh-_a3jPoQ0QaBwcE").strip()
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN غير مضبوط في متغيرات البيئة")
try:
    OWNER_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "8122328795").strip())
except ValueError as exc:
    raise RuntimeError("TELEGRAM_ADMIN_ID يجب أن يكون رقماً صحيحاً") from exc

# إعدادات ربط البوت بلوحة الموقع. ضعها في متغيرات البيئة أو bot_config.json.
BOT_CONFIG_FILE = os.path.join(PANEL_DATA, "bot_config.json")
_bot_cfg = _load_json(BOT_CONFIG_FILE, {})
if not isinstance(_bot_cfg, dict):
    _bot_cfg = {}
_bot_cfg_changed = False
for _secret_key in ("admin_username", "admin_password"):
    if _secret_key in _bot_cfg:
        _bot_cfg.pop(_secret_key, None)
        _bot_cfg_changed = True
if _bot_cfg_changed:
    _save_json(BOT_CONFIG_FILE, _bot_cfg)
_legacy_owner_cfg = _load_json(OWNER_CFG_FILE, {})
if isinstance(_legacy_owner_cfg, dict) and any(k in _legacy_owner_cfg for k in ("telegram_token", "bot_token")):
    _legacy_owner_cfg.pop("telegram_token", None)
    _legacy_owner_cfg.pop("bot_token", None)
    _legacy_owner_cfg["bot_linked"] = False
    _save_json(OWNER_CFG_FILE, _legacy_owner_cfg)
PANEL_URL = (os.getenv("PANEL_URL") or _bot_cfg.get("panel_url") or "https://ms-panel.cc.cd").strip().rstrip("/")
if PANEL_URL and not PANEL_URL.startswith(("http://", "https://")):
    PANEL_URL = "https://" + PANEL_URL

# ─── صلاحية المالك الوحيد ───────────────────────────────────────────────────
def is_admin(user_id):
    try:
        return int(user_id) == OWNER_ID
    except (TypeError, ValueError):
        return False

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

IMAGE_URL = "https://ik.imagekit.io/apkqvdjhg/DO%20MORE_%20Premium%20Dark%20and%20Blue%204k%20Desktop%20Wallpaper.jpeg"
PROFILE_GIF_URL = "https://ik.imagekit.io/apkqvdjhg/Untitled%20(@galvesgal)%20on%20Tumblr.gif"
HOME_IMAGE_URL = "https://ik.imagekit.io/apkqvdjhg/HOME.jpeg"
CODE_IMAGE_URL = "https://ik.imagekit.io/apkqvdjhg/%D8%B5%D9%88%D8%B1%20%D9%83%D9%88%D9%84%20(1).jpeg?updatedAt=1781559877226"
SERVER_IMAGE_URL = "https://ik.imagekit.io/apkqvdjhg/IMG_20260701_184221_810.jpg"

WELCOME_TEXT = """<b>أهـلاً بـك Name فـي بـوت  𝑽𝑷𝑺 MS <tg-emoji emoji-id="5445120234623092340">🍽</tg-emoji>
هنـا تجـد سيـرفرات 𝑽𝑷𝑺 بلـغـه بـايثـون <tg-emoji emoji-id="5282843764451195532">🖥</tg-emoji>

مميـزاتـنا  : 
<blockquote>عـزل آمـن بيـن السـيرفـرات  <tg-emoji emoji-id="5271604874419647061">🔗</tg-emoji></blockquote>
<blockquote>حـمايـه قـويه ضـد الهجـمات  <tg-emoji emoji-id="5251203410396458957">🛡</tg-emoji></blockquote>
<blockquote>سـهـوله التحـكم فـي ملفـاتك <tg-emoji emoji-id="5361741454685256344">🎮</tg-emoji></blockquote>
<blockquote>اسـتقـرار النـظام الـدائــم  <tg-emoji emoji-id="5244837092042750681">📈</tg-emoji></blockquote>
<blockquote>مـراقـبه الشبـكه و حـمايه المـلفات من أي هجـمات  <tg-emoji emoji-id="5231012545799666522">🔍</tg-emoji></blockquote>
<blockquote>عـمل دائـم بـدون تـوقف أبـداً  <tg-emoji emoji-id="5451882707875276247">🕯</tg-emoji></blockquote>

<u><b>قـم بـالبــدء الآن نحـو القـمه <tg-emoji emoji-id="5449449325434266744">❄️</tg-emoji></b></u></b>"""

# =============================================================================
# قاعدة بيانات البوت — دائمة (لا يتم حذفها أبداً عند إعادة التشغيل) وموجودة
# داخل panel_data حتى تبقى بيانات النقاط/الحظر/الأكواد حقيقية ومستمرة
# =============================================================================
def init_db():
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        points INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0,
        join_date TEXT,
        servers TEXT DEFAULT '[]',
        invited_by INTEGER DEFAULT 0,
        invites INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.executemany('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', [
        ('default_price', '20'),
        ('default_days', '10'),
        ('default_storage', '512'),
        ('server_price', '20'),
        ('extra_2gb', '10'),
        ('extra_4gb', '18'),
        ('extra_6gb', '24'),
        ('extra_12gb', '40'),
        ('panel_url', ''),
        ('auto_backup_hours', '0'),
        ('notify_new_users', '1'),
        ('force_sub_enabled', '1'),
        ('force_sub_channel', '@store_MS_almohtal'),
        ('force_sub_channel_link', 'https://t.me/store_MS_almohtal'),
        ('force_sub_channels', '[]'),
        ('force_sub_join_button_text', ''),
        ('force_sub_verify_button_text', ''),
        ('proof_channel', ''),
        ('star_price', '1'),
        ('star_amount_label', 'دعم بنجمة'),
        ('bot_locked', '0'),
        ('bot_lock_msg', 'البوت في وضع الصيانة مؤقتاً 🔧'),
        ('referral_points', '2'),
    ])
    # ترحيل إعداد القناة القديمة إلى القائمة الجديدة مرة واحدة فقط.
    existing_channels = c.execute(
        "SELECT value FROM settings WHERE key = 'force_sub_channels'"
    ).fetchone()
    if not existing_channels or not existing_channels[0] or existing_channels[0] == "[]":
        legacy_channel = c.execute(
            "SELECT value FROM settings WHERE key = 'force_sub_channel'"
        ).fetchone()
        legacy_link = c.execute(
            "SELECT value FROM settings WHERE key = 'force_sub_channel_link'"
        ).fetchone()
        if legacy_channel and legacy_channel[0]:
            migrated = json.dumps([{
                "channel": legacy_channel[0].strip(),
                "link": (legacy_link[0] if legacy_link else "").strip(),
                "button_text": ""
            }], ensure_ascii=False)
            c.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('force_sub_channels', ?)",
                (migrated,)
            )
    c.execute('''CREATE TABLE IF NOT EXISTS redeem_codes (
        code TEXT PRIMARY KEY,
        points INTEGER DEFAULT 0,
        uses INTEGER DEFAULT 1,
        used_by TEXT DEFAULT '[]',
        created TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS referral_rewards (
        new_user_id INTEGER PRIMARY KEY,
        inviter_id INTEGER NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        credited_at TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pending_referrals (
        new_user_id INTEGER PRIMARY KEY,
        inviter_id INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()
    logger.info("✅ قاعدة بيانات البوت جاهزة (دائمة)")

def get_user(user_id):
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def save_pending_referral(new_user_id, inviter_id):
    conn = sqlite3.connect(BOT_DB)
    conn.execute(
        "INSERT OR REPLACE INTO pending_referrals (new_user_id, inviter_id, created_at) VALUES (?, ?, ?)",
        (int(new_user_id), int(inviter_id), datetime.now().isoformat())
    )
    conn.commit(); conn.close()

def pop_pending_referral(new_user_id):
    conn = sqlite3.connect(BOT_DB, timeout=15)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT inviter_id FROM pending_referrals WHERE new_user_id = ?", (int(new_user_id),)).fetchone()
        if row:
            conn.execute("DELETE FROM pending_referrals WHERE new_user_id = ?", (int(new_user_id),))
        conn.commit()
        return int(row[0]) if row else 0
    finally:
        conn.close()

def credit_referral(invited_by, new_user_id):
    """يضيف مكافأة الدعوة مرة واحدة فقط بعد نجاح التحقق والاشتراك."""
    try:
        invited_by, new_user_id = int(invited_by), int(new_user_id)
    except (TypeError, ValueError):
        return False
    if not invited_by or invited_by == new_user_id:
        return False
    try:
        points = max(0, int(get_setting("referral_points") or 2))
    except (TypeError, ValueError):
        points = 2
    if points <= 0:
        return False
    conn = sqlite3.connect(BOT_DB, timeout=15)
    try:
        conn.execute("BEGIN IMMEDIATE")
        inviter = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (invited_by,)).fetchone()
        referred = conn.execute("SELECT invited_by FROM users WHERE user_id = ?", (new_user_id,)).fetchone()
        if not inviter or not referred or int(referred[0] or 0) != invited_by:
            conn.rollback(); return False
        inserted = conn.execute(
            "INSERT OR IGNORE INTO referral_rewards (new_user_id, inviter_id, points, credited_at) VALUES (?, ?, ?, ?)",
            (new_user_id, invited_by, points, datetime.now().isoformat())
        ).rowcount
        if inserted != 1:
            conn.rollback(); return False
        conn.execute("UPDATE users SET points = points + ?, invites = invites + 1 WHERE user_id = ?", (points, invited_by))
        conn.commit()
    except Exception:
        conn.rollback(); logger.exception("خطأ أثناء احتساب مكافأة الدعوة"); return False
    finally:
        conn.close()
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
            "chat_id": invited_by,
            "text": f"<b>🎉 حصلت على {points} نقطة لأن صديقك تحقق واشترك عبر رابط دعوتك!</b>",
            "parse_mode": "HTML"
        })
    except Exception as e:
        logger.error("خطأ إشعار الداعي: %s", type(e).__name__)
    return True

def generate_captcha():
    """يولّد مسألة حسابية بسيطة ويُعيد (نص_السؤال, الجواب_الصحيح, قائمة_الخيارات)."""
    import random as _r
    op = _r.choice(['+', '-', '×'])
    if op == '+':
        a, b = _r.randint(1, 9), _r.randint(1, 9)
        answer = a + b
        q = f"{a} + {b}"
    elif op == '-':
        a = _r.randint(4, 12)
        b = _r.randint(1, a - 1)
        answer = a - b
        q = f"{a} - {b}"
    else:
        a, b = _r.randint(2, 5), _r.randint(2, 5)
        answer = a * b
        q = f"{a} × {b}"
    wrong = set()
    while len(wrong) < 3:
        delta = _r.choice([-3, -2, -1, 1, 2, 3])
        w = answer + delta
        if w > 0 and w != answer:
            wrong.add(w)
    options = list(wrong) + [answer]
    _r.shuffle(options)
    return q, answer, options

def send_captcha_message(chat_id, invited_by, edit_msg_id=None):
    """يرسل (أو يعدّل) رسالة التحقق بالكابتشا."""
    q, answer, options = generate_captcha()
    captcha_pending[chat_id] = {"invited_by": invited_by, "answer": answer}
    buttons = [
        [{"text": str(options[0]), "callback_data": f"captcha_ans_{options[0]}"},
         {"text": str(options[1]), "callback_data": f"captcha_ans_{options[1]}"}],
        [{"text": str(options[2]), "callback_data": f"captcha_ans_{options[2]}"},
         {"text": str(options[3]), "callback_data": f"captcha_ans_{options[3]}"}],
    ]
    text = (
        "<b>🤖 التحقق من الهوية\n\n"
        "حل هذه المسألة للمتابعة:\n\n"
        f"    <code>   {q}  =  ?   </code>\n\n"
        "اختر الإجابة الصحيحة 👇</b>"
    )
    if edit_msg_id:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/editMessageText", json={
            "chat_id": chat_id, "message_id": edit_msg_id,
            "text": text, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons}
        })
    else:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons}
        })

def add_user(user_id, username, first_name, last_name, invited_by=0, skip_ref_points=False):
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
    existed = c.fetchone() is not None
    c.execute('''INSERT OR IGNORE INTO users 
        (user_id, username, first_name, last_name, join_date, invited_by) 
        VALUES (?, ?, ?, ?, ?, ?)''',
        (user_id, username or "", first_name or "", last_name or "", datetime.now().isoformat(), invited_by))
    conn.commit()
    conn.close()
    # ─── إشعار المالك بمستخدم جديد ────────────────────────────────────────────
    if not existed and user_id != OWNER_ID:
        notify = get_setting("notify_new_users") or "1"
        if notify == "1":
            try:
                total_now = len(get_all_users())
                display   = first_name or last_name or "غير معروف"
                uname_str = f"@{username}" if username else "بدون معرف"
                notify_text = (
                    f"<b>🔔 مستخدم جديد دخل البوت!\n\n"
                    f"👤 الاسم  :  {display}\n"
                    f"🔗 اليوزر  :  {uname_str}\n"
                    f"🪪 الـ ID  :  <code>{user_id}</code>\n"
                    f"👥 إجمالي المستخدمين  :  {total_now}</b>"
                )
                notify_buttons = [[{"text": "👤 فتح ملف المستخدم", "callback_data": f"user_{user_id}"}]]
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
                    "chat_id": OWNER_ID,
                    "text": notify_text,
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": notify_buttons}
                })
            except Exception as e:
                logger.error("خطأ إشعار المالك: %s", type(e).__name__)

    # نقاط للداعي — تُضاف مباشرةً فقط إذا لم تكن مؤجلة للكابتشا
    if not skip_ref_points and not existed and invited_by and invited_by != user_id:
        credit_referral(invited_by, user_id)

def update_user_points(user_id, points):
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('UPDATE users SET points = ? WHERE user_id = ?', (points, user_id))
    conn.commit()
    conn.close()

def add_user_points(user_id, points):
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (points, user_id))
    conn.commit()
    conn.close()

def deduct_points_if_available(user_id, amount):
    amount = int(amount)
    if amount < 0:
        return False
    conn = sqlite3.connect(BOT_DB, timeout=15)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute('SELECT points FROM users WHERE user_id = ?', (int(user_id),)).fetchone()
        if not row or int(row[0] or 0) < amount:
            conn.rollback()
            return False
        changed = conn.execute(
            'UPDATE users SET points = points - ? WHERE user_id = ? AND points >= ?',
            (amount, int(user_id), amount)
        ).rowcount
        if changed != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        logger.exception("تعذر خصم نقاط عملية شراء")
        return False
    finally:
        conn.close()

def get_all_users():
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('SELECT user_id, first_name, username, points, is_banned FROM users')
    users = c.fetchall()
    conn.close()
    return users

def toggle_ban(user_id):
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    if result:
        new_status = 0 if result[0] == 1 else 1
        c.execute('UPDATE users SET is_banned = ? WHERE user_id = ?', (new_status, user_id))
        conn.commit()
        conn.close()
        return new_status
    conn.close()
    return None

def get_setting(key):
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('SELECT value FROM settings WHERE key = ?', (key,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def update_setting(key, value):
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
    conn.commit()
    conn.close()

def search_users(query):
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('''SELECT user_id, first_name, username, points, is_banned FROM users 
        WHERE CAST(user_id AS TEXT) LIKE ? OR first_name LIKE ? OR username LIKE ?''',
        (f'%{query}%', f'%{query}%', f'%{query}%'))
    results = c.fetchall()
    conn.close()
    return results

def get_user_servers(user_id):
    row = get_user(user_id)
    if not row:
        return []
    try:
        return json.loads(row[7] or '[]')
    except Exception:
        return []

def add_user_server(user_id, server_info):
    servers = get_user_servers(user_id)
    servers.append(server_info)
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('UPDATE users SET servers = ? WHERE user_id = ?', (json.dumps(servers, ensure_ascii=False), user_id))
    conn.commit()
    conn.close()

# ─── الأكواد (حقيقية 100%، مرتبطة بقاعدة البيانات الدائمة) ─────────────────────
def add_code(code, points, uses=1):
    code = str(code or '').strip().upper()
    points = int(points)
    uses = int(uses)
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('SELECT code FROM redeem_codes WHERE code = ?', (code,))
    if c.fetchone():
        conn.close()
        return False
    c.execute('INSERT INTO redeem_codes (code, points, uses, used_by, created) VALUES (?, ?, ?, ?, ?)',
               (code, points, uses, '[]', datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return True

def redeem_code(code, user_id):
    code = str(code or '').strip().upper()
    conn = sqlite3.connect(BOT_DB, timeout=15)
    try:
        c = conn.cursor()
        conn.execute('BEGIN IMMEDIATE')
        c.execute('SELECT points, uses, used_by FROM redeem_codes WHERE code = ?', (code,))
        row = c.fetchone()
        if not row:
            conn.rollback()
            return None, "not_found"
        points, uses, used_by_raw = row
        try:
            used_by = json.loads(used_by_raw or '[]')
        except (TypeError, ValueError):
            used_by = []
        if int(user_id) in {int(x) for x in used_by}:
            conn.rollback()
            return None, "already_used"
        if int(uses) <= 0:
            conn.rollback()
            return None, "exhausted"
        c.execute('SELECT points FROM users WHERE user_id = ?', (int(user_id),))
        if c.fetchone() is None:
            conn.rollback()
            logger.error("تعذر إضافة نقاط الكود: المستخدم %s غير موجود في قاعدة البيانات", user_id)
            return None, "user_not_found"
        used_by.append(int(user_id))
        c.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (int(points), int(user_id)))
        if c.rowcount != 1:
            conn.rollback()
            return None, "user_not_found"
        c.execute('UPDATE redeem_codes SET uses = uses - 1, used_by = ? WHERE code = ?',
                  (json.dumps(used_by), code))
        if c.rowcount != 1:
            conn.rollback()
            return None, "not_found"
        conn.commit()
        return int(points), "ok"
    except Exception:
        conn.rollback()
        logger.exception("خطأ أثناء استرداد كود النقاط")
        return None, "error"
    finally:
        conn.close()

def list_codes():
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('SELECT code, points, uses FROM redeem_codes')
    rows = c.fetchall()
    conn.close()
    return rows

def delete_code(code):
    conn = sqlite3.connect(BOT_DB)
    c = conn.cursor()
    c.execute('DELETE FROM redeem_codes WHERE code = ?', (code,))
    changed = c.rowcount > 0
    conn.commit()
    conn.close()
    return changed

# ─── ربط حقيقي بملفات البانل (نفس الملفات التي تديرها لوحة التحكم على الموقع) ──
def panel_load_users():
    return _load_json(USERS_FILE, {})

def panel_save_users(data):
    _save_json(USERS_FILE, data)

def panel_load_processes():
    return _load_json(PROCESSES_FILE, {})

def pw_hash(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def load_ips():
    return _load_json(IPS_FILE, {'available': [], 'assigned': {}})

def save_ips(data):
    _save_json(IPS_FILE, data)

def assign_ip(username):
    data = load_ips()
    assigned = data.setdefault('assigned', {})
    if username in assigned:
        return assigned[username]
    available = data.get('available', [])
    if available:
        ip = available.pop(0)
    else:
        ip = f"{random.randint(45,185)}.{random.randint(10,250)}.{random.randint(10,250)}.{random.randint(2,254)}"
    assigned[username] = ip
    save_ips(data)
    return ip

def create_panel_account(base_name, telegram_id, main_file, days, requested_username, requested_password):
    """ينشئ حساب لوحة حقيقياً في users.json المشتركة مع Flask دون حفظ كلمة المرور مكشوفة."""
    username = str(requested_username or "").strip()
    password = str(requested_password or "")
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        raise ValueError("اسم المستخدم غير صالح")
    if len(password) < 12 or len(password) > 64 or not re.fullmatch(r"[A-Za-z0-9!@#$%^&*._-]{12,64}", password):
        raise ValueError("كلمة المرور يجب أن تكون من 12 إلى 64 حرفاً من الحروف والأرقام والرموز المسموحة")
    lock_path = os.path.join(PANEL_DATA, ".users.lock")
    os.makedirs(PANEL_DATA, exist_ok=True)
    access_token = secrets.token_urlsafe(32)
    try:
        with open(lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            users = panel_load_users()
            if not isinstance(users, dict):
                users = {}
            if username in users:
                raise ValueError("اسم المستخدم مستخدم بالفعل، اختر اسماً آخر")
            users[username] = {
                "password": generate_password_hash(password),
                "max_sessions": 1,
                "max_servers": 1,
                "main_file": main_file,
                "created": datetime.now().isoformat(),
                "expiry": None,
                "telegram_id": int(telegram_id),
                "access_token": access_token,
            }
            os.makedirs(os.path.join(PANEL_DATA, "users_data", username), exist_ok=True)
            panel_save_users(users)
    except ValueError:
        raise
    except Exception as exc:
        logger.error("فشل إنشاء حساب اللوحة (%s)", type(exc).__name__)
        raise RuntimeError("تعذر إنشاء الحساب في لوحة الموقع") from None
    try:
        ip = assign_ip(username)
    except Exception as exc:
        logger.warning("تعذر تسجيل عنوان الحساب (%s)", type(exc).__name__)
        ip = "غير متاح"
    return username, password, ip, access_token

def finalize_server_creation(chat_id, user_id, buyer_name, pending, requested_password):
    """ينشئ السيرفر بعد اكتمال بيانات الدخول ويرسل بياناته للمستخدم."""
    price = int(pending.get("price", 0))
    if price < 0 or not deduct_points_if_available(user_id, price):
        send_custom_keyboard(
            chat_id,
            "<b>رصـيدك غيـر كـافـي لإتمـام العمليـة</b>",
            [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745",
              "style": "danger", "callback_data": "back_main"}]]
        )
        return False

    main_file = "main.py" if pending["lang"] == "python" else "index.js"
    try:
        uname, password, ip, access_token = create_panel_account(
            pending["server_name"],
            user_id,
            main_file,
            pending["days"],
            pending["requested_username"],
            requested_password,
        )
    except (ValueError, RuntimeError) as exc:
        add_user_points(user_id, price)
        send_custom_keyboard(
            chat_id,
            f"<b>{html_escape(str(exc))}</b>",
            [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745",
              "style": "danger", "callback_data": "back_main"}]]
        )
        return False

    add_user_server(user_id, {
        "username": uname,
        "lang": pending["lang"],
        "days": pending["days"],
        "created": datetime.now().isoformat()
    })
    panel_url = (os.environ.get("PANEL_URL") or get_setting("panel_url") or PANEL_URL).strip().rstrip("/")
    if panel_url and not panel_url.startswith(("http://", "https://")):
        panel_url = "https://" + panel_url
    direct_link = f"{panel_url.rstrip('/')}/access/{access_token}" if panel_url else None
    panel_port = os.environ.get("PANEL_PORT", "20048")
    panel_host = panel_url or f"http://{ip}:{panel_port}"
    caption = f"""<b><tg-emoji emoji-id="5258514780469075716">✨</tg-emoji> تـم إنشـاء السـيـرفر بـنجـاح

<tg-emoji emoji-id="5253959125838090076">🌐</tg-emoji> رابـط الموقع  :  <code>
  {html_escape(panel_url)}</code>
<tg-emoji emoji-id="5260399854500191689">👤</tg-emoji> اسم المستخدم  :  <code>{uname}</code>
<tg-emoji emoji-id="5447105316967693619">🔑</tg-emoji> كلمة المرور  :  <code>{password}</code>
<tg-emoji emoji-id="5257991477358763590">📅</tg-emoji> صالح لمدة  :  {pending['days']} يوم</b>"""
    buttons = []
    if panel_url:
        buttons.append([{"text": "دخول الموقع", "url": panel_url}])
    buttons.append([{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745",
                     "style": "danger", "callback_data": "back_main"}])
    send_photo_with_custom_keyboard(chat_id, SERVER_IMAGE_URL, caption, buttons)
    try:
        notify_proof_channel(
            uname=uname,
            password=password,
            ip=ip,
            access_token=access_token,
            days=pending["days"],
            buyer_name=buyer_name,
            buyer_id=user_id,
            panel_url=panel_url
        )
    except Exception as exc:
        logger.error("خطأ في إشعار قناة الإثبات: %s", type(exc).__name__)
    return True

init_db()

# ══════════════════════════════════════════════════════════════════════════════
# ─── نظام النسخ الاحتياطية ────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

_auto_backup_timer = None

def create_backup_zip():
    """ينشئ ملف ZIP يحتوي على كل بيانات البوت (panel_data كاملاً)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(PANEL_DATA):
            for root, dirs, files in os.walk(PANEL_DATA):
                for file in files:
                    fp = os.path.join(root, file)
                    arcname = os.path.relpath(fp, BASE_DIR)
                    zf.write(fp, arcname)
    buf.seek(0)
    return buf

def send_backup_to_chat(chat_id):
    """يرسل نسخة احتياطية كـ ZIP لـ chat_id المُحدد."""
    try:
        buf = create_backup_zip()
        now = datetime.now().strftime("%Y-%m-%d_%H-%M")
        total_users = len(get_all_users())
        caption = (
            f"<b>📦 نسخة احتياطية\n"
            f"🕐 التاريخ  :  {now}\n"
            f"👥 المستخدمون  :  {total_users}\n"
            f"✅ تم حفظ جميع البيانات</b>"
        )
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendDocument",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"document": (f"backup_{now}.zip", buf, "application/zip")}
        )
        logger.info(f"✅ تم إرسال النسخة الاحتياطية إلى {chat_id}")
    except Exception as e:
        logger.error("خطأ في إرسال النسخة الاحتياطية: %s", type(e).__name__)

def restore_from_zip(zip_bytes: bytes):
    """يستعيد البيانات من ملف ZIP."""
    buf = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(buf) as zf:
        zf.extractall(BASE_DIR)

def _auto_backup_job():
    """المهمة الدورية للنسخ التلقائي."""
    global _auto_backup_timer
    send_backup_to_chat(OWNER_ID)
    hours = int(get_setting("auto_backup_hours") or 0)
    if hours > 0:
        _auto_backup_timer = threading.Timer(hours * 3600, _auto_backup_job)
        _auto_backup_timer.daemon = True
        _auto_backup_timer.start()

def start_auto_backup(hours):
    """يُفعّل أو يُوقف النسخ التلقائي."""
    global _auto_backup_timer
    if _auto_backup_timer:
        _auto_backup_timer.cancel()
        _auto_backup_timer = None
    update_setting("auto_backup_hours", str(hours))
    if hours > 0:
        _auto_backup_timer = threading.Timer(hours * 3600, _auto_backup_job)
        _auto_backup_timer.daemon = True
        _auto_backup_timer.start()
        logger.info(f"✅ النسخ التلقائي مُفعّل كل {hours} ساعة")
    else:
        logger.info("⏹ تم إيقاف النسخ التلقائي")

# ══════════════════════════════════════════════════════════════════════════════

def get_main_keyboard(user_id=None):
    kbd = [
        [{"text": "ملفـي الشـخصـي", "icon_custom_emoji_id": "5801026762645183933", "style": "primary"}],
        [{"text": "شـراء نـقاط", "icon_custom_emoji_id": "5431492767249342908", "style": "success"}, {"text": "إسـتخـدام كــود", "icon_custom_emoji_id": "5208962449989988262", "style": "success"}],
        [{"text": "إنــشاء سـيرفـر", "icon_custom_emoji_id": "5397916757333654639", "style": "danger"}],
        [{"text": "سيـرفـراتـي", "icon_custom_emoji_id": "5800842714706615055", "style": "danger"}],
        [{"text": "نقـاط مـجانـاً", "icon_custom_emoji_id": "5406756500108501710", "style": "success"}, {"text": "الـمسـاعـده", "icon_custom_emoji_id": "5253742260054409879", "style": "success"}],
        [{"text": "الـدعـم الـفنـي", "icon_custom_emoji_id": "5472239203590888751", "style": "success"}, {"text": "𝑽𝑷𝑺 MS", "icon_custom_emoji_id": "5445379109481891622", "style": "primary"}],
    ]
    # ── زر لوحة الإدارة يظهر لجميع المشرفين تحت زر VPS MS ──────────────────
    if user_id and is_admin(user_id):
        if user_id == OWNER_ID:
            kbd.append([{"text": "👑 لـوحـة المـالـك", "icon_custom_emoji_id": "5282843764451195532", "style": "danger"}])
        else:
            kbd.append([{"text": "🗂 لـوحـة الإدارة", "icon_custom_emoji_id": "5282843764451195532", "style": "danger"}])
    return {"keyboard": kbd, "resize_keyboard": True}

def get_owner_keyboard():
    return {
        "keyboard": [
            [{"text": "🗂 لوحة التحكم", "icon_custom_emoji_id": "5282843764451195532", "style": "primary"}],
            [{"text": "🔙 رجوع للقائمة الرئيسية", "icon_custom_emoji_id": "4949575790002963745", "style": "danger"}]
        ],
        "resize_keyboard": True
    }

def get_owner_panel_buttons(is_owner=True):
    """أزرار لوحة التحكم الداخلية كـ inline keyboard."""
    lock_status = get_setting("bot_locked") or "0"
    lock_label = "🔓 فتح البوت" if lock_status == "1" else "🔒 قفل البوت"
    buttons = [
        [{"text": "👥 المستخدمون", "icon_custom_emoji_id": "5260399854500191689", "callback_data": "op_users"},
         {"text": "🖥 السيرفرات",  "icon_custom_emoji_id": "5282843764451195532", "callback_data": "op_servers"}],
        [{"text": "🎟 الأكواد",    "icon_custom_emoji_id": "5445279839902779679", "callback_data": "op_codes"},
         {"text": "📢 إذاعة",      "icon_custom_emoji_id": "5472239203590888751", "callback_data": "op_broadcast"}],
        [{"text": "⚙️ الإعدادات", "icon_custom_emoji_id": "5447105316967693619", "callback_data": "op_settings"}],
        [{"text": "📦 النسخ الاحتياطية", "callback_data": "op_backup"},
         {"text": "📣 الاشتراك الإجباري", "callback_data": "op_force_sub"}],
        [{"text": "⭐ دفع النجوم", "callback_data": "op_star_payment"},
         {"text": "📡 قناة الإثبات", "callback_data": "op_proof_channel"}],
        [{"text": "🎧 تذاكر الدعم الفني", "callback_data": "op_support_tickets"},
         {"text": lock_label, "callback_data": "op_bot_lock"}],
    ]
    buttons.append([{"text": "🔙 رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}])
    return buttons

def send_main_keyboard(chat_id, text, parse_mode="HTML", user_id=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": get_main_keyboard(user_id=user_id)
        })
    except Exception as e:
        logger.error("خطأ: %s", type(e).__name__)

def send_photo_with_main_keyboard(chat_id, photo_url, caption, parse_mode="HTML", user_id=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": parse_mode,
            "reply_markup": get_main_keyboard(user_id=user_id)
        })
    except Exception as e:
        logger.error("خطأ في إرسال الصورة: %s", type(e).__name__)

def send_custom_keyboard(chat_id, text, buttons, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": {"inline_keyboard": buttons}
        })
    except Exception as e:
        logger.error("خطأ: %s", type(e).__name__)

async def broadcast_original_message(update, context):
    """ينسخ رسالة المالك الأصلية مع الحفاظ على نوعها وكياناتها ووسائطها."""
    source_message = update.effective_message
    source_chat = update.effective_chat
    if not source_message or not source_chat:
        return

    sent = 0
    failed = 0
    for uid, _name, _username, _points, banned in get_all_users():
        if banned:
            continue
        try:
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=source_chat.id,
                message_id=source_message.message_id,
                reply_markup=source_message.reply_markup,
            )
            sent += 1
        except Exception as exc:
            failed += 1
            logger.warning("فشل نسخ الإذاعة إلى %s (%s)", uid, type(exc).__name__)
        # حد محافظ حتى لا تتجاوز الإذاعة حدود Telegram.
        await asyncio.sleep(0.05)

    user_steps[source_chat.id] = "owner_panel"
    send_custom_keyboard(
        source_chat.id,
        f"<b>تمت الإذاعة الأصلية بنجاح\n\n"
        f"تم الإرسال: <code>{sent}</code>\n"
        f"تعذر الإرسال: <code>{failed}</code></b>",
        [[{"text": "لوحة التحكم", "icon_custom_emoji_id": "5282843764451195532",
           "callback_data": "back_owner_panel"}]]
    )

async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يلتقط أي رسالة أثناء الإذاعة: نص، صورة، فيديو، ملف، صوت أو رسالة غنية."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    if chat.type != "private":
        return
    if not is_admin(user.id) or user_steps.get(chat.id) != "waiting_broadcast":
        return
    await broadcast_original_message(update, context)
    raise ApplicationHandlerStop

def send_photo_with_custom_keyboard(chat_id, photo_url, caption, buttons, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": parse_mode,
            "reply_markup": {"inline_keyboard": buttons}
        })
    except Exception as e:
        logger.error("خطأ: %s", type(e).__name__)

def edit_photo_caption(chat_id, message_id, caption, buttons, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/editMessageCaption"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption,
            "parse_mode": parse_mode,
            "reply_markup": {"inline_keyboard": buttons}
        })
    except Exception as e:
        logger.error("خطأ في تعديل كابشن الصورة: %s", type(e).__name__)

def edit_message_text(chat_id, message_id, text, buttons, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": {"inline_keyboard": buttons}
        })
    except Exception as e:
        logger.error("خطأ في تعديل النص: %s", type(e).__name__)

def send_owner_keyboard(chat_id, text, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": get_owner_keyboard()
        })
    except Exception as e:
        logger.error("خطأ: %s", type(e).__name__)

def send_photo_with_owner_keyboard(chat_id, photo_url, caption, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": parse_mode,
            "reply_markup": get_owner_keyboard()
        })
    except Exception as e:
        logger.error("خطأ في إرسال الصورة مع لوحة المدير: %s", type(e).__name__)

def get_bot_username():
    try:
        response = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe")
        data = response.json()
        return data["result"]["username"] if data.get("ok") else None
    except Exception:
        return None

BOT_USERNAME = get_bot_username()
if BOT_USERNAME:
    logger.info(f"✅ اسم البوت: @{BOT_USERNAME}")

# ── ملفات إضافية ─────────────────────────────────────────────────────────────
SUPPORT_TICKETS_FILE = os.path.join(PANEL_DATA, "support_tickets.json")

def load_support_tickets():
    try:
        with open(SUPPORT_TICKETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_support_tickets(data):
    with open(SUPPORT_TICKETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def send_star_invoice(chat_id):
    """يرسل فاتورة دفع نجوم."""
    price = int(get_setting("star_price") or 1)
    label = get_setting("star_amount_label") or "دعم بنجمة"
    url = f"https://api.telegram.org/bot{TOKEN}/sendInvoice"
    data = {
        "chat_id": chat_id,
        "title": label,
        "description": label,
        "payload": "star_support",
        "provider_token": "",
        "currency": "XTR",
        "prices": [{"label": label, "amount": price}],
        "reply_markup": json.dumps({
            "inline_keyboard": [[{"text": f"⭐ ادفع {price} نجمة", "pay": True}]]
        })
    }
    try:
        requests.post(url, json=data)
    except Exception as e:
        logger.error("خطأ في إرسال الفاتورة: %s", type(e).__name__)

def notify_proof_channel(uname, password, ip, access_token, days, buyer_name, buyer_id, panel_url=""):
    """يرسل إشعار الإثبات إلى القناة المحفوظة في لوحة تحكم البوت."""
    channel = (get_setting("proof_channel") or "").strip()
    if not channel:
        logger.warning("لم يتم إرسال إثبات الشراء: قناة الإثبات غير محددة من لوحة التحكم")
        return False
    bot_name = BOT_USERNAME or "vex_bot"
    # حساب تاريخ الانتهاء
    from datetime import datetime, timedelta
    expiry_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    safe_buyer_name = html_escape(str(buyer_name or ""), quote=False)
    safe_bot_name = html_escape(str(bot_name), quote=False)
    safe_uname = html_escape(str(uname or ""), quote=False)

    msg = (
        f"<tg-emoji emoji-id=\"5332614094585345389\">✅</tg-emoji>"
        f"<tg-emoji emoji-id=\"5327844718086733432\">✅</tg-emoji>"
        f"<tg-emoji emoji-id=\"5332815043220225405\">✅</tg-emoji>\n\n"
        f"<tg-emoji emoji-id=\"5922418627377631654\">✅</tg-emoji> "
        f"<b>تم توفير سيرفر VPS جديد</b> "
        f"<tg-emoji emoji-id=\"5197591528368787961\">🚀</tg-emoji>\n\n"
        f"<blockquote>"
        f"🛒 ¦ من ماركت : @{safe_bot_name} 🤍\n"
        f"<tg-emoji emoji-id=\"5258187907688055122\">📦</tg-emoji> ¦ نوع الخدمة :- <b><u>سيرفر VPS</u></b>\n"
        f"<tg-emoji emoji-id=\"5406806184290173437\">🖥</tg-emoji> ¦ اسم السيرفر :- <b><u><s>{safe_uname}</s></u></b>\n"
        f"<tg-emoji emoji-id=\"6334778871258286021\">💾</tg-emoji> ¦ الذاكرة (RAM) :- <b>[4 جيجابايت]</b>\n"
        f"<tg-emoji emoji-id=\"5805364911542376491\">⚡</tg-emoji> ¦ المعالج (CPU) :- <b>[2 نواة]</b>\n"
        f"<tg-emoji emoji-id=\"5215352343419167936\">💽</tg-emoji> ¦ المساحة :- <b>[80 جيجابايت SSD]</b>\n"
        f"<tg-emoji emoji-id=\"5208619406657082341\">⏳</tg-emoji> ¦ المدة :- <b>[{days}] يوم</b>\n"
        f"<tg-emoji emoji-id=\"5258226313285607065\">📅</tg-emoji> ¦ تاريخ الانتهاء :- <b>[{expiry_date}]</b>\n"
        f"<tg-emoji emoji-id=\"5224257782013769471\">💰</tg-emoji> ¦ الحالة :- <b>نشط ومعتمد "
        f"(<tg-emoji emoji-id=\"5922418627377631654\">✅</tg-emoji>)</b>\n\n"
        f"<tg-emoji emoji-id=\"5260399854500191689\">👤</tg-emoji> ¦ معلومات المُشتري :\n"
        f"<tg-emoji emoji-id=\"5296385246579670377\">🏷</tg-emoji> ¦ الاسم :- [{safe_buyer_name}]\n"
        f"<tg-emoji emoji-id=\"5253959125838090076\">🆔</tg-emoji> ¦ المعرف : ({buyer_id})\n\n"
        f"<tg-emoji emoji-id=\"5296369303661067030\">🔒</tg-emoji> ¦ تم إرسال بيانات الدخول (IP / SSH / Password) إلى المشتري عبر الخاص.\n"
        f"<tg-emoji emoji-id=\"5922389155312046911\">📌</tg-emoji> ¦ ملاحظة : يرجى من المشتري تغيير كلمة المرور فور استلامها."
        f"</blockquote>"
    )
    btn_text = f"@{safe_bot_name}"
    bot_url = f"https://t.me/{bot_name}"
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": channel,
                "text": msg,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [[{
                        "text": btn_text,
                        "icon_custom_emoji_id": "6015064898768215055",
                        "url": bot_url
                    }]]
                }
            },
            timeout=15
        )
        result = response.json()
        if not response.ok or not result.get("ok"):
            logger.error(
                f"فشل إرسال إثبات الشراء إلى القناة {channel}: "
                f"{result.get('description', response.text[:300])}"
            )
            return False
        message_id = result.get("result", {}).get("message_id")
        logger.info(
            f"✅ تم إرسال إثبات شراء السيرفر إلى قناة الإثبات {channel} "
            f"(message_id={message_id})"
        )
        return True
    except Exception as e:
        logger.error("خطأ في إرسال قناة الإثبات: %s", type(e).__name__)
        return False

user_steps = {}
pending_creation = {}
captcha_pending = {}   # {chat_id: {"invited_by": int, "answer": int}}
referral_pending = {}  # {chat_id: invited_by} — كابتشا محلولة وتنتظر الاشتراك

def is_banned(user_id):
    row = get_user(user_id)
    return bool(row and row[5])

# ══════════════════════════════════════════════════════════════════════════════
# ─── الاشتراك الإجباري ────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def load_force_sub_channels():
    """يقرأ قائمة القنوات المنظمة مع دعم الإعداد القديم للتوافق."""
    raw = get_setting("force_sub_channels") or "[]"
    try:
        channels = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        channels = []
    if not isinstance(channels, list):
        channels = []
    normalized = []
    for item in channels:
        if isinstance(item, str):
            item = {"channel": item, "link": "", "button_text": ""}
        if not isinstance(item, dict):
            continue
        channel = str(item.get("channel") or "").strip()
        if not channel or any(x["channel"] == channel for x in normalized):
            continue
        normalized.append({
            "channel": channel,
            "link": str(item.get("link") or "").strip(),
            "button_text": str(item.get("button_text") or "").strip(),
        })
    if not normalized:
        legacy = (get_setting("force_sub_channel") or "").strip()
        if legacy:
            normalized = [{
                "channel": legacy,
                "link": (get_setting("force_sub_channel_link") or "").strip(),
                "button_text": (get_setting("force_sub_join_button_text") or "").strip(),
            }]
    return normalized

def save_force_sub_channels(channels):
    clean = []
    for item in channels:
        channel = str(item.get("channel") or "").strip()
        if not channel or any(x["channel"] == channel for x in clean):
            continue
        clean.append({
            "channel": channel,
            "link": str(item.get("link") or "").strip(),
            "button_text": str(item.get("button_text") or "").strip(),
        })
    update_setting("force_sub_channels", json.dumps(clean, ensure_ascii=False))
    # إبقاء الإعداد القديم متزامناً مع أول قناة للتوافق مع النسخ القديمة.
    first = clean[0] if clean else {"channel": "", "link": ""}
    update_setting("force_sub_channel", first["channel"])
    update_setting("force_sub_channel_link", first["link"])

def force_sub_channel_label(channel):
    channel = str(channel or "").strip()
    if channel.startswith("@"):
        return channel[1:] or "القناة"
    return channel or "القناة"

def force_sub_join_url(item):
    link = str(item.get("link") or "").strip()
    channel = str(item.get("channel") or "").strip()
    if link:
        return link
    if channel.startswith("@"):
        return f"https://t.me/{channel.lstrip('@')}"
    return ""

def force_sub_verify_button_label():
    return (get_setting("force_sub_verify_button_text") or "").strip() or "تحقق من الاشتراك"

def check_subscription(user_id):
    """يتحقق مباشرة من Telegram؛ يجب أن يكون المستخدم مشتركاً في كل القنوات."""
    if (get_setting("force_sub_enabled") or "0") != "1":
        return True
    channels = load_force_sub_channels()
    if not channels:
        logger.error("الاشتراك الإجباري مفعّل لكن لا توجد قنوات محددة")
        return False
    for item in channels:
        channel = item["channel"]
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getChatMember",
                params={"chat_id": channel, "user_id": user_id},
                timeout=5
            ).json()
            if not resp.get("ok"):
                logger.error("تعذر التحقق من %s للمستخدم %s: %s",
                             channel, user_id, resp.get("description", "Telegram API error"))
                return False
            member = resp.get("result") or {}
            status = member.get("status")
            if not (
                status in ("member", "administrator", "creator")
                or (status == "restricted" and member.get("is_member", False))
            ):
                return False
        except Exception as exc:
            logger.error("فشل فحص القناة %s للمستخدم %s (%s)", channel, user_id, type(exc).__name__)
            return False
    return True

def force_sub_inline_buttons():
    """زر مستقل لكل قناة، ثم زر تحقق يفحص القائمة كلها."""
    buttons = []
    for item in load_force_sub_channels():
        join_url = force_sub_join_url(item)
        if join_url:
            buttons.append([{
                "text": item.get("button_text") or force_sub_channel_label(item["channel"]),
                "icon_custom_emoji_id": "5251203410396458957",
                "style": "primary",
                "url": join_url
            }])
    buttons.append([{
        "text": force_sub_verify_button_label(),
        "icon_custom_emoji_id": "5206607081334906820",
        "style": "success",
        "callback_data": "check_sub_verify"
    }])
    return buttons

def send_force_sub_message(chat_id):
    """يرسل رسالة تطلب الاشتراك مع زر الانضمام وزر التحقق."""
    channels = load_force_sub_channels()
    labels = "، ".join(force_sub_channel_label(x["channel"]) for x in channels)
    text = (
        "<b>🔒 الوصول متاح للمشتركين فقط</b>\n\n"
        "اشترك في جميع قنواتنا الرسمية لتفعيل البوت والاستفادة من جميع الخدمات:\n"
        f"<b>{html_escape(labels or 'قنوات الاشتراك')}</b>\n\n"
        "بعد الاشتراك اضغط على زر التحقق، وسيتم فحص عضويتك مباشرة من Telegram."
    )
    buttons = force_sub_inline_buttons()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons}
        }, timeout=10)
        result = response.json()
        if not response.ok or not result.get("ok"):
            logger.error("❌ فشل إرسال رسالة الاشتراك الإجباري: %s",
                         result.get("description", response.text[:300]))
    except requests.RequestException as exc:
        logger.error("❌ فشل اتصال إرسال رسالة الاشتراك الإجباري (%s)", type(exc).__name__)

def render_force_sub_panel(chat_id, message_id=None):
    """يعرض قائمة القنوات وإجراءات إدارتها داخل لوحة المالك."""
    channels = load_force_sub_channels()
    enabled = get_setting("force_sub_enabled") or "0"
    status = "مفعّل" if enabled == "1" else "موقوف"
    verify_button = get_setting("force_sub_verify_button_text") or "تحقق من الاشتراك"
    lines = [
        "<b>الاشتراك الإجباري</b>",
        "",
        f"الحالة: <code>{status}</code>",
        f"عدد القنوات: <code>{len(channels)}</code>",
        "",
    ]
    if channels:
        for index, item in enumerate(channels, 1):
            button_name = item.get("button_text") or force_sub_channel_label(item["channel"])
            link = item.get("link") or "رابط القناة تلقائياً"
            lines.append(
                f"<b>{index}.</b> <code>{html_escape(item['channel'])}</code>\n"
                f"الزر: <code>{html_escape(button_name)}</code> | الرابط: <code>{html_escape(link)}</code>"
            )
    else:
        lines.append("<i>لا توجد قنوات مضافة</i>")
    lines.append(f"\nاسم زر التحقق: <code>{html_escape(verify_button)}</code>")
    toggle_label = "إيقاف الاشتراك الإجباري" if enabled == "1" else "تفعيل الاشتراك الإجباري"
    buttons = [
        [{"text": toggle_label, "callback_data": "force_sub_toggle"}],
        [{"text": "إضافة قناة", "callback_data": "force_sub_add"}],
    ]
    for index, _item in enumerate(channels):
        buttons.append([
            {"text": f"تعديل {index + 1}", "callback_data": f"force_sub_edit_{index}"},
            {"text": f"حذف {index + 1}", "callback_data": f"force_sub_delete_{index}"},
        ])
    buttons.extend([
        [{"text": "تعديل اسم زر التحقق", "callback_data": "force_sub_edit_verify_text"}],
        [{"text": "معاينة رسالة الاشتراك", "callback_data": "force_sub_preview"}],
    ])
    if channels:
        buttons.append([{"text": "حذف جميع القنوات", "callback_data": "force_sub_delete_all"}])
    buttons.append([{"text": "رجوع", "callback_data": "back_owner_panel"}])
    if message_id is None:
        send_custom_keyboard(chat_id, "\n".join(lines), buttons)
    else:
        edit_message_text(chat_id, message_id, "\n".join(lines), buttons)

# ══════════════════════════════════════════════════════════════════════════════

def show_owner_panel(chat_id, message_id=None, is_owner=True):
    """يعرض لوحة التحكم الكاملة داخل البوت — إحصائيات + أزرار."""
    users         = get_all_users()
    total         = len(users)
    banned        = sum(1 for u in users if u[4])
    active        = total - banned
    codes         = list_codes()
    servers_count = sum(len(get_user_servers(u[0])) for u in users)
    text = (
        "<b><tg-emoji emoji-id=\"5282843764451195532\">🖥</tg-emoji>  لوحة التحكم</b>\n\n"
        f"<tg-emoji emoji-id=\"5260399854500191689\">👤</tg-emoji>  المستخدمون  :  <b>{total}</b>  "
        f"<tg-emoji emoji-id=\"5206607081334906820\">✔️</tg-emoji> نشط <b>{active}</b>  "
        f"<tg-emoji emoji-id=\"5253959125838090076\">👁</tg-emoji> محظور <b>{banned}</b>\n"
        f"<tg-emoji emoji-id=\"5282843764451195532\">🖥</tg-emoji>  السيرفرات  :  <b>{servers_count}</b>\n"
        f"<tg-emoji emoji-id=\"5445279839902779679\">💎</tg-emoji>  الأكواد  :  <b>{len(codes)}</b>\n\n"
        f"<tg-emoji emoji-id=\"5258368777350816286\">🪙</tg-emoji>  سعر السيرفر  :  <b>{get_setting('server_price')} نقطة</b>\n"
        f"<tg-emoji emoji-id=\"5257991477358763590\">↗️</tg-emoji>  المدة الافتراضية  :  <b>{get_setting('default_days')} يوم</b>\n"
        f"<tg-emoji emoji-id=\"5260450573768990626\">➡️</tg-emoji>  المساحة  :  <b>{get_setting('default_storage')} MB</b>\n"
            f"🎁 نقاط الدعوة  :  <b>{get_setting('referral_points') or 2} نقطة</b>"
    )
    buttons = get_owner_panel_buttons(is_owner=is_owner)
    if message_id:
        edit_message_text(chat_id, message_id, text, buttons)
    else:
        send_custom_keyboard(chat_id, text, buttons)


async def owner_panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /admin — يفتح لوحة التحكم للمالك الوحيد داخل المحادثة الخاصة."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if update.effective_chat.type != "private" or not is_admin(user.id):
        return
    user_steps[chat_id] = "owner_panel"
    show_owner_panel(chat_id, is_owner=(user.id == OWNER_ID))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("استخدم البوت في المحادثة الخاصة لبدء الاستخدام بأمان.")
        return
    user = update.effective_user
    user_name = user.first_name or "صديقي"
    chat_id = update.effective_chat.id
    user_id = user.id

    invited_by = 0
    if context.args and context.args[0].startswith("ref_"):
        try:
            invited_by = int(context.args[0][4:])
        except Exception:
            invited_by = 0

    # فحص إذا كان المستخدم جديداً قبل التسجيل
    is_new_user = get_user(user_id) is None

    # سجّل المستخدم — أجّل نقاط الدعوة إذا كانت عبر رابط (الكابتشا ستكملها)
    needs_captcha = is_new_user and bool(invited_by) and invited_by != user_id and not is_admin(user_id)
    add_user(user_id, user.username, user.first_name, user.last_name,
             invited_by=invited_by, skip_ref_points=needs_captcha)
    user_steps[chat_id] = "main"

    # الأدمن: لا كابتشا ولا اشتراك إجباري
    if is_admin(user_id):
        welcome_msg = WELCOME_TEXT.replace("Name", user_name)
        welcome_msg += "\n\n🔑 مرحباً بك يا مدير"
        send_photo_with_main_keyboard(chat_id, IMAGE_URL, welcome_msg, user_id=user_id)
        return

    # ─── عرض الكابتشا للمستخدم الجديد القادم عبر رابط دعوة ─────────────────
    if needs_captcha:
        send_captcha_message(chat_id, invited_by)
        return

    # ─── فحص الاشتراك الإجباري (لا يُطبّق على الأدمن) ──────────────────────
    if not check_subscription(user_id):
        send_force_sub_message(chat_id)
        return

    welcome_msg = WELCOME_TEXT.replace("Name", user_name)
    send_photo_with_main_keyboard(chat_id, IMAGE_URL, welcome_msg, user_id=user_id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    # أزرار Telegram تُرسل بعد إزالة الإيموجي العادي؛ نظّف الوارد أيضًا
    # حتى تعمل الأزرار القديمة والجديدة بنفس الشكل.
    text = strip_standard_emojis(update.message.text or "").strip()
    user = update.effective_user
    user_name = user.first_name or "صديقي"
    user_id = user.id
    chat_id = update.effective_chat.id

    logger.info("رسالة خاصة واردة من مستخدم البوت")

    current_step = user_steps.get(chat_id, "main")

    if is_admin(user_id):
        # ─── زر لوحة التحكم الرئيسي (القديم للمشرفين من خلال لوحة المالك البسيطة) ──
        if text in ("لوحة التحكم", "لـوحـة الإدارة", "لـوحـة المـالـك"):
            user_steps[chat_id] = "owner_panel"
            show_owner_panel(chat_id, is_owner=(user_id == OWNER_ID))
            return

        elif text == "رجوع للقائمة الرئيسية":
            user_steps[chat_id] = "main"
            welcome_msg = WELCOME_TEXT.replace("Name", user_name)
            welcome_msg += "\n\n🔑 مرحباً بك يا مدير"
            send_photo_with_main_keyboard(chat_id, IMAGE_URL, welcome_msg, user_id=user_id)
            return

        # ─── ردود الإعدادات النصية (button-based) ────────────────────────────
        elif current_step == "editing_server_price" and is_admin(user_id):
            if text.strip().isdigit():
                update_setting("server_price", text.strip())
                send_custom_keyboard(chat_id, f"<b>تم تحديث سعر السيرفر إلى <code>{text.strip()}</code> نقطة</b>",
                                     [[{"text": "الإعدادات", "icon_custom_emoji_id": "5447105316967693619", "callback_data": "op_settings"},
                                       {"text": "رجوع",     "icon_custom_emoji_id": "4949575790002963745", "callback_data": "back_owner_panel"}]])
            else:
                send_custom_keyboard(chat_id, "<b>أرسل رقماً صحيحاً فقط</b>",
                                     [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_settings"}]])
            user_steps[chat_id] = "owner_panel"
            return

        elif current_step == "editing_referral_points" and is_admin(user_id):
            if text.strip().isdigit():
                update_setting("referral_points", text.strip())
                send_custom_keyboard(chat_id, f"<b>تم تحديث نقاط الدعوة إلى <code>{text.strip()}</code> نقطة</b>",
                                     [[{"text": "الإعدادات", "callback_data": "op_settings"},
                                       {"text": "رجوع", "callback_data": "back_owner_panel"}]])
            else:
                send_custom_keyboard(chat_id, "<b>أرسل رقماً صحيحاً (0 أو أكبر)</b>",
                                     [[{"text": "إلغاء", "callback_data": "op_settings"}]])
            user_steps[chat_id] = "owner_panel"
            return

        elif current_step == "editing_default_days" and is_admin(user_id):
            if text.strip().isdigit():
                update_setting("default_days", text.strip())
                send_custom_keyboard(chat_id, f"<b>تم تحديث المدة الافتراضية إلى <code>{text.strip()}</code> يوم</b>",
                                     [[{"text": "الإعدادات", "icon_custom_emoji_id": "5447105316967693619", "callback_data": "op_settings"},
                                       {"text": "رجوع",     "icon_custom_emoji_id": "4949575790002963745", "callback_data": "back_owner_panel"}]])
            else:
                send_custom_keyboard(chat_id, "<b>أرسل رقماً صحيحاً فقط</b>",
                                     [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_settings"}]])
            user_steps[chat_id] = "owner_panel"
            return

        elif current_step == "editing_default_storage" and is_admin(user_id):
            if text.strip().isdigit():
                update_setting("default_storage", text.strip())
                send_custom_keyboard(chat_id, f"<b>تم تحديث المساحة إلى <code>{text.strip()}</code> MB</b>",
                                     [[{"text": "الإعدادات", "icon_custom_emoji_id": "5447105316967693619", "callback_data": "op_settings"},
                                       {"text": "رجوع",     "icon_custom_emoji_id": "4949575790002963745", "callback_data": "back_owner_panel"}]])
            else:
                send_custom_keyboard(chat_id, "<b>أرسل رقماً صحيحاً فقط</b>",
                                     [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_settings"}]])
            user_steps[chat_id] = "owner_panel"
            return

        elif current_step == "adding_code_name" and is_admin(user_id):
            code_name = text.strip().upper()
            if re.match(r'^[A-Za-z0-9]{2,20}$', code_name):
                pending_creation[chat_id] = {"code": code_name}
                user_steps[chat_id] = "adding_code_points"
                send_custom_keyboard(chat_id, f"<b>كود  :  <code>{code_name}</code>\n\nأرسل عدد النقاط</b>",
                                     [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_codes"}]])
            else:
                send_custom_keyboard(chat_id, "<b>اسم غير صالح، استخدم حروف إنجليزية وأرقام فقط (2-20 حرف)</b>",
                                     [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_codes"}]])
            return

        elif current_step == "adding_code_points" and is_admin(user_id):
            if text.strip().isdigit() and int(text.strip()) > 0:
                pending_creation[chat_id]["points"] = int(text.strip())
                user_steps[chat_id] = "adding_code_uses"
                send_custom_keyboard(chat_id, "<b>أرسل عدد مرات الاستخدام</b>",
                                     [[{"text": "مرة واحدة", "callback_data": "code_uses_1"},
                                       {"text": "بلا حد",   "callback_data": "code_uses_9999"}],
                                      [{"text": "إلغاء",    "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_codes"}]])
            else:
                send_custom_keyboard(chat_id, "<b>أرسل رقماً أكبر من صفر</b>",
                                     [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_codes"}]])
            return

        elif current_step == "adding_code_uses" and is_admin(user_id):
            if text.strip().isdigit() and int(text.strip()) > 0:
                uses = int(text.strip())
                p    = pending_creation.get(chat_id, {})
                ok   = add_code(p.get("code", ""), p.get("points", 0), uses)
                pending_creation.pop(chat_id, None)
                user_steps[chat_id] = "owner_panel"
                msg = (f"<b>تم إنشاء الكود\n\n<code>{p.get('code','')}</code>  —  {p.get('points',0)} نقطة  —  {uses} استخدام</b>"
                       if ok else "<b>الكود موجود مسبقاً</b>")
                send_custom_keyboard(chat_id, msg,
                                     [[{"text": "الأكواد", "icon_custom_emoji_id": "5445279839902779679", "callback_data": "op_codes"},
                                       {"text": "رجوع",    "icon_custom_emoji_id": "4949575790002963745", "callback_data": "back_owner_panel"}]])
            else:
                send_custom_keyboard(chat_id, "<b>أرسل رقماً أكبر من صفر</b>",
                                     [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_codes"}]])
            return

    if current_step == "searching_user" and is_admin(user_id):
        results = search_users(text.strip())
        if not results:
            buttons = [[{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_users"}]]
            send_custom_keyboard(chat_id, "<b>لا توجد نتائج</b>", buttons)
        else:
            buttons = []
            for uid, name, username, points, banned in results[:30]:
                display_name = name or username or str(uid)
                prefix = "✖ " if banned else ""
                buttons.append([{"text": f"{prefix}{display_name[:28]}", "callback_data": f"user_{uid}"}])
            buttons.append([{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_users"}])
            send_custom_keyboard(chat_id, f"<b>نتائج البحث عن «{text.strip()}»</b>", buttons)
        user_steps[chat_id] = "viewing_users"
        return

    if current_step.startswith("adding_points_") and is_admin(user_id):
        target_id = int(current_step.split("_")[2])
        amount_text = text.strip()
        if re.match(r'^[+-]\d+$', amount_text):
            delta = int(amount_text)
            add_user_points(target_id, delta)
            target_data = get_user(target_id)
            new_points = target_data[4] if target_data else 0
            buttons = [[{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": f"user_{target_id}"}]]
            action = "إضافة" if delta > 0 else "خصم"
            send_custom_keyboard(chat_id, f"<b>تم {action} {abs(delta)} نقطة  —  الرصيد الآن  :  {new_points}</b>", buttons)
            try:
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
                    "chat_id": target_id,
                    "text": f"<b>تم {'إضافة' if delta > 0 else 'خصم'} {abs(delta)} نقطة من قِبل الإدارة\nرصيدك الآن  :  {new_points} نقطة</b>",
                    "parse_mode": "HTML"
                })
            except Exception:
                pass
        else:
            buttons = [[{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": f"user_{target_id}"}]]
            send_custom_keyboard(chat_id, "<b>أرسل <code>+50</code> لإضافة أو <code>-20</code> للخصم</b>", buttons)
        user_steps[chat_id] = "viewing_users"
        return

    if current_step.startswith("sending_msg_") and is_admin(user_id):
        target_id = int(current_step.split("_")[2])
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
                "chat_id": target_id,
                "text": f"<b>رسالة من الإدارة :</b>\n\n{text}",
                "parse_mode": "HTML"
            })
            buttons = [[{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": f"user_{target_id}"}]]
            send_custom_keyboard(chat_id, "<b>تم إرسال الرسالة</b>", buttons)
        except Exception:
            buttons = [[{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": f"user_{target_id}"}]]
            send_custom_keyboard(chat_id, "<b>فشل الإرسال — ربما حظر المستخدم البوت</b>", buttons)
        user_steps[chat_id] = "viewing_users"
        return

    # ─── إعداد الاشتراك الإجباري متعدد القنوات (أدمن فقط) ───────────────────
    if current_step == "waiting_force_sub_channel" and is_admin(user_id):
        val = text.strip()
        if not val:
            send_custom_keyboard(chat_id, "<b>أرسل معرف قناة صالحاً</b>",
                                 [[{"text": "إلغاء", "callback_data": "op_force_sub"}]])
            return
        pending_creation[chat_id] = {"force_sub_channel": val}
        user_steps[chat_id] = "waiting_force_sub_link"
        send_custom_keyboard(chat_id,
            "<b>تم حفظ معرف القناة مؤقتاً\n\nأرسل رابط القناة، أو أرسل <code>تخطي</code> للرابط التلقائي</b>",
            [[{"text": "إلغاء", "callback_data": "op_force_sub"}]])
        return

    if current_step == "waiting_force_sub_link" and is_admin(user_id):
        val = "" if text.strip().lower() in ("تخطي", "skip", "-") else text.strip()
        pending = pending_creation.get(chat_id, {})
        channel = pending.get("force_sub_channel", "")
        if not channel:
            user_steps[chat_id] = "owner_panel"
            render_force_sub_panel(chat_id)
            return
        pending["force_sub_link"] = val
        user_steps[chat_id] = "waiting_force_sub_button_text"
        send_custom_keyboard(chat_id,
            "<b>أرسل اسم الزر الذي سيظهر للمستخدم، أو أرسل <code>تلقائي</code> ليكون باسم القناة فقط</b>",
            [[{"text": "إلغاء", "callback_data": "op_force_sub"}]])
        return

    if current_step == "waiting_force_sub_button_text" and is_admin(user_id):
        val = "" if text.strip().lower() in ("تلقائي", "default", "-") else text.strip()
        pending = pending_creation.get(chat_id, {})
        channel = pending.get("force_sub_channel", "")
        if not channel:
            user_steps[chat_id] = "owner_panel"
            render_force_sub_panel(chat_id)
            return
        channels = load_force_sub_channels()
        channels.append({"channel": channel, "link": pending.get("force_sub_link", ""), "button_text": val})
        save_force_sub_channels(channels)
        pending_creation.pop(chat_id, None)
        user_steps[chat_id] = "owner_panel"
        send_custom_keyboard(chat_id, "<b>تمت إضافة قناة الاشتراك بنجاح</b>",
            [[{"text": "الاشتراك الإجباري", "callback_data": "op_force_sub"},
              {"text": "لوحة التحكم", "callback_data": "back_owner_panel"}]])
        return

    if current_step == "waiting_force_sub_verify_text" and is_admin(user_id):
        val = text.strip()
        if not val:
            send_custom_keyboard(chat_id, "<b>أرسل اسماً صالحاً للزر</b>",
                                 [[{"text": "رجوع", "callback_data": "op_force_sub"}]])
            return
        update_setting("force_sub_verify_button_text", val)
        user_steps[chat_id] = "owner_panel"
        send_custom_keyboard(chat_id, "<b>تم تحديث اسم زر التحقق</b>",
            [[{"text": "الاشتراك الإجباري", "callback_data": "op_force_sub"},
              {"text": "لوحة التحكم", "callback_data": "back_owner_panel"}]])
        return

    if current_step == "waiting_force_sub_edit_channel" and is_admin(user_id):
        pending = pending_creation.get(chat_id, {})
        index = pending.get("force_sub_index")
        val = text.strip()
        channels = load_force_sub_channels()
        if not isinstance(index, int) or index < 0 or index >= len(channels) or not val:
            user_steps[chat_id] = "owner_panel"
            render_force_sub_panel(chat_id)
            return
        channels[index]["channel"] = val
        pending["force_sub_link"] = channels[index].get("link", "")
        pending["force_sub_button_text"] = channels[index].get("button_text", "")
        pending["force_sub_channel_value"] = val
        user_steps[chat_id] = "waiting_force_sub_edit_link"
        send_custom_keyboard(
            chat_id,
            "<b>أرسل رابط القناة الجديد، أو أرسل <code>حذف</code> للرابط التلقائي</b>",
            [[{"text": "إلغاء", "callback_data": "op_force_sub"}]]
        )
        return

    if current_step == "waiting_force_sub_edit_link" and is_admin(user_id):
        pending = pending_creation.get(chat_id, {})
        index = pending.get("force_sub_index")
        val = "" if text.strip().lower() in ("حذف", "مسح", "clear", "-") else text.strip()
        channels = load_force_sub_channels()
        if isinstance(index, int) and 0 <= index < len(channels):
            pending["force_sub_link"] = val
            user_steps[chat_id] = "waiting_force_sub_edit_button"
            send_custom_keyboard(
                chat_id,
                "<b>أرسل اسم زر القناة الجديد، أو أرسل <code>تلقائي</code> ليظهر باسم القناة</b>",
                [[{"text": "إلغاء", "callback_data": "op_force_sub"}]]
            )
        else:
            pending_creation.pop(chat_id, None)
            user_steps[chat_id] = "owner_panel"
            render_force_sub_panel(chat_id)
        return

    if current_step == "waiting_force_sub_edit_button" and is_admin(user_id):
        pending = pending_creation.get(chat_id, {})
        index = pending.get("force_sub_index")
        val = "" if text.strip().lower() in ("تلقائي", "default", "-") else text.strip()
        channels = load_force_sub_channels()
        if isinstance(index, int) and 0 <= index < len(channels):
            channels[index]["channel"] = pending.get("force_sub_channel_value", channels[index]["channel"])
            channels[index]["button_text"] = val
            channels[index]["link"] = pending.get("force_sub_link", "")
            save_force_sub_channels(channels)
        pending_creation.pop(chat_id, None)
        user_steps[chat_id] = "owner_panel"
        render_force_sub_panel(chat_id)
        return

    # ─── تعيين وقت النسخ التلقائي ─────────────────────────────────────────────
    if current_step == "waiting_auto_backup_hours" and is_admin(user_id):
        val = text.strip()
        if val.isdigit() and int(val) > 0:
            hours = int(val)
            start_auto_backup(hours)
            send_custom_keyboard(chat_id,
                f"<b>✅ تم تفعيل النسخ التلقائي كل <code>{hours}</code> ساعة</b>",
                [[{"text": "📦 النسخ الاحتياطية", "callback_data": "op_backup"},
                  {"text": "رجوع",               "callback_data": "back_owner_panel"}]])
        else:
            send_custom_keyboard(chat_id, "<b>❌ أرسل عدد الساعات كرقم صحيح أكبر من صفر</b>",
                [[{"text": "إلغاء", "callback_data": "op_backup"}]])
        user_steps[chat_id] = "owner_panel"
        return

    # ─── حظر حقيقي: أي مستخدم محظور (باستثناء المالك والمشرفين) لا يمكنه استخدام البوت ──
    if not is_admin(user_id) and is_banned(user_id):
        send_custom_keyboard(chat_id, "<b>🚫 تم حظرك من استخدام هذا البوت</b>", [])
        return

    # ─── قفل البوت: إذا كان البوت مقفلاً لا يتمكن المستخدمون العاديون من الاستخدام ──
    if not is_admin(user_id) and get_setting("bot_locked") == "1":
        lock_msg = get_setting("bot_lock_msg") or "البوت في وضع الصيانة مؤقتاً 🔧"
        send_custom_keyboard(chat_id, f"<b>⚙️ {lock_msg}</b>", [])
        return

    # ─── فحص الاشتراك الإجباري لكل رسالة (غير الأدمن) ────────────────────────
    if not is_admin(user_id) and not check_subscription(user_id):
        send_force_sub_message(chat_id)
        return

    # ─── زر الدعم الفني ───────────────────────────────────────────────────────
    if text == "الـدعـم الـفنـي":
        user_steps[chat_id] = "waiting_support_msg"
        buttons = [[{"text": "❌ إلغاء", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
        send_custom_keyboard(chat_id,
            "<b><tg-emoji emoji-id=\"5472239203590888751\">📩</tg-emoji> الدعم الفني\n\nأرسل رسالتك وسيصلك رد من فريق الدعم قريباً\n\n💬 اكتب رسالتك الآن:</b>",
            buttons)
        return

    # ─── معالجة رسالة الدعم الفني من المستخدم ────────────────────────────────
    if current_step == "waiting_support_msg":
        ticket_id = f"{user_id}_{int(datetime.now().timestamp())}"
        tickets = load_support_tickets()
        tickets[ticket_id] = {
            "user_id": user_id,
            "user_name": user_name,
            "message": text,
            "timestamp": datetime.now().isoformat(),
            "status": "open",
            "reply": None
        }
        save_support_tickets(tickets)
        # أرسل إلى المالك مع زر رد
        owner_msg = (
            f"<b>🎧 تذكرة دعم فني جديدة\n\n"
            f"👤 المستخدم : {user_name}\n"
            f"🆔 المعرف : <code>{user_id}</code>\n\n"
            f"💬 الرسالة :\n{text}</b>"
        )
        reply_btn_data = f"reply_ticket_{ticket_id}"
        try:
            requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={
                    "chat_id": OWNER_ID,
                    "text": owner_msg,
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps({
                        "inline_keyboard": [[{"text": "↩️ رد على المستخدم", "callback_data": reply_btn_data}]]
                    })
                }
            )
        except Exception as e:
            logger.error("خطأ في إرسال تذكرة الدعم للمالك: %s", type(e).__name__)
        user_steps[chat_id] = "main"
        buttons = [[{"text": "🏠 الرئيسية", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
        send_custom_keyboard(chat_id,
            "<b>✅ تم إرسال رسالتك بنجاح!\n\nسيصلك رد من فريق الدعم قريباً 🕐</b>",
            buttons)
        return

    # ─── معالجة رد المالك على تذكرة دعم ────────────────────────────────────
    if current_step.startswith("replying_ticket_") and is_admin(user_id):
        ticket_id = current_step[len("replying_ticket_"):]
        tickets = load_support_tickets()
        ticket = tickets.get(ticket_id)
        if ticket:
            ticket["reply"] = text
            ticket["status"] = "replied"
            save_support_tickets(tickets)
            target_user_id = ticket["user_id"]
            reply_msg = (
                f"<b>📩 رد من فريق الدعم الفني :\n\n"
                f"💬 {text}</b>"
            )
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    json={
                        "chat_id": target_user_id,
                        "text": reply_msg,
                        "parse_mode": "HTML"
                    }
                )
                send_custom_keyboard(chat_id, "<b>✅ تم إرسال الرد للمستخدم بنجاح!</b>",
                    [[{"text": "🎧 التذاكر", "callback_data": "op_support_tickets"},
                      {"text": "🔙 لوحة التحكم", "callback_data": "back_owner_panel"}]])
            except Exception as e:
                send_custom_keyboard(chat_id, f"<b>❌ فشل إرسال الرد: {e}</b>",
                    [[{"text": "🔙 رجوع", "callback_data": "op_support_tickets"}]])
        else:
            send_custom_keyboard(chat_id, "<b>❌ التذكرة غير موجودة</b>",
                [[{"text": "🔙 رجوع", "callback_data": "op_support_tickets"}]])
        user_steps[chat_id] = "owner_panel"
        return

    # ─── معالجة إعدادات النجوم ───────────────────────────────────────────────
    if current_step == "waiting_star_price" and is_admin(user_id):
        val = text.strip()
        if val.isdigit() and int(val) > 0:
            update_setting("star_price", val)
            send_custom_keyboard(chat_id,
                f"<b>✅ تم تحديث سعر النجوم إلى <code>{val}</code> نجمة</b>",
                [[{"text": "⭐ دفع النجوم", "callback_data": "op_star_payment"},
                  {"text": "🔙 رجوع", "callback_data": "back_owner_panel"}]])
        else:
            send_custom_keyboard(chat_id, "<b>❌ أرسل رقماً صحيحاً أكبر من صفر</b>",
                [[{"text": "إلغاء", "callback_data": "op_star_payment"}]])
        user_steps[chat_id] = "owner_panel"
        return

    if current_step == "waiting_star_label" and is_admin(user_id):
        update_setting("star_amount_label", text.strip())
        send_custom_keyboard(chat_id,
            f"<b>✅ تم تحديث وصف النجوم</b>",
            [[{"text": "⭐ دفع النجوم", "callback_data": "op_star_payment"},
              {"text": "🔙 رجوع", "callback_data": "back_owner_panel"}]])
        user_steps[chat_id] = "owner_panel"
        return

    # ─── إعداد قناة الإثبات ──────────────────────────────────────────────────
    if current_step == "waiting_proof_channel" and is_admin(user_id):
        val = text.strip()
        update_setting("proof_channel", val)
        send_custom_keyboard(chat_id,
            f"<b>✅ تم تعيين قناة الإثبات : <code>{val}</code></b>",
            [[{"text": "📡 قناة الإثبات", "callback_data": "op_proof_channel"},
              {"text": "🔙 رجوع", "callback_data": "back_owner_panel"}]])
        user_steps[chat_id] = "owner_panel"
        return

    # ─── إعداد رسالة قفل البوت ───────────────────────────────────────────────
    if current_step == "waiting_lock_msg" and is_admin(user_id):
        update_setting("bot_lock_msg", text.strip())
        send_custom_keyboard(chat_id,
            "<b>✅ تم تحديث رسالة الصيانة</b>",
            [[{"text": "🔒 قفل البوت", "callback_data": "op_bot_lock"},
              {"text": "🔙 رجوع", "callback_data": "back_owner_panel"}]])
        user_steps[chat_id] = "owner_panel"
        return

    if text == "ملفـي الشـخصـي":
        user_data = get_user(user_id)
        points = user_data[4] if user_data else 0
        n_servers = len(get_user_servers(user_id))
        invites = user_data[9] if user_data and len(user_data) > 9 else 0
        profile_text = f"""<b>
<tg-emoji emoji-id="5258514780469075716">📂</tg-emoji> ملف {user_name} الشخصي :

<tg-emoji emoji-id="5260399854500191689">👤</tg-emoji> الاسم  :  {user_name}
<tg-emoji emoji-id="5253959125838090076">👁</tg-emoji> المعرف  :  {user_id}
<tg-emoji emoji-id="5258368777350816286">🪙</tg-emoji> النقاط  :  {points}
<tg-emoji emoji-id="5257991477358763590">↗️</tg-emoji> السيرفرات  :  {n_servers:02d}
<tg-emoji emoji-id="5260450573768990626">➡️</tg-emoji> الـدعـوات  :   {invites:02d}
</b>"""
        buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
        send_photo_with_custom_keyboard(chat_id, PROFILE_GIF_URL, profile_text, buttons)
        return

    elif text == "شـراء نـقاط":
        response = """<b>لــ شـراء نقـاط علـيك إخـتيار طـريـقه الــدفع من الأزرار التـالـيه <tg-emoji emoji-id="5472250091332993630">💳</tg-emoji></b>"""
        buttons = [
            [{"text": "Ton", "icon_custom_emoji_id": "5445055054199411000", "style": "success", "callback_data": "pay_ton"}, {"text": "نـجوم تـليـجرام", "icon_custom_emoji_id": "5258165702707125574", "style": "success", "callback_data": "pay_stars"}],
            [{"text": "Vodafone Cash", "icon_custom_emoji_id": "5258204546391351475", "style": "success", "callback_data": "pay_voda"}, {"text": "طـرق دفـع أُخـرى", "icon_custom_emoji_id": "5447105316967693619", "style": "success", "callback_data": "pay_other"}],
            [{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]
        ]
        send_photo_with_custom_keyboard(chat_id, HOME_IMAGE_URL, response, buttons)
        return

    elif text == "إسـتخـدام كــود":
        response = """<b>إسـتـخدام كـود خـصم أو سـيرفـر  :  
أدخل الكود الخاص بك للحصول على نقاط أو سـيرفـر <tg-emoji emoji-id="5445279839902779679">💎</tg-emoji></b>"""
        buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
        send_photo_with_custom_keyboard(chat_id, CODE_IMAGE_URL, response, buttons)
        user_steps[chat_id] = "waiting_code"
        return

    elif text == "إنــشاء سـيرفـر":
        response = """<b>للـمتابـعة عـليـك إخـتيـار لغـه الـبرمـجه المـطلـوبـه <tg-emoji emoji-id="5913410302841458295">👨‍💻</tg-emoji></b>"""
        buttons = [
            [{"text": "Python", "icon_custom_emoji_id": "5276529733029339480", "style": "success", "callback_data": "lang_python"}, {"text": "Javascript", "icon_custom_emoji_id": "5278742461525532106", "style": "success", "callback_data": "lang_javascript"}],
            [{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]
        ]
        send_photo_with_custom_keyboard(chat_id, SERVER_IMAGE_URL, response, buttons)
        return

    elif text == "سيـرفـراتـي":
        servers = get_user_servers(user_id)
        response = "<b>قـائـمة السـيـرفـرات الـخاصـه بـك <tg-emoji emoji-id='5282843764451195532'>🖥</tg-emoji></b>"
        if not servers:
            buttons = [[{"text": "🚫 لا يـوجـد لــديـك سـيرفـرات بعـد", "callback_data": "no_servers"}]]
        else:
            buttons = []
            panel_users = panel_load_users()
            for s in servers:
                uname = s.get("username", "?")
                info = panel_users.get(uname, {})
                expiry = info.get("expiry", "")
                expired = False
                if expiry:
                    try:
                        expired = datetime.fromisoformat(expiry) < datetime.now()
                    except Exception:
                        expired = False
                status = "🔴 منتهي" if expired else "🟢 نشط"
                buttons.append([{"text": f"{status} | {uname}", "callback_data": f"myserver_{uname}"}])
        send_custom_keyboard(chat_id, response, buttons)
        return

    elif text == "نقـاط مـجانـاً":
        invite_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}" if BOT_USERNAME else "رابط غير متاح"
        response = f"""<b>للـحصـول علـى نقـاط مـجانيـه علـيك إتـباع الخـطوات الـتالـيه <tg-emoji emoji-id="5235794253149394263">🗺</tg-emoji>

- قـم بنسخ رابـط الدعـوه الخاص بك من الأسفل
- قـم بإرسـال رابـط الدعـوه إلـي أصـدقـائـك 
- بـمجـرد دخـول كـل شخـص عـبر الـرابـط الـخـاص بـك ستحـصـل علـى نقاط الدعـوة المحددة من الإدارة مجـانـاً

<u><b>رابط الدعوة الخاص بك:</b></u>
<code>{invite_link}</code>

- سيـتم تنـبيهـك بـكل عـملـيه دعـوه نـاجحـة <tg-emoji emoji-id="5235570365094188078">💬</tg-emoji></b>"""
        buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
        send_custom_keyboard(chat_id, response, buttons)
        return

    elif text == "الـمسـاعـده":
        response = """<b><tg-emoji emoji-id="5440410042773824003">🔗</tg-emoji> المـسـاعـدة والــدعـم :

<tg-emoji emoji-id="5440410042773824003">🔗</tg-emoji> دليل الاستخدام  :
- اضغط على '<u><b>إنــشاء سـيرفـر</b></u>' لإنشاء سيرفر جديد
- اضغط على '<u><b>سيـرفـراتـي</b></u>' لعرض سيرفراتك
- اضغط على '<u><b>شـراء نـقاط</b></u>' لشراء نقاط إضافية
- اضغط على '<u><b>ملفـي الشـخصـي</b></u>' لعرض معلوماتك

<tg-emoji emoji-id="5472239203590888751">📩</tg-emoji> للـتواصـل مـع الـدعـم  : 
- اضغط على 'الدعم الفني' في الأسفل للـتواصـل مع مسـؤول الـدعم</b>"""
        buttons = [
            [{"text": "الدعم الفني", "icon_custom_emoji_id": "5440410042773824003", "style": "primary", "url": "https://t.me/store_MS_almohtal"}],
            [{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]
        ]
        send_custom_keyboard(chat_id, response, buttons)
        return

    elif text == "𝑽𝑷𝑺 MS":
        response = "<b>تـم التـطـوير بالــكامــــــل بــــواسـطه MS \nمـمـنوع التقـليـد \nهـذا هـو الـبوت الـرسـمي و الـــــوحيـد لـدينـــــــا</b>"
        buttons = [[{"text": "@M_S_T_G0", "url": "https://t.me/M_S_T_G0"}]]
        send_custom_keyboard(chat_id, response, buttons)
        return

    else:
        if user_steps.get(chat_id) == "waiting_code":
            code_input = text.strip().upper()
            if re.match(r'^[A-Za-z0-9]+$', code_input):
                points, status = redeem_code(code_input, user_id)
                buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
                if status == "ok":
                    current_user = get_user(user_id)
                    current_balance = current_user[4] if current_user else 0
                    response = f"""<b>تـم إسـترداد الـكـود بنـجاح <tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>
القـيـمه  :  {points} نقـطـه
الرصـيـد الحـالي  :  {current_balance} نقـطـه</b>"""
                elif status == "already_used":
                    response = "<b>لقـد إسـتخـدمت هـذا الـكود مـن قبـل</b>"
                elif status == "exhausted":
                    response = "<b>هـذا الـكود إسـتهـلك كـل مـرات إسـتخـدامـه</b>"
                elif status == "user_not_found":
                    response = "<b>حسابك غير مسجل في قاعدة البيانات، أرسل /start ثم جرّب الكود مرة أخرى</b>"
                else:
                    response = "<b>الـكود الـذي أدخـلـتـه غـيـر صحـيـح</b>"
                send_custom_keyboard(chat_id, response, buttons)
                user_steps[chat_id] = "main"
            else:
                response = """<b>الرجاء إدخال كود صحيح (حروف إنجليزية وأرقام فقط)</b>"""
                buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
                send_custom_keyboard(chat_id, response, buttons)
        elif user_steps.get(chat_id) == "waiting_days":
            days_text = text.strip()
            if not days_text.isdigit():
                buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
                send_custom_keyboard(chat_id, "<b>الـرجـاء إدخـال عـدد أيـام صحيـح</b>", buttons)
                return
            days = int(days_text)
            if days < 10 or days % 10 != 0:
                buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
                send_custom_keyboard(chat_id, "<b>أقـل مـده هـي 10 أيـام، ويـجب أن تـكون مـضاعفـات 10</b>", buttons)
                return
            server_price = int(get_setting('server_price') or 20)
            price = (days // 10) * server_price
            user_data = get_user(user_id)
            points = user_data[4] if user_data else 0
            if points < price:
                buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
                send_custom_keyboard(chat_id, f"<b>رصـيدك غـيـر كـافـي\nالـمطلـوب  :  {price} نقـطـة\nرصـيدك الـحالـي  :  {points} نقـطـة</b>", buttons)
                return
            lang = pending_creation.get(chat_id, {}).get("lang", "python")
            pending_creation[chat_id] = {"lang": lang, "days": days, "price": price}
            user_steps[chat_id] = "waiting_server_name"
            buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
            send_custom_keyboard(chat_id, "<b>الآن قـم بـإرسـال إسـم لـلـسيـرفـر الـخاص بـك</b>", buttons)
        elif user_steps.get(chat_id) == "waiting_server_name":
            server_name = text.strip()
            if not re.match(r'^[A-Za-z0-9_]{3,20}$', server_name):
                buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
                send_custom_keyboard(chat_id, "<b>إسـم غيـر صحـيح، إسـتخـدم حـروف إنجـليزيـة وأرقـام فقـط (3-20 حرف)</b>", buttons)
                return
            pending = pending_creation.get(chat_id)
            if not pending:
                buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
                send_custom_keyboard(chat_id, "<b>حـدث خطـأ، إبـدأ عمليـة إنشـاء السيـرفـر مـن جـديـد</b>", buttons)
                user_steps[chat_id] = "main"
                return
            user_data = get_user(user_id)
            points = user_data[4] if user_data else 0
            price = pending["price"]
            if points < price:
                buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
                send_custom_keyboard(chat_id, "<b>رصـيدك غيـر كـافـي لإتمـام العمليـة</b>", buttons)
                user_steps[chat_id] = "main"
                return
            pending["server_name"] = server_name
            user_steps[chat_id] = "waiting_panel_username"
            buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
            send_custom_keyboard(
                chat_id,
                "<b>أرسل اسم المستخدم الذي ستدخل به إلى لوحة السيرفر\n"
                "استخدم 3 إلى 20 حرفاً إنجليزياً أو رقماً أو <code>_</code></b>",
                buttons
            )
        elif user_steps.get(chat_id) == "waiting_panel_username":
            requested_username = text.strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", requested_username):
                buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
                send_custom_keyboard(
                    chat_id,
                    "<b>اسم المستخدم غير صحيح\n"
                    "استخدم 3 إلى 20 حرفاً إنجليزياً أو رقماً أو <code>_</code></b>",
                    buttons
                )
                return
            if requested_username in panel_load_users():
                buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
                send_custom_keyboard(chat_id, "<b>اسم المستخدم مستخدم بالفعل، أرسل اسماً آخر</b>", buttons)
                return
            pending = pending_creation.get(chat_id)
            if not pending:
                user_steps[chat_id] = "main"
                send_main_keyboard(chat_id, "<b>انتهت عملية الإنشاء، ابدأ من جديد</b>", user_id=user_id)
                return
            pending["requested_username"] = requested_username
            user_steps[chat_id] = "waiting_panel_password"
            buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
            send_custom_keyboard(chat_id, "<b>أرسل كلمة مرور السيرفر\nيجب أن تكون من 12 إلى 64 حرفاً وبدون مسافات.</b>", buttons)
        elif user_steps.get(chat_id) == "waiting_panel_password":
            requested_password = text.strip()
            if not re.fullmatch(r"[A-Za-z0-9!@#$%^&*._-]{12,64}", requested_password):
                buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
                send_custom_keyboard(
                    chat_id,
                    "<b>كلمة المرور غير صحيحة\n"
                    "استخدم من 12 إلى 64 حرفاً وبدون مسافات</b>",
                    buttons
                )
                return
            pending = pending_creation.get(chat_id)
            if not pending or not pending.get("requested_username"):
                user_steps[chat_id] = "main"
                send_main_keyboard(chat_id, "<b>انتهت عملية الإنشاء، ابدأ من جديد</b>", user_id=user_id)
                return
            if finalize_server_creation(chat_id, user_id, user_name, pending, requested_password):
                pending_creation.pop(chat_id, None)
                user_steps[chat_id] = "main"
        else:
            send_main_keyboard(chat_id, f"❓ {user_name}، أرجو استخدام الأزرار أدناه للتنقل:", user_id=user_id)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query.message or query.message.chat.type != "private":
        await query.answer("استخدم البوت في المحادثة الخاصة", show_alert=True)
        return
    await query.answer()
    chat_id = query.message.chat.id
    message_id = query.message.message_id
    user = update.effective_user
    user_name = user.first_name or "صديقي"
    user_id = user.id

    logger.info(f"Callback: {query.data}")
    if not is_admin(user_id):
        customer_callbacks = {
            "back_main", "no_servers", "pay_ton", "pay_stars", "pay_voda", "pay_other",
            "back_payment", "lang_python", "lang_javascript", "skip_storage",
            "storage_2", "storage_4", "storage_6", "storage_12", "confirm_payment",
            "check_sub_verify",
        }
        callback_data = str(query.data or "")
        if callback_data not in customer_callbacks and not callback_data.startswith(("myserver_", "captcha_ans_")):
            return
    # ─── حارس الاشتراك: لا يمكن تجاوز الاشتراك عبر أي زر من أزرار البوت ───
    # نسمح فقط بزر التحقق والكابتشا حتى يستطيع المستخدم إكمال مسار التفعيل.
    subscription_exempt_callbacks = {"check_sub_verify"}
    if (
        not is_admin(user_id)
        and query.data not in subscription_exempt_callbacks
        and not query.data.startswith("captcha_ans_")
        and not check_subscription(user_id)
    ):
        await query.answer("اشترك في قناة MR VPS أولاً", show_alert=True)
        send_force_sub_message(chat_id)
        return

    if not is_admin(user_id) and is_banned(user_id) and query.data != "back_main":
        return

    if query.data == "back_main":
        try:
            await query.message.delete()
        except Exception:
            pass
        welcome_msg = WELCOME_TEXT.replace("Name", user_name)
        if is_admin(user_id):
            welcome_msg += "\n\n🔑 مرحباً بك يا مدير"
        send_photo_with_main_keyboard(chat_id, IMAGE_URL, welcome_msg, user_id=user_id)
        user_steps[chat_id] = "main"
        return

    elif query.data in ("back_owner", "back_owner_panel"):
        if not is_admin(user_id):
            return
        try:
            await query.message.delete()
        except Exception:
            pass
        user_steps[chat_id] = "owner_panel"
        show_owner_panel(chat_id, is_owner=(user_id == OWNER_ID))
        return

    # ─── أزرار لوحة التحكم الداخلية ─────────────────────────────────────────

    elif query.data == "op_users":
        users = get_all_users()
        if not users:
            edit_message_text(chat_id, message_id, "📭 لا يوجد مستخدمين حالياً",
                              [[{"text": "🔙 لوحة التحكم", "callback_data": "back_owner_panel"}]])
            return
        buttons = []
        for uid, name, username, pts, banned in users[:50]:
            display = name or username or str(uid)
            prefix  = "✖ " if banned else ""
            buttons.append([{"text": f"{prefix}{display[:28]}", "callback_data": f"user_{uid}"}])
        buttons.append([{"text": "بحث", "icon_custom_emoji_id": "5253959125838090076", "callback_data": "search_user"}])
        buttons.append([{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_owner_panel"}])
        edit_message_text(chat_id, message_id, "<b>المستخدمون</b>", buttons)
        return

    elif query.data == "op_servers":
        panel_users = panel_load_users()
        all_users   = get_all_users()
        lines = []
        for u in all_users:
            for s in get_user_servers(u[0]):
                uname  = s.get("username", "?")
                info   = panel_users.get(uname, {})
                expiry = info.get("expiry", "")
                try:
                    expired = datetime.fromisoformat(expiry) < datetime.now() if expiry else False
                except Exception:
                    expired = False
                status = "🔴" if expired else "🟢"
                lines.append(f"{status} <code>{uname}</code> — {expiry[:10] if expiry else '—'}")
        text = ("<b>🖥 كل السيرفرات المنشأة:</b>\n\n" + "\n".join(lines)) if lines else "<b>🖥 لا توجد سيرفرات بعد</b>"
        edit_message_text(chat_id, message_id, text,
                          [[{"text": "🔙 لوحة التحكم", "callback_data": "back_owner_panel"}]])
        return

    elif query.data == "op_codes":
        codes = list_codes()
        if codes:
            body = "\n".join(
                f"<tg-emoji emoji-id=\"5445279839902779679\">💎</tg-emoji>  <code>{c}</code>  —  {p} نقطة  —  {u} استخدام"
                for c, p, u in codes
            )
            text = f"<b>الأكواد النشطة</b>\n\n{body}"
        else:
            text = "<b>لا توجد أكواد حالياً</b>"
        buttons = []
        for c, p, u in codes:
            buttons.append([{"text": f"حذف  {c}", "callback_data": f"del_code_{c}"}])
        buttons.append([{"text": "إضافة كود", "icon_custom_emoji_id": "5206607081334906820", "callback_data": "add_code_start"}])
        buttons.append([{"text": "رجوع",       "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_owner_panel"}])
        edit_message_text(chat_id, message_id, text, buttons)
        return

    elif query.data == "add_code_start":
        user_steps[chat_id] = "adding_code_name"
        edit_message_text(chat_id, message_id,
                          "<b>أرسل اسم الكود (حروف إنجليزية وأرقام فقط)</b>",
                          [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_codes"}]])
        return

    elif query.data.startswith("code_uses_"):
        uses = int(query.data.split("_")[2])
        p = pending_creation.get(chat_id, {})
        if "code" in p and "points" in p:
            ok = add_code(p["code"], p["points"], uses)
            pending_creation.pop(chat_id, None)
            user_steps[chat_id] = "owner_panel"
            msg = (f"<b>تم إنشاء الكود\n\n<code>{p['code']}</code>  —  {p['points']} نقطة  —  {uses} استخدام</b>"
                   if ok else "<b>الكود موجود مسبقاً</b>")
            edit_message_text(chat_id, message_id, msg,
                              [[{"text": "الأكواد", "icon_custom_emoji_id": "5445279839902779679", "callback_data": "op_codes"},
                                {"text": "رجوع",    "icon_custom_emoji_id": "4949575790002963745", "callback_data": "back_owner_panel"}]])
        return

    elif query.data.startswith("del_code_"):
        code_to_del = query.data[len("del_code_"):]
        delete_code(code_to_del.upper())
        codes = list_codes()
        if codes:
            body = "\n".join(
                f"<tg-emoji emoji-id=\"5445279839902779679\">💎</tg-emoji>  <code>{c}</code>  —  {p} نقطة  —  {u} استخدام"
                for c, p, u in codes
            )
            text = f"<b>الأكواد النشطة</b>\n\n{body}"
        else:
            text = "<b>لا توجد أكواد حالياً</b>"
        buttons = []
        for c, p, u in codes:
            buttons.append([{"text": f"حذف  {c}", "callback_data": f"del_code_{c}"}])
        buttons.append([{"text": "إضافة كود", "icon_custom_emoji_id": "5206607081334906820", "callback_data": "add_code_start"}])
        buttons.append([{"text": "رجوع",       "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_owner_panel"}])
        edit_message_text(chat_id, message_id, text, buttons)
        return

    elif query.data == "op_broadcast":
        user_steps[chat_id] = "waiting_broadcast"
        edit_message_text(chat_id, message_id,
                          "<b>أرسل الآن أي رسالة لإذاعتها لجميع المستخدمين.\n\n"
                          "يدعم النصوص بكل تنسيقات Telegram، والإيموجي المميز، "
                          "والصور والفيديوهات والملفات والصوت والملصقات والرسائل الغنية.</b>",
                          [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "back_owner_panel"}]])
        return

    elif query.data == "op_settings":
        text = (
            "<b><tg-emoji emoji-id=\"5447105316967693619\">⚙️</tg-emoji>  الإعدادات</b>\n\n"
            f"<tg-emoji emoji-id=\"5258368777350816286\">🪙</tg-emoji>  سعر السيرفر  :  <b>{get_setting('server_price')} نقطة</b>\n"
            f"<tg-emoji emoji-id=\"5257991477358763590\">↗️</tg-emoji>  المدة الافتراضية  :  <b>{get_setting('default_days')} يوم</b>\n"
            f"<tg-emoji emoji-id=\"5260450573768990626\">➡️</tg-emoji>  المساحة  :  <b>{get_setting('default_storage')} MB</b>\n"
            f"🎁 نقاط الدعوة  :  <b>{get_setting('referral_points') or 2} نقطة</b>"
        )
        buttons = [
            [{"text": "تعديل السعر",    "callback_data": "set_server_price"},
             {"text": "تعديل المدة",    "callback_data": "set_default_days"}],
            [{"text": "تعديل المساحة", "callback_data": "set_default_storage"}],
            [{"text": "🎁 تعديل نقاط الدعوة", "callback_data": "set_referral_points"}],
            [{"text": "رجوع",           "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_owner_panel"}]
        ]
        edit_message_text(chat_id, message_id, text, buttons)
        return

    elif query.data == "set_server_price":
        user_steps[chat_id] = "editing_server_price"
        edit_message_text(chat_id, message_id,
                          f"<b>السعر الحالي  :  {get_setting('server_price')} نقطة\n\nأرسل السعر الجديد</b>",
                          [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_settings"}]])
        return

    elif query.data == "set_default_days":
        user_steps[chat_id] = "editing_default_days"
        edit_message_text(chat_id, message_id,
                          f"<b>المدة الحالية  :  {get_setting('default_days')} يوم\n\nأرسل المدة الجديدة</b>",
                          [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_settings"}]])
        return

    elif query.data == "set_referral_points":
        user_steps[chat_id] = "editing_referral_points"
        edit_message_text(chat_id, message_id,
                          f"<b>نقاط الدعوة الحالية : {get_setting('referral_points') or 2} نقطة\n\nأرسل العدد الجديد (0 لتعطيل المكافأة)</b>",
                          [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_settings"}]])
        return

    elif query.data == "set_default_storage":
        user_steps[chat_id] = "editing_default_storage"
        edit_message_text(chat_id, message_id,
                          f"<b>المساحة الحالية  :  {get_setting('default_storage')} MB\n\nأرسل المساحة الجديدة</b>",
                          [[{"text": "إلغاء", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_settings"}]])
        return

    elif query.data == "search_user":
        user_steps[chat_id] = "searching_user"
        edit_message_text(chat_id, message_id,
                          "<b>أرسل اسم المستخدم أو المعرف أو الـ ID للبحث</b>",
                          [[{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "callback_data": "op_users"}]])
        return

    elif query.data.startswith("myserver_"):
        uname = query.data.split("_", 1)[1]
        panel_users = panel_load_users()
        info = panel_users.get(uname, {})
        if not info:
            response = "<b>❌ لم يتم العثور على هذا السيرفر</b>"
            buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
        else:
            expiry = info.get("expiry", "")
            expiry_disp = expiry[:10] if expiry else "غير محدد"
            response = f"""<b>🖥 تفاصيل السيرفر

👤 إسم المستخدم  :  <code>{uname}</code>
📄 نوع الملف  :  {info.get('main_file', '-')}
📅 تاريخ الإنشاء  :  {info.get('created', '')[:10]}
⏳ ينتهي في  :  {expiry_disp}</b>"""
            buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]
        if query.message.photo:
            edit_photo_caption(chat_id, message_id, response, buttons)
        else:
            edit_message_text(chat_id, message_id, response, buttons)
        return

    elif query.data.startswith("user_"):
        target_id = int(query.data.split("_")[1])
        target_data = get_user(target_id)
        if not target_data:
            await query.edit_message_text(strip_standard_emojis("المستخدم غير موجود"))
            return

        uid = target_data[0]
        username = target_data[1] or ""
        first_name = target_data[2] or ""
        points = target_data[4]
        is_banned_flag = target_data[5]
        join_date = target_data[6] or ""
        servers = target_data[7] or "[]"

        status_label = "محظور" if is_banned_flag else "نشط"
        display_name = first_name or username or str(uid)

        response = (
            f"<b><tg-emoji emoji-id=\"5260399854500191689\">👤</tg-emoji>  {display_name}</b>\n\n"
            f"<tg-emoji emoji-id=\"5253959125838090076\">👁</tg-emoji>  الـ ID  :  <code>{uid}</code>\n"
            f"<tg-emoji emoji-id=\"5260450573768990626\">➡️</tg-emoji>  المعرف  :  @{username if username else 'غير متوفر'}\n"
            f"<tg-emoji emoji-id=\"5258368777350816286\">🪙</tg-emoji>  النقاط  :  <b>{points}</b>\n"
            f"<tg-emoji emoji-id=\"5257991477358763590\">↗️</tg-emoji>  الحالة  :  <b>{status_label}</b>\n"
            f"<tg-emoji emoji-id=\"5258514780469075716\">📂</tg-emoji>  الانضمام  :  {join_date[:10] if join_date else '—'}\n"
            f"<tg-emoji emoji-id=\"5282843764451195532\">🖥</tg-emoji>  السيرفرات  :  <b>{len(json.loads(servers)) if servers else 0}</b>"
        )

        ban_text = "فك الحظر" if is_banned_flag else "حظر"
        buttons = [
            [{"text": ban_text, "style": "danger" if not is_banned_flag else "success", "callback_data": f"toggle_ban_{uid}"}],
            [{"text": "النقاط", "icon_custom_emoji_id": "5258368777350816286", "style": "primary", "callback_data": f"manage_points_{uid}"},
             {"text": "رسالة",  "icon_custom_emoji_id": "5472239203590888751", "style": "primary", "callback_data": f"send_msg_{uid}"}],
            [{"text": "رجوع",   "icon_custom_emoji_id": "4949575790002963745", "style": "danger",  "callback_data": "back_users"}]
        ]

        if query.message.photo:
            edit_photo_caption(chat_id, message_id, response, buttons)
        else:
            edit_message_text(chat_id, message_id, response, buttons)
        return

    elif query.data.startswith("toggle_ban_"):
        target_id = int(query.data.split("_")[2])
        new_status = toggle_ban(target_id)
        if new_status is None:
            await query.edit_message_text(strip_standard_emojis("المستخدم غير موجود"))
            return

        status_text = "<b>تم فك الحظر</b>" if new_status == 0 else "<b>تم حظر المستخدم</b>"
        buttons = [[{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_users"}]]
        edit_message_text(chat_id, message_id, status_text, buttons)
        return

    elif query.data.startswith("manage_points_"):
        target_id = int(query.data.split("_")[2])
        target_data = get_user(target_id)
        if not target_data:
            await query.edit_message_text(strip_standard_emojis("المستخدم غير موجود"))
            return

        user_steps[chat_id] = f"adding_points_{target_id}"
        response = f"<b>الرصيد الحالي  :  {target_data[4]} نقطة\n\nأرسل <code>+50</code> لإضافة أو <code>-20</code> للخصم</b>"
        buttons = [[{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": f"user_{target_id}"}]]

        if query.message.photo:
            edit_photo_caption(chat_id, message_id, response, buttons)
        else:
            edit_message_text(chat_id, message_id, response, buttons)
        return

    elif query.data.startswith("send_msg_"):
        target_id = int(query.data.split("_")[2])
        user_steps[chat_id] = f"sending_msg_{target_id}"
        response = "<b>أرسل نص الرسالة للمستخدم</b>"
        buttons = [[{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": f"user_{target_id}"}]]

        if query.message.photo:
            edit_photo_caption(chat_id, message_id, response, buttons)
        else:
            edit_message_text(chat_id, message_id, response, buttons)
        return

    elif query.data == "back_users":
        users = get_all_users()
        buttons = []
        for uid, name, username, pts, banned in users[:50]:
            display_name = name or username or str(uid)
            prefix = "✖ " if banned else ""
            buttons.append([{"text": f"{prefix}{display_name[:28]}", "callback_data": f"user_{uid}"}])
        buttons.append([{"text": "بحث",  "icon_custom_emoji_id": "5253959125838090076", "callback_data": "search_user"}])
        buttons.append([{"text": "رجوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_owner_panel"}])

        if query.message.photo:
            edit_photo_caption(chat_id, message_id, "👥 <b>قائمة المستخدمين</b>", buttons)
        else:
            edit_message_text(chat_id, message_id, "👥 <b>قائمة المستخدمين</b>", buttons)
        return

    if query.data == "no_servers":
        response = "<b>🚫 لا يـوجـد لــديـك سـيرفـرات بعـد\n\nيمكنك إنشاء سيرفر جديد بالضغط على زر 'إنــشاء سـيرفـر'</b>"
        buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]

        if query.message.photo:
            edit_photo_caption(chat_id, message_id, response, buttons)
        else:
            edit_message_text(chat_id, message_id, response, buttons)
        return

    elif query.data in ["pay_ton", "pay_stars", "pay_voda", "pay_other"]:
        payment_names = {"pay_ton": "Ton", "pay_stars": "نـجوم تـليـجرام", "pay_voda": "Vodafone Cash", "pay_other": "طـرق دفـع أُخـرى"}
        response = f"<b>💵 الدفع عبر {payment_names.get(query.data, '')}\n\nرابط الدفع: قيد التطوير\nسيتم إضافته قريباً</b>"
        buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_payment"}]]
        send_photo_with_custom_keyboard(chat_id, HOME_IMAGE_URL, response, buttons)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    elif query.data == "back_payment":
        try:
            await query.message.delete()
        except Exception:
            pass
        response = """<b>لــ شـراء نقـاط علـيك إخـتيار طـريـقه الــدفع من الأزرار التـالـيه <tg-emoji emoji-id="5472250091332993630">💳</tg-emoji></b>"""
        buttons = [
            [{"text": "Ton", "icon_custom_emoji_id": "5445055054199411000", "style": "success", "callback_data": "pay_ton"}, {"text": "نـجوم تـليـجرام", "icon_custom_emoji_id": "5258165702707125574", "style": "success", "callback_data": "pay_stars"}],
            [{"text": "Vodafone Cash", "icon_custom_emoji_id": "5258204546391351475", "style": "success", "callback_data": "pay_voda"}, {"text": "طـرق دفـع أُخـرى", "icon_custom_emoji_id": "5447105316967693619", "style": "success", "callback_data": "pay_other"}],
            [{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]
        ]
        send_photo_with_custom_keyboard(chat_id, HOME_IMAGE_URL, response, buttons)
        return

    elif query.data == "lang_python":
        pending_creation[chat_id] = {"lang": "python"}
        user_steps[chat_id] = "waiting_days"
        response = """<b>الآن أرسـل الـمده الـمطلـوبـه بـالأيـام فـقط <tg-emoji emoji-id="5413879192267805083">🗓</tg-emoji>
<blockquote><b><u><i>أقـل مده هـي 10 أيـام</i></u></b></blockquote>
- سـعـر 10 أيـام  =  20 نقـاط <tg-emoji emoji-id="5422439311196834318">💡</tg-emoji></b>"""
        buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]

        if query.message.photo:
            edit_photo_caption(chat_id, message_id, response, buttons)
        else:
            edit_message_text(chat_id, message_id, response, buttons)
        return

    elif query.data == "lang_javascript":
        pending_creation[chat_id] = {"lang": "javascript"}
        user_steps[chat_id] = "waiting_days"
        response = """<b>الآن أرسـل الـمده الـمطلـوبـه بـالأيـام فـقط <tg-emoji emoji-id="5413879192267805083">🗓</tg-emoji>
<blockquote><b><u><i>أقـل مده هـي 10 أيـام</i></u></b></blockquote>
- سـعـر 10 أيـام  =  20 نقـاط <tg-emoji emoji-id="5422439311196834318">💡</tg-emoji></b>"""
        buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]

        if query.message.photo:
            edit_photo_caption(chat_id, message_id, response, buttons)
        else:
            edit_message_text(chat_id, message_id, response, buttons)
        return

    elif query.data == "skip_storage":
        user_steps[chat_id] = "waiting_server_name"
        response = "<b>الآن قـم بـإرسـال إسـم لـلـسيـرفـر الـخاص بـك</b>"
        buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]

        if query.message.photo:
            edit_photo_caption(chat_id, message_id, response, buttons)
        else:
            edit_message_text(chat_id, message_id, response, buttons)
        return

    elif query.data in ["storage_2", "storage_4", "storage_6", "storage_12"]:
        storage_names = {"storage_2": "2GB", "storage_4": "4GB", "storage_6": "6GB", "storage_12": "12GB"}
        user_steps[chat_id] = "waiting_server_name"
        response = f"<b>تم اختيار مساحة {storage_names.get(query.data, '')}\nالآن قـم بـإرسـال إسـم لـلـسيـرفـر الـخاص بـك</b>"
        buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]

        if query.message.photo:
            edit_photo_caption(chat_id, message_id, response, buttons)
        else:
            edit_message_text(chat_id, message_id, response, buttons)
        return

    elif query.data == "confirm_payment":
        response = "<b>✅ تم تأكيد الدفع بنجاح!\nجاري إنشاء السيرفر...\nسيتم إعلامك عند الانتهاء</b>"
        buttons = [[{"text": "رجــوع", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]]

        if query.message.photo:
            edit_photo_caption(chat_id, message_id, response, buttons)
        else:
            edit_message_text(chat_id, message_id, response, buttons)
        return

    # ══════════════════════════════════════════════════════════════════════════
    # ─── قسم النسخ الاحتياطية ─────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    elif query.data == "op_backup":
        if not is_admin(user_id):
            return
        hours = int(get_setting("auto_backup_hours") or 0)
        auto_status = f"✅ مفعّل كل {hours} ساعة" if hours > 0 else "❌ غير مفعّل"
        notify = get_setting("notify_new_users") or "1"
        notify_status = "✅ مفعّل" if notify == "1" else "❌ موقوف"
        text_msg = (
            "<b>📦 النسخ الاحتياطية\n\n"
            f"🔁 النسخ التلقائي  :  {auto_status}\n"
            f"🔔 إشعار المستخدم الجديد  :  {notify_status}</b>"
        )
        buttons = [
            [{"text": "📥 إنشاء نسخة الآن",      "callback_data": "backup_now"}],
            [{"text": "⬆️ رفع نسخة احتياطية",    "callback_data": "backup_restore"}],
            [{"text": "🕐 النسخ التلقائي",         "callback_data": "backup_auto_menu"}],
            [{"text": "🔔 إشعار المستخدم الجديد", "callback_data": "backup_toggle_notify"}],
            [{"text": "🔙 رجوع", "callback_data": "back_owner_panel"}],
        ]
        edit_message_text(chat_id, message_id, text_msg, buttons)
        return

    elif query.data == "backup_now":
        if not is_admin(user_id):
            return
        edit_message_text(chat_id, message_id,
            "<b>⏳ جارٍ إنشاء النسخة الاحتياطية وإرسالها...</b>", [])
        threading.Thread(target=send_backup_to_chat, args=(chat_id,), daemon=True).start()
        # أعد عرض قائمة النسخ بعد الإرسال
        import time as _time
        def _reshow():
            _time.sleep(2)
            hours = int(get_setting("auto_backup_hours") or 0)
            auto_status = f"✅ مفعّل كل {hours} ساعة" if hours > 0 else "❌ غير مفعّل"
            notify = get_setting("notify_new_users") or "1"
            notify_status = "✅ مفعّل" if notify == "1" else "❌ موقوف"
            t = (
                "<b>📦 النسخ الاحتياطية\n\n"
                f"🔁 النسخ التلقائي  :  {auto_status}\n"
                f"🔔 إشعار المستخدم الجديد  :  {notify_status}\n\n"
                "✅ تم إرسال النسخة الاحتياطية!</b>"
            )
            b = [
                [{"text": "📥 إنشاء نسخة الآن",      "callback_data": "backup_now"}],
                [{"text": "⬆️ رفع نسخة احتياطية",    "callback_data": "backup_restore"}],
                [{"text": "🕐 النسخ التلقائي",         "callback_data": "backup_auto_menu"}],
                [{"text": "🔔 إشعار المستخدم الجديد", "callback_data": "backup_toggle_notify"}],
                [{"text": "🔙 رجوع", "callback_data": "back_owner_panel"}],
            ]
            edit_message_text(chat_id, message_id, t, b)
        threading.Thread(target=_reshow, daemon=True).start()
        return

    elif query.data == "backup_restore":
        if not is_admin(user_id):
            return
        user_steps[chat_id] = "waiting_restore"
        edit_message_text(chat_id, message_id,
            "<b>⬆️ رفع نسخة احتياطية\n\n"
            "أرسل ملف ZIP الاحتياطي الآن\n"
            "سيتم استعادة جميع البيانات منه تلقائياً</b>",
            [[{"text": "إلغاء", "callback_data": "op_backup"}]])
        return

    elif query.data == "backup_auto_menu":
        if not is_admin(user_id):
            return
        hours = int(get_setting("auto_backup_hours") or 0)
        status = f"✅ مفعّل كل {hours} ساعة" if hours > 0 else "❌ غير مفعّل"
        text_msg = (
            f"<b>🕐 النسخ التلقائي\n\nالحالة  :  {status}\n\n"
            "اختر الفترة الزمنية أو اضغط 'تعيين يدوي' لكتابة عدد الساعات:</b>"
        )
        buttons = [
            [{"text": "كل ساعة",    "callback_data": "set_auto_backup_1"},
             {"text": "كل 6 ساعات", "callback_data": "set_auto_backup_6"}],
            [{"text": "كل 12 ساعة", "callback_data": "set_auto_backup_12"},
             {"text": "كل يوم",     "callback_data": "set_auto_backup_24"}],
            [{"text": "كل أسبوع",   "callback_data": "set_auto_backup_168"}],
            [{"text": "✏️ تعيين يدوي (أرسل عدد الساعات)", "callback_data": "backup_set_manual"}],
            [{"text": "⏹ إيقاف النسخ التلقائي", "callback_data": "set_auto_backup_0"}],
            [{"text": "🔙 رجوع", "callback_data": "op_backup"}],
        ]
        edit_message_text(chat_id, message_id, text_msg, buttons)
        return

    elif query.data.startswith("set_auto_backup_"):
        if not is_admin(user_id):
            return
        try:
            hours = int(query.data.split("_")[-1])
        except ValueError:
            return
        start_auto_backup(hours)
        if hours > 0:
            msg = f"<b>✅ تم تفعيل النسخ التلقائي كل <code>{hours}</code> ساعة</b>"
        else:
            msg = "<b>⏹ تم إيقاف النسخ التلقائي</b>"
        edit_message_text(chat_id, message_id, msg,
            [[{"text": "🕐 النسخ التلقائي", "callback_data": "backup_auto_menu"},
              {"text": "🔙 رجوع",           "callback_data": "op_backup"}]])
        return

    elif query.data == "backup_set_manual":
        if not is_admin(user_id):
            return
        user_steps[chat_id] = "waiting_auto_backup_hours"
        edit_message_text(chat_id, message_id,
            "<b>✏️ تعيين يدوي للنسخ التلقائي\n\n"
            "أرسل عدد الساعات كرقم\n"
            "مثال: <code>3</code> = كل 3 ساعات</b>",
            [[{"text": "إلغاء", "callback_data": "backup_auto_menu"}]])
        return

    elif query.data == "backup_toggle_notify":
        if not is_admin(user_id):
            return
        current = get_setting("notify_new_users") or "1"
        new_val = "0" if current == "1" else "1"
        update_setting("notify_new_users", new_val)
        status_msg = "✅ تم تفعيل إشعار المستخدم الجديد" if new_val == "1" else "❌ تم إيقاف إشعار المستخدم الجديد"
        hours = int(get_setting("auto_backup_hours") or 0)
        auto_status = f"✅ مفعّل كل {hours} ساعة" if hours > 0 else "❌ غير مفعّل"
        notify_status = "✅ مفعّل" if new_val == "1" else "❌ موقوف"
        text_msg = (
            f"<b>📦 النسخ الاحتياطية\n\n"
            f"🔁 النسخ التلقائي  :  {auto_status}\n"
            f"🔔 إشعار المستخدم الجديد  :  {notify_status}\n\n"
            f"{status_msg}</b>"
        )
        buttons = [
            [{"text": "📥 إنشاء نسخة الآن",      "callback_data": "backup_now"}],
            [{"text": "⬆️ رفع نسخة احتياطية",    "callback_data": "backup_restore"}],
            [{"text": "🕐 النسخ التلقائي",         "callback_data": "backup_auto_menu"}],
            [{"text": "🔔 إشعار المستخدم الجديد", "callback_data": "backup_toggle_notify"}],
            [{"text": "🔙 رجوع", "callback_data": "back_owner_panel"}],
        ]
        edit_message_text(chat_id, message_id, text_msg, buttons)
        return

    # ══════════════════════════════════════════════════════════════════════════
    # ─── الاشتراك الإجباري ────────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    elif query.data == "op_force_sub":
        if not is_admin(user_id):
            return
        render_force_sub_panel(chat_id, message_id)
        return

    elif query.data == "force_sub_toggle":
        if not is_admin(user_id):
            return
        current = get_setting("force_sub_enabled") or "0"
        update_setting("force_sub_enabled", "0" if current == "1" else "1")
        render_force_sub_panel(chat_id, message_id)
        return

    elif query.data == "force_sub_add":
        if not is_admin(user_id):
            return
        pending_creation.pop(chat_id, None)
        user_steps[chat_id] = "waiting_force_sub_channel"
        edit_message_text(chat_id, message_id,
            "<b>إضافة قناة اشتراك\n\n"
            "أرسل معرف القناة مثل:\n<code>@my_channel</code>\n"
            "أو المعرف الرقمي مثل: <code>-1001234567890</code></b>",
            [[{"text": "إلغاء", "callback_data": "op_force_sub"}]])
        return

    elif re.fullmatch(r"force_sub_edit_\d+", query.data or ""):
        if not is_admin(user_id):
            return
        index = int(query.data.rsplit("_", 1)[1])
        channels = load_force_sub_channels()
        if index < 0 or index >= len(channels):
            render_force_sub_panel(chat_id, message_id)
            return
        pending_creation[chat_id] = {"force_sub_index": index}
        user_steps[chat_id] = "waiting_force_sub_edit_channel"
        edit_message_text(
            chat_id, message_id,
            f"<b>تعديل القناة رقم {index + 1}\n\n"
            f"المعرف الحالي: <code>{html_escape(channels[index]['channel'])}</code>\n"
            "أرسل المعرف الجديد.</b>",
            [[{"text": "إلغاء", "callback_data": "op_force_sub"}]]
        )
        return

    elif re.fullmatch(r"force_sub_delete_\d+", query.data or ""):
        if not is_admin(user_id):
            return
        index = int(query.data.rsplit("_", 1)[1])
        channels = load_force_sub_channels()
        if 0 <= index < len(channels):
            channels.pop(index)
            save_force_sub_channels(channels)
        render_force_sub_panel(chat_id, message_id)
        return

    elif query.data == "force_sub_delete_all":
        if not is_admin(user_id):
            return
        save_force_sub_channels([])
        update_setting("force_sub_enabled", "0")
        render_force_sub_panel(chat_id, message_id)
        return

    elif query.data == "force_sub_set_channel":
        if not is_admin(user_id):
            return
        user_steps[chat_id] = "waiting_force_sub_channel"
        edit_message_text(chat_id, message_id,
            "<b>✏️ أرسل معرّف القناة\n\n"
            "مثال: <code>@my_channel</code>\n"
            "أو الـ ID الرقمي مثل: <code>-1001234567890</code></b>",
            [[{"text": "إلغاء", "callback_data": "op_force_sub"}]])
        return

    elif query.data == "force_sub_skip_link":
        if not is_admin(user_id):
            return
        update_setting("force_sub_channel_link", "")
        user_steps[chat_id] = "owner_panel"
        send_custom_keyboard(
            chat_id,
            "<b>✅ تم تخطي الرابط.\nسيُستخدم رابط القناة العام تلقائياً عند توفره.</b>",
            [[{"text": "📣 الاشتراك الإجباري", "callback_data": "op_force_sub"},
              {"text": "🔙 لوحة التحكم", "callback_data": "back_owner_panel"}]]
        )
        return

    elif query.data == "force_sub_edit_link":
        if not is_admin(user_id):
            return
        user_steps[chat_id] = "waiting_force_sub_link_edit"
        edit_message_text(
            chat_id,
            message_id,
            "<b>🔗 تعديل رابط قناة الاشتراك\n\n"
            "أرسل الرابط الجديد، مثل:\n"
            "<code>https://t.me/store_MS_almohtal</code>\n\n"
            "أرسل <code>حذف</code> لاستخدام رابط القناة العام تلقائياً.</b>",
            [[{"text": "❌ إلغاء", "callback_data": "op_force_sub"}]]
        )
        return

    elif query.data == "force_sub_remove_channel":
        if not is_admin(user_id):
            return
        save_force_sub_channels([])
        update_setting("force_sub_enabled", "0")
        render_force_sub_panel(chat_id, message_id)
        return

    elif query.data == "force_sub_preview":
        if not is_admin(user_id):
            return
        if not load_force_sub_channels():
            await query.answer("أضف قناة واحدة على الأقل أولاً", show_alert=True)
            return
        send_force_sub_message(chat_id)
        await query.answer("تم إرسال المعاينة", show_alert=True)
        return

    elif query.data == "force_sub_edit_join_text":
        if not is_admin(user_id):
            return
        render_force_sub_panel(chat_id, message_id)
        return

    elif query.data == "force_sub_edit_verify_text":
        if not is_admin(user_id):
            return
        user_steps[chat_id] = "waiting_force_sub_verify_text"
        edit_message_text(chat_id, message_id,
            "<b>✏️ تعديل اسم زر التحقق\n\n"
            "أرسل الاسم الذي سيظهر على الزر.\n"
            "مثال: <code>تحققت من الاشتراك</code></b>",
            [[{"text": "🔄 إعادة الاسم الافتراضي", "callback_data": "force_sub_reset_verify_text"}],
             [{"text": "❌ إلغاء", "callback_data": "op_force_sub"}]])
        return

    elif query.data == "force_sub_reset_join_text":
        if not is_admin(user_id):
            return
        channels = load_force_sub_channels()
        for item in channels:
            item["button_text"] = ""
        save_force_sub_channels(channels)
        render_force_sub_panel(chat_id, message_id)
        return

    elif query.data == "force_sub_reset_verify_text":
        if not is_admin(user_id):
            return
        update_setting("force_sub_verify_button_text", "")
        render_force_sub_panel(chat_id, message_id)
        return

    elif query.data == "check_sub_verify":
        # زر "تحققت من الاشتراك" للمستخدم العادي
        if check_subscription(user_id):
            try:
                await query.message.delete()
            except Exception:
                pass
            # ── إضافة نقاط الدعوة إذا كانت الكابتشا محلولة وتنتظر الاشتراك ──
            inviter_id = referral_pending.pop(chat_id, None) or pop_pending_referral(user_id)
            if inviter_id:
                credit_referral(inviter_id, user_id)
            user_name_v = update.effective_user.first_name or "صديقي"
            welcome_msg = WELCOME_TEXT.replace("Name", user_name_v)
            send_photo_with_main_keyboard(chat_id, IMAGE_URL, welcome_msg, user_id=user_id)
        else:
            await query.answer("لم تشترك بعد! اشترك في القناة أولاً", show_alert=True)
        return

    # ══════════════════════════════════════════════════════════════════════════
    # ─── كابتشا التحقق من الدعوة 🤖 ─────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    elif query.data.startswith("captcha_ans_"):
        # استخرج الرقم المُختار
        try:
            chosen = int(query.data.split("_")[2])
        except (ValueError, IndexError):
            return

        pending = captcha_pending.get(chat_id)
        if not pending:
            # انتهت الجلسة أو حُلّت سابقاً
            await query.answer("انتهت صلاحية التحقق، أرسل /start مجدداً", show_alert=True)
            return

        if chosen != pending["answer"]:
            # ─── إجابة خاطئة → كابتشا جديدة ─────────────────────────────────
            await query.answer("إجابة خاطئة! حاول مرة أخرى", show_alert=True)
            send_captcha_message(chat_id, pending["invited_by"],
                                 edit_msg_id=query.message.message_id)
            return

        # ─── إجابة صحيحة ✅ ───────────────────────────────────────────────────
        del captcha_pending[chat_id]
        invited_by_id = pending["invited_by"]

        # فحص الاشتراك الإجباري
        if not check_subscription(user_id):
            # احفظ الدعوة معلقة حتى يتم الاشتراك
            referral_pending[chat_id] = invited_by_id
            save_pending_referral(user_id, invited_by_id)
            try:
                await query.message.delete()
            except Exception:
                pass
            # أرسل رسالة اشتراك مع تهنئة بحل الكابتشا
            channel = (get_setting("force_sub_channel") or "").strip()
            link = (get_setting("force_sub_channel_link") or "").strip()
            sub_text = (
                "<b>✅ أحسنت! حللت التحقق بنجاح 🎉\n\n"
                "📣 الخطوة الأخيرة: اشترك في قناتنا لتفعيل حسابك\n"
                "وستحصل على نقاطك فور التحقق!</b>"
            )
            sub_buttons = force_sub_inline_buttons()
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
                "chat_id": chat_id,
                "text": sub_text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": sub_buttons}
            })
            return

        # لا اشتراك إجباري → اعطِ النقاط مباشرة وأظهر القائمة
        try:
            await query.message.delete()
        except Exception:
            pass
        credit_referral(invited_by_id, user_id)
        user_name_v = update.effective_user.first_name or "صديقي"
        welcome_msg = WELCOME_TEXT.replace("Name", user_name_v)
        send_photo_with_main_keyboard(chat_id, IMAGE_URL, welcome_msg, user_id=user_id)
        return

    # ══════════════════════════════════════════════════════════════════════════
    # ─── دفع النجوم ⭐ ────────────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    elif query.data == "op_star_payment":
        if not is_admin(user_id):
            return
        price = get_setting("star_price") or "1"
        label = get_setting("star_amount_label") or "دعم بنجمة"
        text_msg = (
            f"<b>⭐ إعدادات دفع النجوم\n\n"
            f"🔢 عدد النجوم : <code>{price}</code>\n"
            f"📝 وصف الدفع : <code>{label}</code>\n\n"
            f"يمكنك تعديل الإعدادات من الأزرار أدناه</b>"
        )
        buttons = [
            [{"text": "✏️ تعديل عدد النجوم", "callback_data": "star_edit_price"},
             {"text": "📝 تعديل الوصف", "callback_data": "star_edit_label"}],
            [{"text": "🧪 اختبار الدفع (إرسال فاتورة لك)", "callback_data": "star_test_invoice"}],
            [{"text": "🔙 رجوع", "callback_data": "back_owner_panel"}],
        ]
        edit_message_text(chat_id, message_id, text_msg, buttons)
        return

    elif query.data == "star_edit_price":
        if not is_admin(user_id):
            return
        user_steps[chat_id] = "waiting_star_price"
        price = get_setting("star_price") or "1"
        edit_message_text(chat_id, message_id,
            f"<b>⭐ تعديل عدد النجوم\n\nالقيمة الحالية : <code>{price}</code>\n\nأرسل العدد الجديد (رقم صحيح ≥ 1)</b>",
            [[{"text": "❌ إلغاء", "callback_data": "op_star_payment"}]])
        return

    elif query.data == "star_edit_label":
        if not is_admin(user_id):
            return
        user_steps[chat_id] = "waiting_star_label"
        label = get_setting("star_amount_label") or "دعم بنجمة"
        edit_message_text(chat_id, message_id,
            f"<b>📝 تعديل وصف الدفع\n\nالوصف الحالي : <code>{label}</code>\n\nأرسل الوصف الجديد</b>",
            [[{"text": "❌ إلغاء", "callback_data": "op_star_payment"}]])
        return

    elif query.data == "star_test_invoice":
        if not is_admin(user_id):
            return
        send_star_invoice(chat_id)
        await query.answer("تم إرسال فاتورة الاختبار", show_alert=True)
        return

    elif query.data == "pay_stars":
        # زر "نجوم تيليجرام" من لوحة شراء النقاط
        send_star_invoice(chat_id)
        return

    # ══════════════════════════════════════════════════════════════════════════
    # ─── قناة الإثبات 📡 ─────────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    elif query.data == "op_proof_channel":
        if not is_admin(user_id):
            return
        channel = get_setting("proof_channel") or "غير محددة"
        text_msg = (
            f"<b>📡 قناة الإثبات\n\n"
            f"القناة الحالية : <code>{channel}</code>\n\n"
            f"عند إنشاء سيرفر جديد سيُرسل إشعار لهذه القناة</b>"
        )
        buttons = [
            [{"text": "✏️ تعيين القناة", "callback_data": "proof_set_channel"}],
            [{"text": "🧪 اختبار الإشعار", "callback_data": "proof_test_notify"}],
            [{"text": "🗑 إزالة القناة", "callback_data": "proof_remove_channel"}],
            [{"text": "🔙 رجوع", "callback_data": "back_owner_panel"}],
        ]
        edit_message_text(chat_id, message_id, text_msg, buttons)
        return

    elif query.data == "proof_set_channel":
        if not is_admin(user_id):
            return
        user_steps[chat_id] = "waiting_proof_channel"
        edit_message_text(chat_id, message_id,
            "<b>📡 تعيين قناة الإثبات\n\n"
            "أرسل معرف القناة\n"
            "مثال: <code>@my_channel</code> أو <code>-1001234567890</code>\n\n"
            "⚠️ تأكد أن البوت مشرف في القناة</b>",
            [[{"text": "❌ إلغاء", "callback_data": "op_proof_channel"}]])
        return

    elif query.data == "proof_remove_channel":
        if not is_admin(user_id):
            return
        update_setting("proof_channel", "")
        edit_message_text(chat_id, message_id,
            "<b>✅ تم إزالة قناة الإثبات</b>",
            [[{"text": "📡 قناة الإثبات", "callback_data": "op_proof_channel"},
              {"text": "🔙 رجوع", "callback_data": "back_owner_panel"}]])
        return

    elif query.data == "proof_test_notify":
        if not is_admin(user_id):
            return
        channel = get_setting("proof_channel") or ""
        if not channel:
            await query.answer("لم يتم تعيين قناة الإثبات بعد", show_alert=True)
            return
        try:
            sent = notify_proof_channel(
                uname="TEST_SERVER",
                password="Test@12345",
                ip="51.75.118.170",
                access_token="test_token",
                days=30,
                buyer_name="مستخدم تجريبي",
                buyer_id=user_id,
                panel_url=""
            )
            if sent:
                await query.answer("تم إرسال الإشعار التجريبي فعلياً إلى القناة", show_alert=True)
            else:
                await query.answer("لم يتم إرسال الإشعار. راجع صلاحيات البوت والقناة وسجل التشغيل.", show_alert=True)
        except Exception as e:
            await query.answer(f"فشل: {e}", show_alert=True)
        return

    # ══════════════════════════════════════════════════════════════════════════
    # ─── قفل البوت 🔒 ────────────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    elif query.data == "op_bot_lock":
        if not is_admin(user_id):
            return
        locked = get_setting("bot_locked") == "1"
        lock_msg = get_setting("bot_lock_msg") or "البوت في وضع الصيانة مؤقتاً 🔧"
        status = "🔴 مقفل (صيانة)" if locked else "🟢 مفتوح (يعمل)"
        toggle_label = "🔓 فتح البوت" if locked else "🔒 قفل البوت (صيانة)"
        text_msg = (
            f"<b>🔒 حالة البوت\n\n"
            f"الحالة : {status}\n"
            f"رسالة الصيانة : <code>{lock_msg}</code></b>"
        )
        buttons = [
            [{"text": toggle_label, "callback_data": "bot_lock_toggle"}],
            [{"text": "✏️ تعديل رسالة الصيانة", "callback_data": "bot_lock_edit_msg"}],
            [{"text": "🔙 رجوع", "callback_data": "back_owner_panel"}],
        ]
        edit_message_text(chat_id, message_id, text_msg, buttons)
        return

    elif query.data == "bot_lock_toggle":
        if not is_admin(user_id):
            return
        current = get_setting("bot_locked") == "1"
        new_val = "0" if current else "1"
        update_setting("bot_locked", new_val)
        locked = new_val == "1"
        status = "🔴 مقفل (صيانة)" if locked else "🟢 مفتوح (يعمل)"
        toggle_label = "🔓 فتح البوت" if locked else "🔒 قفل البوت (صيانة)"
        lock_msg = get_setting("bot_lock_msg") or "البوت في وضع الصيانة مؤقتاً 🔧"
        action_msg = "✅ تم قفل البوت — المستخدمون يرون رسالة الصيانة" if locked else "✅ تم فتح البوت — يعمل بشكل طبيعي"
        edit_message_text(chat_id, message_id,
            f"<b>🔒 حالة البوت\n\n"
            f"الحالة : {status}\n"
            f"رسالة الصيانة : <code>{lock_msg}</code>\n\n"
            f"{action_msg}</b>",
            [[{"text": toggle_label, "callback_data": "bot_lock_toggle"}],
             [{"text": "✏️ تعديل رسالة الصيانة", "callback_data": "bot_lock_edit_msg"}],
             [{"text": "🔙 رجوع", "callback_data": "back_owner_panel"}]])
        return

    elif query.data == "bot_lock_edit_msg":
        if not is_admin(user_id):
            return
        user_steps[chat_id] = "waiting_lock_msg"
        lock_msg = get_setting("bot_lock_msg") or "البوت في وضع الصيانة مؤقتاً 🔧"
        edit_message_text(chat_id, message_id,
            f"<b>✏️ تعديل رسالة الصيانة\n\nالرسالة الحالية :\n<code>{lock_msg}</code>\n\nأرسل الرسالة الجديدة</b>",
            [[{"text": "❌ إلغاء", "callback_data": "op_bot_lock"}]])
        return

    # ══════════════════════════════════════════════════════════════════════════
    # ─── تذاكر الدعم الفني 🎧 ────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    elif query.data == "op_support_tickets":
        if not is_admin(user_id):
            return
        tickets = load_support_tickets()
        open_tickets = [(tid, t) for tid, t in tickets.items() if t.get("status") == "open"]
        all_tickets = list(tickets.items())[-20:]  # آخر 20 تذكرة
        text_msg = f"<b>🎧 تذاكر الدعم الفني\n\n📊 الإجمالي : {len(tickets)} | 🔴 مفتوحة : {len(open_tickets)}</b>"
        buttons = []
        for tid, t in reversed(all_tickets):
            status_icon = "🔴" if t.get("status") == "open" else "✅"
            uname = t.get("user_name", "مجهول")
            msg_preview = t.get("message", "")[:30]
            buttons.append([{"text": f"{status_icon} {uname}: {msg_preview}", "callback_data": f"view_ticket_{tid}"}])
        buttons.append([{"text": "🔙 رجوع", "callback_data": "back_owner_panel"}])
        if not all_tickets:
            text_msg += "\n\n<i>لا توجد تذاكر دعم حالياً</i>"
        edit_message_text(chat_id, message_id, text_msg, buttons)
        return

    elif query.data.startswith("view_ticket_"):
        if not is_admin(user_id):
            return
        ticket_id = query.data[len("view_ticket_"):]
        tickets = load_support_tickets()
        ticket = tickets.get(ticket_id)
        if not ticket:
            await query.answer("التذكرة غير موجودة", show_alert=True)
            return
        status_label = "🔴 مفتوحة" if ticket.get("status") == "open" else "✅ مُغلقة"
        text_msg = (
            f"<b>🎧 تذكرة دعم\n\n"
            f"👤 المستخدم : {ticket.get('user_name','')}\n"
            f"🆔 المعرف : <code>{ticket.get('user_id','')}</code>\n"
            f"📅 التاريخ : {ticket.get('timestamp','')[:16]}\n"
            f"📊 الحالة : {status_label}\n\n"
            f"💬 الرسالة :\n{ticket.get('message','')}"
        )
        if ticket.get("reply"):
            text_msg += f"\n\n↩️ الرد :\n{ticket['reply']}"
        buttons = []
        if ticket.get("status") == "open":
            buttons.append([{"text": "↩️ رد على المستخدم", "callback_data": f"reply_ticket_{ticket_id}"}])
        buttons.append([{"text": "🔙 رجوع للتذاكر", "callback_data": "op_support_tickets"}])
        edit_message_text(chat_id, message_id, text_msg, buttons)
        return

    elif query.data.startswith("reply_ticket_"):
        if not is_admin(user_id):
            return
        ticket_id = query.data[len("reply_ticket_"):]
        user_steps[chat_id] = f"replying_ticket_{ticket_id}"
        tickets = load_support_tickets()
        ticket = tickets.get(ticket_id, {})
        edit_message_text(chat_id, message_id,
            f"<b>↩️ الرد على المستخدم {ticket.get('user_name','')}\n\n"
            f"💬 رسالته :\n{ticket.get('message','')}\n\n"
            f"✏️ أرسل ردك الآن:</b>",
            [[{"text": "❌ إلغاء", "callback_data": f"view_ticket_{ticket_id}"}]])
        return

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("حدث خطأ في معالج البوت (%s)", type(context.error).__name__ if context.error else "UnknownError")

# ──────────────────────────────────────────────────────────────────────────────
# معالج الوثائق — لاستعادة النسخة الاحتياطية
# ──────────────────────────────────────────────────────────────────────────────
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    if update.effective_chat.type != "private" or not is_admin(user.id):
        return
    if user_steps.get(chat_id) != "waiting_restore":
        return
    doc = update.message.document
    if not doc.file_name.endswith(".zip"):
        await update.message.reply_text(
            "<b>يرجى إرسال ملف ZIP فقط</b>", parse_mode="HTML"
        )
        return
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        restore_from_zip(buf.getvalue())
        user_steps[chat_id] = "owner_panel"
        send_custom_keyboard(chat_id,
            "<b>✅ تم رفع واستعادة النسخة الاحتياطية بنجاح!\n\n"
            "⚠️ أعد تشغيل البوت لتطبيق التغييرات كاملاً</b>",
            [[{"text": "📦 النسخ الاحتياطية", "callback_data": "op_backup"},
              {"text": "🔙 لوحة التحكم",      "callback_data": "back_owner_panel"}]])
    except Exception as e:
        user_steps[chat_id] = "owner_panel"
        send_custom_keyboard(chat_id,
            f"<b>❌ فشل الاستعادة: {e}</b>",
            [[{"text": "🔙 رجوع", "callback_data": "op_backup"}]])

async def handle_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يوافق تلقائياً على أي طلب دفع نجوم."""
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج الدفع الناجح بالنجوم."""
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    if not is_admin(user.id) and not check_subscription(user.id):
        send_force_sub_message(chat_id)
        return
    payment = update.message.successful_payment
    stars_count = payment.total_amount
    logger.info(f"✅ دفع ناجح من {user.id} بـ {stars_count} نجمة")
    # إضافة نقاط للمستخدم (كل نجمة = 5 نقاط مثلاً)
    points_per_star = 5
    add_user_points(user.id, stars_count * points_per_star)
    user_data = get_user(user.id)
    new_points = user_data[4] if user_data else 0
    send_custom_keyboard(chat_id,
        f"<b>⭐ شكراً لدعمك!\n\nتم استلام {stars_count} نجمة\n"
        f"✅ تم إضافة {stars_count * points_per_star} نقطة لحسابك\n"
        f"💎 رصيدك الحالي : {new_points} نقطة</b>",
        [[{"text": "🏠 الرئيسية", "icon_custom_emoji_id": "4949575790002963745", "style": "danger", "callback_data": "back_main"}]])
    # إرسال إشعار للمالك
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": OWNER_ID,
                "text": f"<b>⭐ دفع ناجح!\n\n👤 المستخدم : {user.first_name} ({user.id})\n💫 النجوم : {stars_count}\n💎 النقاط المضافة : {stars_count * points_per_star}</b>",
                "parse_mode": "HTML"
            }
        )
    except Exception:
        pass

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler(["admin", "m"], owner_panel_cmd))
    # أولوية قصوى: أثناء الإذاعة ننسخ الرسالة الأصلية بكل نوعها وكياناتها.
    # هذا يسبق معالجات النص والوثائق حتى لا تُحوّل الرسالة إلى HTML أو ZIP.
    application.add_handler(
        MessageHandler(filters.ALL, handle_broadcast_message),
        group=-1,
    )
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))
    from telegram.ext import PreCheckoutQueryHandler
    application.add_handler(PreCheckoutQueryHandler(handle_pre_checkout))
    application.add_error_handler(error_handler)
    # تشغيل النسخ التلقائي إن كان مفعّلاً من قبل
    saved_hours = int(get_setting("auto_backup_hours") or 0)
    if saved_hours > 0:
        start_auto_backup(saved_hours)
        logger.info(f"✅ تم استئناف النسخ التلقائي كل {saved_hours} ساعة")
    print("🚀 البوت يعمل الآن...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
