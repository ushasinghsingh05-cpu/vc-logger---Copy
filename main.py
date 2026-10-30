import os
import asyncio
import html
from datetime import datetime

import requests
from dotenv import load_dotenv

from telethon import TelegramClient, events, functions
from telethon.sessions import StringSession
from telethon.tl.types import UpdateGroupCallParticipants


# ============================================================
# LOAD ENVIRONMENT
# ============================================================

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

MONITORED_CHAT_ID = os.getenv("MONITORED_CHAT_ID")
BOT_TOKEN = os.getenv("BOT_TOKEN")
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID")


# ============================================================
# CHECK ENVIRONMENT VARIABLES
# ============================================================

if not API_ID:
    raise ValueError("❌ API_ID is missing")

if not API_HASH:
    raise ValueError("❌ API_HASH is missing")

if not SESSION_STRING:
    raise ValueError("❌ SESSION_STRING is missing")

if not MONITORED_CHAT_ID:
    raise ValueError("❌ MONITORED_CHAT_ID is missing")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN is missing")

if not LOG_CHAT_ID:
    raise ValueError("❌ LOG_CHAT_ID is missing")


API_ID = int(API_ID)
MONITORED_CHAT_ID = int(MONITORED_CHAT_ID)
LOG_CHAT_ID = int(LOG_CHAT_ID)


# ============================================================
# TELETHON CLIENT
# ============================================================

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
)


# ============================================================
# GLOBAL VARIABLES
# ============================================================

chat_entity = None
chat_title = "YOU AND ME"

current_call_id = None

# Currently tracked VC users
active_users = {}

# Cached Telegram users
user_cache = {}


# ============================================================
# TIME FUNCTIONS
# ============================================================

def now():
    return datetime.now()


def format_time(dt):
    return dt.strftime(
        "%d-%m-%Y %I:%M:%S %p"
    )


def format_duration(start_time, end_time):

    seconds = int(
        (end_time - start_time).total_seconds()
    )

    if seconds < 0:
        seconds = 0

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    parts = []

    if hours:
        parts.append(f"{hours}h")

    if minutes:
        parts.append(f"{minutes}m")

    if secs or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)


# ============================================================
# SEND LOG TO TELEGRAM
# ============================================================

# Stores only the latest VC JOINED / VC LEFT log message ID.
# This is intentionally separate from VC STARTED / VC ENDED / ONLINE logs.
last_vc_event_message_id = None


def send_log(message):

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    data = {
        "chat_id": LOG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:

        response = requests.post(
            url,
            data=data,
            timeout=15
        )

        if response.status_code != 200:

            print(
                "❌ Bot API error:",
                response.text
            )
            return None

        result = response.json()

        if not result.get("ok"):
            print(
                "❌ Telegram could not send log:",
                result
            )
            return None

        return result["result"]["message_id"]

    except Exception as e:

        print(
            "❌ Failed to send log:",
            e
        )
        return None


def send_vc_event_log(message):
    """
    Delete the previous JOINED/LEFT notification, then send the new one.
    VC STARTED, VC ENDED and LOGGER ONLINE messages are not affected.
    """

    global last_vc_event_message_id

    if last_vc_event_message_id is not None:

        delete_url = (
            f"https://api.telegram.org/"
            f"bot{BOT_TOKEN}/deleteMessage"
        )

        delete_data = {
            "chat_id": LOG_CHAT_ID,
            "message_id": last_vc_event_message_id,
        }

        try:

            response = requests.post(
                delete_url,
                data=delete_data,
                timeout=15
            )

            result = response.json()

            if response.status_code != 200 or not result.get("ok"):
                print(
                    "⚠️ Could not delete previous VC event log:",
                    result
                )
            else:
                print(
                    f"🗑️ Deleted previous VC event log: "
                    f"{last_vc_event_message_id}"
                )

        except Exception as e:

            print(
                "⚠️ Failed to delete previous VC event log:",
                e
            )

    # Send the new JOINED / LEFT notification and remember its message ID.
    last_vc_event_message_id = None
    new_message_id = send_log(message)

    if new_message_id is not None:
        last_vc_event_message_id = new_message_id


# ============================================================
# USER NAME
# ============================================================

def get_display_name(user):

    if not user:
        return "Unknown"

    first_name = (
        getattr(
            user,
            "first_name",
            None
        )
        or ""
    )

    last_name = (
        getattr(
            user,
            "last_name",
            None
        )
        or ""
    )

    name = (
        f"{first_name} {last_name}"
    ).strip()

    if name:
        return name

    username = getattr(
        user,
        "username",
        None
    )

    if username:
        return username

    return "Unknown"


# ============================================================
# USERNAME
# ============================================================

def get_username(user):

    if not user:
        return None

    username = getattr(
        user,
        "username",
        None
    )

    if username:
        return f"@{username}"

    return None


# ============================================================
# GET USER ID FROM VC PARTICIPANT
# ============================================================

def get_participant_user_id(participant):

    try:

        peer = getattr(
            participant,
            "peer",
            None
        )

        if peer is None:
            return None

        user_id = getattr(
            peer,
            "user_id",
            None
        )

        if user_id:
            return int(user_id)

    except Exception as e:

        print(
            "⚠️ Error extracting participant ID:",
            e
        )

    return None


# ============================================================
# RESOLVE USER
# ============================================================

async def resolve_user(user_id):

    if user_id in user_cache:
        return user_cache[user_id]

    try:

        user = await client.get_entity(
            user_id
        )

        user_cache[user_id] = user

        return user

    except Exception as e:

        print(
            f"⚠️ Could not resolve user "
            f"{user_id}: {e}"
        )

        return None


# ============================================================
# LOAD GROUP MEMBERS
# ============================================================

async def load_group_members():

    print("👥 Loading group members...")

    count = 0

    try:

        async for user in client.iter_participants(
            chat_entity
        ):

            user_cache[user.id] = user

            count += 1

        print(
            f"✅ Loaded {count} group members"
        )

    except Exception as e:

        print(
            "⚠️ Could not load group members:",
            e
        )


# ============================================================
# GET CURRENT VC
# ============================================================

async def get_current_call():

    try:

        if (
            getattr(
                chat_entity,
                "broadcast",
                False
            )
            or
            getattr(
                chat_entity,
                "megagroup",
                False
            )
        ):

            full = await client(
                functions.channels.GetFullChannelRequest(
                    channel=chat_entity
                )
            )

            return full.full_chat.call

        else:

            full = await client(
                functions.messages.GetFullChatRequest(
                    chat_id=chat_entity.id
                )
            )

            return full.full_chat.call

    except Exception as e:

        print(
            "⚠️ Error getting current VC:",
            e
        )

        return None


# ============================================================
# GET CURRENT VC PARTICIPANTS
# ============================================================

async def get_current_participants(call):

    if not call:
        return []

    try:

        result = await client(
            functions.phone.GetGroupCallRequest(
                call=call,
                limit=100
            )
        )

        return result.participants

    except Exception as e:

        print(
            "⚠️ Error getting VC participants:",
            e
        )

        return []


# ============================================================
# REGISTER JOIN
# ============================================================

async def register_join(
    user_id,
    joined_time=None
):

    if not user_id:
        return

    # Already inside
    if user_id in active_users:
        return

    user = await resolve_user(
        user_id
    )

    name = get_display_name(
        user
    )

    username = get_username(
        user
    )

    joined_time = (
        joined_time
        or now()
    )

    # Save active user
    active_users[user_id] = {
        "name": name,
        "username": username,
        "joined": joined_time,
    }

    username_text = (
        html.escape(username)
        if username
        else "No username"
    )

    message = (
        "🟢 <b>VC JOINED</b>\n\n"

        f"👤 <b>Name:</b> "
        f"{html.escape(name)}\n"

        f"🔗 <b>Username:</b> "
        f"{username_text}\n"

        f"🆔 <b>User ID:</b> "
        f"<code>{user_id}</code>\n\n"

        f"🕐 <b>Joined:</b> "
        f"{format_time(joined_time)}\n\n"

        f"🎙️ <b>VC:</b> "
        f"{html.escape(chat_title)}"
    )

    send_vc_event_log(message)

    print(
        f"🟢 JOIN | "
        f"{name} | "
        f"{username or 'No username'} | "
        f"{user_id}"
    )


# ============================================================
# REGISTER LEAVE
# ============================================================

async def register_leave(
    user_id,
    left_time=None
):

    if not user_id:
        return

    # User is not being tracked
    if user_id not in active_users:

        print(
            f"⚠️ Leave detected for "
            f"untracked user: {user_id}"
        )

        return

    user_data = active_users.pop(
        user_id
    )

    name = user_data["name"]
    username = user_data["username"]
    joined_time = user_data["joined"]

    left_time = (
        left_time
        or now()
    )

    duration = format_duration(
        joined_time,
        left_time
    )

    username_text = (
        html.escape(username)
        if username
        else "No username"
    )

    message = (
        "🔴 <b>VC LEFT</b>\n\n"

        f"👤 <b>Name:</b> "
        f"{html.escape(name)}\n"

        f"🔗 <b>Username:</b> "
        f"{username_text}\n"

        f"🆔 <b>User ID:</b> "
        f"<code>{user_id}</code>\n\n"

        f"🟢 <b>Joined:</b> "
        f"{format_time(joined_time)}\n"

        f"🔴 <b>Left:</b> "
        f"{format_time(left_time)}\n"

        f"⏱️ <b>Duration:</b> "
        f"{duration}\n\n"

        f"🎙️ <b>VC:</b> "
        f"{html.escape(chat_title)}"
    )

    send_vc_event_log(message)

    print(
        f"🔴 LEFT | "
        f"{name} | "
        f"{username or 'No username'} | "
        f"{user_id} | "
        f"Duration: {duration}"
    )


# ============================================================
# VC STARTED
# ============================================================

async def handle_vc_started(call):

    global current_call_id
    global active_users

    if not call:
        return

    current_call_id = call.id

    active_users.clear()

    start_time = now()

    print(
        f"🟢 VC STARTED | "
        f"Call ID: {call.id}"
    )

    # --------------------------------------------------------
    # VC START MESSAGE
    # --------------------------------------------------------

    message = (
        "🟢 <b>VOICE CHAT STARTED</b>\n\n"

        f"🎙️ <b>VC:</b> "
        f"{html.escape(chat_title)}\n"

        f"🆔 <b>Call ID:</b> "
        f"<code>{call.id}</code>\n"

        f"🕐 <b>Started:</b> "
        f"{format_time(start_time)}"
    )

    send_log(message)

    # --------------------------------------------------------
    # GET CURRENT PARTICIPANTS
    # --------------------------------------------------------

    participants = await get_current_participants(
        call
    )

    print(
        f"👥 Current VC participants: "
        f"{len(participants)}"
    )

    # --------------------------------------------------------
    # REGISTER CURRENT PARTICIPANTS
    # --------------------------------------------------------

    for participant in participants:

        user_id = get_participant_user_id(
            participant
        )

        if not user_id:

            print(
                "⚠️ Could not get user ID "
                "from participant"
            )

            continue

        await register_join(
            user_id,
            start_time
        )


# ============================================================
# VC ENDED
# ============================================================

async def handle_vc_ended():

    global current_call_id

    if current_call_id is None:
        return

    end_time = now()

    print("🔴 VC ENDED")

    # --------------------------------------------------------
    # CLOSE EVERYONE STILL INSIDE
    # --------------------------------------------------------

    remaining_users = list(
        active_users.keys()
    )

    for user_id in remaining_users:

        await register_leave(
            user_id,
            end_time
        )

    # --------------------------------------------------------
    # VC END MESSAGE
    # --------------------------------------------------------

    message = (
        "🔴 <b>VOICE CHAT ENDED</b>\n\n"

        f"🎙️ <b>VC:</b> "
        f"{html.escape(chat_title)}\n"

        f"🕐 <b>Ended:</b> "
        f"{format_time(end_time)}"
    )

    send_log(message)

    current_call_id = None

    active_users.clear()


# ============================================================
# REAL-TIME TELEGRAM VC EVENT
# ============================================================

@client.on(events.Raw)
async def vc_participant_update(update):

    global current_call_id

    if not isinstance(
        update,
        UpdateGroupCallParticipants
    ):
        return

    if current_call_id is None:
        return

    try:

        update_call_id = update.call.id

    except Exception:

        return

    if update_call_id != current_call_id:
        return

    print(
        f"📡 Telegram VC update received: "
        f"{len(update.participants)} participant(s)"
    )

    for participant in update.participants:

        user_id = get_participant_user_id(
            participant
        )

        if not user_id:
            continue

        just_joined = getattr(
            participant,
            "just_joined",
            False
        )

        just_left = getattr(
            participant,
            "just_left",
            False
        )

        print(
            f"👤 User {user_id} | "
            f"joined={just_joined} | "
            f"left={just_left}"
        )

        # JOIN
        if just_joined and not just_left:

            await register_join(
                user_id,
                now()
            )

        # LEAVE
        elif just_left:

            await register_leave(
                user_id,
                now()
            )


# ============================================================
# MAIN VC MONITOR
# ============================================================

async def vc_monitor():

    global current_call_id

    print("🔎 VC monitor started")

    while True:

        try:

            # ------------------------------------------------
            # GET CURRENT VC
            # ------------------------------------------------

            call = await get_current_call()

            # ------------------------------------------------
            # NO VC
            # ------------------------------------------------

            if not call:

                if current_call_id is not None:

                    await handle_vc_ended()

                await asyncio.sleep(5)

                continue

            # ------------------------------------------------
            # NEW VC
            # ------------------------------------------------

            if current_call_id is None:

                print(
                    f"🟢 New VC detected | "
                    f"Call ID: {call.id}"
                )

                await handle_vc_started(
                    call
                )

                await asyncio.sleep(5)

                continue

            # ------------------------------------------------
            # DIFFERENT VC
            # ------------------------------------------------

            if current_call_id != call.id:

                print(
                    "🔄 Different VC detected"
                )

                await handle_vc_ended()

                await handle_vc_started(
                    call
                )

                await asyncio.sleep(5)

                continue

            # ------------------------------------------------
            # SAME VC
            # CHECK ACTUAL PARTICIPANTS
            # ------------------------------------------------

            participants = await get_current_participants(
                call
            )

            current_user_ids = set()

            for participant in participants:

                user_id = get_participant_user_id(
                    participant
                )

                if user_id:

                    current_user_ids.add(
                        user_id
                    )

            # ------------------------------------------------
            # DETECT NEW USERS
            # ------------------------------------------------

            for user_id in current_user_ids:

                if user_id not in active_users:

                    print(
                        f"🟢 PARTICIPANT JOINED | "
                        f"User ID: {user_id}"
                    )

                    await register_join(
                        user_id,
                        now()
                    )

            # ------------------------------------------------
            # DETECT USERS WHO LEFT
            # ------------------------------------------------

            tracked_users = list(
                active_users.keys()
            )

            for user_id in tracked_users:

                if user_id not in current_user_ids:

                    print(
                        f"🔴 PARTICIPANT LEFT | "
                        f"User ID: {user_id}"
                    )

                    await register_leave(
                        user_id,
                        now()
                    )

            # ------------------------------------------------
            # DEBUG
            # ------------------------------------------------

            print(
                f"👥 VC participants: "
                f"{len(current_user_ids)} | "
                f"Tracked: "
                f"{len(active_users)}"
            )

        except Exception as e:

            print(
                f"⚠️ VC monitor error: {e}"
            )

        # Check every 5 seconds
        await asyncio.sleep(5)


# ============================================================
# MAIN PROGRAM
# ============================================================

async def main():

    global chat_entity
    global chat_title

    print("=" * 60)
    print("🚀 STARTING TELEGRAM VC LOGGER")
    print("=" * 60)

    # --------------------------------------------------------
    # CONNECT TELEGRAM
    # --------------------------------------------------------

    print(
        "🔐 Connecting Telegram account..."
    )

    await client.start()

    me = await client.get_me()

    me_name = get_display_name(
        me
    )

    print(
        f"✅ Telegram account connected: "
        f"{me_name} "
        f"(ID: {me.id})"
    )

    # --------------------------------------------------------
    # GET GROUP
    # --------------------------------------------------------

    print(
        f"🎯 Loading monitored group: "
        f"{MONITORED_CHAT_ID}"
    )

    chat_entity = await client.get_entity(
        MONITORED_CHAT_ID
    )

    chat_title = (
        getattr(
            chat_entity,
            "title",
            None
        )
        or "YOU AND ME"
    )

    print(
        f"✅ Monitoring group: "
        f"{chat_title}"
    )

    # --------------------------------------------------------
    # LOAD GROUP MEMBERS
    # --------------------------------------------------------

    await load_group_members()

    # --------------------------------------------------------
    # TEST BOT
    # --------------------------------------------------------

    print(
        "🤖 Testing log bot..."
    )

    test_message = (
        "🟢 <b>VC LOGGER ONLINE</b>\n\n"

        f"👥 <b>Group:</b> "
        f"{html.escape(chat_title)}\n"

        f"🆔 <b>Group ID:</b> "
        f"<code>{MONITORED_CHAT_ID}</code>\n"

        f"🕐 <b>Started:</b> "
        f"{format_time(now())}\n\n"

        "📡 <b>Real-time VC monitoring "
        "is active.</b>"
    )

    send_log(test_message)

    # --------------------------------------------------------
    # START MONITOR
    # --------------------------------------------------------

    asyncio.create_task(
        vc_monitor()
    )

    print("=" * 60)
    print("🟢 VC LOGGER IS RUNNING 24/7")
    print("=" * 60)

    # Keep connection alive
    await client.run_until_disconnected()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        print(
            "\n🛑 VC Logger stopped."
        )

    except Exception as e:

        print(
            f"\n❌ Fatal error: {e}"
        )
