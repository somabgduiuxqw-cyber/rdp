import logging
import os
import random
import re
import secrets
import sqlite3
import string
import html
import time
from urllib.parse import urlparse

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler
)

# ATP Temp Mail Bot v1
# Required environment variables: BOT_TOKEN, OWNER_ID
# The bot must be an administrator in @atp_nst.

BOT_TOKEN = os.getenv("9209794201:AAHWmy2PTAodpkoZIYBA8eb1xCMuUdtDf-U", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "9232177140"))
CHANNELS = ("@atp_nst",)
API = "https://api.mail.tm"
DB_PATH = os.getenv("DB_PATH", "atp_temp_mail.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("atp-tempmail")

CREATE_NAME, CREATE_LIMIT, CREATE_CREDITS = range(3)
CODE_LIMITS = (1, 10, 50, 100)
CREDIT_OPTIONS = (10, 50, 100, 1000)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen INTEGER NOT NULL,
            credits INTEGER NOT NULL DEFAULT 0,
            referred_by INTEGER,
            is_blocked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS referrals (
            new_user_id INTEGER PRIMARY KEY,
            referrer_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS redeem_codes (
            code TEXT PRIMARY KEY COLLATE NOCASE,
            max_uses INTEGER NOT NULL,
            credits INTEGER NOT NULL,
            uses INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS redemptions (
            code TEXT NOT NULL COLLATE NOCASE,
            user_id INTEGER NOT NULL,
            redeemed_at INTEGER NOT NULL,
            PRIMARY KEY(code, user_id),
            FOREIGN KEY(code) REFERENCES redeem_codes(code),
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS mailboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            address TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            token TEXT NOT NULL,
            password_revealed INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        """)


def user_row(user_id):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def register_user(user, referrer_id=None):
    now = int(time.time())
    uid = user.id
    with db() as con:
        existing = con.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
        if existing:
            con.execute("UPDATE users SET username=? WHERE user_id=?",
                        (user.username or "", uid))
            return False, None

        valid_ref = None
        if referrer_id and referrer_id != uid:
            ref = con.execute("SELECT user_id FROM users WHERE user_id=? AND is_blocked=0",
                              (referrer_id,)).fetchone()
            if ref:
                valid_ref = referrer_id

        con.execute(
            "INSERT INTO users(user_id, username, first_seen, credits, referred_by) VALUES(?,?,?,?,?)",
            (uid, user.username or "", now, 0, valid_ref)
        )
        if valid_ref:
            con.execute(
                "INSERT OR IGNORE INTO referrals(new_user_id, referrer_id, created_at) VALUES(?,?,?)",
                (uid, valid_ref, now)
            )
            con.execute("UPDATE users SET credits=credits+1 WHERE user_id=?", (valid_ref,))
        return True, valid_ref


async def membership_ok(context, user_id):
    # Only @atp_nst channel join/admin check is required.
    for channel in CHANNELS:
        try:
            member = await context.bot.get_chat_member(channel, user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return False, channel
            if member.status == ChatMemberStatus.RESTRICTED and not getattr(member, "is_member", False):
                return False, channel
        except Exception as exc:
            log.warning("Membership check failed for %s / %s: %s", channel, user_id, exc)
            return None, channel
    return True, None


async def gate(update, context):
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member("@atp_nst", user_id)
        still_member = member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
        if member.status == ChatMemberStatus.RESTRICTED:
            still_member = bool(getattr(member, "is_member", False))
        if not still_member:
            with db() as con:
                con.execute("UPDATE users SET credits=0 WHERE user_id=?", (user_id,))
    except Exception as exc:
        log.warning("Could not verify @atp_nst for %s during gate: %s", user_id, exc)
    ok, channel = await membership_ok(context, user_id)
    if ok is True:
        return True
    if ok is None:
        msg = "I couldn't verify channel membership. Please try again later or contact the bot owner."
    else:
        msg = "Please join the required channel @atp_nst, then tap Verify."
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Join @atp_nst", url="https://t.me/atp_nst")],
        [InlineKeyboardButton("Verify membership", callback_data="verify_join")]
    ])
    target = update.effective_message
    if target:
        await target.reply_text(msg, reply_markup=keyboard)
    return False


async def enforce_atp_membership(update, context):
    uid = update.effective_user.id
    try:
        member = await context.bot.get_chat_member("@atp_nst", uid)
        is_member = member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
        if member.status == ChatMemberStatus.RESTRICTED:
            is_member = bool(getattr(member, "is_member", False))
        if not is_member:
            with db() as con:
                con.execute("UPDATE users SET credits=0 WHERE user_id=?", (uid,))
            return False
    except Exception as exc:
        log.warning("Could not verify @atp_nst for %s: %s", uid, exc)
        return False
    return True


def credits(user_id):
    row = user_row(user_id)
    return int(row["credits"]) if row else 0


def spend_credit(user_id, amount):
    with db() as con:
        cur = con.execute(
            "UPDATE users SET credits=credits-? WHERE user_id=? AND credits>=?",
            (amount, user_id, amount)
        )
        return cur.rowcount == 1


def randstr(n=14):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def mail_request(method, url, **kwargs):
    kwargs.setdefault("timeout", 25)
    response = requests.request(method, url, **kwargs)
    response.raise_for_status()
    return response


def create_mailbox(user_id, reveal_password):
    domains_data = mail_request("GET", f"{API}/domains").json()
    domains = domains_data if isinstance(domains_data, list) else domains_data.get("hydra:member", [])
    domain = next((x.get("domain") for x in domains
                   if isinstance(x, dict) and x.get("isActive", True) and x.get("domain")), None)
    if not domain:
        raise RuntimeError("Mail.tm did not return an active email domain.")

    for _ in range(5):
        address = f"{randstr()}@{domain}"
        password = secrets.token_urlsafe(18)
        payload = {"address": address, "password": password}
        response = requests.post(f"{API}/accounts", json=payload, timeout=25)
        if response.status_code in (200, 201):
            break
        if response.status_code == 422:
            continue
        response.raise_for_status()
    else:
        raise RuntimeError("Could not create a unique mailbox. Please try again.")

    token_data = mail_request("POST", f"{API}/token", json=payload).json()
    token = token_data.get("token")
    if not token:
        raise RuntimeError("Mail.tm did not return an authentication token.")

    with db() as con:
        con.execute(
            """INSERT INTO mailboxes(user_id,address,password,token,password_revealed,created_at)
               VALUES(?,?,?,?,?,?)""",
            (user_id, address, password, token, int(reveal_password), int(time.time()))
        )
    return address, password


def current_mailbox(user_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM mailboxes WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,)
        ).fetchone()


def get_messages(mailbox):
    headers = {"Authorization": f"Bearer {mailbox['token']}", "Accept": "application/json"}
    data = mail_request("GET", f"{API}/messages", headers=headers).json()
    return data if isinstance(data, list) else data.get("hydra:member", [])


def full_message(mailbox, message_id):
    headers = {"Authorization": f"Bearer {mailbox['token']}", "Accept": "application/json"}
    return mail_request("GET", f"{API}/messages/{message_id}", headers=headers).json()


def clean_body(message):
    text_body = message.get("text") or ""
    raw_html = message.get("html") or ""
    if isinstance(raw_html, list):
        raw_html = "\n".join(raw_html)
    body = text_body or raw_html or message.get("intro") or "No message content."
    body = html.unescape(body)
    body = re.sub(r"<br\s*/?>|</p>|</div>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", "", body)
    return body.strip()[:3500]


def find_otp(text):
    patterns = [
        r"(?:otp|code|verification|verify|pin)\s*(?:is|:|-)?\s*([0-9]{4,8})\b",
        r"\b([0-9]{6})\b",
        r"\b([0-9]{4,8})\b"
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match and not re.fullmatch(r"20\d{2}", match.group(1)):
            return match.group(1)
    return None


def home_keyboard(owner=False):
    rows = [
        [InlineKeyboardButton("📧 Create normal email (1 credit)", callback_data="new_normal")],
        [InlineKeyboardButton("🔐 Create email with password (3 credits)", callback_data="new_private")],
        [InlineKeyboardButton("📥 Inbox", callback_data="inbox"),
         InlineKeyboardButton("📨 Read email", callback_data="read_list")],
        [InlineKeyboardButton("🎁 Redeem code", callback_data="redeem")],
        [InlineKeyboardButton("👥 My referrals", callback_data="referrals"),
         InlineKeyboardButton("💳 My credits", callback_data="balance")]
    ]
    if owner:
        rows.append([InlineKeyboardButton("🛠 Create redeem code", callback_data="owner_create")])
        rows.append([InlineKeyboardButton("📊 Owner stats", callback_data="owner_stats")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = context.args[0] if context.args else None
    referrer = None
    if payload and payload.isdigit():
        referrer = int(payload)
    register_user(update.effective_user, referrer)
    if not await gate(update, context):
        return
    if not await enforce_atp_membership(update, context):
        await update.effective_message.reply_text(
            "Your @atp_nst membership could not be verified. Your credits have been cleared if you left the channel. Join again and contact the owner if you think this is an error."
        )
        return
    owner = update.effective_user.id == OWNER_ID
    await update.effective_message.reply_text(
        f"Welcome to ATP Temp Mail Bot v1!\n\nCredits: {credits(update.effective_user.id)}\n1 credit = 1 normal email. 3 credits = 1 email with its password.",
        reply_markup=home_keyboard(owner)
    )


async def guarded_callback(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if q.data == "verify_join":
        ok, _ = await membership_ok(context, uid)
        if ok is True and await enforce_atp_membership(update, context):
            await q.message.reply_text("Membership verified.", reply_markup=home_keyboard(uid == OWNER_ID))
        else:
            await q.message.reply_text("Membership is not verified yet. Join @atp_nst and try again.")
        return
    if not await gate(update, context):
        return
    if not await enforce_atp_membership(update, context):
        await q.message.reply_text("Access is paused because @atp_nst membership is not verified. Your credits have been cleared.")
        return

    data = q.data
    if data == "balance":
        await q.message.reply_text(f"Your balance: {credits(uid)} credits.")
    elif data == "referrals":
        with db() as con:
            count = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start={uid}"
        await q.message.reply_text(
            f"Successful referrals: {count}\nCredits: {credits(uid)}\n\nYour referral link:\n{link}"
        )
    elif data in ("new_normal", "new_private"):
        cost = 1 if data == "new_normal" else 3
        if credits(uid) < cost:
            await q.message.reply_text(f"You need {cost} credits. Your balance is {credits(uid)}.")
            return
        if not spend_credit(uid, cost):
            await q.message.reply_text("Insufficient credits.")
            return
        try:
            address, password = create_mailbox(uid, data == "new_private")
            text = f"📧 Email created:\n{address}"
            if data == "new_private":
                text += f"\n\n🔑 Password:\n{password}"
            else:
                text += "\n\nPassword is hidden for this mailbox."
            text += f"\n\nRemaining credits: {credits(uid)}"
            await q.message.reply_text(text)
        except Exception as exc:
            with db() as con:
                con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (cost, uid))
            log.exception("Mailbox creation failed")
            await q.message.reply_text(f"Mailbox creation failed; your {cost} credit(s) were refunded.\n{exc}")
    elif data == "inbox":
        mailbox = current_mailbox(uid)
        if not mailbox:
            await q.message.reply_text("You have no mailbox yet. Create one first.")
            return
        try:
            messages = get_messages(mailbox)
            if not messages:
                await q.message.reply_text(f"📭 Inbox is empty for {mailbox['address']}.")
                return
            lines = [f"📥 Inbox: {mailbox['address']}"]
            for i, m in enumerate(messages[:20], 1):
                sender = m.get("from") or {}
                sender = sender.get("address", "Unknown") if isinstance(sender, dict) else str(sender)
                lines.append(f"\n{i}. {m.get('subject') or '(No subject)'}\nFrom: {sender}")
            await q.message.reply_text("\n".join(lines))
        except Exception as exc:
            await q.message.reply_text(f"Could not load inbox: {exc}")
    elif data == "read_list":
        mailbox = current_mailbox(uid)
        if not mailbox:
            await q.message.reply_text("Create a mailbox first.")
            return
        try:
            messages = get_messages(mailbox)
            if not messages:
                await q.message.reply_text("Inbox is empty.")
                return
            context.user_data["mail_ids"] = [m.get("id") for m in messages[:10]]
            buttons = []
            for i, m in enumerate(messages[:10]):
                title = (m.get("subject") or "(No subject)")[:45]
                buttons.append([InlineKeyboardButton(f"{i+1}. {title}", callback_data=f"read_{i}")])
            await q.message.reply_text("Choose an email:", reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as exc:
            await q.message.reply_text(f"Could not load messages: {exc}")
    elif data.startswith("read_") and data[5:].isdigit():
        ids = context.user_data.get("mail_ids", [])
        index = int(data[5:])
        if index >= len(ids):
            await q.message.reply_text("That message list expired. Open Inbox again.")
            return
        mailbox = current_mailbox(uid)
        try:
            msg = full_message(mailbox, ids[index])
            sender = msg.get("from") or {}
            sender = sender.get("address", "Unknown") if isinstance(sender, dict) else str(sender)
            body = clean_body(msg)
            otp = find_otp(body)
            result = f"📨 {msg.get('subject') or '(No subject)'}\nFrom: {sender}\n"
            if otp:
                result += f"\n🔑 Possible OTP: {otp}\n"
            result += f"\n{body}"
            await q.message.reply_text(result[:4000])
        except Exception as exc:
            await q.message.reply_text(f"Could not read email: {exc}")
    elif data == "redeem":
        context.user_data["await_redeem"] = True
        await q.message.reply_text("Send the redeem code now:")
    elif data == "owner_create":
        if uid != OWNER_ID:
            await q.message.reply_text("Owner only.")
            return
        await q.message.reply_text("Send a unique redeem code name (example: free100).")
        return CREATE_NAME
    elif data == "owner_stats":
        if uid != OWNER_ID:
            return
        with db() as con:
            users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            refs = con.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
            codes = con.execute("SELECT COUNT(*) FROM redeem_codes").fetchone()[0]
            boxes = con.execute("SELECT COUNT(*) FROM mailboxes").fetchone()[0]
        await q.message.reply_text(
            f"Owner statistics\nUsers: {users}\nSuccessful referrals: {refs}\nRedeem codes: {codes}\nMailboxes: {boxes}"
        )
    return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await gate(update, context):
        return
    if not await enforce_atp_membership(update, context):
        await update.effective_message.reply_text("Access is paused because @atp_nst membership is not verified. Your credits have been cleared.")
        return
    if context.user_data.pop("await_redeem", False):
        code = update.message.text.strip()
        with db() as con:
            row = con.execute(
                "SELECT * FROM redeem_codes WHERE code=? COLLATE NOCASE AND active=1",
                (code,)
            ).fetchone()
            if not row:
                await update.message.reply_text("Invalid or inactive redeem code.")
                return
            already = con.execute(
                "SELECT 1 FROM redemptions WHERE code=? COLLATE NOCASE AND user_id=?",
                (code, uid)
            ).fetchone()
            if already:
                await update.message.reply_text("You have already redeemed this code. Each code can only be used once per user.")
                return
            if row["uses"] >= row["max_uses"]:
                con.execute("UPDATE redeem_codes SET active=0 WHERE code=? COLLATE NOCASE", (code,))
                await update.message.reply_text("This redeem code has reached its usage limit.")
                return
            con.execute("INSERT INTO redemptions(code,user_id,redeemed_at) VALUES(?,?,?)",
                        (row["code"], uid, int(time.time())))
            con.execute("UPDATE redeem_codes SET uses=uses+1 WHERE code=? COLLATE NOCASE", (row["code"],))
            con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (row["credits"], uid))
            new_balance = con.execute("SELECT credits FROM users WHERE user_id=?", (uid,)).fetchone()["credits"]
            con.execute("UPDATE redeem_codes SET active=0 WHERE code=? COLLATE NOCASE AND uses>=max_uses", (row["code"],))
        await update.message.reply_text(f"Redeemed successfully! You received {row['credits']} credits.\nBalance: {new_balance}")
    else:
        await update.message.reply_text("Use the menu buttons below.", reply_markup=home_keyboard(uid == OWNER_ID))


async def create_code_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return ConversationHandler.END
    name = update.message.text.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", name):
        await update.message.reply_text("Use 3–32 characters: letters, numbers, _ or -.")
        return CREATE_NAME
    with db() as con:
        exists = con.execute("SELECT 1 FROM redeem_codes WHERE code=? COLLATE NOCASE", (name,)).fetchone()
    if exists:
        await update.message.reply_text("That code already exists. Send a different name.")
        return CREATE_NAME
    context.user_data["new_code"] = name
    buttons = [[InlineKeyboardButton(str(n), callback_data=f"limit_{n}") for n in CODE_LIMITS]]
    await update.message.reply_text("How many users can redeem this code?", reply_markup=InlineKeyboardMarkup(buttons))
    return CREATE_LIMIT


async def create_code_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID:
        return ConversationHandler.END
    if not q.data.startswith("limit_"):
        return CREATE_LIMIT
    limit = int(q.data.split("_", 1)[1])
    if limit not in CODE_LIMITS:
        return CREATE_LIMIT
    context.user_data["new_code_limit"] = limit
    buttons = [[InlineKeyboardButton(str(n), callback_data=f"amount_{n}") for n in CREDIT_OPTIONS]]
    await q.message.reply_text("How many credits should each user receive?", reply_markup=InlineKeyboardMarkup(buttons))
    return CREATE_CREDITS


async def create_code_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID:
        return ConversationHandler.END
    amount = int(q.data.split("_", 1)[1])
    if amount not in CREDIT_OPTIONS:
        return CREATE_CREDITS
    code = context.user_data.get("new_code")
    limit = context.user_data.get("new_code_limit")
    with db() as con:
        con.execute(
            "INSERT INTO redeem_codes(code,max_uses,credits,created_by,created_at) VALUES(?,?,?,?,?)",
            (code, limit, amount, OWNER_ID, int(time.time()))
        )
    context.user_data.pop("new_code", None)
    context.user_data.pop("new_code_limit", None)
    await q.message.reply_text(
        f"Redeem code created!\n\nCode: {code}\nMaximum users: {limit}\nCredits per user: {amount}\n\nShare this code with users."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def post_init(app):
    await app.bot.set_my_commands([
        ("start", "Start the bot"),
        ("cancel", "Cancel the current action")
    ])


def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN environment variable.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    create_code_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(guarded_callback, pattern="^owner_create$")],
        states={
            CREATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_code_name)],
            CREATE_LIMIT: [CallbackQueryHandler(create_code_limit, pattern="^limit_")],
            CREATE_CREDITS: [CallbackQueryHandler(create_code_amount, pattern="^amount_")]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(create_code_conv)
    app.add_handler(CallbackQueryHandler(guarded_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    log.info("ATP Temp Mail Bot v1 is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
