from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import sqlite3
import datetime
import re
import asyncio

import os
TOKEN = os.getenv("BOT_TOKEN")

conn = sqlite3.connect("recognition.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("CREATE TABLE IF NOT EXISTS points(username TEXT PRIMARY KEY, score INTEGER)")
cursor.execute("CREATE TABLE IF NOT EXISTS daily(giver TEXT, date TEXT, count INTEGER, PRIMARY KEY(giver,date))")

# Leaderboard command
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):

    cursor.execute("SELECT username, score FROM points ORDER BY score DESC LIMIT 10")
    rows = cursor.fetchall()

    text = "🏆 Leaderboard\n\n"

    for i,row in enumerate(rows,start=1):
        text += f"{i}. @{row[0]} — {row[1]} points\n"

    await update.message.reply_text(text)


# My points command
async def mypoints(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.message.from_user.username

    cursor.execute("SELECT score FROM points WHERE username=?", (user,))
    row = cursor.fetchone()

    score = row[0] if row else 0

    await update.message.reply_text(f"⭐ @{user} you have {score} points.")


# Recognition system
async def recognize(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.message
    giver = message.from_user.username

    today = str(datetime.date.today())

    # check daily limit
    cursor.execute("SELECT count FROM daily WHERE giver=? AND date=?", (giver,today))
    result = cursor.fetchone()

    if result and result[0] >= 5:
        await message.reply_text("⚠️ You already used your 5 recognitions today.")
        return

    receiver = None

    # METHOD 1: emoji + username
    if "👏" in message.text if message.text else False:

        match = re.search(r'@(\w+)', message.text)
        if match:
            receiver = match.group(1)

    # METHOD 2: reply with 👏
    if message.reply_to_message and message.text == "👏":

        if message.reply_to_message.from_user.username:
            receiver = message.reply_to_message.from_user.username

    if not receiver:
        return

    # anti cheating
    if giver == receiver:
        await message.reply_text("❌ You cannot give recognition to yourself.")
        return

    cursor.execute("SELECT score FROM points WHERE username=?", (receiver,))
    row = cursor.fetchone()

    if row:
        cursor.execute("UPDATE points SET score=score+1 WHERE username=?", (receiver,))
    else:
        cursor.execute("INSERT INTO points VALUES (?,1)", (receiver,))

    if result:
        cursor.execute("UPDATE daily SET count=count+1 WHERE giver=? AND date=?", (giver,today))
    else:
        cursor.execute("INSERT INTO daily VALUES (?,?,1)", (giver,today))

    conn.commit()

    cursor.execute("SELECT score FROM points WHERE username=?", (receiver,))
    score = cursor.fetchone()[0]

    await message.reply_text(f"👏 @{receiver} now has {score} points!")


# automatic friday leaderboard
async def friday_leaderboard(context: ContextTypes.DEFAULT_TYPE):

    cursor.execute("SELECT username, score FROM points ORDER BY score DESC LIMIT 10")
    rows = cursor.fetchall()

    text = "🏆 Weekly Recognition Leaderboard\n\n"

    for i,row in enumerate(rows,start=1):
        text += f"{i}. @{row[0]} — {row[1]} points\n"

    for chat_id in context.application.chat_data:
        await context.bot.send_message(chat_id=chat_id, text=text)


async def track_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.chat_data[update.effective_chat.id] = True


app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("leaderboard", leaderboard))
app.add_handler(CommandHandler("mypoints", mypoints))
app.add_handler(MessageHandler(filters.ALL, track_chat))
app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), recognize))

job_queue = app.job_queue
job_queue.run_daily(friday_leaderboard, time=datetime.time(hour=17), days=(4,))

app.run_polling()

