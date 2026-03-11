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

MAX_DAILY_RECOGNITIONS = 5

MILESTONES = [10,25,50,100,200]

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

rewards_sheet = sheet.worksheet("Rewards")
redemptions_sheet = sheet.worksheet("Redemptions")
team_sheet = sheet.worksheet("Team")

# -----------------------
# DATABASE
# -----------------------

conn = sqlite3.connect("recognition.db", check_same_thread=False)

cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users(
user_id INTEGER PRIMARY KEY,
username TEXT,
name TEXT,
normalized_name TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS points(
user_id INTEGER PRIMARY KEY,
name TEXT,
points INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS recognitions(
sender_id INTEGER,
sender_name TEXT,
receiver_id INTEGER,
receiver_name TEXT,
date TEXT,
points INTEGER,
message_id INTEGER,
UNIQUE(message_id,receiver_id)
)
""")

conn.commit()

# -----------------------
# HELPERS
# -----------------------

def normalize_name(name):

    if not name:
        return None

    return name.lower().strip().replace("@","")

def register_user(user):

    username = normalize_name(user.username) if user.username else None
    name = user.first_name
    normalized = normalize_name(name)

    cursor.execute("""
    INSERT INTO users(user_id,username,name,normalized_name)
    VALUES(?,?,?,?)
    ON CONFLICT(user_id) DO UPDATE SET
    username=excluded.username,
    name=excluded.name,
    normalized_name=excluded.normalized_name
    """,(user.id,username,name,normalized))

    conn.commit()

def add_point(user_id,name,amount):

    cursor.execute("""
    INSERT INTO points(user_id,name,points)
    VALUES(?,?,?)
    ON CONFLICT(user_id) DO UPDATE SET
    points=points+?
    """,(user_id,name,amount,amount))

    conn.commit()

def daily_count(user_id):

    today=datetime.now().strftime("%Y-%m-%d")

    cursor.execute("""
    SELECT COALESCE(SUM(points),0)
    FROM recognitions
    WHERE sender_id=? AND date=?
    """,(user_id,today))

    return cursor.fetchone()[0]

# -----------------------
# NAME MATCH FIX
# -----------------------

def find_sheet_name_match(word):

    matches = []

    try:
        rows = recognitions_sheet.get_all_records()

        for r in rows:

            receiver = str(r["Receiver"]).strip()

            parts = receiver.lower().split()

            if word.lower() in parts:
                matches.append(receiver)

    except Exception as e:
        print("Sheet lookup error:", e)

    return matches

# -----------------------
# TEAM SHEET LOOKUP
# -----------------------

def find_team_name_match(word):

    matches = []

    try:
        rows = team_sheet.get_all_records()

        for r in rows:

            name = str(r["Name"]).strip()

            parts = name.lower().split()

            if word.lower() in parts:
                matches.append(name)

    except Exception as e:
        print("Team sheet lookup error:", e)

    return matches

# -----------------------
# COMMANDS
# -----------------------

async def ping(update,context):
    await update.message.reply_text("✅ Bot working!")

async def mypoints(update,context):

    user=update.message.from_user
    register_user(user)

    cursor.execute("SELECT points FROM points WHERE user_id=?",(user.id,))
    row=cursor.fetchone()

    pts=row[0] if row else 0

    await update.message.reply_text(f"🏆 {user.first_name}, you have {pts} points!")

# -----------------------
# LEADERBOARD COMMAND
# -----------------------

async def leaderboard(update,context):

    today = datetime.utcnow()

    monday = today - timedelta(days=today.weekday())

    start = monday.strftime("%Y-%m-%d")

    cursor.execute("""
    SELECT receiver_name, SUM(points)
    FROM recognitions
    WHERE date >= ?
    GROUP BY receiver_name
    ORDER BY SUM(points) DESC
    LIMIT 10
    """,(start,))

    rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("No recognitions yet this week.")
        return

    message = "🏆 Weekly Recognition Leaderboard\n\n"

    medals = ["🥇","🥈","🥉"]

    for i,(name,pts) in enumerate(rows):

        medal = medals[i] if i < len(medals) else "🏅"

        message += f"{medal} {name} — {pts} points\n"

    await update.message.reply_text(message)

# -----------------------
# AUTO LEARN USERS
# -----------------------

async def learn_users(update,context):

    if not update.message or not update.message.from_user:
        return

    register_user(update.message.from_user)

# -----------------------
# RECOGNITION ENGINE
# -----------------------

async def reaction_recognition(update,context):

    if not update.message or not update.message.text:
        return

    text=update.message.text

    if "🌱" not in text:
        return

    sender_user=update.message.from_user
    register_user(sender_user)

    sender_id=sender_user.id
    sender_name=sender_user.first_name

    today=datetime.now().strftime("%Y-%m-%d")
    message_id=update.message.message_id

    points=max(1,min(text.count("🌱"),5))

    receivers=set()
    matched_names=set()

    clean_text = text.replace("🌱","").lower()
    words = clean_text.split()

    cursor.execute("SELECT user_id,name,normalized_name FROM users")
    users = cursor.fetchall()

    for uid,name,norm in users:

        if uid != sender_id and norm in words and norm not in matched_names:

            receivers.add((uid,name))
            matched_names.add(norm)

    for w in words:

        team_matches = find_team_name_match(w)

        for team_name in team_matches:

            if w not in matched_names:

                receivers.add((0, team_name))
                matched_names.add(w)

    for w in words:

        sheet_matches = find_sheet_name_match(w)

        for sheet_name in sheet_matches:

            if w not in matched_names:

                receivers.add((0, sheet_name))
                matched_names.add(w)

    if not receivers:

        if normalize_name(sender_name) in words:
            await update.message.reply_text("🚫 You cannot recognize yourself.")

        return

    if daily_count(sender_id) + points > MAX_DAILY_RECOGNITIONS:

        await update.message.reply_text("Daily recognition limit reached (5).")
        return

    names=[]

    for r_id,r_name in receivers:

        add_point(r_id,r_name,points)

        cursor.execute("""
        INSERT OR IGNORE INTO recognitions
        (sender_id,sender_name,receiver_id,receiver_name,date,points,message_id)
        VALUES(?,?,?,?,?,?,?)
        """,(sender_id,sender_name,r_id,r_name,today,points,message_id))

        names.append(r_name)

        try:
            await asyncio.to_thread(
                recognitions_sheet.append_row,
                [today, sender_name, r_name, text, points]
            )
        except Exception as e:
            print("Sheet log error:", e)

    conn.commit()

    reply_text = f"🌱 {', '.join(names)} received {points} recognition point(s)!"

    await update.message.reply_text(reply_text)

# -----------------------
# FRIDAY LEADERBOARD
# -----------------------

async def friday_leaderboard(context):

    today = datetime.utcnow()

    monday = today - timedelta(days=today.weekday())

    start = monday.strftime("%Y-%m-%d")

    cursor.execute("""
    SELECT receiver_name, SUM(points)
    FROM recognitions
    WHERE date >= ?
    GROUP BY receiver_name
    ORDER BY SUM(points) DESC
    LIMIT 3
    """,(start,))

    rows = cursor.fetchall()

    if not rows:
        return

    message = "🏆 Friday Recognition Leaderboard\n\n"

    medals = ["🥇","🥈","🥉"]

    for i,(name,pts) in enumerate(rows):

        medal = medals[i] if i < len(medals) else "🏅"

        message += f"{medal} {name} — {pts} points\n"

    message += "\nAmazing work team! 🌱"

    try:
        await context.bot.send_message(
            chat_id=os.environ["GROUP_CHAT_ID"],
            text=message
        )
    except Exception as e:
        print("Leaderboard send error:", e)

# -----------------------
# REWARDS
# -----------------------

def get_rewards():

    rows = rewards_sheet.get_all_records()

    rewards = []

    for r in rows:

        if str(r["Active"]).lower() == "true":

            rewards.append({
                "id": int(r["ID"]),
                "name": r["Reward"],
                "cost": int(r["Cost"])
            })

    return rewards

def get_user_points(user_id):

    cursor.execute("SELECT points FROM points WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else 0

def deduct_points(user_id, cost):

    cursor.execute("""
    UPDATE points
    SET points = points - ?
    WHERE user_id = ?
    """, (cost, user_id))

    conn.commit()

async def rewards(update,context):

    rewards_list = get_rewards()

    if not rewards_list:
        await update.message.reply_text("No rewards available.")
        return

    text = "🎁 Available Rewards\n\n"

    for r in rewards_list:
        text += f"{r['id']}. {r['name']} — {r['cost']} pts\n"

    text += "\nUse /redeem <id>"

    await update.message.reply_text(text)

async def redeem(update,context):

    user = update.message.from_user

    if not context.args:
        await update.message.reply_text("Usage: /redeem <reward id>")
        return

    reward_id = int(context.args[0])

    rewards_list = get_rewards()
    reward = next((r for r in rewards_list if r["id"] == reward_id), None)

    if not reward:
        await update.message.reply_text("Reward not found.")
        return

    user_points = get_user_points(user.id)

    if user_points < reward["cost"]:
        await update.message.reply_text("Not enough points.")
        return

    deduct_points(user.id, reward["cost"])

    today = datetime.now().strftime("%Y-%m-%d")

    try:
        await asyncio.to_thread(
            redemptions_sheet.append_row,
            [today, user.first_name, reward["name"], reward["cost"]]
        )
    except Exception as e:
        print("Redemption log error:", e)

    await update.message.reply_text(
        f"🎉 {reward['name']} redeemed!\nRemaining points: {user_points - reward['cost']}"
    )

# -----------------------
# MAIN
# -----------------------

def main():

    logging.basicConfig(level=logging.INFO)

    app=Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("ping",ping))
    app.add_handler(CommandHandler("mypoints",mypoints))
    app.add_handler(CommandHandler("rewards",rewards))
    app.add_handler(CommandHandler("redeem",redeem))
    app.add_handler(CommandHandler("leaderboard",leaderboard))

    app.add_handler(MessageHandler(filters.ALL,learn_users),group=0)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,reaction_recognition),group=1)

    job_queue = app.job_queue

    # --- crash-proof scheduler ---
    if job_queue:
        job_queue.run_daily(
            friday_leaderboard,
            time=datetime.strptime("17:00","%H:%M").time(),
            days=(4,)
        )
    else:
        print("JobQueue not available — Friday leaderboard disabled")
    # --- end fix ---

    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
