from telegram import Bot
import os

TOKEN = "7927494300:AAGie3-OytbDajmuw7ZcPhZqLNmoKvvpHy8"
bot = Bot(TOKEN)
bot.delete_webhook(drop_pending_updates=True)
print("Old sessions cleared!")
