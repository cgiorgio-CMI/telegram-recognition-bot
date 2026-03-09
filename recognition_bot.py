import logging
import sqlite3
from datetime import datetime
import schedule
import threading
import time

from telegram.ext import Application, CommandHandler, MessageHandler, filters

import gspread
from oauth2client.service_account import ServiceAccountCredentials


# -----------------------
# SETTINGS
# -----------------------

BOT_TOKEN = "8745044757:AAGmObzW1reBx82IAQR2_pgMFe2y2ofbySA"
ADMINS = ["cassg13", "Kennedy", "cass"]

MAX_DAILY_RECOGNITIONS = 5


# -----------------------
# GOOGLE SHEETS
# -----------------------

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

import os
import json
from oauth2client.service_account import ServiceAccountCredentials

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
CREATE TABLE IF NOT EXISTS recognitions(
id INTEGER PRIMARY KEY AUTOINCREMENT,
sender TEXT,
receiver TEXT,
date TEXT
)
""")

conn.commit()


# -----------------------
# DAILY LIMIT CHECK
# -----------------------

def daily_count(user):

    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
        "SELECT COUNT(*) FROM recognitions WHERE sender=? AND date=?",
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

# -----------------------
# RECOGNITION BY COMMAND
# -----------------------

async def recognize(update, context):

    sender_user = update.message.from_user
    sender_id = sender_user.id
    sender = sender_user.username if sender_user.username else sender_user.first_name

    # ----- METHOD 1: Reply recognition -----
    if update.message.reply_to_message:

        receiver_user = update.message.reply_to_message.from_user
        receiver_id = receiver_user.id
        receiver = receiver_user.username if receiver_user.username else receiver_user.first_name

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

        # Prevent multiple mentions
        if context.args[0].count("@") > 1:
            await update.message.reply_text(
                "❌ Please recognize only one person at a time."
            )
            return

        receiver = context.args[0].replace("@", "")
        message = " ".join(context.args[1:])

        if receiver.lower() == sender.lower():
            await update.message.reply_text("❌ You cannot recognize yourself.")
            return

        # Verify the user exists in the group
        try:
            member = await context.bot.get_chat_member(update.effective_chat.id, f"@{receiver}")
        except:
            await update.message.reply_text(
                "❌ That user is not a member of this group."
            )
            return

    # Daily recognition limit
    if daily_count(sender) >= MAX_DAILY_RECOGNITIONS:
        await update.message.reply_text(
            "Daily recognition limit reached (5)."
        )
        return

    add_point(receiver)

    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
        "INSERT INTO recognitions(sender,receiver,date) VALUES(?,?,?)",
        (sender, receiver, today)
    )

    conn.commit()

    recognitions_sheet.append_row([
        today,
        sender,
        receiver,
        message,
        1
    ])

    await update.message.reply_text(
        f"👏 {receiver} received recognition!"
    )


# -----------------------
# RECOGNITION VIA 👏 REACTION
# -----------------------

async def reaction_recognition(update, context):

    if not update.message:
        return

    if not update.message.reply_to_message:
        return

    if "👏" not in update.message.text:
        return

    sender_user = update.message.from_user
    receiver_user = update.message.reply_to_message.from_user

    sender = sender_user.username if sender_user.username else sender_user.first_name
    receiver = receiver_user.username if receiver_user.username else receiver_user.first_name

    if sender == receiver:
        return

    if daily_count(sender) >= MAX_DAILY_RECOGNITIONS:
        return

    add_point(receiver)

    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
        "INSERT INTO recognitions(sender,receiver,date) VALUES(?,?,?)",
        (sender, receiver, today)
    )

    conn.commit()

    recognitions_sheet.append_row([
        today,
        sender,
        receiver,
        "👏 reaction",
        1
    ])

    await update.message.reply_text(
        f"👏 {receiver} received recognition!"
    )
# -----------------------
# LEADERBOARD
# -----------------------

async def leaderboard(update, context):

    cursor.execute(
        "SELECT user,points FROM points ORDER BY points DESC LIMIT 10"
    )

    rows = cursor.fetchall()

    text = "🏆 Leaderboard\n\n"

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

    user = update.message.from_user.username

    if not context.args:
        await update.message.reply_text(
            "Usage: /redeem reward_number"
        )
        return

    reward_id = int(context.args[0])

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

    user = update.message.from_user.username

    if user not in ADMINS:
        return

    cursor.execute("SELECT SUM(points) FROM points")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM points")
    users = cursor.fetchone()[0]

    await update.message.reply_text(
        f"📊 Stats\nTotal points: {total}\nUsers: {users}"
    )


# -----------------------
# ADMIN ADD REWARD
# -----------------------

async def addreward(update, context):

    user = update.message.from_user.username

    if user not in ADMINS:
        return

    text = " ".join(context.args)

    if "|" not in text:
        await update.message.reply_text(
            "Usage: /addreward Reward | Points"
        )
        return

    name, cost = text.split("|")

    cursor.execute(
        "INSERT INTO rewards(name,cost) VALUES (?,?)",
        (name.strip(), int(cost))
    )

    conn.commit()

    await update.message.reply_text("Reward added")


# -----------------------
# ADMIN REMOVE REWARD
# -----------------------

async def removereward(update, context):

    user = update.message.from_user.username

    if user not in ADMINS:
        return

    reward_id = int(context.args[0])

    cursor.execute(
        "DELETE FROM rewards WHERE id=?",
        (reward_id,)
    )

    conn.commit()

    await update.message.reply_text("Reward removed")


# -----------------------
# WEEKLY RESET
# -----------------------

def weekly_reset():

    cursor.execute("DELETE FROM recognitions")
    conn.commit()

    print("Weekly recognition reset complete")


# -----------------------
# AUTO LEADERBOARD
# -----------------------

def friday_leaderboard(app):

    cursor.execute(
        "SELECT user,points FROM points ORDER BY points DESC LIMIT 10"
    )

    rows = cursor.fetchall()

    text = "🏆 Weekly Leaderboard\n\n"

    for i, r in enumerate(rows, 1):
        text += f"{i}. {r[0]} — {r[1]} pts\n"

    print("Friday leaderboard generated")


# -----------------------
# SCHEDULER
# -----------------------

def run_scheduler(app):

    schedule.every().friday.at("17:00").do(friday_leaderboard, app)
    schedule.every().monday.at("00:01").do(weekly_reset)

    while True:
        schedule.run_pending()
        time.sleep(60)


# -----------------------
# MAIN
# -----------------------

def main():

    logging.basicConfig(level=logging.INFO)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("recognize", recognize))
    app.add_handler(CommandHandler("mypoints", mypoints))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("rewards", rewards))
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(CommandHandler("results", results))
    app.add_handler(CommandHandler("addreward", addreward))
    app.add_handler(CommandHandler("removereward", removereward))

    app.add_handler(MessageHandler(filters.TEXT, reaction_recognition))

    scheduler_thread = threading.Thread(
        target=run_scheduler,
        args=(app,),
        daemon=True
    )

    scheduler_thread.start()

    print("Bot running...")

    app.run_polling()


if __name__ == "__main__":
    main()

