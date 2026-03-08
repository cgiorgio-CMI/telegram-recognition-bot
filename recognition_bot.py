from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import sqlite3
import datetime
import re

TOKEN = "8745044757:AAGmObzW1reBx82IAQR2_pgMFe2y2ofbySA"

conn = sqlite3.connect("recognition.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("CREATE TABLE IF NOT EXISTS points(username TEXT PRIMARY KEY, score INTEGER)")
cursor.execute("CREATE TABLE IF NOT EXISTS daily(giver TEXT, date TEXT, count INTEGER, PRIMARY KEY(giver,date))")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):

    cursor.execute("SELECT username, score FROM points ORDER BY score DESC LIMIT 10")
    rows = cursor.fetchall()

    text = "🏆 Leaderboard\n\n"

    for i,row in enumerate(rows,start=1):
        text += f"{i}. @{row[0]} — {row[1]} points\n"

    await update.message.reply_text(text)


async def mypoints(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.message.from_user.username

    cursor.execute("SELECT score FROM points WHERE username=?", (user,))
    row = cursor.fetchone()

    score = row[0] if row else 0

    await update.message.reply_text(f"⭐ @{user} you have {score} points.")


async def recognize(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    if "👏" not in text:
        return

    giver = update.message.from_user.username

    match = re.search(r'@(\w+)', text)
    if not match:
        return

    receiver = match.group(1)

    if giver == receiver:
        await update.message.reply_text("❌ You cannot give recognition to yourself.")
        return

    today = str(datetime.date.today())

    cursor.execute("SELECT count FROM daily WHERE giver=? AND date=?", (giver,today))
    result = cursor.fetchone()

    if result and result[0] >= 5:
        await update.message.reply_text("⚠️ You already used your 5 recognitions today.")
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

    await update.message.reply_text(f"👏 @{receiver} now has {score} points!")


app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("leaderboard", leaderboard))
app.add_handler(CommandHandler("mypoints", mypoints))
app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), recognize))

app.run_polling()