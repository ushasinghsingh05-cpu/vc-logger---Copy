import os
import json
import html
import asyncio
from pymongo import MongoClient
import requests

from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

from telethon import TelegramClient, events, functions
from telethon.sessions import StringSession
from telethon.tl.types import UpdateGroupCallParticipants


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
MONGO_URL = os.getenv("MONGO_URL")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "vc_logger")

required = {
    "API_ID": API_ID,
    "API_HASH": API_HASH,
    "SESSION_STRING": SESSION_STRING,
    "BOT_TOKEN": BOT_TOKEN,
    "OWNER_ID": OWNER_ID,
    "MONGO_URL": MONGO_URL,
}
for key, value in required.items():
    if not value:
        raise ValueError(f"Missing environment variable: {key}")

API_ID = int(API_ID)
OWNER_ID = int(OWNER_ID)

IST = ZoneInfo("Asia/Kolkata")
BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

BOT_ID = None
BOT_USERNAME = None
CENTRAL_ID = None
CENTRAL_USERNAME = None

mongo_client = None
groups_collection = None
update_offset = None

# Per-group runtime state. Each group has its own VC, users and log message.
group_entities = {}
group_titles = {}
group_states = {}
call_to_group = {}
user_cache = {}

EXTRA_ADMIN_RIGHTS = (
    "can_delete_messages",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
    "can_invite_users",
    "can_pin_messages",
    "can_manage_topics",
    "can_post_stories",
    "can_edit_stories",
    "can_delete_stories",
)


# ============================================================
# DATABASE — MONGODB
# ============================================================

def init_database():
    global mongo_client, groups_collection

    if not MONGO_URL:
        raise ValueError("Missing environment variable: MONGO_URL")

    mongo_client = MongoClient(
        MONGO_URL,
        serverSelectionTimeoutMS=15000,
        connectTimeoutMS=15000,
    )
    # Verify connectivity during startup instead of failing later.
    mongo_client.admin.command("ping")

    database = mongo_client[MONGO_DB_NAME]
    groups_collection = database["groups"]
    groups_collection.create_index("chat_id", unique=True)
    groups_collection.create_index("status")

    print(f"MongoDB connected successfully. Database: {MONGO_DB_NAME}")


def save_group(chat_id, title, added_by, status="pending"):
    groups_collection.update_one(
        {"chat_id": int(chat_id)},
        {
            "$set": {
                "title": title,
                "added_by": added_by,
                "status": status,
                "updated_at": now().isoformat(),
            },
            "$setOnInsert": {
                "chat_id": int(chat_id),
                "authorized_by": None,
            },
        },
        upsert=True,
    )


def set_group_status(chat_id, status, authorized_by=None):
    update = {
        "$set": {
            "status": status,
            "updated_at": now().isoformat(),
        }
    }
    if authorized_by is not None:
        update["$set"]["authorized_by"] = int(authorized_by)

    groups_collection.update_one(
        {"chat_id": int(chat_id)},
        update,
        upsert=False,
    )


def get_group(chat_id):
    return groups_collection.find_one({"chat_id": int(chat_id)})


def get_known_groups():
    return list(
        groups_collection.find({"status": {"$ne": "removed"}})
        .sort("title", 1)
    )


def get_active_groups():
    return list(
        groups_collection.find({"status": "active"})
        .sort("title", 1)
    )


# ============================================================
# TIME HELPERS
# ============================================================

def now():
    return datetime.now(IST)


def format_time(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return value.astimezone(IST).strftime("%d-%m-%Y %I:%M:%S %p IST")


def format_duration(start_time, end_time):
    seconds = max(0, int((end_time - start_time).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


# ============================================================
# BOT API HELPERS
# ============================================================

def bot_api(method, data=None):
    try:
        response = requests.post(
            f"{BOT_API}/{method}",
            data=data or {},
            timeout=20
        )
        result = response.json()
        if not result.get("ok"):
            print(f"Bot API {method} error: {result}")
            return None
        return result.get("result")
    except Exception as exc:
        print(f"Bot API {method} request failed: {exc}")
        return None


def send_message(chat_id, text, reply_markup=None):
    data = {
        "chat_id": int(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return bot_api("sendMessage", data)


def answer_callback(callback_id, text=None, alert=False):
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    if alert:
        data["show_alert"] = "true"
    return bot_api("answerCallbackQuery", data)


def get_chat_member(chat_id, user_id):
    return bot_api("getChatMember", {
        "chat_id": int(chat_id),
        "user_id": int(user_id),
    })


def is_admin(member):
    return bool(
        member and member.get("status") in ("administrator", "creator")
    )


def owner_button():
    return {
        "inline_keyboard": [[
            {
                "text": "👑 OWNER PROFILE",
                "url": f"tg://user?id={OWNER_ID}"
            }
        ]]
    }


def welcome_keyboard():
    return {
        "inline_keyboard": [
            [{
                "text": "➕ ADD BOT TO YOUR GROUP",
                "url": f"https://t.me/{BOT_USERNAME}?startgroup=true"
            }],
            [
                {
                    "text": "👑 OWNER",
                    "url": f"tg://user?id={OWNER_ID}"
                },
                {
                    "text": "⚙️ HELP & SETUP",
                    "callback_data": "help"
                }
            ]
        ]
    }


def help_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🔗 CONNECT GROUP", "callback_data": "connect"}],
            [{"text": "⬅️ BACK", "callback_data": "home"}]
        ]
    }


def connect_keyboard(groups):
    rows = []
    for group in groups[:40]:
        title = (group["title"] or "Unnamed group")[:45]
        rows.append([{
            "text": f"🎙️ {title}",
            "callback_data": f"choose:{group['chat_id']}"
        }])
    rows.append([{"text": "⬅️ BACK", "callback_data": "home"}])
    return {"inline_keyboard": rows}


def authorization_keyboard(chat_id):
    return {
        "inline_keyboard": [
            [{"text": "✅ AUTHORIZE & CONTINUE",
              "callback_data": f"authorize:{chat_id}"}],
            [{"text": "❌ CANCEL", "callback_data": "cancel"}]
        ]
    }


# ============================================================
# WELCOME / HELP / GROUP SETUP
# ============================================================

WELCOME_TEXT = (
    "👋 <b>WELCOME TO VC LOGGER!</b>\n\n"
    "Monitor your Telegram group's voice-chat activity and receive "
    "JOINED / LEFT / STARTED / ENDED logs.\n\n"
    "Add the bot to your group, make it an administrator, then open "
    "<b>Help &amp; Setup</b> to connect your group."
)

HELP_TEXT = (
    "⚙️ <b>VC LOGGER — SETUP GUIDE</b>\n\n"
    "<b>STEP 1:</b> Add this bot to your Telegram group.\n\n"
    "<b>STEP 2:</b> Promote this bot to administrator so it can send "
    "setup and VC log messages.\n\n"
    "<b>STEP 3:</b> Return to this DM and tap <b>CONNECT GROUP</b>.\n\n"
    "<b>STEP 4:</b> Select your group. The bot will verify that you "
    "are an administrator.\n\n"
    "<b>STEP 5:</b> Follow the monitoring-account notice. The central "
    "Telethon account must be added to your group with the "
    "<b>Manage Video Chats</b> permission.\n\n"
    "<b>STEP 6:</b> Tap <b>AUTHORIZE &amp; CONTINUE</b>. The bot checks "
    "the required permissions before enabling logging.\n\n"
    "The monitoring account will remain visible as a group member while "
    "monitoring is enabled. VC activity logging should be disclosed to "
    "your group members."
)


def monitoring_notice(chat_title, chat_id):
    account = f"@{html.escape(CENTRAL_USERNAME)}"
    return (
        "⚠️ <b>IMPORTANT: VOICE CHAT MONITORING SETUP</b>\n\n"
        f"<b>Group:</b> {html.escape(chat_title)}\n"
        f"<b>Group ID:</b> <code>{chat_id}</code>\n\n"
        "To enable accurate voice-chat participant JOINED / LEFT logs, "
        "please complete this step:\n\n"
        f"1. Add <b>{account}</b> to your group.\n"
        "2. Promote this account to group administrator.\n"
        "3. Grant it <b>Manage Video Chats</b> only. Do not grant "
        "unnecessary administrator permissions.\n"
        "4. Return here and tap <b>AUTHORIZE &amp; CONTINUE</b>.\n\n"
        "<b>WHY IS THIS REQUIRED?</b>\n"
        "The central Telethon account needs to be a member of your group "
        "to access voice-chat participant updates.\n\n"
        "<b>PRIVACY NOTICE:</b> This account will be visible in your group. "
        "Voice-chat participant activity will be logged and sent to this "
        "group by the VC Logger bot. Please inform your group members.\n\n"
        "Do not continue until you have completed the setup."
    )


async def handle_private_message(message):
    user_id = message["from"]["id"]
    text = (message.get("text") or "").strip()

    if text.startswith("/start"):
        send_message(user_id, WELCOME_TEXT, welcome_keyboard())
    elif text.startswith("/help"):
        send_message(user_id, HELP_TEXT, help_keyboard())
    elif text.startswith("/connect"):
        await show_connect_groups(user_id)
    else:
        send_message(
            user_id,
            "Use the buttons below to configure VC Logger.",
            welcome_keyboard()
        )


async def show_connect_groups(user_id):
    groups = get_known_groups()
    if not groups:
        send_message(
            user_id,
            "I haven't detected any group yet.\n\n"
            "First add me to your group, promote me to administrator, "
            "then return here and try again.",
            help_keyboard()
        )
        return

    send_message(
        user_id,
        "🔗 <b>SELECT YOUR GROUP</b>\n\n"
        "Choose the group you administer. I will verify your permissions "
        "before showing the monitoring authorization notice.",
        connect_keyboard(groups)
    )


async def handle_choose_group(user_id, chat_id):
    group = get_group(chat_id)
    if not group or group["status"] == "removed":
        send_message(user_id, "This group is not available. Add the bot again.")
        return

    bot_member = get_chat_member(chat_id, BOT_ID)
    if not is_admin(bot_member):
        send_message(
            user_id,
            "❌ The bot is not an administrator in this group yet.\n"
            "Promote the bot to administrator, then try again."
        )
        return

    admin_member = get_chat_member(chat_id, user_id)
    if not is_admin(admin_member):
        send_message(
            user_id,
            "❌ You are not an administrator of this group. "
            "Only a group administrator can authorize setup."
        )
        return

    title = group["title"]
    send_message(
        user_id,
        monitoring_notice(title, chat_id),
        authorization_keyboard(chat_id)
    )


async def handle_authorize(user_id, chat_id):
    group = get_group(chat_id)
    if not group or group["status"] == "removed":
        send_message(user_id, "❌ Group not found. Add the bot again.")
        return

    # Re-check both the person authorizing and the bot at confirmation time.
    admin_member = get_chat_member(chat_id, user_id)
    if not is_admin(admin_member):
        send_message(
            user_id,
            "❌ Authorization rejected. You must still be a group admin."
        )
        return

    bot_member = get_chat_member(chat_id, BOT_ID)
    if not is_admin(bot_member):
        send_message(
            user_id,
            "❌ The bot must be an administrator in the group."
        )
        return

    # Verify the central Telethon account, not the admin's ID/session.
    central_member = get_chat_member(chat_id, CENTRAL_ID)
    if not is_admin(central_member):
        send_message(
            user_id,
            f"❌ I cannot verify {CENTRAL_USERNAME} as a group admin.\n\n"
            "Add the central monitoring account to this group and promote "
            "it to administrator with Manage Video Chats permission."
        )
        return

    if (
        central_member.get("status") != "creator"
        and not central_member.get("can_manage_video_chats", False)
    ):
        send_message(
            user_id,
            f"❌ {CENTRAL_USERNAME} is an administrator, but the "
            "<b>Manage Video Chats</b> permission is missing.\n\n"
            "Enable that permission and try again."
        )
        return

    # Warn and block if unrelated elevated rights were also granted.
    extra = [
        right for right in EXTRA_ADMIN_RIGHTS
        if central_member.get(right, False)
    ]
    if extra:
        readable = ", ".join(right.replace("can_", "").replace("_", " ")
                              for right in extra)
        send_message(
            user_id,
            "⚠️ <b>Extra administrator permissions detected.</b>\n\n"
            f"Detected: <code>{html.escape(readable)}</code>\n\n"
            "For least-privilege setup, edit the central account's admin "
            "permissions so that only <b>Manage Video Chats</b> is enabled, "
            "then press Authorize &amp; Continue again."
        )
        return

    try:
        entity = await client.get_entity(chat_id)
    except Exception as exc:
        print(f"Could not access group {chat_id} with Telethon: {exc}")
        send_message(
            user_id,
            "❌ The central Telethon account cannot access this group. "
            "Check that it has joined the group."
        )
        return

    group_entities[chat_id] = entity
    group_titles[chat_id] = getattr(entity, "title", None) or group["title"]
    group_states.setdefault(chat_id, {
        "call_id": None,
        "active_users": {},
        "last_event_message_id": None,
    })

    set_group_status(chat_id, "active", user_id)

    send_message(
        user_id,
        "⏳ <b>Group verified. Loading group members...</b>\n\n"
        "I am checking the central account's access and preparing VC logging."
    )

    await load_group_members(chat_id)

    success = (
        "🎉 <b>VC LOGGER SETUP SUCCESSFUL!</b>\n\n"
        f"<b>Group:</b> {html.escape(group_titles[chat_id])}\n"
        f"<b>Monitoring account:</b> {html.escape(CENTRAL_USERNAME)}\n"
        "<b>Admin verification:</b> ✅\n"
        "<b>Manage Video Chats permission:</b> ✅\n"
        "<b>Member loading:</b> ✅\n"
        "<b>VC monitoring:</b> 🟢 ACTIVE\n\n"
        "JOINED / LEFT / STARTED / ENDED logs will be sent to this group."
    )
    send_message(user_id, success)
    send_message(chat_id, success)


async def handle_callback(query):
    data = query.get("data", "")
    user_id = query["from"]["id"]
    callback_id = query["id"]
    message = query.get("message", {})
    private_chat_id = message.get("chat", {}).get("id", user_id)

    answer_callback(callback_id)

    if data == "home":
        send_message(private_chat_id, WELCOME_TEXT, welcome_keyboard())
    elif data == "help":
        send_message(private_chat_id, HELP_TEXT, help_keyboard())
    elif data == "connect":
        await show_connect_groups(user_id)
    elif data == "cancel":
        send_message(private_chat_id, "Setup cancelled.", welcome_keyboard())
    elif data.startswith("choose:"):
        try:
            chat_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        await handle_choose_group(user_id, chat_id)
    elif data.startswith("authorize:"):
        try:
            chat_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        await handle_authorize(user_id, chat_id)


async def process_update(update):
    # Detect when the bot is added, promoted, demoted or removed.
    member_update = update.get("my_chat_member")
    if member_update:
        chat = member_update.get("chat", {})
        if chat.get("type") in ("group", "supergroup"):
            new_status = (
                member_update.get("new_chat_member", {}).get("status")
            )
            actor_id = member_update.get("from", {}).get("id")
            if new_status in ("left", "kicked"):
                set_group_status(chat["id"], "removed")
                group_states.pop(chat["id"], None)
                group_entities.pop(chat["id"], None)
                group_titles.pop(chat["id"], None)
            else:
                save_group(
                    chat["id"],
                    chat.get("title", "Telegram Group"),
                    actor_id,
                    "pending"
                )
        return

    callback = update.get("callback_query")
    if callback:
        await handle_callback(callback)
        return

    message = update.get("message")
    if message and message.get("chat", {}).get("type") == "private":
        await handle_private_message(message)


# ============================================================
# TELEGRAM BOT UPDATE POLLING
# ============================================================

def fetch_updates(offset):
    params = {
        "timeout": 25,
        "allowed_updates": json.dumps([
            "message",
            "callback_query",
            "my_chat_member"
        ])
    }
    if offset is not None:
        params["offset"] = offset

    response = requests.get(
        f"{BOT_API}/getUpdates",
        params=params,
        timeout=35
    )
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return payload.get("result", [])


async def bot_update_loop():
    global update_offset
    print("Bot DM/setup polling started.")

    while True:
        try:
            updates = await asyncio.to_thread(fetch_updates, update_offset)
            for update in updates:
                update_offset = update["update_id"] + 1
                try:
                    await process_update(update)
                except Exception as exc:
                    print(f"Update handler error: {exc}")
        except Exception as exc:
            print(f"Bot polling error: {exc}")
            await asyncio.sleep(3)


# ============================================================
# TELETHON MEMBER / VOICE CHAT HELPERS
# ============================================================

def get_display_name(user):
    if not user:
        return "Unknown"
    name = (
        f"{getattr(user, 'first_name', '') or ''} "
        f"{getattr(user, 'last_name', '') or ''}"
    ).strip()
    return name or getattr(user, "username", None) or "Unknown"


def get_username(user):
    username = getattr(user, "username", None) if user else None
    return f"@{username}" if username else None


def get_participant_user_id(participant):
    peer = getattr(participant, "peer", None)
    user_id = getattr(peer, "user_id", None) if peer else None
    return int(user_id) if user_id else None


async def resolve_user(user_id):
    if user_id in user_cache:
        return user_cache[user_id]
    try:
        user = await client.get_entity(user_id)
        user_cache[user_id] = user
        return user
    except Exception as exc:
        print(f"Could not resolve user {user_id}: {exc}")
        return None


async def load_group_members(chat_id):
    entity = group_entities.get(chat_id)
    if not entity:
        return
    print(f"Loading members for {group_titles.get(chat_id, chat_id)}...")
    count = 0
    try:
        async for user in client.iter_participants(entity):
            user_cache[user.id] = user
            count += 1
        print(f"Loaded {count} members for group {chat_id}.")
    except Exception as exc:
        print(f"Could not load members for {chat_id}: {exc}")


async def get_current_call(entity):
    try:
        if (
            getattr(entity, "broadcast", False)
            or getattr(entity, "megagroup", False)
        ):
            full = await client(
                functions.channels.GetFullChannelRequest(channel=entity)
            )
        else:
            full = await client(
                functions.messages.GetFullChatRequest(chat_id=entity.id)
            )
        return full.full_chat.call
    except Exception as exc:
        print(f"Error getting current VC for {getattr(entity, 'id', '?')}: {exc}")
        return None


async def get_current_participants(call):
    if not call:
        return []
    try:
        result = await client(
            functions.phone.GetGroupCallRequest(call=call, limit=100)
        )
        return result.participants
    except Exception as exc:
        print(f"Error getting VC participants: {exc}")
        return []


# ============================================================
# LOG SENDING — EACH GROUP GETS ITS OWN LOGS
# ============================================================

def send_group_log(chat_id, message, reply_markup=None):
    data = {
        "chat_id": int(chat_id),
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return bot_api("sendMessage", data)


def send_vc_event_log(chat_id, message):
    state = group_states[chat_id]
    previous_id = state.get("last_event_message_id")

    if previous_id:
        bot_api("deleteMessage", {
            "chat_id": int(chat_id),
            "message_id": previous_id,
        })

    state["last_event_message_id"] = None
    result = send_group_log(chat_id, message, owner_button())
    if result:
        state["last_event_message_id"] = result["message_id"]


async def register_join(chat_id, user_id, joined_time=None):
    if not user_id:
        return

    state = group_states[chat_id]
    active_users = state["active_users"]
    if user_id in active_users:
        return

    user = await resolve_user(user_id)
    name = get_display_name(user)
    username = get_username(user)
    joined_time = joined_time or now()

    active_users[user_id] = {
        "name": name,
        "username": username,
        "joined": joined_time,
    }

    username_text = html.escape(username) if username else "No username"
    title = group_titles.get(chat_id, "Telegram Group")

    message = (
        "🟢 <b>VC JOINED</b>\n\n"
        f"👤 <b>Name:</b> {html.escape(name)}\n"
        f"🔗 <b>Username:</b> {username_text}\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
        f"🕐 <b>Joined:</b> {format_time(joined_time)}\n\n"
        f"🎙️ <b>VC:</b> {html.escape(title)}"
    )
    send_vc_event_log(chat_id, message)
    print(f"JOIN | {title} | {name} | {user_id} | {format_time(joined_time)}")


async def register_leave(chat_id, user_id, left_time=None):
    state = group_states[chat_id]
    active_users = state["active_users"]
    if user_id not in active_users:
        return

    user_data = active_users.pop(user_id)
    name = user_data["name"]
    username = user_data["username"]
    joined_time = user_data["joined"]
    left_time = left_time or now()
    duration = format_duration(joined_time, left_time)

    username_text = html.escape(username) if username else "No username"
    title = group_titles.get(chat_id, "Telegram Group")

    message = (
        "🔴 <b>VC LEFT</b>\n\n"
        f"👤 <b>Name:</b> {html.escape(name)}\n"
        f"🔗 <b>Username:</b> {username_text}\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
        f"🟢 <b>Joined:</b> {format_time(joined_time)}\n"
        f"🔴 <b>Left:</b> {format_time(left_time)}\n"
        f"⏱️ <b>Duration:</b> {duration}\n\n"
        f"🎙️ <b>VC:</b> {html.escape(title)}"
    )
    send_vc_event_log(chat_id, message)
    print(f"LEFT | {title} | {name} | {user_id} | Duration: {duration}")


async def handle_vc_started(chat_id, call):
    state = group_states[chat_id]
    state["call_id"] = call.id
    state["active_users"].clear()
    call_to_group[call.id] = chat_id

    start_time = now()
    title = group_titles.get(chat_id, "Telegram Group")
    send_group_log(
        chat_id,
        "🟢 <b>VOICE CHAT STARTED</b>\n\n"
        f"🎙️ <b>VC:</b> {html.escape(title)}\n"
        f"🆔 <b>Call ID:</b> <code>{call.id}</code>\n"
        f"🕐 <b>Started:</b> {format_time(start_time)}"
    )

    participants = await get_current_participants(call)
    for participant in participants:
        user_id = get_participant_user_id(participant)
        if user_id:
            await register_join(chat_id, user_id, start_time)


async def handle_vc_ended(chat_id):
    state = group_states[chat_id]
    call_id = state.get("call_id")
    if call_id is None:
        return

    end_time = now()
    for user_id in list(state["active_users"].keys()):
        await register_leave(chat_id, user_id, end_time)

    title = group_titles.get(chat_id, "Telegram Group")
    send_group_log(
        chat_id,
        "🔴 <b>VOICE CHAT ENDED</b>\n\n"
        f"🎙️ <b>VC:</b> {html.escape(title)}\n"
        f"🕐 <b>Ended:</b> {format_time(end_time)}"
    )

    call_to_group.pop(call_id, None)
    state["call_id"] = None
    state["active_users"].clear()


# ============================================================
# REAL-TIME TELETHON PARTICIPANT UPDATES
# ============================================================

@client.on(events.Raw)
async def vc_participant_update(update):
    if not isinstance(update, UpdateGroupCallParticipants):
        return

    call_id = getattr(getattr(update, "call", None), "id", None)
    chat_id = call_to_group.get(call_id)
    if chat_id is None or chat_id not in group_states:
        return

    for participant in update.participants:
        user_id = get_participant_user_id(participant)
        if not user_id:
            continue

        just_joined = getattr(participant, "just_joined", False)
        just_left = getattr(participant, "just_left", False)

        if just_joined and not just_left:
            await register_join(chat_id, user_id, now())
        elif just_left:
            await register_leave(chat_id, user_id, now())


# ============================================================
# MULTI-GROUP VOICE CHAT MONITOR
# ============================================================

async def monitor_one_group(chat_id):
    row = get_group(chat_id)
    if not row or row["status"] != "active":
        return

    if chat_id not in group_entities:
        try:
            entity = await client.get_entity(chat_id)
            group_entities[chat_id] = entity
            group_titles[chat_id] = (
                getattr(entity, "title", None) or row["title"]
            )
            group_states.setdefault(chat_id, {
                "call_id": None,
                "active_users": {},
                "last_event_message_id": None,
            })
        except Exception as exc:
            print(f"Cannot load group {chat_id}: {exc}")
            return

    state = group_states[chat_id]
    call = await get_current_call(group_entities[chat_id])

    if not call:
        if state["call_id"] is not None:
            await handle_vc_ended(chat_id)
        return

    call_to_group[call.id] = chat_id

    if state["call_id"] is None:
        await handle_vc_started(chat_id, call)
        return

    if state["call_id"] != call.id:
        await handle_vc_ended(chat_id)
        await handle_vc_started(chat_id, call)
        return

    participants = await get_current_participants(call)
    current_ids = {
        user_id
        for user_id in (
            get_participant_user_id(participant)
            for participant in participants
        )
        if user_id
    }

    for user_id in current_ids:
        if user_id not in state["active_users"]:
            await register_join(chat_id, user_id, now())

    for user_id in list(state["active_users"].keys()):
        if user_id not in current_ids:
            await register_leave(chat_id, user_id, now())


async def vc_monitor_loop():
    print("Multi-group VC monitor started.")
    semaphore = asyncio.Semaphore(5)

    async def guarded(chat_id):
        async with semaphore:
            try:
                await monitor_one_group(chat_id)
            except Exception as exc:
                print(f"Monitor error for group {chat_id}: {exc}")

    while True:
        try:
            rows = get_active_groups()
            active_ids = {row["chat_id"] for row in rows}

            # Stop tracking groups that have been disconnected or removed.
            for chat_id in list(group_states):
                if chat_id not in active_ids:
                    if group_states[chat_id].get("call_id") is not None:
                        await handle_vc_ended(chat_id)
                    group_states.pop(chat_id, None)
                    group_entities.pop(chat_id, None)
                    group_titles.pop(chat_id, None)

            if rows:
                await asyncio.gather(
                    *(guarded(row["chat_id"]) for row in rows)
                )
        except Exception as exc:
            print(f"VC monitor loop error: {exc}")

        await asyncio.sleep(5)


# ============================================================
# STARTUP
# ============================================================

async def main():
    global BOT_ID, BOT_USERNAME, CENTRAL_ID, CENTRAL_USERNAME

    init_database()

    print("=" * 60)
    print("STARTING MULTI-GROUP TELEGRAM VC LOGGER")
    print("=" * 60)

    # The bot uses polling for DM buttons and group-added updates.
    bot_info = await asyncio.to_thread(bot_api, "getMe", {})
    if not bot_info:
        raise RuntimeError("Could not connect to the Bot API.")

    BOT_ID = bot_info["id"]
    BOT_USERNAME = bot_info["username"]

    # Remove an old webhook if this bot was previously configured with one.
    await asyncio.to_thread(
        bot_api, "deleteWebhook", {"drop_pending_updates": "false"}
    )

    print(f"Bot connected: @{BOT_USERNAME}")

    # Connect the one central Telethon account.
    await client.start()
    central = await client.get_me()
    CENTRAL_ID = central.id
    CENTRAL_USERNAME = central.username

    if not CENTRAL_USERNAME:
        raise RuntimeError(
            "The central Telethon account needs a public username. "
            "Set one, then restart the service."
        )

    print(
        f"Central Telethon account: @{CENTRAL_USERNAME} "
        f"(ID: {CENTRAL_ID})"
    )
    print(f"Indian time: {format_time(now())}")
    print(f"MongoDB database: {MONGO_DB_NAME}")

    # Rehydrate already-authorized groups after a restart.
    for row in get_active_groups():
        try:
            entity = await client.get_entity(row["chat_id"])
            group_entities[row["chat_id"]] = entity
            group_titles[row["chat_id"]] = (
                getattr(entity, "title", None) or row["title"]
            )
            group_states[row["chat_id"]] = {
                "call_id": None,
                "active_users": {},
                "last_event_message_id": None,
            }
            await load_group_members(row["chat_id"])
        except Exception as exc:
            print(
                f"Could not restore group {row['chat_id']}: {exc}. "
                "It will be retried by the monitor."
            )

    print("=" * 60)
    print("VC LOGGER IS RUNNING")
    print("TIMEZONE: Asia/Kolkata (IST)")
    print("MULTI-GROUP SETUP: ENABLED")
    print("DM WELCOME / HELP / CONNECT: ENABLED")
    print("=" * 60)

    await asyncio.gather(
        bot_update_loop(),
        vc_monitor_loop(),
        client.run_until_disconnected(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nVC Logger stopped.")
    except Exception as exc:
        print(f"\nFatal error: {exc}")
