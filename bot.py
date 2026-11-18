#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
استار | دکمه‌ساز شیشه‌ای بله
ربات کامل با حساب کاربری، تیکت، امضا، اشتراک، ارسال پست
"""

import os
import re
import json
import time
import logging
import sqlite3
import secrets
import string
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from dotenv import load_dotenv

load_dotenv()

# ───────────────────────── تنظیمات ─────────────────────────
TOKEN = os.environ.get("BALE_TOKEN") or os.environ.get("TOKEN")
if not TOKEN:
    raise SystemExit("BALE_TOKEN تنظیم نشده")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "1369192211"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Glass_star_bot")
SUPPORT_USER = "@fazellrahimii"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_PATH = os.environ.get("DB_PATH", "./data/bot.db")

API = f"https://tapi.bale.ai/bot{TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("star")

# ───────────────────────── تاریخ شمسی ساده ─────────────────────────
def to_jalali(dt: datetime) -> str:
    """تبدیل تقریبی میلادی به شمسی (کافی برای نمایش)"""
    if dt is None:
        return "—"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", ""))
        except Exception:
            return dt
    g_y, g_m, g_d = dt.year, dt.month, dt.day
    g_days_in_month = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    jy = g_y - 621 if g_m > 2 or (g_m == 2 and g_d >= 20) else g_y - 622
    # الگوریتم ساده و پایدار
    gy2 = g_y + 1 if g_m > 2 else g_y
    days = (
        365 * g_y
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + g_d
        + sum(g_days_in_month[:g_m])
    )
    j_days_in_month = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    jm = 0
    for i, dim in enumerate(j_days_in_month):
        if days < dim:
            jm = i + 1
            jd = days + 1
            break
        days -= dim
    else:
        jm, jd = 12, days + 1
    return f"{jy:04d}/{jm:02d}/{jd:02d}"


def remaining_text(expires_at) -> str:
    if not expires_at:
        return "ندارد"
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", ""))
        except Exception:
            return "نامشخص"
    now = datetime.utcnow()
    if expires_at <= now:
        return "منقضی شده"
    delta = expires_at - now
    days = delta.days
    hours = delta.seconds // 3600
    if days > 0:
        return f"{days} روز و {hours} ساعت"
    mins = (delta.seconds % 3600) // 60
    return f"{hours} ساعت و {mins} دقیقه"


# ───────────────────────── دیتابیس ─────────────────────────
_pg = None
_use_pg = bool(DATABASE_URL)


def _connect_pg():
    global _pg
    import psycopg2
    import psycopg2.extras

    if _pg is None or _pg.closed:
        _pg = psycopg2.connect(DATABASE_URL, sslmode="require")
        _pg.autocommit = True
    return _pg


class DB:
    """لایه نازک SQLite / Postgres با پیشوند star_"""

    def __init__(self):
        if _use_pg:
            self.kind = "pg"
        else:
            self.kind = "sqlite"
            os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
            self.path = DB_PATH

    def conn(self):
        if self.kind == "pg":
            return _connect_pg()
        c = sqlite3.connect(self.path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def execute(self, sql, params=(), fetch=None):
        # تبدیل ? به %s برای postgres
        if self.kind == "pg":
            sql = sql.replace("?", "%s")
            conn = self.conn()
            cur = conn.cursor()
            cur.execute(sql, params)
            if fetch == "one":
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
            if fetch == "all":
                cols = [d[0] for d in cur.description] if cur.description else []
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            return cur.rowcount
        else:
            conn = self.conn()
            cur = conn.cursor()
            cur.execute(sql, params)
            if fetch == "one":
                row = cur.fetchone()
                conn.commit()
                conn.close()
                return dict(row) if row else None
            if fetch == "all":
                rows = cur.fetchall()
                conn.commit()
                conn.close()
                return [dict(r) for r in rows]
            conn.commit()
            conn.close()
            return cur.rowcount

    def init(self):
        stmts = [
            """CREATE TABLE IF NOT EXISTS star_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                started_at TEXT,
                referred_by BIGINT,
                ref_count INTEGER DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS star_subs (
                user_id BIGINT PRIMARY KEY,
                plan TEXT,
                expires_at TEXT,
                channel_id TEXT,
                signature_text TEXT,
                can_edit INTEGER DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS star_codes (
                code TEXT PRIMARY KEY,
                owner_id BIGINT,
                payload TEXT,
                created_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS star_tickets (
                id INTEGER PRIMARY KEY,
                tracking TEXT UNIQUE,
                user_id BIGINT,
                status TEXT,
                created_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS star_ticket_msgs (
                id INTEGER PRIMARY KEY,
                ticket_id INTEGER,
                from_admin INTEGER,
                text TEXT,
                created_at TEXT
            )""",
        ]
        # postgres: SERIAL
        if self.kind == "pg":
            stmts = [
                """CREATE TABLE IF NOT EXISTS star_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    started_at TEXT,
                    referred_by BIGINT,
                    ref_count INTEGER DEFAULT 0
                )""",
                """CREATE TABLE IF NOT EXISTS star_subs (
                    user_id BIGINT PRIMARY KEY,
                    plan TEXT,
                    expires_at TEXT,
                    channel_id TEXT,
                    signature_text TEXT,
                    can_edit INTEGER DEFAULT 0
                )""",
                """CREATE TABLE IF NOT EXISTS star_codes (
                    code TEXT PRIMARY KEY,
                    owner_id BIGINT,
                    payload TEXT,
                    created_at TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS star_tickets (
                    id SERIAL PRIMARY KEY,
                    tracking TEXT UNIQUE,
                    user_id BIGINT,
                    status TEXT,
                    created_at TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS star_ticket_msgs (
                    id SERIAL PRIMARY KEY,
                    ticket_id INTEGER,
                    from_admin INTEGER,
                    text TEXT,
                    created_at TEXT
                )""",
            ]
        for s in stmts:
            try:
                self.execute(s)
            except Exception as e:
                log.warning("init table: %s", e)
        
        # مهاجرت ستون‌های دعوت
        for alter in [
            "ALTER TABLE star_users ADD COLUMN referred_by BIGINT",
            "ALTER TABLE star_users ADD COLUMN ref_count INTEGER DEFAULT 0",
        ]:
            try:
                self.execute(alter)
            except Exception:
                pass


db = DB()

# ───────────────────────── وضعیت موقت کاربران ─────────────────────────
# state[user_id] = dict
states = {}

def get_state(uid):
    return states.setdefault(uid, {"step": None, "data": {}})

def clear_state(uid):
    states.pop(uid, None)


# ───────────────────────── API بله ─────────────────────────
def api(method, **kwargs):
    try:
        r = requests.post(f"{API}/{method}", json=kwargs, timeout=30)
        data = r.json()
        if not data.get("ok"):
            log.warning("api %s: %s", method, data)
        return data
    except Exception as e:
        log.exception("api %s error: %s", method, e)
        return {"ok": False, "description": str(e)}


def send_message(chat_id, text, reply_markup=None, reply_to=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return api("sendMessage", **payload)


def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api("editMessageText", **payload)


def answer_callback(cq_id, text=None):
    payload = {"callback_query_id": cq_id}
    if text:
        payload["text"] = text
    return api("answerCallbackQuery", **payload)


def delete_message(chat_id, message_id):
    return api("deleteMessage", chat_id=chat_id, message_id=message_id)


def get_chat_member(chat_id, user_id):
    return api("getChatMember", chat_id=chat_id, user_id=user_id)


def get_chat(chat_id):
    return api("getChat", chat_id=chat_id)


# ───────────────────────── کیبوردها ─────────────────────────
def main_kb(is_admin=False):
    rows = [
        [{"text": "📝 ساخت پست"}],
        [{"text": "👤 حساب کاربری"}, {"text": "✍️ امضا زیر پیام"}],
        [{"text": "🎫 تیکت و پشتیبانی"}, {"text": "🔗 دعوت دوستان"}],
        [{"text": "🤖 ربات‌های دیگر ما"}, {"text": "📖 راهنما"}],
    ]
    return {"keyboard": rows, "resize_keyboard": True}


def back_kb(text="🔙 بازگشت"):
    return {"keyboard": [[{"text": text}]], "resize_keyboard": True}


def cancel_kb():
    return {"keyboard": [[{"text": "🔙 بازگشت"}]], "resize_keyboard": True}


def inline(rows):
    return {"inline_keyboard": rows}


# ───────────────────────── کاربران و اشتراک ─────────────────────────
def ensure_user(user, referred_by=None):
    """ثبت کاربر؛ True اگر کاربر کاملاً جدید باشد"""
    uid = user["id"]
    username = user.get("username") or ""
    first_name = user.get("first_name") or ""
    now = datetime.utcnow().isoformat()
    row = db.execute("SELECT user_id FROM star_users WHERE user_id=?", (uid,), fetch="one")
    if not row:
        db.execute(
            "INSERT INTO star_users (user_id, username, first_name, started_at, referred_by, ref_count) VALUES (?,?,?,?,?,?)",
            (uid, username, first_name, now, referred_by, 0),
        )
        return True
    db.execute(
        "UPDATE star_users SET username=?, first_name=? WHERE user_id=?",
        (username, first_name, uid),
    )
    return False


def get_sub(uid):
    return db.execute("SELECT * FROM star_subs WHERE user_id=?", (uid,), fetch="one")


def is_sub_active(uid):
    sub = get_sub(uid)
    if not sub or not sub.get("expires_at"):
        return False
    try:
        exp = datetime.fromisoformat(sub["expires_at"].replace("Z", ""))
        return exp > datetime.utcnow()
    except Exception:
        return False


def add_sub_days(uid, days, plan_label=None):
    """اضافه کردن روز به اشتراک (جمع با باقیمانده)"""
    now = datetime.utcnow()
    row = get_sub(uid)
    base = now
    channel_id = signature_text = None
    can_edit = 0
    if row:
        channel_id = row.get("channel_id")
        signature_text = row.get("signature_text")
        can_edit = row.get("can_edit") or 0
        if row.get("expires_at"):
            try:
                exp = datetime.fromisoformat(str(row["expires_at"]).replace("Z", ""))
                if exp > now:
                    base = exp
            except Exception:
                pass
    new_exp = base + timedelta(days=int(days))
    exp_s = new_exp.isoformat()
    plan = plan_label or (row.get("plan") if row else "gift")
    if row:
        db.execute(
            "UPDATE star_subs SET plan=?, expires_at=? WHERE user_id=?",
            (plan, exp_s, uid),
        )
    else:
        db.execute(
            "INSERT INTO star_subs (user_id, plan, expires_at, channel_id, signature_text, can_edit) VALUES (?,?,?,?,?,?)",
            (uid, plan, exp_s, channel_id, signature_text, can_edit),
        )
    return exp_s


def activate_sub(uid, plan):
    days = {"1m": 30, "3m": 90, "1y": 365}.get(plan, 30)
    return add_sub_days(uid, days, plan_label=plan)


def apply_referral_reward(referrer_id):
    """referral reward"""
    row = db.execute("SELECT ref_count FROM star_users WHERE user_id=?", (referrer_id,), fetch="one")
    count = int((row or {}).get("ref_count") or 0) + 1
    db.execute("UPDATE star_users SET ref_count=? WHERE user_id=?", (count, referrer_id))
    send_message(referrer_id, "✅ دعوت موفق بود!\nتعداد دعوت‌های موفق شما: *%d*" % count)
    if count % 3 == 0:
        months = min(count // 3, 12)
        exp = add_sub_days(referrer_id, months * 30, plan_label="ref%dm" % months)
        send_message(
            referrer_id,
            "🎁 با %d دعوت موفق، *%d ماه* اشتراک برای شما فعال/تمدید شد.\nتاریخ اتمام (شمسی): %s"
            % (count, months, to_jalali(datetime.fromisoformat(exp))),
        )
    return count



def revoke_sub(uid):
    db.execute("DELETE FROM star_subs WHERE user_id=?", (uid,))


def count_users():
    row = db.execute("SELECT COUNT(*) AS c FROM star_users", fetch="one")
    return row["c"] if row else 0


# ───────────────────────── تیکت ─────────────────────────
def new_tracking():
    return "ST" + "".join(secrets.choice(string.digits) for _ in range(8))


def create_ticket(user_id, text):
    tracking = new_tracking()
    now = datetime.utcnow().isoformat()
    # insert ticket
    if db.kind == "pg":
        db.execute(
            "INSERT INTO star_tickets (tracking, user_id, status, created_at) VALUES (?,?,?,?)",
            (tracking, user_id, "open", now),
        )
        row = db.execute(
            "SELECT id FROM star_tickets WHERE tracking=?", (tracking,), fetch="one"
        )
        tid = row["id"]
    else:
        db.execute(
            "INSERT INTO star_tickets (tracking, user_id, status, created_at) VALUES (?,?,?,?)",
            (tracking, user_id, "open", now),
        )
        row = db.execute(
            "SELECT id FROM star_tickets WHERE tracking=?", (tracking,), fetch="one"
        )
        tid = row["id"]
    db.execute(
        "INSERT INTO star_ticket_msgs (ticket_id, from_admin, text, created_at) VALUES (?,?,?,?)",
        (tid, 0, text, now),
    )
    return tracking, tid


def get_ticket_by_tracking(tracking):
    return db.execute(
        "SELECT * FROM star_tickets WHERE tracking=?", (tracking,), fetch="one"
    )


def get_open_ticket_for_user(uid):
    return db.execute(
        "SELECT * FROM star_tickets WHERE user_id=? AND status='open' ORDER BY id DESC",
        (uid,),
        fetch="one",
    )


def add_ticket_msg(ticket_id, from_admin, text):
    now = datetime.utcnow().isoformat()
    db.execute(
        "INSERT INTO star_ticket_msgs (ticket_id, from_admin, text, created_at) VALUES (?,?,?,?)",
        (ticket_id, 1 if from_admin else 0, text, now),
    )


def close_ticket(tracking):
    db.execute(
        "UPDATE star_tickets SET status='closed' WHERE tracking=?", (tracking,)
    )


def ticket_msgs(ticket_id):
    return db.execute(
        "SELECT * FROM star_ticket_msgs WHERE ticket_id=? ORDER BY id",
        (ticket_id,),
        fetch="all",
    )


# ───────────────────────── ساخت پست / امضا ─────────────────────────
def build_inline_from_buttons(buttons):
    """
    buttons: list of rows, each row list of {text, url}
    """
    rows = []
    for row in buttons:
        rows.append([{"text": b["text"], "url": b["url"]} for b in row])
    return {"inline_keyboard": rows} if rows else None


def format_preview(content, buttons):
    lines = []
    if content.get("text"):
        lines.append(content["text"])
    if content.get("type") and content["type"] != "text":
        lines.append(f"[{content['type']}]")
    if buttons:
        lines.append("\nدکمه‌ها:")
        for row in buttons:
            lines.append(" | ".join(f"{b['text']} → {b['url']}" for b in row))
    return "\n".join(lines) if lines else "(خالی)"


# ───────────────────────── هندلرها ─────────────────────────
def handle_start(chat_id, user, text):
    uid = user["id"]
    parts = (text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) == 2 else ""

    if arg.lower() == "restart":
        clear_state(uid)
        ensure_user(user)
        send_message(chat_id, "به منوی اصلی برگشتید.", reply_markup=main_kb())
        return

    referred_by = None
    if arg.startswith("ref_"):
        try:
            referred_by = int(arg[4:].strip())
            if referred_by == uid:
                referred_by = None
        except Exception:
            referred_by = None

    is_new = ensure_user(user, referred_by=referred_by)
    if is_new and referred_by:
        ref_row = db.execute("SELECT user_id FROM star_users WHERE user_id=?", (referred_by,), fetch="one")
        if ref_row:
            apply_referral_reward(referred_by)

    welcome = (
        "🌟 سلام! به ربات *استار* خوش آمدید.\n\n"
        "با من می‌تونی:\n"
        "📝 پست با دکمه شیشه‌ای بسازی\n"
        "📢 پست را مستقیم به کانال بفرستی\n"
        "✍️ امضا زیر پست‌های کانالت بذاری\n"
        "🎫 تیکت و پشتیبانی بگیری\n"
        "🔗 دوستان را دعوت کن و اشتراک رایگان بگیر\n\n"
        "👇 از دکمه‌های پایین شروع کن."
    )
    send_message(chat_id, welcome, reply_markup=main_kb())
    clear_state(uid)




def handle_account(chat_id, user):
    uid = user["id"]
    ensure_user(user)
    sub = get_sub(uid)
    active = is_sub_active(uid)
    uname = f"@{user.get('username')}" if user.get("username") else "—"
    name = user.get("first_name") or "—"
    if active and sub:
        exp = sub.get("expires_at")
        try:
            exp_dt = datetime.fromisoformat(exp.replace("Z", ""))
        except Exception:
            exp_dt = None
        status = "✅ فعال"
        remain = remaining_text(exp)
        jalali = to_jalali(exp_dt)
        plan = sub.get("plan") or "—"
        ch = sub.get("channel_id") or "تنظیم نشده"
        sig = "دارد" if sub.get("signature_text") else "ندارد"
    else:
        status = "❌ غیرفعال"
        remain = "—"
        jalali = "—"
        plan = "—"
        ch = "—"
        sig = "—"
    text = (
        "👤 *حساب کاربری*\n\n"
        f"🆔 آیدی عددی: `{uid}`\n"
        f"📛 نام: {name}\n"
        f"🔗 یوزرنیم: {uname}\n\n"
        f"📦 وضعیت اشتراک: {status}\n"
        f"🗓 پلن: {plan}\n"
        f"⏳ مانده: {remain}\n"
        f"📅 تاریخ اتمام (شمسی): {jalali}\n"
        f"📢 کانال امضا: {ch}\n"
        f"✍️ امضا: {sig}"
    )
    rowu = db.execute("SELECT ref_count FROM star_users WHERE user_id=?", (uid,), fetch="one")
    refs = int((rowu or {}).get("ref_count") or 0)
    text += f"\n\n🔗 دعوت‌های موفق: *{refs}*"
    send_message(chat_id, text, reply_markup=main_kb())


def handle_other_bots(chat_id, uid):
    text = (
        "🤖 *ربات‌های دیگر ما*\n\n"
        "🧬 ربات شبیه‌ساز زندگی\n"
        "@Shabihsaz_zendegi_bot\n\n"
        "🧮 ماشین حساب مهندسی\n"
        "@Calculator5bot"
    )
    send_message(chat_id, text, reply_markup=main_kb())


def handle_help(chat_id, uid):
    text = (
        "📖 *راهنما*\n\n"
        "1️⃣ روی «📝 ساخت پست» بزنید و محتوا را بفرستید.\n"
        "2️⃣ دکمه دستوری یا با دکمه (➕) بسازید.\n"
        "3️⃣ پیش‌نمایش را تأیید کنید.\n"
        "4️⃣ آیدی کانال را بفرستید (با @ یا عددی).\n"
        "5️⃣ پست مستقیم در کانال ارسال می‌شود.\n\n"
        "✍️ امضا: بعد از فعال‌سازی اشتراک، کانال و متن امضا را تنظیم کنید.\n"
        "🎫 پشتیبانی: از دکمه تیکت استفاده کنید.\n\n"
        f"پشتیبانی انسانی: {SUPPORT_USER}"
    )
    send_message(chat_id, text, reply_markup=main_kb())


def handle_admin_panel(chat_id, uid):
    if uid != ADMIN_ID:
        return
    n = count_users()
    text = (
        "🛠 *پنل ادمین*\n\n"
        f"👥 تعداد کاربرانی که ربات را استارت کرده‌اند: *{n}*\n\n"
        "از دکمه‌های زیر استفاده کنید."
    )
    kb = {
        "keyboard": [
            [{"text": "📊 آمار کاربران"}, {"text": "📘 راهنمای ادمین"}],
            [{"text": "🔙 بازگشت به منو"}],
        ],
        "resize_keyboard": True,
    }
    send_message(chat_id, text, reply_markup=kb)


def handle_admin_guide(chat_id, uid):
    if uid != ADMIN_ID:
        return
    n = count_users()
    msg = (
        "📘 *راهنمای ادمین*\n\n"
        "این بخش فقط برای شماست.\n\n"
        "✅ فعال‌سازی:\n`برای 123456789 اشتراک 1m فعال کن`\n"
        "پلن‌ها: `1m` | `3m` | `1y`\n\n"
        "❌ لغو:\n`برای 123456789 اشتراک لغو کن`\n\n"
        "📣 اطلاع‌رسانی همگانی:\n`اطلاع رسانی ربات`\n"
        "سپس متن/عکس/فایل یا فوروارد را بفرستید.\n\n"
        "🎫 تیکت‌های باز:\n`تیکت های باز`\n"
        "بستن: `بستن تیکت ST...` یا ریپلای `تیکت ببند`\n"
        "مشاهده: `تیکت ST...`\n\n"
        "📋 /subs\n\n"
        f"👥 تعداد استارت‌ها: *{n}*"
    )
    send_message(chat_id, msg)




def handle_admin_stats(chat_id, uid):
    if uid != ADMIN_ID:
        return
    n = count_users()
    active = 0
    rows = db.execute("SELECT * FROM star_subs", fetch="all") or []
    for r in rows:
        try:
            if datetime.fromisoformat(r["expires_at"].replace("Z", "")) > datetime.utcnow():
                active += 1
        except Exception:
            pass
    send_message(
        chat_id,
        f"📊 *آمار*\n\n👥 کل استارت‌ها: {n}\n✅ اشتراک فعال: {active}",
    )


# ─── تیکت و پشتیبانی ───
def handle_support_menu(chat_id, uid):
    st = get_state(uid)
    st["step"] = "support_menu"
    kb = {
        "keyboard": [
            [{"text": "💬 پشتیبانی در پیوی"}, {"text": "🤖 پشتیبانی از داخل بات"}],
            [{"text": "🔙 بازگشت"}],
        ],
        "resize_keyboard": True,
    }
    send_message(chat_id, "🎫 *تیکت و پشتیبانی*\n\nیکی از گزینه‌ها را انتخاب کنید:", reply_markup=kb)


def open_ticket_from_user(chat_id, user, text):
    uid = user["id"]
    tracking, tid = create_ticket(uid, text)
    uname = f"@{user.get('username')}" if user.get("username") else "—"
    name = user.get("first_name") or "—"
    # به کاربر
    send_message(
        chat_id,
        f"✅ تیکت شما ثبت شد.\n\n🔢 شماره پیگیری: `{tracking}`\n\nپاسخ ادمین همین‌جا اعلام می‌شود.",
        reply_markup=main_kb(),
    )
    clear_state(uid)
    # به ادمین
    admin_text = (
        f"🎫 *تیکت جدید*\n\n"
        f"🔢 پیگیری: `{tracking}`\n"
        f"👤 نام: {name}\n"
        f"🔗 یوزرنیم: {uname}\n"
        f"🆔 آیدی: `{uid}`\n\n"
        f"💬 پیام:\n{text}"
    )
    ikb = inline(
        [
            [
                {"text": "💬 پاسخ", "callback_data": f"treply:{tracking}"},
                {"text": "🗑 بستن تیکت", "callback_data": f"tclose:{tracking}"},
            ]
        ]
    )
    send_message(ADMIN_ID, admin_text, reply_markup=ikb)


def admin_reply_ticket(tracking, admin_text):
    t = get_ticket_by_tracking(tracking)
    if not t or t["status"] != "open":
        return False, "تیکت باز نیست."
    add_ticket_msg(t["id"], True, admin_text)
    uid = t["user_id"]
    msg = (
        f"💬 *پاسخ پشتیبانی* (پیگیری `{tracking}`)\n\n{admin_text}"
    )
    ikb = inline(
        [
            [
                {"text": "💬 پاسخ", "callback_data": f"ureply:{tracking}"},
                {"text": "🗑 بستن تیکت", "callback_data": f"uclose:{tracking}"},
            ]
        ]
    )
    send_message(uid, msg, reply_markup=ikb)
    return True, "ok"


# ─── ساخت پست ───
def start_create_post(chat_id, uid):
    st = get_state(uid)
    st["step"] = "wait_content"
    st["data"] = {"content": {}, "buttons": [], "current_row": []}
    send_message(
        chat_id,
        "📝 *ساخت پست*\n\n"
        "لطفاً مطلب خودتان را بفرستید.\n"
        "متن تنها، عکس، فیلم یا فایل…\n\n"
        "برای انصراف: 🔙 بازگشت",
        reply_markup=back_kb(),
    )


def after_content(chat_id, uid):
    st = get_state(uid)
    st["step"] = "choose_btn_mode"
    kb = inline(
        [
            [
                {"text": "⌨️ دکمه دستوری", "callback_data": "btnmode:cmd"},
                {"text": "➕ با دکمه", "callback_data": "btnmode:plus"},
            ],
            [{"text": "⏭ بدون دکمه", "callback_data": "btnmode:none"}],
        ]
    )
    send_message(chat_id, "محتوا دریافت شد ✅\n\nنوع دکمه را انتخاب کنید:", reply_markup=kb)
    send_message(chat_id, "برای انصراف: 🔙 بازگشت", reply_markup=back_kb())




def normalize_url(u: str) -> str:
    """تصحیح طرح لینک (Https -> https) و افزودن https در صورت نیاز"""
    u = (u or "").strip()
    if not u:
        return u
    if "://" in u:
        scheme, rest = u.split("://", 1)
        u = scheme.lower() + "://" + rest
    else:
        u = "https://" + u.lstrip("/")
    return u


def parse_command_buttons(text):
    """
    هر خط یک ردیف
    دکمه‌های یک ردیف با && جدا می‌شوند
    فرمت هر دکمه: متن - لینک
    """
    rows = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("&&")]
        row = []
        for p in parts:
            if " - " in p:
                t, u = p.split(" - ", 1)
            elif "-" in p:
                t, u = p.split("-", 1)
            else:
                continue
            t, u = t.strip(), u.strip()
            if t and u:
                row.append({"text": t, "url": normalize_url(u)})
        if row:
            rows.append(row)
    return rows


def plus_menu_markup():
    return inline(
        [
            [
                {"text": "➕", "callback_data": "plus:same"},
                {"text": "➕ ردیف پایین", "callback_data": "plus:new"},
            ],
            [{"text": "✅ تمام", "callback_data": "plus:done"}],
        ]
    )


def ask_channel(chat_id, uid):
    st = get_state(uid)
    st["step"] = "wait_channel"
    send_message(
        chat_id,
        "📢 *کانال مقصد*\n\n"
        "آیدی کانال را بفرستید (با @ یا عددی).\n\n"
        f"⚠️ اگر می‌خواهید در کانال از ربات استفاده کنید:\n"
        f"بخش *مدیران* ← *افزودن مدیر* ← سرچ کنید `{BOT_USERNAME}` و ربات را اضافه کنید.\n\n"
        "ربات و خودتان باید ادمین کانال باشید.",
        reply_markup=back_kb(),
    )


def ask_send_method(chat_id, uid):
    """بعد از انتخاب کانال: در صورت نیاز امضا بپرس، وگرنه مستقیم ارسال کن"""
    st = get_state(uid)
    data = st["data"]
    ch = data.get("channel_id")
    sub = get_sub(uid)
    if sub and sub.get("signature_text") and sub.get("channel_id"):
        sch = str(sub["channel_id"])
        chs = str(ch)
        if sch == chs or sch.lstrip("@") == chs.lstrip("@"):
            st["data"]["sig_text"] = sub["signature_text"]
            st["step"] = "ask_include_sig"
            kb = {
                "keyboard": [
                    [{"text": "✅ بله، امضا اضافه شود"}, {"text": "❌ نه، بدون امضا"}],
                    [{"text": "🔙 بازگشت"}],
                ],
                "resize_keyboard": True,
            }
            send_message(
                chat_id,
                "✍️ برای این کانال *امضا* فعال است.\n"
                "متن امضا:\n%s\n\n"
                "آیا موقع ارسال، امضا به انتهای مطلب اضافه شود؟"
                % (sub["signature_text"],),
                reply_markup=kb,
            )
            return
    do_direct_send(chat_id, uid)




def apply_signature_if_needed(data):
    if data.get("include_sig") and data.get("sig_text"):
        c = data.get("content") or {}
        if c.get("text"):
            c["text"] = c["text"].rstrip() + "\n\n" + data["sig_text"]
        else:
            c["text"] = data["sig_text"]
        data["content"] = c


def send_post_payload(chat_id, payload, as_preview=False):
    content = payload.get("content") or {}
    buttons = payload.get("buttons") or []
    markup = build_inline_from_buttons(buttons)
    ctype = content.get("type", "text")
    caption = content.get("text") or content.get("caption") or ""
    if ctype == "text" or not content.get("file_id"):
        return send_message(chat_id, caption or "(بدون متن)", reply_markup=markup)
    method = {
        "photo": "sendPhoto",
        "video": "sendVideo",
        "document": "sendDocument",
        "audio": "sendAudio",
        "voice": "sendVoice",
    }.get(ctype, "sendDocument")
    body = {"chat_id": chat_id, ctype if ctype != "document" else "document": content["file_id"]}
    # normalize
    if ctype == "photo":
        body = {"chat_id": chat_id, "photo": content["file_id"]}
    elif ctype == "video":
        body = {"chat_id": chat_id, "video": content["file_id"]}
    elif ctype == "document":
        body = {"chat_id": chat_id, "document": content["file_id"]}
    else:
        body = {"chat_id": chat_id, "document": content["file_id"]}
    if caption:
        body["caption"] = caption
    if markup:
        body["reply_markup"] = markup
    return api(method if method != "sendDocument" or ctype == "document" else "sendDocument", **body)


def extract_content_from_message(msg):
    content = {"type": "text", "text": msg.get("text") or ""}
    if msg.get("photo"):
        content = {
            "type": "photo",
            "file_id": msg["photo"][-1]["file_id"],
            "text": msg.get("caption") or "",
        }
    elif msg.get("video"):
        content = {
            "type": "video",
            "file_id": msg["video"]["file_id"],
            "text": msg.get("caption") or "",
        }
    elif msg.get("document"):
        content = {
            "type": "document",
            "file_id": msg["document"]["file_id"],
            "text": msg.get("caption") or "",
        }
    elif msg.get("audio"):
        content = {
            "type": "audio",
            "file_id": msg["audio"]["file_id"],
            "text": msg.get("caption") or "",
        }
    elif msg.get("voice"):
        content = {
            "type": "voice",
            "file_id": msg["voice"]["file_id"],
            "text": msg.get("caption") or "",
        }
    return content


def do_direct_send(chat_id, uid):
    st = get_state(uid)
    data = st["data"]
    apply_signature_if_needed(data)
    channel = data["channel_id"]
    payload = {"content": data["content"], "buttons": data.get("buttons") or []}
    res = send_post_payload(channel, payload)
    if not res.get("ok"):
        send_message(
            chat_id,
            f"❌ ارسال ناموفق بود.\n{res.get('description', '')}\n"
            f"مطمئن شوید ربات ادمین کانال است و `{BOT_USERNAME}` را اضافه کرده‌اید.",
            reply_markup=main_kb(),
        )
        clear_state(uid)
        return
    # لینک پیام
    result = res.get("result") or {}
    mid = result.get("message_id")
    link = None
    # تلاش برای ساخت لینک بله
    ch_info = get_chat(channel)
    uname = None
    if ch_info.get("ok"):
        uname = (ch_info.get("result") or {}).get("username")
    if uname and mid:
        link = f"https://ble.ir/{uname}/{mid}"
    elif mid:
        # عددی
        cid = str(channel).replace("-100", "")
        link = f"https://ble.ir/c/{cid}/{mid}"
    msg = "✅ پست در کانال ارسال شد."
    if link:
        msg += f"\n\n🔗 لینک پیام:\n{link}"
    send_message(chat_id, msg, reply_markup=main_kb())
    clear_state(uid)


def do_code_send(chat_id, uid):
    st = get_state(uid)
    data = st["data"]
    apply_signature_if_needed(data)
    code = secrets.token_hex(4)
    payload = {"content": data["content"], "buttons": data.get("buttons") or []}
    db.execute(
        "INSERT INTO star_codes (code, owner_id, payload, created_at) VALUES (?,?,?,?)",
        (code, uid, json.dumps(payload, ensure_ascii=False), datetime.utcnow().isoformat()),
    )
    send_message(
        chat_id,
        "✅ کد یکتا ساخته شد.\n\n"
        f"این دستور را در گروه/کانالی که ربات عضو است بفرستید:\n\n"
        f"`/start {code}`\n\n"
        "اگر ربات مجوز حذف پیام داشته باشد، خود دستور پاک می‌شود.",
        reply_markup=main_kb(),
    )
    clear_state(uid)


# ─── امضا ───
def start_signature_flow(chat_id, uid):
    if not is_sub_active(uid):
        # فروش اشتراک
        kb = {
            "keyboard": [
                [{"text": "۱ ماهه — ۷ هزار"}, {"text": "۳ ماهه — ۱۲ هزار"}],
                [{"text": "۱ ساله — ۸۰ هزار"}],
                [{"text": "🔙 بازگشت"}],
            ],
            "resize_keyboard": True,
        }
        st = get_state(uid)
        st["step"] = "buy_plan"
        send_message(
            chat_id,
            "✍️ *امضا زیر پیام*\n\n"
            "این بخش نیاز به اشتراک دارد.\n"
            "یکی از پلن‌ها را انتخاب کنید:",
            reply_markup=kb,
        )
        return
    sub = get_sub(uid)
    if sub and sub.get("channel_id") and sub.get("signature_text"):
        kb = {
            "keyboard": [
                [{"text": "✏️ ویرایش متن امضا"}, {"text": "📢 تغییر کانال"}],
                [{"text": "🔙 بازگشت"}],
            ],
            "resize_keyboard": True,
        }
        send_message(
            chat_id,
            f"✍️ امضای شما فعال است.\n\n📢 کانال: {sub.get('channel_id')}\n"
            f"متن:\n{sub.get('signature_text')}",
            reply_markup=kb,
        )
        st = get_state(uid)
        st["step"] = "sig_menu"
        return
    st = get_state(uid)
    st["step"] = "sig_channel"
    send_message(
        chat_id,
        "✍️ اشتراک شما فعال است.\n\n"
        "آیدی کانالی که می‌خواهید امضا زیر پست‌هایش بیاید را بفرستید "
        "(با @ یا عددی).\n\n"
        f"ربات `{BOT_USERNAME}` باید ادمین کانال باشد.",
        reply_markup=back_kb(),
    )


def plan_invoice(chat_id, user, plan_key, price):
    uid = user["id"]
    plan_name = {"1m": "یک‌ماهه", "3m": "سه‌ماهه", "1y": "یک‌ساله"}.get(plan_key, plan_key)
    text = (
        "🛒 درخواست اشتراک *%s*\n\n"
        "🆔 آیدی عددی شما: `%s`\n"
        "مبلغ: *%s هزار تومان*\n\n"
        "این پیام را برای %s فوروارد کنید و "
        "پاکت هدیه *%s هزار تومنی* را هم ارسال کنید تا اشتراک شما فعال شود."
    ) % (plan_name, uid, price, SUPPORT_USER, price)
    send_message(chat_id, text, reply_markup=main_kb())
    try:
        send_message(
            ADMIN_ID,
            "🛒 درخواست خرید\nکاربر: `%s`\nپلن: %s (%sهزار)\nفعال‌سازی: برای %s اشتراک %s فعال کن"
            % (uid, plan_key, price, uid, plan_key),
        )
    except Exception:
        pass
    clear_state(uid)



def process_admin_text_commands(chat_id, uid, text, msg):
    if uid != ADMIN_ID:
        return False
    t = text.strip()

    # راهنمای ادمین
    if t in ("راهنمای ادمین", "/admin", "admin"):
        handle_admin_guide(chat_id, uid)
        return True

    # لیست تیکت‌های باز
    if t in ("تیکت های باز", "تیکت‌های باز", "تیکتهای باز"):
        rows = db.execute(
            "SELECT * FROM star_tickets WHERE status='open' ORDER BY id DESC",
            fetch="all",
        ) or []
        if not rows:
            send_message(chat_id, "تیکت بازی وجود ندارد.")
            return True
        lines = [f"🎫 تیکت‌های باز ({len(rows)}):"]
        for r in rows[:40]:
            lines.append(f"• `{r['tracking']}` | user=`{r['user_id']}` | {r.get('created_at','')[:16]}")
        send_message(chat_id, "\n".join(lines))
        return True

    # اطلاع‌رسانی
    if t in ("اطلاع رسانی ربات", "اطلاع‌رسانی ربات", "اطلاع رسانی"):
        st = get_state(uid)
        st["step"] = "broadcast_wait"
        send_message(chat_id, "📣 پیام اطلاع‌رسانی را بفرستید (متن، عکس، فایل یا فوروارد).")
        return True

    # فعال‌سازی
    mact = re.search(
        r"برای\s+(\d+)\s+اشتراک\s+(1m|3m|1y)\s+فعال\s*کن",
        text,
        re.I,
    )
    if mact:
        target, plan = int(mact.group(1)), mact.group(2).lower()
        exp = activate_sub(target, plan)
        send_message(chat_id, f"✅ اشتراک {plan} برای `{target}` تا {to_jalali(datetime.fromisoformat(exp))} فعال شد.")
        try:
            send_message(target, f"✅ اشتراک شما ({plan}) فعال شد.\nاز منوی «✍️ امضا زیر پیام» ادامه دهید.")
        except Exception:
            pass
        return True

    # لغو
    mrev = re.search(r"برای\s+(\d+)\s+اشتراک\s+لغو\s*کن", text, re.I)
    if not mrev:
        mrev = re.search(r"اشتراک\s+(\d+)\s+لغو", text, re.I)
    if mrev:
        target = int(mrev.group(1))
        revoke_sub(target)
        send_message(chat_id, f"❌ اشتراک `{target}` لغو شد.")
        try:
            send_message(target, "❌ اشتراک شما توسط ادمین لغو شد.")
        except Exception:
            pass
        return True

    # /subs
    if t in ("/subs", "subs"):
        rows = db.execute("SELECT * FROM star_subs", fetch="all") or []
        if not rows:
            send_message(chat_id, "هیچ اشتراکی نیست.")
            return True
        lines = []
        for r in rows:
            lines.append(
                f"`{r['user_id']}` | {r.get('plan')} | تا {to_jalali(r.get('expires_at'))} | ch={r.get('channel_id')}"
            )
        send_message(chat_id, "📋 اشتراک‌ها:\n\n" + "\n".join(lines[:50]))
        return True

    # بستن تیکت با دستور
    mclose = re.search(r"بستن\s*تیکت\s*(ST\d+)", text, re.I)
    if mclose:
        tr = mclose.group(1).upper()
        close_ticket(tr)
        tk = get_ticket_by_tracking(tr)
        send_message(chat_id, f"✅ تیکت `{tr}` بسته شد.")
        if tk:
            try:
                send_message(tk["user_id"], f"🔒 تیکت `{tr}` توسط پشتیبانی بسته شد.")
            except Exception:
                pass
        return True

    # مشاهده تیکت
    mview = re.search(r"تیکت\s+(ST\d+)", text, re.I)
    if mview:
        tr = mview.group(1).upper()
        tk = get_ticket_by_tracking(tr)
        if not tk:
            send_message(chat_id, "تیکت پیدا نشد.")
            return True
        msgs = ticket_msgs(tk["id"])
        body = [f"🎫 تیکت `{tr}` | status={tk['status']} | user=`{tk['user_id']}`"]
        for x in msgs:
            who = "ادمین" if x["from_admin"] else "کاربر"
            body.append(f"• [{who}] {x['text']}")
        send_message(chat_id, "\n".join(body)[:3500])
        return True

    # ریپلای روی پیام تیکت: «تیکت ببند»
    if t in ("تیکت ببند", "بستن تیکت") and msg.get("reply_to_message"):
        rt = msg["reply_to_message"].get("text") or ""
        mm = re.search(r"(ST\d+)", rt)
        if mm:
            tr = mm.group(1).upper()
            close_ticket(tr)
            tk = get_ticket_by_tracking(tr)
            send_message(chat_id, f"✅ تیکت `{tr}` بسته شد.")
            if tk:
                try:
                    send_message(tk["user_id"], f"🔒 تیکت `{tr}` بسته شد.")
                except Exception:
                    pass
            return True
    return False


def copy_message_to_chat(to_chat_id, from_chat_id, message_id):
    """کپی پیام (برای فوروارد/مدیا در اطلاع‌رسانی)"""
    return api(
        "copyMessage",
        chat_id=to_chat_id,
        from_chat_id=from_chat_id,
        message_id=message_id,
    )


def broadcast_message(admin_chat_id, msg):
    users = db.execute("SELECT user_id FROM star_users", fetch="all") or []
    ok = fail = 0
    mid = msg.get("message_id")
    from_chat = admin_chat_id
    for u in users:
        uid = u["user_id"]
        try:
            # اول copyMessage (عکس/فایل/فوروارد)
            r = copy_message_to_chat(uid, from_chat, mid)
            if r.get("ok"):
                ok += 1
            else:
                # فالبک متن
                txt = msg.get("text") or msg.get("caption")
                if txt:
                    send_message(uid, txt)
                    ok += 1
                else:
                    fail += 1
        except Exception:
            fail += 1
        time.sleep(0.05)
    send_message(admin_chat_id, f"📣 اطلاع‌رسانی تمام شد.\n✅ موفق: {ok}\n❌ ناموفق: {fail}")




def process_message(msg):
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type", "")
    user = msg.get("from") or {}
    uid = user.get("id")
    text = (msg.get("text") or "").strip()

    if not uid or not chat_id:
        return

    # فقط در خصوصی منوی کامل؛ در گروه فقط /start code
    is_private = chat_type == "private"

    # دستور کد در گروه/کانال
    # /restart در هر مرحله
    if is_private and text.strip().lower() in ("/restart", "restart"):
        clear_state(uid)
        ensure_user(user)
        send_message(chat_id, "به منوی اصلی برگشتید.", reply_markup=main_kb())
        return

    if text.startswith("/start"):
        if is_private:
            handle_start(chat_id, user, text)
        return

    if not is_private:
        return

    ensure_user(user)

    # دستورات ادمین
    if process_admin_text_commands(chat_id, uid, text, msg):
        return

    st = get_state(uid)
    step = st.get("step")

    # اطلاع‌رسانی (ادمین)
    if step == "broadcast_wait" and uid == ADMIN_ID:
        broadcast_message(chat_id, msg)
        clear_state(uid)
        return

    # دکمه‌های ثابت منو
    if text == "🔙 بازگشت" or text == "🔙 بازگشت به منو":
        clear_state(uid)
        send_message(chat_id, "منوی اصلی:", reply_markup=main_kb())
        return

    if text == "📝 ساخت پست":
        start_create_post(chat_id, uid)
        return
    if text == "👤 حساب کاربری":
        clear_state(uid)
        handle_account(chat_id, user)
        return
    if text == "✍️ امضا زیر پیام":
        start_signature_flow(chat_id, uid)
        return
    if text == "🎫 تیکت و پشتیبانی":
        handle_support_menu(chat_id, uid)
        return
    if text == "🔗 دعوت دوستان":
        clear_state(uid)
        link = "https://ble.ir/%s?start=ref_%s" % (BOT_USERNAME, uid)
        row = db.execute("SELECT ref_count FROM star_users WHERE user_id=?", (uid,), fetch="one")
        cnt = int((row or {}).get("ref_count") or 0)
        next_need = 3 - (cnt % 3)
        if next_need == 0:
            next_need = 3
        months_next = min((cnt // 3) + 1, 12)
        msg = (
            "🔗 *دعوت دوستان*\n\n"
            "با معرفی ربات به دوستان، اشتراک رایگان بگیر!\n\n"
            "لینک اختصاصی شما:\n%s\n\n"
            "✅ دعوت موفق تا الان: *%d*\n"
            "برای پاداش بعدی *%d* دعوت دیگر لازم است (پاداش بعدی: *%d ماه*).\n\n"
            "هر ۳ دعوت موفق = اشتراک هدیه (۱ تا ۱۲ ماه)\n"
            "مدت اشتراک‌ها با هم جمع می‌شود."
        ) % (link, cnt, next_need, months_next)
        send_message(chat_id, msg, reply_markup=main_kb())
        return

    if text == "🤖 ربات‌های دیگر ما":
        clear_state(uid)
        handle_other_bots(chat_id, uid)
        return
    if text == "📖 راهنما":
        clear_state(uid)
        handle_help(chat_id, uid)
        return

    # پشتیبانی
    if text == "💬 پشتیبانی در پیوی":
        send_message(
            chat_id,
            f"برای پشتیبانی مستقیم پیام دهید:\n{SUPPORT_USER}",
            reply_markup=main_kb(),
        )
        clear_state(uid)
        return
    if text == "🤖 پشتیبانی از داخل بات":
        st["step"] = "wait_ticket"
        send_message(
            chat_id,
            "لطفاً پیام خودتان را بفرستید.\nبعد از ارسال، شماره پیگیری دریافت می‌کنید.",
            reply_markup=back_kb(),
        )
        return
    if step == "wait_ticket" and text and text != "🔙 بازگشت":
        open_ticket_from_user(chat_id, user, text)
        return

    # پاسخ کاربر به تیکت (حالت)
    if step and step.startswith("user_reply_ticket:"):
        tr = step.split(":", 1)[1]
        t = get_ticket_by_tracking(tr)
        if not t or t["status"] != "open":
            send_message(chat_id, "این تیکت بسته است.", reply_markup=main_kb())
            clear_state(uid)
            return
        add_ticket_msg(t["id"], False, text)
        uname = f"@{user.get('username')}" if user.get("username") else "—"
        admin_text = (
            f"💬 ادامه تیکت `{tr}`\n"
            f"از: {user.get('first_name')} | {uname} | `{uid}`\n\n{text}"
        )
        ikb = inline(
            [
                [
                    {"text": "💬 پاسخ", "callback_data": f"treply:{tr}"},
                    {"text": "🗑 بستن تیکت", "callback_data": f"tclose:{tr}"},
                ]
            ]
        )
        send_message(ADMIN_ID, admin_text, reply_markup=ikb)
        send_message(chat_id, "✅ پیام شما ارسال شد.", reply_markup=main_kb())
        clear_state(uid)
        return

    if step and step.startswith("admin_reply_ticket:"):
        tr = step.split(":", 1)[1]
        ok, _ = admin_reply_ticket(tr, text)
        if ok:
            send_message(chat_id, "✅ پاسخ ارسال شد.")
        else:
            send_message(chat_id, "تیکت باز نیست.")
        clear_state(uid)
        return

    # خرید پلن
    if step == "buy_plan":
        mapping = {
            "۱ ماهه — ۷ هزار": ("1m", 7),
            "۳ ماهه — ۱۲ هزار": ("3m", 12),
            "۱ ساله — ۸۰ هزار": ("1y", 80),
        }
        if text in mapping:
            plan_invoice(chat_id, user, mapping[text][0], mapping[text][1])
            return

    # امضا
    if step == "sig_channel" and text:
        st["data"]["sig_channel"] = text.strip()
        st["step"] = "sig_can_edit"
        kb = {
            "keyboard": [[{"text": "✅ بله"}, {"text": "❌ خیر"}], [{"text": "🔙 بازگشت"}]],
            "resize_keyboard": True,
        }
        send_message(
            chat_id,
            "آیا ربات اجازه *ویرایش پیام* در کانال دارد؟\n"
            "(اگر نه، امضا به‌صورت ریپلای زیر پست ارسال می‌شود)",
            reply_markup=kb,
        )
        return
    if step == "sig_can_edit":
        if text in ("✅ بله", "❌ خیر"):
            st["data"]["can_edit"] = 1 if text == "✅ بله" else 0
            st["step"] = "sig_text"
            send_message(chat_id, "متن امضا را بفرستید:", reply_markup=back_kb())
        return
    if step == "sig_text" and text:
        ch = st["data"].get("sig_channel")
        can_edit = st["data"].get("can_edit", 0)
        sub = get_sub(uid)
        if sub:
            db.execute(
                "UPDATE star_subs SET channel_id=?, signature_text=?, can_edit=? WHERE user_id=?",
                (ch, text, can_edit, uid),
            )
        else:
            db.execute(
                "INSERT INTO star_subs (user_id, plan, expires_at, channel_id, signature_text, can_edit) VALUES (?,?,?,?,?,?)",
                (uid, "manual", (datetime.utcnow() + timedelta(days=30)).isoformat(), ch, text, can_edit),
            )
        send_message(
            chat_id,
            f"✅ امضا ذخیره شد.\n📢 کانال: {ch}\n\nاز این به بعد زیر پست‌های این کانال اعمال می‌شود.",
            reply_markup=main_kb(),
        )
        clear_state(uid)
        return
    if step == "sig_menu":
        if text == "✏️ ویرایش متن امضا":
            st["step"] = "sig_text"
            send_message(chat_id, "متن جدید امضا را بفرستید:", reply_markup=back_kb())
            return
        if text == "📢 تغییر کانال":
            st["step"] = "sig_channel"
            send_message(chat_id, "آیدی کانال جدید را بفرستید:", reply_markup=back_kb())
            return

    # ساخت پست — محتوا
    if step == "wait_content":
        content = extract_content_from_message(msg)
        if not content.get("text") and not content.get("file_id"):
            send_message(chat_id, "محتوای معتبر نفرستادید. دوباره تلاش کنید.")
            return
        st["data"]["content"] = content
        after_content(chat_id, uid)
        return

    if step == "choose_btn_mode":
        if text == "⌨️ ساخت دکمه دستوری":
            st["step"] = "wait_cmd_buttons"
            send_message(
                chat_id,
                "دکمه‌ها را با این فرمت بفرستید:\n\n"
                "کانال ما - https://...\n"
                "ربات ما - https://...\n\n"
                "در یک ردیف چند دکمه: با && جدا کنید\n"
                "مثال:\n"
                "کانال - https://a.com && ربات - https://b.com\n"
                "سایت - https://c.com",
                reply_markup=back_kb(),
            )
            return
        if text == "➕ با دکمه":
            st["step"] = "plus_menu"
            st["data"]["buttons"] = []
            st["data"]["current_row"] = []
            send_message(
                chat_id,
                "حالت ➕ فعال شد.\n"
                "• ➕ → دکمه در همان ردیف.\n"
                "• ➕ ردیف پایین → ردیف جدید.\n"
                "• ✅ تمام → پایان",
                reply_markup=plus_menu_markup(),
            )
            send_message(chat_id, "برای انصراف: 🔙 بازگشت", reply_markup=back_kb())
            return


    if step == "wait_cmd_buttons" and text:
        rows = parse_command_buttons(text)
        if not rows:
            send_message(chat_id, "فرمت نادرست است. دوباره بفرستید.")
            return
        st["data"]["buttons"] = rows
        # پیش‌نمایش
        st["step"] = "preview"
        prev = format_preview(st["data"]["content"], rows)
        kb = {
            "keyboard": [
                [{"text": "✅ درست است"}, {"text": "✏️ نیاز به اصلاح دارد"}],
                [{"text": "🔙 بازگشت"}],
            ],
            "resize_keyboard": True,
        }
        send_message(chat_id, f"👁 *پیش‌نمایش*\n\n{prev}", reply_markup=kb)
        send_post_payload(chat_id, {"content": st["data"]["content"], "buttons": rows})
        return

    if step == "plus_menu":
        if text == "➕":
            st["step"] = "plus_text"
            st["data"]["_row_mode"] = "same"
            send_message(chat_id, "متن دکمه را بفرستید:", reply_markup=back_kb())
            return
        if text == "➕ ردیف پایین":
            st["step"] = "plus_text"
            st["data"]["_row_mode"] = "new"
            send_message(chat_id, "متن دکمه (ردیف جدید) را بفرستید:", reply_markup=back_kb())
            return
        if text == "✅ تمام":
            # بستن ردیف جاری
            if st["data"].get("current_row"):
                st["data"].setdefault("buttons", []).append(st["data"]["current_row"])
                st["data"]["current_row"] = []
            st["step"] = "preview"
            prev = format_preview(st["data"]["content"], st["data"].get("buttons") or [])
            kb = {
                "keyboard": [
                    [{"text": "✅ درست است"}, {"text": "✏️ نیاز به اصلاح دارد"}],
                    [{"text": "🔙 بازگشت"}],
                ],
                "resize_keyboard": True,
            }
            send_message(chat_id, f"👁 *پیش‌نمایش*\n\n{prev}", reply_markup=kb)
            send_post_payload(
                chat_id,
                {"content": st["data"]["content"], "buttons": st["data"].get("buttons") or []},
            )
            return

    if step == "plus_text" and text:
        st["data"]["_btn_text"] = text
        st["step"] = "plus_url"
        send_message(chat_id, "لینک دکمه را بفرستید:", reply_markup=back_kb())
        return
    if step == "plus_url" and text:
        btn = {"text": st["data"].get("_btn_text", "دکمه"), "url": normalize_url(text)}
        if st["data"].get("_row_mode") == "new":
            if st["data"].get("current_row"):
                st["data"].setdefault("buttons", []).append(st["data"]["current_row"])
            st["data"]["current_row"] = [btn]
        else:
            st["data"].setdefault("current_row", []).append(btn)
        st["step"] = "plus_menu"
        send_message(chat_id, "✅ دکمه اضافه شد.", reply_markup=plus_menu_markup())
        return

    if step == "preview":
        if text == "✅ درست است":
            ask_channel(chat_id, uid)
            return
        if text == "✏️ نیاز به اصلاح دارد":
            start_create_post(chat_id, uid)
            return

    if step == "wait_channel" and text:
        ch = text.strip()
        # چک ادمین بودن ربات و کاربر
        me = api("getMe")
        bot_id = (me.get("result") or {}).get("id")
        cm_bot = get_chat_member(ch, bot_id) if bot_id else {}
        cm_user = get_chat_member(ch, uid)
        def is_admin(cm):
            stt = ((cm.get("result") or {}).get("status") or "")
            return stt in ("administrator", "creator")
        if not is_admin(cm_bot):
            send_message(
                chat_id,
                f"❌ ربات در این کانال ادمین نیست.\n"
                f"مدیران ← افزودن مدیر ← `{BOT_USERNAME}` را اضافه کنید.",
            )
            return
        if not is_admin(cm_user):
            send_message(chat_id, "❌ شما ادمین این کانال نیستید.")
            return
        st["data"]["channel_id"] = ch
        ask_send_method(chat_id, uid)
        return

    if step == "ask_include_sig":
        if text == "✅ بله، امضا اضافه شود":
            st["data"]["include_sig"] = True
        elif text == "❌ نه، بدون امضا":
            st["data"]["include_sig"] = False
        else:
            return
        do_direct_send(chat_id, uid)
        return

    # پیش‌فرض
    if is_private and not step:
        send_message(
            chat_id,
            "از دکمه‌های پایین استفاده کنید 👇",
            reply_markup=main_kb(),
        )


def process_callback(cq):
    cq_id = cq.get("id")
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    user = cq.get("from") or {}
    uid = user.get("id")
    answer_callback(cq_id)

    # انتخاب نوع دکمه
    if data.startswith("btnmode:"):
        mode = data.split(":", 1)[1]
        st = get_state(uid)
        if mode == "cmd":
            st["step"] = "wait_cmd_buttons"
            send_message(
                chat_id,
                "دکمه‌ها را با این فرمت بفرستید:\n\n"
                "کانال ما - https://...\n"
                "ربات ما - https://...\n\n"
                "در یک ردیف چند دکمه: با && جدا کنید\n"
                "مثال:\n"
                "کانال - https://a.com && ربات - https://b.com\n"
                "سایت - https://c.com",
                reply_markup=back_kb(),
            )
        elif mode == "plus":
            st["step"] = "plus_menu"
            st["data"]["buttons"] = []
            st["data"]["current_row"] = []
            send_message(
                chat_id,
                "حالت ➕ فعال شد.\n"
                "• ➕ → دکمه در همان ردیف\n"
                "• ➕ ردیف پایین → ردیف جدید\n"
                "• ✅ تمام → پایان",
                reply_markup=plus_menu_markup(),
            )
            send_message(chat_id, "برای انصراف: 🔙 بازگشت", reply_markup=back_kb())
        elif mode == "none":
            st["data"]["buttons"] = []
            st["step"] = "preview"
            prev = format_preview(st["data"].get("content") or {}, [])
            kb = {
                "keyboard": [
                    [{"text": "✅ درست است"}, {"text": "✏️ نیاز به اصلاح دارد"}],
                    [{"text": "🔙 بازگشت"}],
                ],
                "resize_keyboard": True,
            }
            send_message(chat_id, "👁 *پیش‌نمایش*\n\n" + prev, reply_markup=kb)
            send_post_payload(chat_id, {"content": st["data"].get("content") or {}, "buttons": []})
        return

    # منوی ➕ شیشه‌ای
    if data.startswith("plus:"):
        action = data.split(":", 1)[1]
        st = get_state(uid)
        if action == "same":
            st["step"] = "plus_text"
            st["data"]["_row_mode"] = "same"
            send_message(chat_id, "متن دکمه را بفرستید:", reply_markup=back_kb())
        elif action == "new":
            st["step"] = "plus_text"
            st["data"]["_row_mode"] = "new"
            send_message(chat_id, "متن دکمه (ردیف جدید) را بفرستید:", reply_markup=back_kb())
        elif action == "done":
            if st["data"].get("current_row"):
                st["data"].setdefault("buttons", []).append(st["data"]["current_row"])
                st["data"]["current_row"] = []
            st["step"] = "preview"
            prev = format_preview(st["data"].get("content") or {}, st["data"].get("buttons") or [])
            kb = {
                "keyboard": [
                    [{"text": "✅ درست است"}, {"text": "✏️ نیاز به اصلاح دارد"}],
                    [{"text": "🔙 بازگشت"}],
                ],
                "resize_keyboard": True,
            }
            send_message(chat_id, "👁 *پیش‌نمایش*\n\n" + prev, reply_markup=kb)
            send_post_payload(
                chat_id,
                {"content": st["data"].get("content") or {}, "buttons": st["data"].get("buttons") or []},
            )
        return

    if data.startswith("treply:") and uid == ADMIN_ID:
        tr = data.split(":", 1)[1]
        st = get_state(uid)
        st["step"] = f"admin_reply_ticket:{tr}"
        send_message(chat_id, f"پاسخ برای تیکت `{tr}` را بنویسید:")
        return
    if data.startswith("tclose:") and uid == ADMIN_ID:
        tr = data.split(":", 1)[1]
        close_ticket(tr)
        t = get_ticket_by_tracking(tr)
        send_message(chat_id, f"✅ تیکت `{tr}` بسته شد.")
        if t:
            send_message(t["user_id"], f"🔒 تیکت `{tr}` بسته شد.")
        return
    if data.startswith("ureply:"):
        tr = data.split(":", 1)[1]
        st = get_state(uid)
        st["step"] = f"user_reply_ticket:{tr}"
        send_message(chat_id, "پیام خود را برای پشتیبانی بنویسید:")
        return
    if data.startswith("uclose:"):
        tr = data.split(":", 1)[1]
        t = get_ticket_by_tracking(tr)
        if t and t["user_id"] == uid:
            close_ticket(tr)
            send_message(chat_id, f"✅ تیکت `{tr}` بسته شد.")
            send_message(ADMIN_ID, f"🔒 کاربر تیکت `{tr}` را بست.")
        return


def process_channel_post(msg):
    """امضا زیر پست کانال"""
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    # پیدا کردن ساب‌هایی که این کانال را دارند
    rows = db.execute("SELECT * FROM star_subs", fetch="all") or []
    for sub in rows:
        if not sub.get("signature_text") or not sub.get("channel_id"):
            continue
        if not is_sub_active(sub["user_id"]):
            continue
        ch = str(sub["channel_id"])
        # تطبیق
        ok = False
        if str(chat_id) == ch or ch == str(chat.get("username") or ""):
            ok = True
        if ch.startswith("@") and chat.get("username") and ch[1:] == chat.get("username"):
            ok = True
        if not ok:
            continue
        sig = sub["signature_text"]
        mid = msg.get("message_id")
        # اول edit
        base = msg.get("text") or msg.get("caption") or ""
        if sub.get("can_edit") and base is not None:
            new_text = (base.rstrip() + "\n\n" + sig) if base else sig
            if msg.get("text"):
                r = edit_message(chat_id, mid, new_text)
                if r.get("ok"):
                    log.info("signature edit ok chat=%s mid=%s", chat_id, mid)
                    return
        # ریپلای
        send_message(chat_id, sig, reply_to=mid)
        log.info("signature reply chat=%s mid=%s", chat_id, mid)
        return


# ───────────────────────── حلقه اصلی + HTTP ─────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_http():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("http listening on 0.0.0.0:%s", port)
    server.serve_forever()


def main():
    db.init()
    log.info("db ready kind=%s", db.kind)
    api("deleteWebhook")
    # رد کردن آپدیت‌های قدیمی
    try:
        r = requests.post(f"{API}/getUpdates", json={"offset": -1, "timeout": 0}, timeout=10)
        data = r.json()
        if data.get("ok") and data.get("result"):
            last = data["result"][-1]["update_id"]
            requests.post(f"{API}/getUpdates", json={"offset": last + 1}, timeout=10)
    except Exception:
        pass

    t = threading.Thread(target=start_http, daemon=True)
    t.start()

    log.info("star bot polling, db=%s", "postgres" if _use_pg else DB_PATH)
    offset = 0
    while True:
        try:
            r = requests.post(
                f"{API}/getUpdates",
                json={"offset": offset, "timeout": 30},
                timeout=35,
            )
            data = r.json()
            if not data.get("ok"):
                time.sleep(2)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    if "callback_query" in upd:
                        process_callback(upd["callback_query"])
                    elif "channel_post" in upd:
                        process_channel_post(upd["channel_post"])
                    elif "message" in upd:
                        m = upd["message"]
                        # گاهی پست کانال به صورت message می‌آید
                        if (m.get("chat") or {}).get("type") == "channel":
                            process_channel_post(m)
                        else:
                            process_message(m)
                except Exception as e:
                    log.exception("update error: %s", e)
        except Exception as e:
            log.warning("loop: %s", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
