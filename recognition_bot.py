import logging
import sqlite3
from datetime import datetime, timedelta
import asyncio
import os
import json

from telegram.ext import Application, CommandHandler, MessageHandler, filters
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# -----------------------
# SETTINGS
# -----------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMINS = ["cassg13", "kennedy", "cass", "sandra", "annemarie", "laura"]
MAX_DAILY_RECOGNITIONS = 5
MILESTONES = [10, 25, 50, 100, 200]
STOP_WORDS = [
    "thanks","thank","great","awesome","amazing",
    "work","job","team","today","yesterday",
    "for","the","a","to","and","everyone",
    "good","nice","help","helping","support"
]

# -----------------------
# GOOGLE SHEETS
# -----------------------
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open("Recognition Tracker")
recognitions_sheet = sheet.worksheet("Recognitions")

# -----------------------
# DATABASE
# -----------------------
conn = sqlite3.connect("recognition.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS points(
    user TEXT PRIMARY KEY,
    points INTEGER
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS rewards(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    cost INTEGER
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS recognitions (
    sender TEXT,
    sender_id INTEGER,
    receiver TEXT,
    receiver_id INTEGER,
    date TEXT,
    points INTEGER,
    message_id INTEGER UNIQUE
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT
)
""")
conn.commit()

def register_user(user):
    username = user.username.lower() if user.username else None
    name = user.first_name
    cursor.execute("""
        INSERT INTO users(user_id, username, name)
        VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username,
        name=excluded.name
    """, (user.id, username, name))
    conn.commit()

def find_user_in_text(text):
    words = text.replace("👏","").split()
    for word in words:
        clean = word.lower().strip("@,!.")
        if clean in STOP_WORDS: continue
        cursor.execute("""
            SELECT user_id, name, username
            FROM users
            WHERE lower(name)=? OR lower(username)=?
        """, (clean, clean))
        user = cursor.fetchone()
        if user:
            return user
    return None

def daily_count(user):
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute(
        "SELECT COALESCE(SUM(points),0) FROM recognitions WHERE sender=? AND date=?",
        (user, today)
    )
    return cursor.fetchone()[0]

def add_point(user):
    cursor.execute("""
        INSERT INTO points(user,points)
        VALUES(?,1)
        ON CONFLICT(user) DO UPDATE SET points=points+1
    """, (user,))
    conn.commit()

def check_milestone(user):
    cursor.execute("SELECT SUM(points) FROM recognitions WHERE receiver=?", (user,))
    row = cursor.fetchone()
    points = row[0] if row and row[0] else 0
    previous = points - 1
    for m in MILESTONES:
        if points >= m and previous < m:
            return m
    return None

# -----------------------
# COMMAND HANDLERS
# -----------------------
async def ping(update, context):
    await update.message.reply_text("✅ Bot working!")

async def mypoints(update, context):
    user_obj = update.message.from_user
    name = user_obj.username.lower() if user_obj.username else user_obj.first_name
    cursor.execute("SELECT points FROM points WHERE user=?", (name,))
    row = cursor.fetchone()
    points = row[0] if row else 0
    await update.message.reply_text(f"🏆 {name}, you have {points} points!")

# -----------------------
# RECOGNIZE COMMAND
# -----------------------
async def recognize(update, context):
    sender_user = update.message.from_user
    register_user(sender_user)
    sender_id = sender_user.id
    sender = sender_user.username.lower() if sender_user.username else sender_user.first_name
    message_id = update.message.message_id

    # Prevent duplicates
    cursor.execute("SELECT 1 FROM recognitions WHERE message_id=?", (message_id,))
    if cursor.fetchone(): return

    # Reply to a message
    if update.message.reply_to_message:
        receiver_user = update.message.reply_to_message.from_user
        register_user(receiver_user)
        receiver = receiver_user.username.lower() if receiver_user.username else receiver_user.first_name
        receiver_id = receiver_user.id
        if sender_id == receiver_id:
            await update.message.reply_text("❌ You cannot recognize yourself.")
            return
        if len(context.args) < 1:
            await update.message.reply_text("❌ Please include a message.")
            return
        message = " ".join(context.args)

    # Or @username
    else:
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /recognize @username message")
            return
        receiver = context.args[0].replace("@", "")
        receiver_id = None
        message = " ".join(context.args[1:])
        if receiver.lower() == sender.lower():
            await update.message.reply_text("❌ You cannot recognize yourself.")
            return

    # Daily limit
    if daily_count(sender) + 1 > MAX_DAILY_RECOGNITIONS:
        await update.message.reply_text("Daily limit reached.")
        return

    add_point(receiver)
    milestone = check_milestone(receiver)

    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("""
        INSERT INTO recognitions(sender,sender_id,receiver,receiver_id,date,points,message_id)
        VALUES(?,?,?,?,?,?,?)
    """, (sender, sender_id, receiver, receiver_id, today, 1, message_id))
    conn.commit()

    # Log to Google Sheets
    try:
        recognitions_sheet.append_row([today, sender, receiver, message, 1])
    except Exception as e:
        print("Google Sheets failed:", e)

    if milestone:
        await update.message.reply_text(f"👏 {receiver} received recognition! 🎉 Reached {milestone} points!")
    else:
        await update.message.reply_text(f"👏 {receiver} received recognition!")

# -----------------------
# REACTION RECOGNITION
# -----------------------
async def reaction_recognition(update, context):
    if not update.message or not update.message.text: return
    text = update.message.text
    if "👏" not in text: return

    sender_user = update.message.from_user
    register_user(sender_user)
    sender = sender_user.username.lower() if sender_user.username else sender_user.first_name
    message_id = update.message.message_id

    # Prevent duplicate
    cursor.execute("SELECT 1 FROM recognitions WHERE message_id=?", (message_id,))
    if cursor.fetchone(): return

    points = max(1, min(text.count("👏"), 5))
    receivers = set()

    # Reply recognition
    if update.message.reply_to_message:
        receiver_user = update.message.reply_to_message.from_user
        register_user(receiver_user)
        receivers.add((receiver_user.id, receiver_user.username.lower() if receiver_user.username else receiver_user.first_name))

    # Smart detection
    words = text.replace("👏","").split()
    for w in words:
        clean = w.lower().strip("@,.!:;")
        if clean in STOP_WORDS: continue
        cursor.execute("SELECT user_id,name,username FROM users WHERE lower(name)=? OR lower(username)=?", (clean, clean))
        row = cursor.fetchone()
        if row: receivers.add((row[0], row[1]))

    if not receivers: return

    names = []
    for r_id, r_name in receivers:
        if r_id == sender_user.id: continue
        for _ in range(points): add_point(r_name)
        names.append(r_name)
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute("""
            INSERT INTO recognitions(sender,sender_id,receiver,receiver_id,date,points,message_id)
            VALUES(?,?,?,?,?,?,?)
        """, (sender, sender_user.id, r_name, r_id, today, points, message_id))
        try: recognitions_sheet.append_row([today, sender, r_name, text, points])
        except: pass
    conn.commit()
    await update.message.reply_text(f"👏 {', '.join(names)} received {points} point(s)!")

# -----------------------
# ASYNC SCHEDULER (FRIDAY LEADERBOARD)
# -----------------------
async def friday_leaderboard(app):
    while True:
        now = datetime.now()
        # Calculate next Friday 17:00
        days_until_friday = (4 - now.weekday()) % 7
        target = datetime(now.year, now.month, now.day, 17, 0, 0) + timedelta(days=days_until_friday)
        sleep_seconds = (target - now).total_seconds()
        if sleep_seconds < 0: sleep_seconds += 7*24*3600  # next week
        await asyncio.sleep(sleep_seconds)

        start_of_week = datetime.now() - timedelta(days=datetime.now().weekday())
        start_of_week_str = start_of_week.strftime("%Y-%m-%d")
        cursor.execute("""
            SELECT receiver, SUM(points)
            FROM recognitions
            WHERE date >= ?
            GROUP BY receiver
            ORDER BY SUM(points) DESC
            LIMIT 10
        """, (start_of_week_str,))
        rows = cursor.fetchall()
        text = "🏆 Weekly Leaderboard\n\n"
        for i, r in enumerate(rows, 1):
            text += f"{i}. {r[0]} — {r[1]} pts\n"
        await app.bot.send_message(chat_id=-1003846532829, text=text)

# -----------------------
# MAIN
# -----------------------
def main():
    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("mypoints", mypoints))
    app.add_handler(CommandHandler("recognize", recognize))

    # Reaction recognition
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reaction_recognition))

    # Debug
    async def debug_message(update, context):
        if update.message and update.message.text:
            print("MESSAGE RECEIVED:", update.message.text)
    app.add_handler(MessageHandler(filters.ALL, debug_message), group=-1)

    # Start async scheduler
    asyncio.create_task(friday_leaderboard(app))

    print("Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
