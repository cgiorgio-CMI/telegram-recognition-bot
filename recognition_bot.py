import logging
import sqlite3
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
import asyncio
import os
import json
import re
import unicodedata

from telegram.ext import Application, CommandHandler, MessageHandler, filters
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# -----------------------
# SETTINGS
# -----------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])
LOCAL_TZ = ZoneInfo("America/Toronto")

MAX_DAILY_RECOGNITIONS = 5
MAX_DAILY_RECEIVED = 5

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

DB_PATH = os.environ.get("DB_PATH", "/data/recognition.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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

def strip_accents(text):
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )

def normalize_text(text):
    text = strip_accents(text or "")
    text = text.lower()
    text = text.replace("🌱", " ")
    text = text.replace("@", " ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def normalize_name(name):
    return normalize_text(name)

def normalize_username(username):
    return normalize_text(username)

def register_user(user):
    username = normalize_username(user.username) if user.username else None
    name = f"{user.first_name} {user.last_name or ''}".strip()
    normalized = normalize_name(name)

    cursor.execute("""
    INSERT INTO users(user_id,username,name,normalized_name)
    VALUES(?,?,?,?)
    ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username,
        name=excluded.name,
        normalized_name=excluded.normalized_name
    """, (user.id, username, name, normalized))
    conn.commit()

def get_user_points(user_id):
    cursor.execute("SELECT points FROM points WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else 0

def update_points(user_id, name, amount):
    old_points = get_user_points(user_id)

    cursor.execute("""
    INSERT INTO points(user_id,name,points)
    VALUES(?,?,?)
    ON CONFLICT(user_id) DO UPDATE SET
        points=points+?
    """, (user_id, name, amount, amount))
    conn.commit()

    new_points = get_user_points(user_id)
    return old_points, new_points

def daily_given_count(user_id):
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    cursor.execute("""
    SELECT COALESCE(SUM(points),0)
    FROM recognitions
    WHERE sender_id=? AND date=?
    """, (user_id, today))
    return cursor.fetchone()[0]

def daily_received_count(user_id):
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    cursor.execute("""
    SELECT COALESCE(SUM(points),0)
    FROM recognitions
    WHERE receiver_id=? AND date=?
    """, (user_id, today))
    return cursor.fetchone()[0]

def milestone_hits(old_points, new_points):
    hits = []
    for milestone in MILESTONES:
        if old_points < milestone <= new_points:
            hits.append(milestone)
    return hits

def is_admin_private(update):
    return (
        update.message
        and update.message.from_user
        and update.message.chat
        and update.message.chat.type == "private"
        and update.message.from_user.id == ADMIN_USER_ID
    )

def load_team_directory():
    try:
        rows = team_sheet.get_all_records()
    except Exception as e:
        print("Team sheet load error:", e)
        rows = []

    team_by_name = {}
    team_by_username = {}

    for row in rows:
        team_name = (
            row.get("Name")
            or row.get("name")
            or row.get("Full Name")
            or row.get("full name")
        )

        team_username = (
            row.get("Username")
            or row.get("username")
            or row.get("Telegram Username")
            or row.get("telegram username")
        )

        if team_name:
            normalized_name = normalize_name(team_name)
            if normalized_name:
                team_by_name[normalized_name] = team_name.strip()

        if team_name and team_username:
            normalized_username = normalize_username(team_username)
            if normalized_username:
                team_by_username[normalized_username] = team_name.strip()

    return team_by_name, team_by_username

def get_or_create_team_user(team_name, team_username=None):
    normalized = normalize_name(team_name)
    normalized_username = normalize_username(team_username) if team_username else None

    cursor.execute("""
    SELECT user_id, name
    FROM users
    WHERE normalized_name=?
    """, (normalized,))
    row = cursor.fetchone()
    if row:
        if normalized_username:
            cursor.execute("""
            UPDATE users
            SET username=COALESCE(username, ?)
            WHERE user_id=?
            """, (normalized_username, row[0]))
            conn.commit()
        return row

    cursor.execute("""
    INSERT INTO users(username,name,normalized_name)
    VALUES(?,?,?)
    """, (normalized_username, team_name, normalized))
    conn.commit()
    return (cursor.lastrowid, team_name)

def resolve_user_by_name(name):
    normalized = normalize_name(name)
    if not normalized:
        return None

    cursor.execute("""
    SELECT user_id, name
    FROM users
    WHERE normalized_name=?
    """, (normalized,))
    row = cursor.fetchone()
    if row:
        return row

    team_by_name, _ = load_team_directory()
    if normalized in team_by_name:
        return get_or_create_team_user(team_by_name[normalized])

    return None

async def user_is_in_chat(bot, chat_id, user_id):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return False

async def log_manual_adjustment_to_sheet(admin_name, receiver_name, amount, reason):
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    try:
        await asyncio.to_thread(
            recognitions_sheet.append_row,
            [today, f"ADMIN ADJUST ({admin_name})", receiver_name, reason, amount]
        )
    except Exception as e:
        print("Manual adjustment sheet log error:", e)

async def resolve_mention_entity(text, entity, sender_id):
    if entity.type == "text_mention" and entity.user:
        mentioned_user = entity.user
        if mentioned_user.id == sender_id:
            return "self", None

        register_user(mentioned_user)
        full_name = f"{mentioned_user.first_name} {mentioned_user.last_name or ''}".strip()
        return "ok", (mentioned_user.id, full_name)

    if entity.type == "mention":
        mention_text = text[entity.offset: entity.offset + entity.length]
        username = mention_text.replace("@", "").strip().lower()
        normalized_username = normalize_username(username)

        cursor.execute("""
        SELECT user_id, name
        FROM users
        WHERE username=?
        """, (normalized_username,))
        row = cursor.fetchone()

        if row:
            if row[0] == sender_id:
                return "self", None
            return "ok", row

        _, team_by_username = load_team_directory()
        if normalized_username in team_by_username:
            team_name = team_by_username[normalized_username]
            row = get_or_create_team_user(team_name, normalized_username)
            if row[0] == sender_id:
                return "self", None
            return "ok", row

        return "unknown_username", mention_text

    return "skip", None

# -----------------------
# COMMANDS
# -----------------------

async def ping(update, context):
    await update.message.reply_text("✅ Bot working!")

async def mypoints(update, context):
    user = update.message.from_user
    register_user(user)
    pts = get_user_points(user.id)
    await update.message.reply_text(f"🏆 {user.first_name}, you have {pts} points!")

async def adjust(update, context):
    if not is_admin_private(update):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n/adjust Full Name 5\n/adjust Full Name -3"
        )
        return

    try:
        amount = int(context.args[-1])
    except ValueError:
        await update.message.reply_text(
            "Last value must be a whole number.\nExample: /adjust Anne Marie 5"
        )
        return

    name = " ".join(context.args[:-1]).strip()
    result = resolve_user_by_name(name)

    if not result:
        await update.message.reply_text(f"Could not find: {name}")
        return

    user_id, resolved_name = result
    old_points, new_points = update_points(user_id, resolved_name, amount)

    admin_name = update.message.from_user.first_name
    reason = f"/adjust {resolved_name} {amount}"
    await log_manual_adjustment_to_sheet(admin_name, resolved_name, amount, reason)

    message = []
    if amount >= 0:
        message.append(f"✅ Added {amount} point(s) to {resolved_name}.")
    else:
        message.append(f"✅ Removed {abs(amount)} point(s) from {resolved_name}.")
    message.append(f"New total: {new_points}")

    hits = milestone_hits(old_points, new_points)
    for milestone in hits:
        message.append(f"🎉 {resolved_name} reached {milestone} points!")

    await update.message.reply_text("\n".join(message))

async def bulkadjust(update, context):
    if not is_admin_private(update):
        return

    if not update.message.text:
        return

    lines = update.message.text.split("\n")[1:]
    if not lines:
        await update.message.reply_text(
            "Usage:\n/bulkadjust\nAnne Marie 5\nSandra -2\nKennedy 3"
        )
        return

    results = []
    admin_name = update.message.from_user.first_name

    for line in lines:
        line = line.strip()
        if not line:
            continue

        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            results.append(f"⚠️ Skipped: {line}")
            continue

        name, amount_text = parts

        try:
            amount = int(amount_text)
        except ValueError:
            results.append(f"⚠️ Skipped: {line}")
            continue

        result = resolve_user_by_name(name)
        if not result:
            results.append(f"⚠️ Not found: {name}")
            continue

        user_id, resolved_name = result
        old_points, new_points = update_points(user_id, resolved_name, amount)

        reason = f"/bulkadjust {resolved_name} {amount}"
        await log_manual_adjustment_to_sheet(admin_name, resolved_name, amount, reason)

        if amount >= 0:
            results.append(f"✅ {resolved_name}: +{amount} (Total: {new_points})")
        else:
            results.append(f"✅ {resolved_name}: {amount} (Total: {new_points})")

        hits = milestone_hits(old_points, new_points)
        for milestone in hits:
            results.append(f"🎉 {resolved_name} reached {milestone} points!")

    if not results:
        await update.message.reply_text("No valid adjustments found.")
        return

    await update.message.reply_text("\n".join(results))

async def allpoints(update, context):
    cursor.execute("""
    SELECT name, points
    FROM points
    WHERE points > 0
    ORDER BY points DESC, name ASC
    """)
    rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("No points recorded yet.")
        return

    message = "🏆 All-Time Leaderboard\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for i, (name, pts) in enumerate(rows):
        medal = medals[i] if i < len(medals) else "🏅"
        message += f"{medal} {name} — {pts} points\n"

    await update.message.reply_text(message)

async def todayreceived(update, context):
    if not is_admin_private(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /todayreceived Full Name")
        return

    name = " ".join(context.args).strip()
    result = resolve_user_by_name(name)
    if not result:
        await update.message.reply_text(f"Could not find: {name}")
        return

    user_id, resolved_name = result
    total = daily_received_count(user_id)
    await update.message.reply_text(
        f"📥 {resolved_name} has received {total}/{MAX_DAILY_RECEIVED} points today."
    )

async def todaygiven(update, context):
    if not is_admin_private(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /todaygiven Full Name")
        return

    name = " ".join(context.args).strip()
    result = resolve_user_by_name(name)
    if not result:
        await update.message.reply_text(f"Could not find: {name}")
        return

    user_id, resolved_name = result
    total = daily_given_count(user_id)
    await update.message.reply_text(
        f"📤 {resolved_name} has given {total}/{MAX_DAILY_RECOGNITIONS} points today."
    )

async def adminstats(update, context):
    if not is_admin_private(update):
        return

    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

    cursor.execute("""
    SELECT receiver_name, SUM(points)
    FROM recognitions
    WHERE date=?
    GROUP BY receiver_name
    ORDER BY SUM(points) DESC, receiver_name ASC
    LIMIT 10
    """, (today,))
    received_rows = cursor.fetchall()

    cursor.execute("""
    SELECT sender_name, SUM(points)
    FROM recognitions
    WHERE date=?
    GROUP BY sender_name
    ORDER BY SUM(points) DESC, sender_name ASC
    LIMIT 10
    """, (today,))
    given_rows = cursor.fetchall()

    message = [f"📊 Admin Stats for {today}"]

    message.append("\nTop Received Today:")
    if received_rows:
        for name, pts in received_rows:
            message.append(f"• {name} — {pts}")
    else:
        message.append("• No recognitions yet")

    message.append("\nTop Given Today:")
    if given_rows:
        for name, pts in given_rows:
            message.append(f"• {name} — {pts}")
    else:
        message.append("• No recognitions yet")

    await update.message.reply_text("\n".join(message))

# -----------------------
# LEADERBOARD COMMAND
# -----------------------

async def leaderboard(update, context):
    today = datetime.now(LOCAL_TZ)
    monday = today - timedelta(days=today.weekday())
    start = monday.strftime("%Y-%m-%d")

    cursor.execute("""
    SELECT receiver_name, SUM(points)
    FROM recognitions
    WHERE date >= ?
    GROUP BY receiver_name
    ORDER BY SUM(points) DESC
    LIMIT 10
    """, (start,))
    rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("No recognitions yet this week.")
        return

    message = "🏆 Weekly Recognition Leaderboard\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, pts) in enumerate(rows):
        medal = medals[i] if i < len(medals) else "🏅"
        message += f"{medal} {name} — {pts} points\n"

    await update.message.reply_text(message)

# -----------------------
# AUTO LEARN USERS
# -----------------------

async def learn_users(update, context):
    if not update.message or not update.message.from_user:
        return
    register_user(update.message.from_user)

# -----------------------
# RECOGNITION ENGINE
# -----------------------

async def reaction_recognition(update, context):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    if "🌱" not in text:
        return

    sender_user = update.message.from_user
    register_user(sender_user)

    sender_id = sender_user.id
    sender_name = sender_user.first_name
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    message_id = update.message.message_id
    chat_id = update.effective_chat.id

    message_points = max(1, min(text.count("🌱"), 5))
    team_by_name, _ = load_team_directory()

    entities = update.message.entities or []
    mention_entities = [e for e in entities if e.type in ("mention", "text_mention")]

    if not mention_entities:
        await update.message.reply_text(
            "⚠️ Please use @ and select the person from the Telegram popup list so they are properly tagged and notified."
        )
        return

    receivers = {}
    self_detected = False
    unknown_mentions = []

    for entity in mention_entities:
        status, result = await resolve_mention_entity(text, entity, sender_id)

        if status == "self":
            self_detected = True
            continue

        if status == "unknown_username":
            unknown_mentions.append(result)
            continue

        if status == "ok" and result:
            user_id, name = result
            receivers[user_id] = (user_id, name)

    if not receivers and self_detected:
        await update.message.reply_text("⚠️ You cannot recognize yourself.")
        return

    if not receivers and unknown_mentions:
        await update.message.reply_text(
            "⚠️ I could not identify that @mention yet. Please make sure that person is on the Team sheet with their Telegram username, or has spoken in the chat at least once."
        )
        return

    if not receivers:
        await update.message.reply_text(
            "⚠️ Please use @ and select the person from the Telegram popup list."
        )
        return

    valid_receivers = {}
    invalid_receivers = []

    for user_id, (r_id, r_name) in receivers.items():
        normalized = normalize_name(r_name)
        in_team_sheet = normalized in team_by_name
        in_group = await user_is_in_chat(context.bot, chat_id, user_id)

        if in_team_sheet or in_group:
            valid_receivers[user_id] = (r_id, r_name)
        else:
            invalid_receivers.append(r_name)

    if not valid_receivers and self_detected:
        await update.message.reply_text("⚠️ You cannot recognize yourself.")
        return

    if not valid_receivers:
        await update.message.reply_text(
            "⚠️ That person must either be in this group or listed on the Team sheet."
        )
        return

    sender_total_today = daily_given_count(sender_id)

    awards = []
    capped_names = []
    milestone_lines = []
    awarded_names = []

    total_to_award = 0
    for r_id, r_name in valid_receivers.values():
        received_today = daily_received_count(r_id)
        remaining_receive = max(0, MAX_DAILY_RECEIVED - received_today)
        award_points = min(message_points, remaining_receive)

        if award_points <= 0:
            capped_names.append(r_name)
            continue

        total_to_award += award_points
        awards.append((r_id, r_name, award_points))

    if not awards:
        if capped_names:
            await update.message.reply_text(
                "⚠️ " + ", ".join(capped_names) + " already reached the daily receive max of 5."
            )
        else:
            await update.message.reply_text("⚠️ No valid recognitions found.")
        return

    if sender_total_today + total_to_award > MAX_DAILY_RECOGNITIONS:
        await update.message.reply_text("⚠️ Daily giving limit reached (5 points max per day).")
        return

    for r_id, r_name, award_points in awards:
        old_points, new_points = update_points(r_id, r_name, award_points)

        cursor.execute("""
        INSERT OR IGNORE INTO recognitions
        (sender_id, sender_name, receiver_id, receiver_name, date, points, message_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (sender_id, sender_name, r_id, r_name, today, award_points, message_id))

        awarded_names.append(f"{r_name} (+{award_points})")

        try:
            await asyncio.to_thread(
                recognitions_sheet.append_row,
                [today, sender_name, r_name, text, award_points]
            )
        except Exception as e:
            print("Sheet log error:", e)

        hits = milestone_hits(old_points, new_points)
        for milestone in hits:
            milestone_lines.append(f"🎉 {r_name} reached {milestone} points!")

    conn.commit()

    response_lines = [f"🌱 Recognition sent: {', '.join(awarded_names)}"]

    if capped_names:
        response_lines.append(
            f"⚠️ Daily receive max reached for: {', '.join(capped_names)}"
        )

    if invalid_receivers:
        response_lines.append(
            f"⚠️ Not valid for recognition: {', '.join(invalid_receivers)}"
        )

    if unknown_mentions:
        response_lines.append(
            "⚠️ Some @mentions could not be identified yet. Add their Telegram username to the Team sheet, or have them speak once in the chat."
        )

    if self_detected:
        response_lines.append("⚠️ You cannot recognize yourself.")

    response_lines.extend(milestone_lines)

    await update.message.reply_text("\n".join(response_lines))

# -----------------------
# FRIDAY LEADERBOARD
# -----------------------

async def friday_leaderboard(context):
    today = datetime.now(LOCAL_TZ)
    monday = today - timedelta(days=today.weekday())
    start = monday.strftime("%Y-%m-%d")

    cursor.execute("""
    SELECT receiver_name, SUM(points)
    FROM recognitions
    WHERE date >= ?
    GROUP BY receiver_name
    ORDER BY SUM(points) DESC
    LIMIT 3
    """, (start,))
    rows = cursor.fetchall()

    if not rows:
        return

    message = "🏆 Friday Recognition Leaderboard\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for i, (name, pts) in enumerate(rows):
        medal = medals[i] if i < len(medals) else "🏅"
        message += f"{medal} {name} — {pts} points\n"

    message += "\nAmazing work team! 🌱"

    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
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

def deduct_points(user_id, cost):
    cursor.execute("""
    UPDATE points
    SET points = points - ?
    WHERE user_id = ?
    """, (cost, user_id))
    conn.commit()

async def rewards(update, context):
    rewards_list = get_rewards()
    if not rewards_list:
        await update.message.reply_text("No rewards available.")
        return

    text = "🎁 Available Rewards\n\n"
    for r in rewards_list:
        text += f"{r['id']}. {r['name']} — {r['cost']} pts\n"
    text += "\nUse /redeem <id>"

    await update.message.reply_text(text)

async def redeem(update, context):
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
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

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
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("mypoints", mypoints))
    app.add_handler(CommandHandler("adjust", adjust))
    app.add_handler(CommandHandler("bulkadjust", bulkadjust))
    app.add_handler(CommandHandler("allpoints", allpoints))
    app.add_handler(CommandHandler("todayreceived", todayreceived))
    app.add_handler(CommandHandler("todaygiven", todaygiven))
    app.add_handler(CommandHandler("adminstats", adminstats))
    app.add_handler(CommandHandler("rewards", rewards))
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(CommandHandler("leaderboard", leaderboard))

    app.add_handler(MessageHandler(filters.ALL, learn_users), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reaction_recognition), group=1)

    job_queue = app.job_queue
    if job_queue:
        job_queue.run_daily(
            friday_leaderboard,
            time=datetime.strptime("17:00", "%H:%M").time().replace(tzinfo=LOCAL_TZ),
            days=(5,)
        )
    else:
        print("JobQueue not available — Friday leaderboard disabled")

    try:
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        print("⚠️ Bot polling error (maybe another instance running):", e)

if __name__ == "__main__":
    main()
