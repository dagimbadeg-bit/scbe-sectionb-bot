from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = "7927494300:AAGie3-OytbDajmuw7ZcPhZqLNmoKvvpHy8"  # Your bot token

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I’m the SCBE Section B bot 👋\nI welcome newcomers. Type /help for commands."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - say hi\n/help - show commands"
    )

async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            name_to_use = member.username or member.full_name
            await update.message.reply_text(
                f"🎉 Hey {member.full_name}! Welcome to SCBE Section B! 🚀\n\n"
                "This is your space to meet classmates, share ideas, and stay updated on everything Section B. \n"
                "Feel free to introduce yourself! 😄"
            )

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
