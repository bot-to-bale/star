#!/usr/bin/env python3
"""ربات استار — سازنده دکمه شیشه‌ای بله. اجرا با long polling روی سرور مجازی."""

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["BALE_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "1369192211"))
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent / "data" / "bot.db"))
API = f"https://tapi.bale.ai/bot{TOKEN}"

PLANS = {
    "1m": {"days": 30, "amount": 7000, "title": "یک‌ماهه"},
    "3m": {"days": 90, "amount": 12000, "title": "سه‌ماهه"},
    "1y": {"days": 365, "amount": 80000, "title": "یک‌ساله"},
}
PLAN_ALIASES = {
    "1m": "1m", "۱m": "1m", "یکماهه": "1m", "یک‌ماهه": "1m", "یکماه": "1m",
    "3m": "3m", "۳m": "3m", "سه‌ماهه": "3m", "سهماهه": "3m", "سه‌ماه": "3m",
    "1y": "1y", "۱y": "1y", "یکساله": "1y", "یک‌ساله": "1y", "یکسال": "1y",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("star")

MEDIA_FIELDS = (
    ("photo", "photo", True),
    ("video", "video", True),
    ("animation", "animation", True),
    ("document", "document", True),
    ("audio", "audio", True),
    ("voice", "voice", False),
    ("video_note", "video_note", False),
)
SEND_METHOD = {
    "text": ("sendMessage", "text"),
    "photo": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "animation": ("sendAnimation", "animation"),
    "document": ("sendDocument", "document"),
    "audio": ("sendAudio", "audio"),
    "voice": ("sendVoice", "voice"),
    "video_note": ("sendVideoNote", "video_note"),
}


def utcnow():
    return datetime.now(timezone.utc)


def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def connect():
    conn = db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS states (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                caption TEXT,
                buttons TEXT NOT NULL,
                code TEXT UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subs (
                user_id INTEGER PRIMARY KEY,
                plan TEXT,
                active INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                channel_id TEXT,
                channel_username TEXT,
                signature TEXT,
                can_edit INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )


def get_state(user_id):
    with connect() as conn:
        row = conn.execute("SELECT state, data FROM states WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return "idle", {}
    return row["state"], json.loads(row["data"] or "{}")


def set_state(user_id, state, data=None):
    payload = json.dumps(data or {}, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            "INSERT INTO states(user_id, state, data) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, data=excluded.data",
            (user_id, state, payload),
        )


def meta_get(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def api(method, **payload):
    try:
        r = requests.post(f"{API}/{method}", json=payload, timeout=40)
        data = r.json()
    except Exception as exc:
        log.exception("api %s failed", method)
        return {"ok": False, "description": str(exc)}
    if not data.get("ok"):
        log.warning("api %s: %s", method, data)
    return data


def send(chat_id, text, reply_markup=None):
    body = {"chat_id": chat_id, "text": text}
    if reply_markup:
        body["reply_markup"] = reply_markup
    return api("sendMessage", **body)


def answer_cb(callback_id, text=None):
    body = {"callback_query_id": callback_id}
    if text:
        body["text"] = text
    return api("answerCallbackQuery", **body)


KB_MAIN = {
    "keyboard": [[{"text": "ساخت پست"}], [{"text": "راهنما"}, {"text": "امضا زیر پیام"}]],
    "resize_keyboard": True,
}
KB_BACK = {"keyboard": [[{"text": "بازگشت"}]], "resize_keyboard": True}
KB_TYPE = {
    "keyboard": [[{"text": "ساخت دکمه دستوری"}, {"text": "با دکمه"}], [{"text": "بازگشت"}]],
    "resize_keyboard": True,
}
KB_DONE = {"keyboard": [[{"text": "تمام"}], [{"text": "بازگشت"}]], "resize_keyboard": True}
KB_YESNO = {"keyboard": [[{"text": "بله"}, {"text": "خیر"}], [{"text": "بازگشت"}]], "resize_keyboard": True}


def kb_preview():
    return {
        "inline_keyboard": [[
            {"text": "درست است", "callback_data": "pok"},
            {"text": "نیاز به اصلاح دارد", "callback_data": "pedit"},
        ]]
    }


def kb_send_method():
    return {
        "inline_keyboard": [
            [{"text": "ارسال مستقیم در کانال", "callback_data": "sdir"}],
            [{"text": "ارسال با دستور", "callback_data": "scmd"}],
        ]
    }


def kb_plans():
    return {
        "inline_keyboard": [
            [{"text": "یک‌ماهه ۷ هزار", "callback_data": "sub1"}],
            [{"text": "سه‌ماهه ۱۲ هزار", "callback_data": "sub3"}],
            [{"text": "یک‌ساله ۸۰ هزار", "callback_data": "suby"}],
        ]
    }


def kb_sig_manage():
    return {
        "inline_keyboard": [
            [{"text": "ویرایش متن امضا", "callback_data": "esig"}],
            [{"text": "تغییر آیدی کانال", "callback_data": "echan"}],
        ]
    }


def url_keyboard(buttons):
    return {"inline_keyboard": buttons} if buttons else None


def plus_keyboard(buttons):
    if not buttons:
        return {"inline_keyboard": [[{"text": "➕", "callback_data": "pnew"}]]}
    rows = []
    for i, row in enumerate(buttons):
        built = [{"text": b["text"], "url": b["url"]} for b in row]
        if i == len(buttons) - 1:
            built.append({"text": "➕", "callback_data": "psame"})
        rows.append(built)
    rows.append([{"text": "➕", "callback_data": "pnew"}])
    return {"inline_keyboard": rows}


def extract_content(message):
    caption = message.get("caption")
    if message.get("text"):
        return {"kind": "text", "payload": message["text"], "caption": None}
    for field, kind, has_caption in MEDIA_FIELDS:
        if field not in message:
            continue
        media = message[field]
        file_id = media[-1]["file_id"] if field == "photo" else media["file_id"]
        return {"kind": kind, "payload": file_id, "caption": caption if has_caption else None}
    return None


def deliver(chat_id, draft, buttons=None):
    kind = draft["kind"]
    method, field = SEND_METHOD[kind]
    body = {"chat_id": chat_id, field: draft["payload"]}
    if kind != "text" and draft.get("caption"):
        body["caption"] = draft["caption"]
    if buttons:
        body["reply_markup"] = url_keyboard(buttons)
    return api(method, **body)


def show_post(chat_id, draft, markup):
    return deliver(chat_id, draft, None if markup is None else markup.get("inline_keyboard"))


def parse_command_buttons(text):
    rows = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        row = []
        for part in line.split("&&"):
            part = part.strip()
            if " - " in part:
                name, url = part.split(" - ", 1)
            elif "-" in part:
                name, url = part.split("-", 1)
            else:
                continue
            name, url = name.strip(), url.strip()
            if name and (url.startswith("http://") or url.startswith("https://")):
                row.append({"text": name, "url": url})
        if row:
            rows.append(row)
    return rows


def parse_chat_ref(text):
    text = (text or "").strip()
    if text.startswith("https://ble.ir/") or text.startswith("http://ble.ir/"):
        slug = text.split("ble.ir/", 1)[1].split("/", 1)[0].strip()
        if slug:
            return "@" + slug if not slug.startswith("@") and not slug.lstrip("-").isdigit() else slug
    if text.startswith("@"):
        return text
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return None


def member_status(chat_id, user_id):
    res = api("getChatMember", chat_id=chat_id, user_id=user_id)
    if not res.get("ok"):
        return None
    return res["result"].get("status")


def is_admin_status(status):
    return status in ("creator", "administrator")


_bot_id = None


def bot_id():
    global _bot_id
    if _bot_id is None:
        me = api("getMe")
        if not me.get("ok"):
            return None
        _bot_id = me["result"]["id"]
    return _bot_id


def resolve_chat(ref):
    res = api("getChat", chat_id=ref)
    if not res.get("ok"):
        return None
    chat = res["result"]
    return {"id": chat["id"], "username": chat.get("username"), "title": chat.get("title") or chat.get("username")}


def post_link(chat, message_id):
    username = chat.get("username")
    if username:
        return f"https://ble.ir/{username}/{chat['id']}/{message_id}"
    return f"شناسه پیام: {message_id}"


def active_sub(user_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM subs WHERE user_id=?", (user_id,)).fetchone()
    if not row or not row["active"] or not row["expires_at"]:
        return None
    expires = datetime.fromisoformat(row["expires_at"])
    if expires <= utcnow():
        with connect() as conn:
            conn.execute("UPDATE subs SET active=0 WHERE user_id=?", (user_id,))
        return None
    return row


def save_sub_channel(user_id, chat, signature, can_edit, plan=None, expires=None):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO subs(user_id, plan, active, expires_at, channel_id, channel_username, signature, can_edit)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                plan=COALESCE(excluded.plan, subs.plan),
                active=COALESCE(excluded.active, subs.active),
                expires_at=COALESCE(excluded.expires_at, subs.expires_at),
                channel_id=excluded.channel_id,
                channel_username=excluded.channel_username,
                signature=COALESCE(excluded.signature, subs.signature),
                can_edit=excluded.can_edit
            """,
            (
                user_id,
                plan,
                1 if expires else None,
                expires,
                str(chat["id"]) if chat else None,
                chat.get("username") if chat else None,
                signature,
                1 if can_edit else 0,
            ),
        )


def go_main(chat_id, user_id, text="به منوی اصلی برگشتید."):
    set_state(user_id, "idle", {})
    send(chat_id, text, KB_MAIN)


HELP = (
    "راهنمای ربات استار\n\n"
    "ساخت پست:\n"
    "۱. مطلب را بفرستید (متن، عکس، فیلم یا فایل).\n"
    "۲. ساخت دکمه دستوری: هر خط یک ردیف است. دکمه‌های یک ردیف را با && جدا کنید.\n"
    "مثال:\n"
    "کانال ما - https://ble.ir/mychannel && ربات ما - https://ble.ir/mybot\n"
    "بله - https://ble.ir/bale\n"
    "نتیجه:\n"
    "دکمه | دکمه\n"
    "دکمه\n\n"
    "با دکمه: زیر مطلب ➕ می‌آید. ➕ همان ردیف، دکمه را کنار قبلی می‌گذارد. "
    "➕ ردیف پایین، ردیف تازه می‌سازد. در پایان «تمام» را بزنید.\n\n"
    "بعد از تأیید، ربات را ادمین کانال کنید و آیدی کانال را بفرستید "
    "(@username یا عدد).\n"
    "ارسال مستقیم همان لحظه در کانال منتشر می‌شود.\n"
    "ارسال با دستور یک کد می‌دهد؛ هر کس در چتی که ربات عضو است "
    "/start کد را بفرستد، همان پست ارسال می‌شود.\n\n"
    "امضا زیر پیام: با اشتراک فعال، متن شما زیر پست‌های یک کانال چسبانده می‌شود. "
    "فقط یک کانال قابل فعال‌سازی است."
)


def start_create(chat_id, user_id):
    set_state(user_id, "waiting_content", {})
    send(chat_id, "لطفاً مطلب خودتون رو بفرستید؛ متن، عکس، فیلم یا فایل.", KB_BACK)


def after_content(chat_id, user_id, draft):
    set_state(user_id, "choose_type", {"draft": draft, "buttons": []})
    send(chat_id, "مطلب دریافت شد. نوع دکمه‌ها را انتخاب کنید.", KB_TYPE)


def begin_command(chat_id, user_id):
    state, data = get_state(user_id)
    set_state(user_id, "waiting_command", data)
    send(
        chat_id,
        "دستور دکمه‌ها را بفرستید.\n"
        "هر خط یک ردیف است و دکمه‌های یک ردیف با && جدا می‌شوند.\n\n"
        "کانال ما - https://ble.ir/channel && ربات ما - https://ble.ir/bot\n"
        "بله - https://ble.ir/bale",
        KB_BACK,
    )


def begin_plus(chat_id, user_id):
    state, data = get_state(user_id)
    data["buttons"] = []
    set_state(user_id, "plus", data)
    send(chat_id, "برای افزودن دکمه روی ➕ بزنید. وقتی تمام شد «تمام» را بزنید.", KB_DONE)
    show_post(chat_id, data["draft"], plus_keyboard([]))


def refresh_plus(chat_id, user_id, data):
    set_state(user_id, "plus", data)
    show_post(chat_id, data["draft"], plus_keyboard(data.get("buttons") or []))
    send(chat_id, "دکمه اضافه شد. ادامه دهید یا «تمام» را بزنید.", KB_DONE)


def show_preview(chat_id, user_id, data):
    set_state(user_id, "preview", data)
    send(chat_id, "پیش‌نمایش:")
    show_post(chat_id, data["draft"], url_keyboard(data.get("buttons") or []))
    send(chat_id, "این پست درست است؟", kb_preview())


def ask_channel(chat_id, user_id, data):
    set_state(user_id, "waiting_channel", data)
    send(
        chat_id,
        "ربات را در کانال یا گروه مورد نظر ادمین کنید و خودتان هم ادمین باشید.\n"
        "سپس آیدی را بفرستید؛ هم @username و هم عدد قبول است.",
        KB_BACK,
    )


def save_code_draft(user_id, data):
    code = secrets.token_hex(4)
    draft = data["draft"]
    with connect() as conn:
        conn.execute(
            "INSERT INTO drafts(user_id, kind, payload, caption, buttons, code, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                user_id,
                draft["kind"],
                draft["payload"],
                draft.get("caption"),
                json.dumps(data.get("buttons") or [], ensure_ascii=False),
                code,
                utcnow().isoformat(),
            ),
        )
    return code


def draft_by_code(code):
    with connect() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE code=?", (code,)).fetchone()
    if not row:
        return None
    return {
        "kind": row["kind"],
        "payload": row["payload"],
        "caption": row["caption"],
        "buttons": json.loads(row["buttons"] or "[]"),
    }


def publish_saved(chat_id, saved):
    return deliver(chat_id, saved, saved["buttons"])


def handle_start_code(chat_id, message_id, arg):
    saved = draft_by_code(arg)
    if not saved:
        return False
    api("deleteMessage", chat_id=chat_id, message_id=message_id)
    publish_saved(chat_id, saved)
    return True


def verify_and_choose_method(chat_id, user_id, ref, data):
    chat = resolve_chat(ref)
    if not chat:
        send(chat_id, "این آیدی پیدا نشد. ربات باید عضو و ادمین باشد. دوباره بفرستید.")
        return
    bid = bot_id()
    if not bid or not is_admin_status(member_status(chat["id"], bid)):
        send(chat_id, "ربات در این کانال یا گروه ادمین نیست. ادمین کنید و آیدی را دوباره بفرستید.")
        return
    if not is_admin_status(member_status(chat["id"], user_id)):
        send(chat_id, "شما در این کانال یا گروه ادمین نیستید.")
        return
    data["target"] = chat
    set_state(user_id, "choose_send", data)
    send(chat_id, "دسترسی تأیید شد. روش ارسال را انتخاب کنید.", kb_send_method())


def do_direct(chat_id, user_id, data):
    target = data["target"]
    result = deliver(target["id"], data["draft"], data.get("buttons") or [])
    if not result.get("ok"):
        send(chat_id, "ارسال نشد. دسترسی ارسال پیام ربات را بررسی کنید و دوباره تلاش کنید.", KB_MAIN)
        set_state(user_id, "choose_send", data)
        return
    mid = result["result"]["message_id"]
    send(chat_id, "پست در کانال ارسال شد.\n" + post_link(target, mid), KB_MAIN)
    set_state(user_id, "idle", {})


def do_command(chat_id, user_id, data):
    code = save_code_draft(user_id, data)
    send(
        chat_id,
        "کد پست ساخته شد. این دستور را در گروه یا کانالی که ربات در آن عضو است بفرستید:\n"
        f"/start {code}\n\n"
        "پیام دستور در صورت داشتن مجوز حذف می‌شود و خود پست ارسال می‌گردد.",
        KB_MAIN,
    )
    set_state(user_id, "idle", {})


def payment_text(user_id, plan_key):
    plan = PLANS[plan_key]
    return (
        f"درخواست فعال‌سازی اشتراک {plan['title']}\n"
        f"آیدی عددی: {user_id}\n"
        f"مبلغ: {plan['amount']} تومان\n\n"
        f"این پیام را برای @fazellrahimii فوروارد کنید و پاکت هدیه "
        f"{plan['amount']} تومانی را هم ارسال کنید تا اشتراک فعال شود."
    )


def activate_plan(admin_chat, target_id, plan_key):
    plan = PLANS[plan_key]
    expires = (utcnow() + timedelta(days=plan["days"])).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO subs(user_id, plan, active, expires_at, can_edit)
            VALUES(?,?,1,?,0)
            ON CONFLICT(user_id) DO UPDATE SET
                plan=excluded.plan, active=1, expires_at=excluded.expires_at
            """,
            (target_id, plan_key, expires),
        )
    send(admin_chat, f"اشتراک {plan['title']} برای {target_id} فعال شد.")
    sent = send(
        target_id,
        f"اشتراک {plan['title']} برای شما فعال شد.\n"
        "از منو «امضا زیر پیام» را بزنید، آیدی کانال، مجوز ویرایش و متن امضا را وارد کنید.",
        KB_MAIN,
    )
    if not sent.get("ok"):
        send(admin_chat, "به کاربر پیام نرسید؛ احتمالاً هنوز ربات را استارت نکرده است.")


def begin_signature(chat_id, user_id):
    sub = active_sub(user_id)
    if not sub:
        send(chat_id, "برای چسباندن خودکار امضا زیر پست کانال، یک اشتراک انتخاب کنید.", kb_plans())
        return
    extra = ""
    if sub["channel_id"]:
        extra = f"\nکانال فعلی: {sub['channel_username'] or sub['channel_id']}\nمتن: {sub['signature'] or '—'}"
    send(chat_id, f"اشتراک {PLANS.get(sub['plan'], {}).get('title', sub['plan'])} فعال است.{extra}", kb_sig_manage())


def ask_sig_channel(chat_id, user_id, data):
    set_state(user_id, "sig_channel", data)
    send(chat_id, "آیدی تنها کانالی که امضا باید در آن فعال شود را بفرستید. @ یا عدد.", KB_BACK)


def apply_signature(post):
    chat = post.get("chat") or {}
    chat_id = str(chat.get("id"))
    text = post.get("text")
    caption = post.get("caption")
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM subs WHERE active=1 AND channel_id=? AND can_edit=1 AND signature IS NOT NULL",
            (chat_id,),
        ).fetchone()
    if not row:
        return
    expires = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
    if not expires or expires <= utcnow():
        return
    signature = row["signature"].strip()
    if not signature:
        return
    if text is not None:
        if text.endswith(signature):
            return
        new_text = (text + "\n\n" + signature).strip()
        api("editMessageText", chat_id=chat["id"], message_id=post["message_id"], text=new_text)
    elif caption is not None or any(k in post for k in ("photo", "video", "animation", "document", "audio")):
        base = caption or ""
        if base.endswith(signature):
            return
        new_caption = (base + "\n\n" + signature).strip()
        api("editMessageCaption", chat_id=chat["id"], message_id=post["message_id"], caption=new_caption)


def on_text_flow(chat_id, user_id, text, message):
    state, data = get_state(user_id)

    if text == "بازگشت":
        if state in ("waiting_btn_text", "waiting_btn_url"):
            data.pop("temp_text", None)
            data.pop("add_mode", None)
            set_state(user_id, "plus", data)
            send(chat_id, "افزودن دکمه لغو شد.", KB_DONE)
            return
        go_main(chat_id, user_id)
        return

    if text == "ساخت پست":
        start_create(chat_id, user_id)
        return
    if text == "راهنما":
        send(chat_id, HELP, KB_MAIN)
        return
    if text == "امضا زیر پیام":
        begin_signature(chat_id, user_id)
        return
    if text == "ساخت دکمه دستوری" and state == "choose_type":
        begin_command(chat_id, user_id)
        return
    if text == "با دکمه" and state == "choose_type":
        begin_plus(chat_id, user_id)
        return
    if text == "تمام" and state == "plus":
        if not data.get("buttons"):
            send(chat_id, "حداقل یک دکمه اضافه کنید یا بازگشت را بزنید.")
            return
        show_preview(chat_id, user_id, data)
        return

    if state == "waiting_content":
        draft = extract_content(message)
        if not draft:
            send(chat_id, "این نوع پیام پشتیبانی نمی‌شود. متن، عکس، فیلم یا فایل بفرستید.")
            return
        after_content(chat_id, user_id, draft)
        return

    if state == "waiting_command":
        buttons = parse_command_buttons(text)
        if not buttons:
            send(chat_id, "فرمت درست نیست. نمونه:\nکانال ما - https://ble.ir/a && ربات ما - https://ble.ir/b")
            return
        data["buttons"] = buttons
        show_preview(chat_id, user_id, data)
        return

    if state == "waiting_btn_text":
        data["temp_text"] = text.strip()
        set_state(user_id, "waiting_btn_url", data)
        send(chat_id, "لینک دکمه را بفرستید. باید با https:// شروع شود.", KB_BACK)
        return

    if state == "waiting_btn_url":
        url = text.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            send(chat_id, "لینک نامعتبر است. دوباره بفرستید.")
            return
        btn = {"text": data.pop("temp_text"), "url": url}
        mode = data.pop("add_mode", "new")
        buttons = data.setdefault("buttons", [])
        if mode == "same" and buttons:
            buttons[-1].append(btn)
        else:
            buttons.append([btn])
        refresh_plus(chat_id, user_id, data)
        return

    if state == "waiting_channel":
        ref = parse_chat_ref(text)
        if ref is None:
            send(chat_id, "آیدی نامعتبر است. @username یا عدد بفرستید.")
            return
        verify_and_choose_method(chat_id, user_id, ref, data)
        return

    if state == "sig_channel":
        ref = parse_chat_ref(text)
        if ref is None:
            send(chat_id, "آیدی نامعتبر است.")
            return
        chat = resolve_chat(ref)
        if not chat:
            send(chat_id, "کانال پیدا نشد. ربات باید عضو آن باشد.")
            return
        bid = bot_id()
        if not bid or not is_admin_status(member_status(chat["id"], bid)):
            send(chat_id, "ربات در این کانال ادمین نیست.")
            return
        if not is_admin_status(member_status(chat["id"], user_id)):
            send(chat_id, "شما ادمین این کانال نیستید.")
            return
        data["sig_chat"] = chat
        set_state(user_id, "sig_edit", data)
        send(chat_id, "ربات اجازه ویرایش پیام‌های این کانال را دارد؟", KB_YESNO)
        return

    if state == "sig_edit":
        if text not in ("بله", "خیر"):
            send(chat_id, "فقط «بله» یا «خیر» را بفرستید.")
            return
        data["can_edit"] = text == "بله"
        set_state(user_id, "sig_text", data)
        send(chat_id, "متن امضا را بفرستید. این متن زیر پست‌های کانال چسبانده می‌شود.", KB_BACK)
        return

    if state == "sig_text":
        chat = data.get("sig_chat")
        if not chat:
            go_main(chat_id, user_id, "تنظیم امضا ناتمام ماند. از نو شروع کنید.")
            return
        with connect() as conn:
            conn.execute(
                "UPDATE subs SET channel_id=?, channel_username=?, signature=?, can_edit=? WHERE user_id=?",
                (str(chat["id"]), chat.get("username"), text.strip(), 1 if data.get("can_edit") else 0, user_id),
            )
        note = "" if data.get("can_edit") else "\nچون مجوز ویرایش ندارید، امضا خودکار اعمال نمی‌شود."
        go_main(chat_id, user_id, "امضا برای همین یک کانال فعال شد." + note)
        return

    if state == "sig_text_only":
        with connect() as conn:
            conn.execute("UPDATE subs SET signature=? WHERE user_id=?", (text.strip(), user_id))
        go_main(chat_id, user_id, "متن امضا به‌روز شد.")
        return

    if state == "idle":
        send(chat_id, "از دکمه‌های پایین استفاده کنید.", KB_MAIN)


def on_callback(cq):
    user_id = cq["from"]["id"]
    chat_id = cq["message"]["chat"]["id"]
    data_cb = cq.get("data") or ""
    answer_cb(cq["id"])
    state, data = get_state(user_id)

    if data_cb in ("psame", "pnew") and state == "plus":
        data["add_mode"] = "same" if data_cb == "psame" else "new"
        set_state(user_id, "waiting_btn_text", data)
        send(chat_id, "متن دکمه را بفرستید.", KB_BACK)
        return
    if data_cb == "pok" and state == "preview":
        ask_channel(chat_id, user_id, data)
        return
    if data_cb == "pedit":
        if data.get("draft"):
            data["buttons"] = []
            set_state(user_id, "choose_type", data)
            send(chat_id, "نوع دکمه‌ها را دوباره انتخاب کنید.", KB_TYPE)
        else:
            go_main(chat_id, user_id)
        return
    if data_cb == "sdir" and state == "choose_send":
        do_direct(chat_id, user_id, data)
        return
    if data_cb == "scmd" and state == "choose_send":
        do_command(chat_id, user_id, data)
        return
    if data_cb in ("sub1", "sub3", "suby"):
        plan = {"sub1": "1m", "sub3": "3m", "suby": "1y"}[data_cb]
        send(chat_id, payment_text(user_id, plan), KB_MAIN)
        return
    if data_cb == "esig":
        if not active_sub(user_id):
            send(chat_id, "اشتراک فعال ندارید.")
            return
        set_state(user_id, "sig_text_only", {})
        send(chat_id, "متن جدید امضا را بفرستید.", KB_BACK)
        return
    if data_cb == "echan":
        if not active_sub(user_id):
            send(chat_id, "اشتراک فعال ندارید.")
            return
        ask_sig_channel(chat_id, user_id, {})
        return


def on_message(message):
    if "from" not in message or message.get("from", {}).get("is_bot"):
        return
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = (message.get("text") or "").strip()

    if user_id == ADMIN_ID and text.startswith("برای "):
        match = re.search(r"برای\s+(\d+)\s+اشتراک\s+(\S+)\s+فعال\s*کن", text)
        if not match:
            send(chat_id, "فرمت: برای 123456789 اشتراک 1m فعال کن")
            return
        plan = PLAN_ALIASES.get(match.group(2))
        if not plan:
            send(chat_id, "نوع اشتراک نامعتبر است. 1m یا 3m یا 1y")
            return
        activate_plan(chat_id, int(match.group(1)), plan)
        return

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        arg = parts[1].split()[0] if len(parts) > 1 else ""
        arg = arg.split("@", 1)[0]
        if arg and handle_start_code(chat_id, message["message_id"], arg):
            return
        go_main(chat_id, user_id, "سلام. ربات استار آماده است. از دکمه‌های پایین استفاده کنید.")
        return

    if text or message.get("caption") or extract_content(message):
        on_text_flow(chat_id, user_id, text, message)


def process(update):
    try:
        if "message" in update:
            on_message(update["message"])
        elif "channel_post" in update:
            post = update["channel_post"]
            text = (post.get("text") or "").strip()
            if text.startswith("/start"):
                parts = text.split(maxsplit=1)
                arg = parts[1].split()[0] if len(parts) > 1 else ""
                if arg and handle_start_code(post["chat"]["id"], post["message_id"], arg):
                    return
            apply_signature(post)
        elif "callback_query" in update:
            on_callback(update["callback_query"])
        elif "edited_channel_post" in update:
            return
    except Exception:
        log.exception("update failed")


def main():
    init_db()
    start_http()
    api("deleteWebhook")
    offset = int(meta_get("offset", "0") or 0)
    log.info("star bot polling, db=%s", DB_PATH)
    while True:
        try:
            res = api("getUpdates", offset=offset, timeout=30)
            if not res.get("ok"):
                time.sleep(3)
                continue
            for upd in res.get("result") or []:
                offset = upd["update_id"] + 1
                meta_set("offset", offset)
                process(upd)
        except Exception:
            log.exception("poll loop")
            time.sleep(5)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def start_http():
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("http listening on 0.0.0.0:%s", port)


if __name__ == "__main__":
    main()
