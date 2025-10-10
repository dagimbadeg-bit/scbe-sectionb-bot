import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = "7927494300:AAGie3-OytbDajmuw7ZcPhZqLNmoKvvpHy8"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I’m the SCBE Section B bot 👋\nI welcome newcomers. Type /help for commands."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start - say hi\n/help - show commands")

async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Delete Telegram's default join message
    try:
        await update.message.delete()
    except Exception as e:
        print(f"Failed to delete join message: {e}")

    # Send custom welcome messages directly to chat
    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            name_to_use = member.username or member.full_name
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"🎉 Hey @{name_to_use}! Welcome to SCBE Section B! 🚀\n\n"
                    "This is your space to meet classmates, share ideas, and stay updated on everything Section B. \n"
                    "Feel free to introduce yourself! 😄"
                )
            )
            # Auto-delete the welcome message after 60 seconds
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

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
