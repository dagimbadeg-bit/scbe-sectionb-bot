import os
import asyncio
import threading
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- Flask setup (for Render keep-alive) ---
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "✅ Freakyfacts HQ Bot is alive!"

def run_flask():
    web_app.run(host="0.0.0.0", port=8080)

# Run Flask in a background thread
threading.Thread(target=run_flask).start()

# --- Telegram bot setup ---
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise ValueError("Bot token not found. Set it in environment variables as TOKEN.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I’m Freackyfacts HQ bot 👋\nI welcome newcomers."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start - say hi\n/help - show commands")

async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception as e:
        print(f"Failed to delete join message: {e}")

    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            name_to_use = member.username or member.full_name
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"☢️ YO @{name_to_use}! Welcome to FreakyFacts HQ! 😈💀🔥\n\n"
"Welcome to the most unstable corner of the internet - where the facts are cursed, the stories glow in the dark, and your brain might start buffering from confusion 😭🤯\n"
"Every story here is narrated in full GEN-Z chaotic mode, delivered so wild you’ll laugh😂, scream😱, and question🤔⁉️ reality at the same time 😭🔥\n"
"If you feel chills or hear random noises… don’t panic. That’s just the freaky vibes syncing with your soul 😏⚡\n\n"
"Also be a real one - add your friends, your enemies, your crush, your ex, your neighbor, literally anyone who deserves to have their brain melted too😀😉 💀🔥\n"
"Strap in legend - you’ve officially entered the danger zone😁 🚀☣️"

                )
            )
            await asyncio.sleep(60)
            try:
                await msg.delete()
            except Exception as e:
                print(f"Failed to delete welcome message: {e}")

async def member_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception as e:
        print(f"Failed to delete left message: {e}")

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, member_left))

    print("🚀 Bot is now running on Render...")
    app.run_polling()

if __name__ == "__main__":
    main()

this is my earlier code 

