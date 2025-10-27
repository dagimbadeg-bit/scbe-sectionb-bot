import os
import json
import asyncio
import threading
import logging
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- Flask setup for Render keep-alive ---
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "✅ SCBE Section B Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# --- Logging ---
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

# --- Telegram token ---
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise ValueError("⚠️ Bot token not found! Set environment variable TOKEN.")

ADMIN_USERNAME = "@dagimbruhi"  # your username

# --- Valid student IDs ---
VALID_IDS = [
    "UGR/4985/17", "UGR/0963/17", "UGR/3796/17", "UGR/9509/17", "UGR/6315/17",
    # Add all your students here...
]

# --- JSON store for pending verifications ---
PENDING_FILE = "pending_verifications.json"

def load_pending():
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, "r") as f:
            return json.load(f)
    return {}

def save_pending(data):
    with open(PENDING_FILE, "w") as f:
        json.dump(data, f)

pending_verification = load_pending()

def normalize_id(student_id: str) -> str:
    return student_id.replace(" ", "").replace("/", "").upper()

VALID_IDS_NORMALIZED = [normalize_id(i) for i in VALID_IDS]

# --- DM after kick ---
async def dm_after_kick(context, user_id, reason):
    await asyncio.sleep(2)
    try:
        if reason == "invalid":
            msg = (
                "❌ Your ID was invalid and you’ve been removed from the SCBE Section B group.\n"
                f"If this was a mistake, please contact {ADMIN_USERNAME}."
            )
        else:
            msg = (
                "⏰ You didn’t verify within the required time and were removed from the SCBE Section B group.\n"
                f"If you’d like to rejoin, contact {ADMIN_USERNAME}."
            )
        await context.bot.send_message(chat_id=user_id, text=msg)
        logging.info(f"✅ DM sent to {user_id}")
    except Exception as e:
        logging.warning(f"⚠️ Could not DM {user_id}: {e}")

# --- Auto-remove unverified users ---
async def auto_remove_unverified(user_id, chat_id, context):
    await asyncio.sleep(86400)  # 24hrs
    if str(user_id) in pending_verification:
        try:
            await context.bot.ban_chat_member(chat_id, int(user_id))
            await context.bot.unban_chat_member(chat_id, int(user_id))
            asyncio.create_task(dm_after_kick(context, user_id, "timeout"))
            del pending_verification[str(user_id)]
            save_pending(pending_verification)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🚫 <a href='tg://user?id={user_id}'>User</a> was removed for not verifying.",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.warning(f"Failed to auto-remove {user_id}: {e}")

# --- Verify newcomers ---
async def verify_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        user_id = member.id
        chat_id = update.effective_chat.id

        pending_verification[str(user_id)] = chat_id
        save_pending(pending_verification)

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"👋 Welcome {member.full_name}!\nPlease reply with your **student ID** within 10 minutes to stay in the group."
        )

        try:
            await update.message.delete()
        except:
            pass

        asyncio.create_task(auto_remove_unverified(user_id, chat_id, context))

# --- Handle messages (for verification) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    chat_id = update.effective_chat.id
    text = update.message.text.strip().upper()

    if user_id in pending_verification:
        normalized = normalize_id(text)
        if normalized in VALID_IDS_NORMALIZED:
            await update.message.reply_text(
                f"✅ {update.message.from_user.full_name}, your ID has been verified! Welcome 🎉"
            )
            del pending_verification[user_id]
            save_pending(pending_verification)
        else:
            await update.message.reply_text("❌ Invalid ID. You will be removed.")
            try:
                await context.bot.ban_chat_member(chat_id, int(user_id))
                await context.bot.unban_chat_member(chat_id, int(user_id))
                asyncio.create_task(dm_after_kick(context, int(user_id), "invalid"))
            except Exception as e:
                logging.warning(f"Error removing invalid user {user_id}: {e}")
            del pending_verification[user_id]
            save_pending(pending_verification)

# --- Member left cleanup ---
async def member_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass

# --- Verify all (for old members) ---
async def verify_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    admins = [a.user.id for a in await context.bot.get_chat_administrators(chat.id)]

    if user.id not in admins:
        await update.message.reply_text("⚠️ Only admins can use this command.")
        return

    await update.message.reply_text(
        "🚨 Verification alert! Submit your student ID within 24 hours to stay in the group."

    )

    recent_users = set()

    # Scan recent messages to find active users (Telegram API limits to recent 100 msgs)
    async for msg in context.bot.get_chat_history(chat.id, limit=100):
        if msg.from_user and not msg.from_user.is_bot:
            recent_users.add(msg.from_user.id)

    for uid in recent_users:
        pending_verification[str(uid)] = chat.id
        save_pending(pending_verification)
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=f"@{uid}, please reply with your student ID to stay in the group."
            )
        except:
            pass

    await asyncio.sleep(900)  # 15 minutes
    removed = 0

    for uid, c_id in list(pending_verification.items()):
        if c_id == chat.id:
            try:
                await context.bot.ban_chat_member(chat.id, int(uid))
                await context.bot.unban_chat_member(chat.id, int(uid))
                asyncio.create_task(dm_after_kick(context, int(uid), "timeout"))
                del pending_verification[uid]
                removed += 1
            except Exception as e:
                logging.warning(f"Could not remove {uid}: {e}")

    save_pending(pending_verification)
    await context.bot.send_message(
        chat_id=chat.id,
        text=f"✅ Re-verification complete. {removed} inactive members removed."
    )

# --- Main entry ---
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("verify_all", verify_all))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, verify_new_member))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, member_left))
    logging.info("🚀 Bot running — verifying newcomers and rechecking old members.")
    app.run_polling()

# --- Start + Help commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! I’m the SCBE Section B bot.\n"
        "I verify members by their student ID.\n"
        "Admins can use /verify_all to recheck everyone."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - greet the bot\n/help - show commands\n/verify_all - recheck all active members"
    )

if __name__ == "__main__":
    main()
