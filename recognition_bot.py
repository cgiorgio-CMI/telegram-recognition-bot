import logging
import sqlite3
from datetime import datetime, timedelta
import asyncio
import os
import json

from telegram.ext import Application, CommandHandler, MessageHandler, filters
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import aioschedule  # make sure this is installed: pip install aioschedule

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

# -----------------------
# DATABASE UTILITIES
# -----------------------
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
    if not update.message:
        return

    sender_user = update.message.from_user
    register_user(sender_user)
    sender_id = sender_user.id
    sender = sender_user.username.lower() if sender_user.username else sender_user.first_name
    message_id = update.message.message_id
    today = datetime.now().strftime("%Y-%m-%d")

    # Prevent duplicate processing
    cursor.execute("SELECT 1 FROM recognitions WHERE message_id=?", (message_id,))
    if cursor.fetchone():
        return

    # Determine receiver
    if update.message.reply_to_message:
        receiver_user = update.message.reply_to_message.from_user
        register_user(receiver_user)
        receiver = receiver_user.username.lower() if receiver_user.username else receiver_user.first_name
        receiver_id = receiver_user.id
        if sender_id == receiver_id:
            await update.message.reply_text("❌ You cannot recognize yourself.")
            return

        if len(context.args) < 1:
            await update.message.reply_text(
                "❌ Please include a message.\n\nExample:\n/recognize Thanks for helping today!"
            )
            return

        message = " ".join(context.args)
    else:
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage:\n"
                "Reply to a message and type:\n"
                "/recognize Thanks for helping!\n\n"
                "OR\n\n"
                "/recognize @username message"
            )
            return

        receiver = context.args[0].replace("@", "")
        receiver_id = None
        message = " ".join(context.args[1:])
        if receiver.lower() == sender.lower():
            await update.message.reply_text("❌ You cannot recognize yourself.")
            return

    # Daily recognition limit
    if daily_count(sender) + 1 > MAX_DAILY_RECOGNITIONS:
        await update.message.reply_text("Daily recognition limit reached (5).")
        return

    add_point(receiver)
    milestone = check_milestone(receiver)

    # Insert into SQLite
    cursor.execute(
        """
        INSERT INTO recognitions(sender, sender_id, receiver, receiver_id, date, points, message_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (sender, sender_id, receiver, receiver_id, today, 1, message_id)
    )
    conn.commit()

    # Async Google Sheets logging
    try:
        await asyncio.to_thread(
            recognitions_sheet.append_row,
            [today, sender, receiver, message, 1]
        )
    except Exception as e:
        print("Google Sheets logging failed:", e)

    # Reply message
    if milestone:
        await update.message.reply_text(
            f"👏 {receiver} received recognition!\n\n🔥 {receiver} just reached {milestone} points!"
        )
    else:
        await update.message.reply_text(f"👏 {receiver} received recognition!")

# -----------------------
# REACTION RECOGNITION
# -----------------------
async def reaction_recognition(update, context):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    if "👏" not in text:
        return

    sender_user = update.message.from_user
    register_user(sender_user)
    sender = sender_user.username.lower() if sender_user.username else sender_user.first_name
    message_id = update.message.message_id
    today = datetime.now().strftime("%Y-%m-%d")

    # Prevent duplicate processing
    cursor.execute("SELECT 1 FROM recognitions WHERE message_id=?", (message_id,))
    if cursor.fetchone():
        return

    points = max(1, min(text.count("👏"), 5))

    # Daily limit check
    if daily_count(sender) + points > MAX_DAILY_RECOGNITIONS:
        await update.message.reply_text("Daily recognition limit reached (5).")
        return

    receivers = set()

    # Method 1: Reply recognition
    if update.message.reply_to_message:
        receiver_user = update.message.reply_to_message.from_user
        receiver = receiver_user.username.lower() if receiver_user.username else receiver_user.first_name
        receiver_id = receiver_user.id
        register_user(receiver_user)
        receivers.add((receiver_id, receiver))

    # Method 2: Smart user detection
    words = text.replace("👏", "").split()
    for w in words:
        clean = w.lower().strip("@,.!:;")
        if clean in STOP_WORDS:
            continue

        cursor.execute(
            "SELECT user_id, name FROM users WHERE lower(name)=? OR lower(username)=?",
            (clean, clean)
        )
        row = cursor.fetchone()
        if row:
            receivers.add((row[0], row[1]))

    if not receivers:
        return  # No valid receivers

    names = []
    for r_id, r_name in receivers:
        if r_id == sender_user.id:
            continue

        for _ in range(points):
            add_point(r_name)

        names.append(r_name)

        # Insert into SQLite
        cursor.execute(
            """
            INSERT INTO recognitions(sender, sender_id, receiver, receiver_id, date, points, message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sender, sender_user.id, r_name, r_id, today, points, message_id)
        )

        # Async Google Sheets logging
        try:
            await asyncio.to_thread(
                recognitions_sheet.append_row,
                [today, sender, r_name, text, points]
            )
        except Exception as e:
            print("Google Sheets logging failed:", e)

    conn.commit()

    names_text = ", ".join(names)
    await update.message.reply_text(f"👏 {names_text} received {points} recognition point(s)!")

# -----------------------
# FRIDAY LEADERBOARD
# -----------------------
async def friday_leaderboard(app):
    today = datetime.now()
    start_of_week = today - timedelta(days=today.weekday())
    start_of_week = start_of_week.strftime("%Y-%m-%d")

    cursor.execute(
        """
        SELECT receiver, SUM(points)
        FROM recognitions
        WHERE date >= ?
        GROUP BY receiver
        ORDER BY SUM(points) DESC
        LIMIT 10
        """,
        (start_of_week,)
    )
    rows = cursor.fetchall()

    text = "🏆 Weekly Leaderboard\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. {r[0]} — {r[1]} pts\n"

    try:
        await app.bot.send_message(
            chat_id=-1003846532829,  # replace with your actual chat_id
            text=text
        )
    except Exception as e:
        print("Failed to send leaderboard:", e)

    print("Friday leaderboard sent")

# -----------------------
# SCHEDULER LOOP
# -----------------------
async def scheduler_loop(app):
    aioschedule.every().friday.at("17:00").do(lambda: asyncio.create_task(friday_leaderboard(app)))

    while True:
        await aioschedule.run_pending()
        await asyncio.sleep(60)

# -----------------------
# MAIN
# -----------------------
def main():
    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("recognize", recognize))
    app.add_handler(CommandHandler("mypoints", mypoints))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reaction_recognition))

    # Debug
    async def debug_message(update, context):
        if update.message and update.message.text:
            print("MESSAGE RECEIVED:", update.message.text)
    app.add_handler(MessageHandler(filters.ALL, debug_message), group=-1)

    # Start scheduler after app initialization
    async def start_tasks():
        asyncio.create_task(scheduler_loop(app))
    app.post_init = start_tasks

    # Run the bot
    print("Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
