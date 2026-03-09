import logging
import sqlite3
from datetime import datetime, timedelta
import schedule
import threading
import time

from telegram.ext import Application, CommandHandler, MessageHandler, filters

import gspread
from oauth2client.service_account import ServiceAccountCredentials



# -----------------------
# SETTINGS
# -----------------------

import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMINS = ["cassg13", "kennedy", "cass", "sandra", "anne marie", "laura"]

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

import json


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

    cursor.execute(
        """
        INSERT INTO users(user_id, username, name)
        VALUES(?,?,?)
        ON CONFLICT(user_id)
        DO UPDATE SET
        username=excluded.username,
        name=excluded.name
        """,
        (user.id, username, name)
    )

    conn.commit()

def find_user_in_text(text):
    words = text.replace("👏", "").split()

    for word in words:

        clean = word.replace("@","").replace(",","").replace(".","").replace("!","").lower()

        if clean in STOP_WORDS:
            continue

        cursor.execute(
            """
            SELECT user_id, name, username
            FROM users
            WHERE lower(name)=?
            OR lower(username)=?
            """,
            (clean, clean)
        )

        user = cursor.fetchone()

        if user:
            return user

    return None

# -----------------------
# DAILY LIMIT CHECK
# -----------------------

def daily_count(user):

    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
        "SELECT COALESCE(SUM(points),0) FROM recognitions WHERE sender=? AND date=?",
        (user, today)
    )

    return cursor.fetchone()[0]


# -----------------------
# ADD POINT
# -----------------------

def add_point(user):

    cursor.execute(
        """
        INSERT INTO points(user,points)
        VALUES(?,1)
        ON CONFLICT(user)
        DO UPDATE SET points=points+1
        """,
        (user,)
    )

    conn.commit()

def check_milestone(user):

    cursor.execute(
        "SELECT SUM(points) FROM recognitions WHERE receiver=?",
        (user,)
    )

    row = cursor.fetchone()
    points = row[0] if row and row[0] else 0

    previous = points - 1

    for m in MILESTONES:
        if points >= m and previous < m:
            return m

    return None

# -----------------------
# RECOGNITION BY COMMAND
# -----------------------

async def recognize(update, context):

    sender_user = update.message.from_user
    register_user(sender_user)
    sender_id = sender_user.id
    sender = sender_user.username.lower() if sender_user.username else sender_user.first_name
    message_id = update.message.message_id

    cursor.execute(
        "SELECT 1 FROM recognitions WHERE message_id=?",
        (message_id,)
    )

    if cursor.fetchone():
        return

    # ----- METHOD 1: Reply recognition -----
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

    # ----- METHOD 2: @username recognition -----
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

        if context.args[0].count("@") > 1:
            await update.message.reply_text(
                "❌ Please recognize only one person at a time."
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
        await update.message.reply_text(
            "Daily recognition limit reached (5)."
        )
        return

    add_point(receiver)
    milestone = check_milestone(receiver)

    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
    """
    INSERT INTO recognitions(sender,sender_id,receiver,receiver_id,date,points,message_id)
    VALUES(?,?,?,?,?,?,?)
    """,
    (
        sender,
        sender_id,
        receiver,
        receiver_id,
        today,
        1,
        message_id
    )
    )

    conn.commit()

    try:
        recognitions_sheet.append_row([
            today,
            sender,
            receiver,
            message,
            1
        ])
    except Exception as e:
        print("Google Sheets logging failed:", e)

    if milestone:
        await update.message.reply_text(
            f"👏 {receiver} received recognition!\n\n🔥 {receiver} just reached {milestone} points!"
        )
    else:
        await update.message.reply_text(
            f"👏 {receiver} received recognition!"
        )
async def debug_message(update, context):
    if update.message and update.message.text:
        print("MESSAGE RECEIVED:", update.message.text)

# -----------------------
# RECOGNITION VIA 👏 REACTION
# -----------------------

async def reaction_recognition(update, context):

    if not update.message or not update.message.text:
        return

    text = update.message.text

    # Only continue if clap emoji exists
    if "👏" not in text:
        return

    print("Clap recognition triggered:", text)

    # Count claps
    points = max(1, min(text.count("👏"), 5))

    sender_user = update.message.from_user
    register_user(sender_user)
    sender = sender_user.username.lower() if sender_user.username else sender_user.first_name
    message_id = update.message.message_id

    # Prevent duplicate processing
    cursor.execute(
        "SELECT 1 FROM recognitions WHERE message_id=?",
        (message_id,)
    )
    if cursor.fetchone():
        return

    # Daily limit check
    if daily_count(sender) + points > MAX_DAILY_RECOGNITIONS:
        await update.message.reply_text("Daily recognition limit reached (5).")
        return

    # Determine receiver
    receiver = None
    receiver_id = None

    # METHOD 1: reply recognition (BEST METHOD)
    if update.message.reply_to_message:

        receiver_user = update.message.reply_to_message.from_user
        receiver = receiver_user.username.lower() if receiver_user.username else receiver_user.first_name
        receiver_id = receiver_user.id
        register_user(receiver_user)

    # METHOD 2: smart user detection
    else:

        user = find_user_in_text(text)

        if user:
            receiver_id = user[0]
            receiver = user[1]

        # fallback detection
        if not receiver:
            words = text.replace("👏", "").split()

            for w in words:
                clean = w.lower().strip(",.!")

                if clean in STOP_WORDS:
                    continue

                cursor.execute(
                    "SELECT user_id FROM users WHERE lower(name)=? OR lower(username)=?",
                    (clean, clean)
                )

                row = cursor.fetchone()

                if row:
                    receiver = clean
                    break

    # Prevent self recognition
    if not receiver:
        return

    if receiver_id and receiver_id == sender_user.id:
        return

    # Give points
    for _ in range(points):
        add_point(receiver)

    milestone = check_milestone(receiver)

    if milestone:
        await update.message.reply_text(
            f"👏 {receiver} received {points} recognition point(s)!\n\n🔥 {receiver} just reached {milestone} points!"
    )
    else:
        await update.message.reply_text(
            f"👏 {receiver} received {points} recognition point(s)!"
        )

    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
    """
    INSERT INTO recognitions(sender,sender_id,receiver,receiver_id,date,points,message_id)
    VALUES(?,?,?,?,?,?,?)
    """,
    (
        sender,
        sender_user.id,
        receiver,
        receiver_id,
        today,
        points,
        message_id
    )
    )

    conn.commit()

    try:
        recognitions_sheet.append_row([
            today,
            sender,
            receiver,
            f"{points} 👏",
            points
        ])
    except Exception as e:
        print("Google Sheets logging failed:", e)

# -----------------------
# LEADERBOARD
# -----------------------

async def leaderboard(update, context):

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

    await update.message.reply_text(text)


# -----------------------
# REWARDS
# -----------------------

async def rewards(update, context):

    cursor.execute("SELECT id,name,cost FROM rewards")

    rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text(
            "No rewards available."
        )
        return

    text = "🎁 Available Rewards\n\n"

    for r in rows:
        text += f"{r[0]}. {r[1]} — {r[2]} pts\n"

    text += "\nUse /redeem NUMBER"

    await update.message.reply_text(text)


# -----------------------
# REDEEM
# -----------------------

async def redeem(update, context):

    user_obj = update.message.from_user
    user = user_obj.username.lower() if user_obj.username else user_obj.first_name

    if not context.args:
        await update.message.reply_text(
            "Usage: /redeem reward_number"
        )
        return

    try:
        reward_id = int(context.args[0])
    except:
        await update.message.reply_text("Please enter a valid reward number.")
        return

    cursor.execute(
        "SELECT name,cost FROM rewards WHERE id=?",
        (reward_id,)
    )

    reward = cursor.fetchone()

    if not reward:
        await update.message.reply_text("Invalid reward")
        return

    name, cost = reward

    cursor.execute(
        "SELECT points FROM points WHERE user=?",
        (user,)
    )

    row = cursor.fetchone()

    points = row[0] if row else 0

    if points < cost:
        await update.message.reply_text(
            "Not enough points"
        )
        return

    cursor.execute(
        "UPDATE points SET points=points-? WHERE user=?",
        (cost, user)
    )

    conn.commit()

    await update.message.reply_text(
        f"🎉 You redeemed: {name}"
    )


# -----------------------
# ADMIN RESULTS
# -----------------------

async def results(update, context):

    user_obj = update.message.from_user
    user = user_obj.username.lower() if user_obj.username else user_obj.first_name

    if user not in ADMINS:
        return

    cursor.execute("SELECT SUM(points) FROM points")
    total = cursor.fetchone()[0] or 0

    cursor.execute("SELECT COUNT(*) FROM points")
    users = cursor.fetchone()[0]

    await update.message.reply_text(
        f"📊 Stats\nTotal points: {total}\nUsers: {users}"
    )


# -----------------------
# ADMIN ADD REWARD
# -----------------------

async def addreward(update, context):

    user_obj = update.message.from_user
    user = user_obj.username.lower() if user_obj.username else user_obj.first_name

    if user not in ADMINS:
        return

    text = " ".join(context.args)

    if "|" not in text:
        await update.message.reply_text(
            "Usage: /addreward Reward | Points"
        )
        return

    name, cost = text.split("|")

    try:
        cost = int(cost)
    except:
        await update.message.reply_text("Points must be a number.")
        return

    cursor.execute(
    "INSERT INTO rewards(name,cost) VALUES (?,?)",
    (name.strip(), cost)
    )

    conn.commit()

    await update.message.reply_text("Reward added")


# -----------------------
# ADMIN REMOVE REWARD
# -----------------------

async def removereward(update, context):

    user_obj = update.message.from_user
    user = user_obj.username.lower() if user_obj.username else user_obj.first_name

    if user not in ADMINS:
        return

    if not context.args:
        await update.message.reply_text("Usage: /removereward reward_id")
        return

    try:
        reward_id = int(context.args[0])
    except:
        await update.message.reply_text("Please enter a valid reward ID.")
        return

    cursor.execute(
        "DELETE FROM rewards WHERE id=?",
        (reward_id,)
    )

    conn.commit()

    await update.message.reply_text("Reward removed")




# -----------------------
# AUTO LEADERBOARD
# -----------------------

def friday_leaderboard(app):

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

    import asyncio

    app.create_task(
        app.bot.send_message(
            chat_id=-1003846532829,
            text=text
        )
    )

    print("Friday leaderboard sent")


# -----------------------
# SCHEDULER
# -----------------------

def run_scheduler(app):

    schedule.every().friday.at("17:00").do(friday_leaderboard, app)
    

    while True:
        schedule.run_pending()
        time.sleep(60)


# -----------------------
# MAIN
# -----------------------
async def mypoints(update, context):

    user_obj = update.message.from_user
    name = user_obj.username.lower() if user_obj.username else user_obj.first_name

    cursor.execute(
        "SELECT points FROM points WHERE user=?",
        (name,)
    )

    row = cursor.fetchone()
    points = row[0] if row else 0

    await update.message.reply_text(
        f"🏆 {name}, you currently have {points} recognition points!"
    )
    
async def ping(update, context):
    chat_type = update.message.chat.type
    chat_id = update.message.chat.id

    await update.message.reply_text(
        f"✅ Bot working\nChat type: {chat_type}\nChat ID: {chat_id}"
    )

async def track_user(update, context):

    user = update.message.from_user

    if "known_users" not in context.chat_data:
        context.chat_data["known_users"] = []

    for u in context.chat_data["known_users"]:
        if u["id"] == user.id:
            return

    context.chat_data["known_users"].append({
        "id": user.id,
        "name": user.first_name
    })
    
def main():

    logging.basicConfig(level=logging.INFO)

    app = Application.builder().token(BOT_TOKEN).build()

    app.bot.delete_webhook(drop_pending_updates=True)

    app.add_handler(MessageHandler(filters.ALL, debug_message), group=-1)

    app.add_handler(MessageHandler(filters.ALL, track_user), group=0)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reaction_recognition))

    app.add_handler(CommandHandler("recognize", recognize))
    app.add_handler(CommandHandler("mypoints", mypoints))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("rewards", rewards))
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(CommandHandler("results", results))
    app.add_handler(CommandHandler("addreward", addreward))
    app.add_handler(CommandHandler("removereward", removereward))
    app.add_handler(CommandHandler("ping", ping))

    scheduler_thread = threading.Thread(
        target=run_scheduler,
        args=(app,),
        daemon=True
    )

    scheduler_thread.start()

    print("Bot running...")

    async def error_handler(update, context):
        print("Error:", context.error)

    app.add_error_handler(error_handler)

    app.run_polling()


if __name__ == "__main__":
    main()
