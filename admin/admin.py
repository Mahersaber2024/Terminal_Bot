"""
admin.py
"""
import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

import bot_settings
import config
import subscription
from db.database import get_db
from ServerManager import health as svm_health

logger = logging.getLogger(__name__)

# ====================== Conversation states ======================
ADMIN_CHANNEL_ADD_ID, ADMIN_CHANNEL_ADD_TITLE, ADMIN_CHANNEL_ADD_LINK = range(3)
(
    ADMIN_PLAN_ADD_NAME,
    ADMIN_PLAN_ADD_PRICE,
    ADMIN_PLAN_ADD_DAYS,
    ADMIN_PLAN_ADD_MAXSERVERS,
    ADMIN_PLAN_ADD_MAXTABS,
    ADMIN_PLAN_ADD_DESC,
) = range(3, 9)
ADMIN_CARD_NUMBER, ADMIN_CARD_HOLDER, ADMIN_CARD_BANK = range(9, 12)
(
    ADMIN_MON_INTERVAL,
    ADMIN_MON_TIMEOUT,
    ADMIN_MON_DISKPCT,
    ADMIN_MON_HYSTERESIS,
) = range(12, 16)
ADMIN_USER_SEARCH, ADMIN_USER_BALANCE_ADJUST = range(16, 18)
USERS_PAGE_SIZE = 10

CANCEL_BUTTON_TEXT = "❌ Cancel"


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# ====================== Shared main-menu wiring ======================
# Wired up from main.py the same way get_main_menu is wired into
# ServerManager (see main.py: svm.set_get_main_menu), so this module doesn't
# need to import main.py directly and risk a circular import.
_get_main_menu_func = None


def set_get_main_menu(func):
    global _get_main_menu_func
    _get_main_menu_func = func


def get_main_menu():
    if _get_main_menu_func:
        return _get_main_menu_func()
    return ReplyKeyboardMarkup([[]], resize_keyboard=True)


def _cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(CANCEL_BUTTON_TEXT)]], resize_keyboard=True)


async def _edit_then_prompt_cancel(query, text: str):
    """Edit the callback-query message with `text`, then send a small
    follow-up message carrying the Cancel reply keyboard - Telegram doesn't
    allow a ReplyKeyboardMarkup on an edited message, only inline or none."""
    try:
        await query.edit_message_text(text)
    except BadRequest as e:
        logger.warning(f"_edit_then_prompt_cancel: edit failed ({e}); sending a new message instead")
        await query.message.reply_text(text)
    await query.message.reply_text("👇 Tap below to cancel:", reply_markup=_cancel_kb())


async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback for /cancel or the "❌ Cancel" reply-keyboard button while an
    admin conversation (add channel) is waiting for text input."""
    await update.message.reply_text("❌ Operation cancelled.", reply_markup=get_main_menu())
    return ConversationHandler.END


# ====================== Admin panel entry ======================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔️ You do not have admin access.")
        return

    keyboard = [
        [InlineKeyboardButton("👥 Manage Users", callback_data="admin_users_menu")],
        [InlineKeyboardButton("📢 Sponsor Channel Settings", callback_data="admin_channel_settings")],
        [InlineKeyboardButton("📦 Manage Plans", callback_data="admin_plans_menu")],
        [InlineKeyboardButton("💳 Payment Settings", callback_data="admin_payment_settings")],
        [InlineKeyboardButton("🩺 Monitoring Settings", callback_data="admin_monitoring_settings")],
    ]
    await update.message.reply_text(
        "🛠 Admin Panel\n\nPlease select an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ====================== Sponsor channel settings ======================

def _channel_settings_text_and_keyboard():
    channels = bot_settings.get_sponsor_channels()
    required = bot_settings.is_membership_required()
    status_text = "✅ Enabled (mandatory)" if required else "❌ Disabled (not required)"

    if channels:
        channel_lines = "\n".join(f"• {ch['title']} ({ch['id']})" for ch in channels)
    else:
        channel_lines = "(none configured)"

    text = (
        f"📢 Sponsor Channel Settings\n\n"
        f"🔗 Current channel(s):\n{channel_lines}\n\n"
        f"🔰 Membership check status: {status_text}\n\n"
        f"Use the buttons below to add or remove a channel, or toggle the requirement."
    )

    keyboard = [
        [InlineKeyboardButton("➕ Add Channel", callback_data="admin_channel_add")],
    ]
    if channels:
        keyboard.append([InlineKeyboardButton("➖ Remove Channel", callback_data="admin_channel_remove")])
    keyboard.append([InlineKeyboardButton(
        "🔴 Disable Membership Check" if required else "🟢 Enable Membership Check",
        callback_data="admin_channel_toggle",
    )])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back_to_main")])
    return text, InlineKeyboardMarkup(keyboard)


async def admin_channel_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return

    text, reply_markup = _channel_settings_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


async def admin_back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("👥 Manage Users", callback_data="admin_users_menu")],
        [InlineKeyboardButton("📢 Sponsor Channel Settings", callback_data="admin_channel_settings")],
        [InlineKeyboardButton("📦 Manage Plans", callback_data="admin_plans_menu")],
        [InlineKeyboardButton("💳 Payment Settings", callback_data="admin_payment_settings")],
        [InlineKeyboardButton("🩺 Monitoring Settings", callback_data="admin_monitoring_settings")],
    ]
    await query.edit_message_text(
        "🛠 Admin Panel\n\nPlease select an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def admin_channel_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle the membership requirement on/off."""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("⛔️ No access.", show_alert=True)
        return

    new_value = not bot_settings.is_membership_required()
    bot_settings.set_membership_required(new_value)
    await query.answer("✅ Status updated.")

    logger.info(
        f"Admin {query.from_user.id} set membership_required={new_value}"
    )

    text, reply_markup = _channel_settings_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


# ====================== Add channel ======================

async def admin_channel_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return ConversationHandler.END

    await _edit_then_prompt_cancel(
        query,
        "📢 Add Sponsor Channel\n\n"
        "Please send the channel's numeric id (e.g. -1001234567890) or its "
        "@username.\n\n"
        "⚠️ Note: the bot must be an admin in the target channel to check "
        "membership.",
    )
    return ADMIN_CHANNEL_ADD_ID


async def admin_channel_add_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text(
            "❌ Invalid value. Please send the channel id/username again, or tap Cancel.",
            reply_markup=_cancel_kb(),
        )
        return ADMIN_CHANNEL_ADD_ID

    context.user_data["admin_new_channel_id"] = text
    await update.message.reply_text(
        "🏷 Now send a display title for this channel (shown to users in the "
        "join prompt), or send \"-\" to just use the id/username.",
        reply_markup=_cancel_kb(),
    )
    return ADMIN_CHANNEL_ADD_TITLE


async def admin_channel_add_title_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    title = "" if text == "-" else text
    context.user_data["admin_new_channel_title"] = title

    await update.message.reply_text(
        "🔗 Optionally send an invite link (e.g. https://t.me/mychannel) to "
        "show on the \"Join\" button, or send \"-\" to skip (falls back to "
        "the @username / title).",
        reply_markup=_cancel_kb(),
    )
    return ADMIN_CHANNEL_ADD_LINK


async def admin_channel_add_link_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    link = "" if text == "-" else text

    channel_id = context.user_data.pop("admin_new_channel_id", "")
    title = context.user_data.pop("admin_new_channel_title", "")

    channel = bot_settings.add_sponsor_channel(channel_id, title, link)
    logger.info(f"Admin {update.effective_user.id} added sponsor channel: {channel}")

    await update.message.reply_text(
        f"✅ Channel added!\n\n"
        f"🔗 {channel['title']} ({channel['id']})\n\n"
        f"⚠️ Make sure the bot is an admin in this channel, otherwise "
        f"membership checks will fail.",
        reply_markup=get_main_menu(),
    )
    return ConversationHandler.END


# ====================== Remove channel ======================

async def admin_channel_remove_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return

    channels = bot_settings.get_sponsor_channels()
    if not channels:
        await query.edit_message_text(
            "No channels configured.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="admin_channel_settings")]]
            ),
        )
        return

    keyboard = [
        [InlineKeyboardButton(f"🗑 {ch['title']} ({ch['id']})", callback_data=f"admin_channel_remove_{i}")]
        for i, ch in enumerate(channels)
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_channel_settings")])
    await query.edit_message_text(
        "➖ Remove Channel\n\nTap a channel to remove it:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_channel_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("⛔️ No access.", show_alert=True)
        return

    index = int(query.data.rsplit("_", 1)[-1])
    channels = bot_settings.get_sponsor_channels()
    removed = channels[index] if 0 <= index < len(channels) else None

    if bot_settings.remove_sponsor_channel(index):
        await query.answer("✅ Channel removed.")
        logger.info(f"Admin {query.from_user.id} removed sponsor channel: {removed}")
    else:
        await query.answer("❌ Could not find that channel.", show_alert=True)

    text, reply_markup = _channel_settings_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


# ====================== Manage Users ======================
# Lists/searches the `users` table (db/database.py), and lets an admin drill
# into a single user to see their wallet balance, subscription status, and
# ban/unban them or top up their balance directly (no card-to-card flow).

def _user_summary_line(u: dict) -> str:
    who = u.get("username")
    who = f"@{who}" if who else (u.get("first_name") or str(u["user_id"]))
    banned = " 🚫" if u.get("is_banned") else ""
    return f"• {who} (id: {u['user_id']}){banned}"


def _users_list_keyboard(users: list, offset: int, total: int, search: str = None):
    rows = [
        [InlineKeyboardButton(_user_summary_line(u), callback_data=f"admin_user_view_{u['user_id']}")]
        for u in users
    ]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_users_page_{max(0, offset - USERS_PAGE_SIZE)}"))
    if offset + USERS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"admin_users_page_{offset + USERS_PAGE_SIZE}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔍 Search by id / username", callback_data="admin_user_search_start")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back_to_main")])
    return InlineKeyboardMarkup(rows)


async def admin_users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return

    db = get_db()
    total = db.count_users()
    users = db.list_users(limit=USERS_PAGE_SIZE, offset=0)
    text = f"👥 Users\n\nTotal registered: {total}\n\nTap a user to view details, or search below."
    if not users:
        text = "👥 Users\n\nNobody has started the bot yet."
    await query.edit_message_text(text, reply_markup=_users_list_keyboard(users, 0, total))


async def admin_users_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return

    offset = int(query.data.replace("admin_users_page_", ""))
    search = context.user_data.get("admin_user_search")
    db = get_db()
    total = db.count_users()
    users = db.list_users(limit=USERS_PAGE_SIZE, offset=offset, search=search)
    text = f"👥 Users\n\nTotal registered: {total}\n\nTap a user to view details, or search below."
    await query.edit_message_text(text, reply_markup=_users_list_keyboard(users, offset, total, search))


async def admin_user_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return ConversationHandler.END

    context.user_data.pop("admin_user_search", None)
    await _edit_then_prompt_cancel(query, "🔍 Send a numeric user id, or a @username to search for:")
    return ADMIN_USER_SEARCH


async def admin_user_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Please send a user id or @username, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_USER_SEARCH

    context.user_data["admin_user_search"] = text
    db = get_db()
    total_all = db.count_users()
    users = db.list_users(limit=USERS_PAGE_SIZE, offset=0, search=text)
    if not users:
        await update.message.reply_text(
            f"❌ No users matched \"{text}\".", reply_markup=get_main_menu()
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"👥 Results for \"{text}\":",
        reply_markup=_users_list_keyboard(users, 0, total_all, text),
    )
    return ConversationHandler.END


def _user_detail_text(u: dict) -> str:
    who = f"@{u['username']}" if u.get("username") else (u.get("first_name") or "(no name)")
    balance = subscription.get_balance(u["user_id"])
    lines = [
        f"👤 {who}",
        f"🆔 ID: {u['user_id']}",
        f"📅 Joined: {u['created_at']}",
        f"🚦 Status: {'🚫 Banned' if u.get('is_banned') else '✅ Active'}",
        f"💰 Wallet balance: {balance:,}",
    ]
    if subscription.is_active(u["user_id"]):
        sub = subscription.get_subscription(u["user_id"])
        lines.append(f"📦 Plan: {sub['plan_name']} ({subscription.days_remaining(u['user_id'])}d left)")
    else:
        lines.append("📦 Plan: none active")
    return "\n".join(lines)


def _user_detail_keyboard(u: dict):
    ban_btn = (
        InlineKeyboardButton("🟢 Unban", callback_data=f"admin_user_unban_{u['user_id']}")
        if u.get("is_banned")
        else InlineKeyboardButton("🚫 Ban", callback_data=f"admin_user_ban_{u['user_id']}")
    )
    return InlineKeyboardMarkup([
        [ban_btn],
        [InlineKeyboardButton("💰 Adjust balance", callback_data=f"admin_user_balance_{u['user_id']}")],
        [InlineKeyboardButton("🔙 Back to list", callback_data="admin_users_menu")],
    ])


async def admin_user_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return

    user_id = int(query.data.replace("admin_user_view_", ""))
    u = get_db().get_user(user_id)
    if not u:
        await query.answer("❌ User not found.", show_alert=True)
        return
    await query.edit_message_text(_user_detail_text(u), reply_markup=_user_detail_keyboard(u))


async def admin_user_ban_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔️ No access.", show_alert=True)
        return

    banned = query.data.startswith("admin_user_ban_")
    prefix = "admin_user_ban_" if banned else "admin_user_unban_"
    user_id = int(query.data.replace(prefix, ""))

    db = get_db()
    db.set_banned(user_id, banned)
    u = db.get_user(user_id)
    await query.answer("🚫 User banned." if banned else "🟢 User unbanned.")
    await query.edit_message_text(_user_detail_text(u), reply_markup=_user_detail_keyboard(u))
    logger.info(f"Admin {query.from_user.id} {'banned' if banned else 'unbanned'} user {user_id}")


async def admin_user_balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return ConversationHandler.END

    user_id = int(query.data.replace("admin_user_balance_", ""))
    context.user_data["admin_balance_target"] = user_id
    await _edit_then_prompt_cancel(
        query,
        "💰 Send the amount to add to this user's wallet.\n"
        "Use a negative number to deduct (e.g. -5000).",
    )
    return ADMIN_USER_BALANCE_ADJUST


async def admin_user_balance_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    try:
        amount = int(text)
    except ValueError:
        await update.message.reply_text("❌ Please send a whole number (e.g. 5000 or -2000), or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_USER_BALANCE_ADJUST

    user_id = context.user_data.pop("admin_balance_target", None)
    if not user_id:
        await update.message.reply_text("❌ Something went wrong, please try again.", reply_markup=get_main_menu())
        return ConversationHandler.END

    new_balance = subscription.update_balance(user_id, amount)
    subscription.add_transaction(user_id, amount, "admin_adjustment", f"Adjusted by admin {update.effective_user.id}")

    await update.message.reply_text(
        f"✅ Done.\n💰 New balance for {user_id}: {new_balance:,}", reply_markup=get_main_menu()
    )
    logger.info(f"Admin {update.effective_user.id} adjusted balance of {user_id} by {amount}")

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"💰 Your wallet balance was adjusted by an admin: {amount:+,}\nNew balance: {new_balance:,}",
        )
    except Exception as e:
        logger.warning(f"Could not notify user {user_id} of balance adjustment: {e}")

    return ConversationHandler.END


# ====================== Manage Plans ======================
# Plans are created entirely by hand here (name, price, days, max servers,
# max concurrent tabs) - nothing is auto-seeded. See subscription.py.

def _plans_menu_text_and_keyboard():
    all_plans = subscription.get_all_plans(active_only=False)
    if all_plans:
        lines = []
        for p in all_plans.values():
            status = "✅" if p.get("enabled", True) else "❌"
            lines.append(
                f"{status} {p['name']} - {p['price']:,} / {p['days']}d "
                f"(🖥{p['max_servers']} 📑{p['max_tabs']})"
            )
        plan_lines = "\n".join(lines)
    else:
        plan_lines = "(no plans yet)"

    text = f"📦 Plans\n\n{plan_lines}\n\nTap a plan below to toggle or delete it."

    keyboard = [[InlineKeyboardButton("➕ Add Plan", callback_data="admin_plan_add")]]
    for pid, p in all_plans.items():
        status = "🟢" if p.get("enabled", True) else "🔴"
        keyboard.append([
            InlineKeyboardButton(f"{status} {p['name']}", callback_data=f"admin_plan_toggle_{pid}"),
            InlineKeyboardButton("🗑", callback_data=f"admin_plan_delete_{pid}"),
        ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back_to_main")])
    return text, InlineKeyboardMarkup(keyboard)


async def admin_plans_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return
    text, reply_markup = _plans_menu_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


async def admin_plan_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔️ No access.", show_alert=True)
        return
    plan_id = query.data.replace("admin_plan_toggle_", "")
    if subscription.toggle_plan(plan_id):
        await query.answer("✅ Updated.")
    else:
        await query.answer("❌ Plan not found.", show_alert=True)
    text, reply_markup = _plans_menu_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


async def admin_plan_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔️ No access.", show_alert=True)
        return
    plan_id = query.data.replace("admin_plan_delete_", "")
    if subscription.delete_plan(plan_id):
        await query.answer("🗑 Deleted.")
        logger.info(f"Admin {query.from_user.id} deleted plan {plan_id}")
    else:
        await query.answer("❌ Plan not found.", show_alert=True)
    text, reply_markup = _plans_menu_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


# ====================== Add plan (step-by-step wizard) ======================

async def admin_plan_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return ConversationHandler.END
    context.user_data["admin_new_plan"] = {}
    await _edit_then_prompt_cancel(query, "📦 Add Plan\n\nSend the plan's name:")
    return ADMIN_PLAN_ADD_NAME


async def admin_plan_add_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Please send a name, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_NAME
    context.user_data["admin_new_plan"]["name"] = text
    await update.message.reply_text("💰 Send the price (a plain number):", reply_markup=_cancel_kb())
    return ADMIN_PLAN_ADD_PRICE


async def admin_plan_add_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid positive number, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_PRICE
    context.user_data["admin_new_plan"]["price"] = int(text)
    await update.message.reply_text("⏳ Send the duration in days:", reply_markup=_cancel_kb())
    return ADMIN_PLAN_ADD_DAYS


async def admin_plan_add_days_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Please send a valid number of days, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_DAYS
    context.user_data["admin_new_plan"]["days"] = int(text)
    await update.message.reply_text("🖥 Max number of servers allowed on this plan:", reply_markup=_cancel_kb())
    return ADMIN_PLAN_ADD_MAXSERVERS


async def admin_plan_add_maxservers_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Please send a valid positive number, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_MAXSERVERS
    context.user_data["admin_new_plan"]["max_servers"] = int(text)
    await update.message.reply_text("📑 Max concurrent terminal tabs on this plan:", reply_markup=_cancel_kb())
    return ADMIN_PLAN_ADD_MAXTABS


async def admin_plan_add_maxtabs_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Please send a valid positive number, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_MAXTABS
    context.user_data["admin_new_plan"]["max_tabs"] = int(text)
    await update.message.reply_text(
        "📝 Send a short description, or \"-\" to skip:", reply_markup=_cancel_kb()
    )
    return ADMIN_PLAN_ADD_DESC


async def admin_plan_add_desc_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    description = "" if text == "-" else text

    data = context.user_data.pop("admin_new_plan", {})
    plan_id = subscription.add_plan(
        name=data["name"],
        price=data["price"],
        days=data["days"],
        max_servers=data["max_servers"],
        max_tabs=data["max_tabs"],
        description=description,
    )
    logger.info(f"Admin {update.effective_user.id} added plan {plan_id}: {data}")

    await update.message.reply_text(
        f"✅ Plan added!\n\n"
        f"📦 {data['name']} - {data['price']:,} / {data['days']}d "
        f"(🖥{data['max_servers']} 📑{data['max_tabs']})",
        reply_markup=get_main_menu(),
    )
    return ConversationHandler.END


# ====================== Payment Settings (card-to-card) ======================

def _payment_settings_text_and_keyboard():
    number = bot_settings.get_card_number() or "(not set)"
    holder = bot_settings.get_card_holder() or "(not set)"
    bank = bot_settings.get_card_bank() or "(not set)"
    text = (
        f"💳 Payment Settings\n\n"
        f"Card number: {number}\n"
        f"Holder: {holder}\n"
        f"Bank: {bank}\n\n"
        f"This is shown to users paying by card-to-card for a subscription or wallet top-up."
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Set Card Info", callback_data="admin_card_set")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_back_to_main")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def admin_payment_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return
    text, reply_markup = _payment_settings_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


async def admin_card_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return ConversationHandler.END
    context.user_data["admin_new_card"] = {}
    await _edit_then_prompt_cancel(query, "💳 Send the card number:")
    return ADMIN_CARD_NUMBER


async def admin_card_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Please send the card number, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_CARD_NUMBER
    context.user_data["admin_new_card"]["number"] = text
    await update.message.reply_text("🏷 Send the card holder's name:", reply_markup=_cancel_kb())
    return ADMIN_CARD_HOLDER


async def admin_card_holder_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Please send the holder's name, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_CARD_HOLDER
    context.user_data["admin_new_card"]["holder"] = text
    await update.message.reply_text("🏦 Send the bank name:", reply_markup=_cancel_kb())
    return ADMIN_CARD_BANK


async def admin_card_bank_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Please send the bank name, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_CARD_BANK
    data = context.user_data.pop("admin_new_card", {})
    bot_settings.set_card_info(card_number=data.get("number", ""), card_holder=data.get("holder", ""), card_bank=text)
    logger.info(f"Admin {update.effective_user.id} updated card payment info")

    await update.message.reply_text("✅ Card info updated!", reply_markup=get_main_menu())
    return ConversationHandler.END


# ====================== Monitoring Settings (health checks / disk alerts) ======================
# These used to be fixed at process start via SERVERMGR_* env vars (see
# ServerManager/health.py); they now live in bot_settings.json and are
# editable here. Changing the interval also reschedules the background
# job via svm_health.reschedule_job() - the job_queue instance is reached
# through context.job_queue, which every handler gets for free, so this
# module doesn't need main.py's Application object.

def _monitoring_settings_text_and_keyboard():
    text = (
        f"🩺 Monitoring Settings\n\n"
        f"⏱ Check interval: {svm_health.get_interval_seconds() / 3600:.1f}h\n"
        f"⌛ Per-server SSH timeout: {svm_health.get_check_timeout()}s\n"
        f"💾 Disk alert threshold: {svm_health.get_disk_alert_percent()}%\n"
        f"📉 Disk alert hysteresis: {svm_health.get_disk_alert_hysteresis()} points\n\n"
        f"Tap a setting below to change it."
    )
    keyboard = [
        [
            InlineKeyboardButton("⏱ Check Interval", callback_data="admin_mon_interval"),
            InlineKeyboardButton("⌛ SSH Timeout", callback_data="admin_mon_timeout"),
        ],
        [
            InlineKeyboardButton("💾 Disk Threshold", callback_data="admin_mon_diskpct"),
            InlineKeyboardButton("📉 Disk Hysteresis", callback_data="admin_mon_hysteresis"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_back_to_main")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def admin_monitoring_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return
    text, reply_markup = _monitoring_settings_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


async def admin_mon_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return ConversationHandler.END
    await _edit_then_prompt_cancel(
        query,
        f"⏱ Send the new check interval in hours (current: {svm_health.get_interval_seconds() / 3600:.1f}h).\n"
        f"Minimum 0.5h (30 min). Decimals are fine, e.g. 1.5.",
    )
    return ADMIN_MON_INTERVAL


async def admin_mon_interval_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        hours = float(text)
    except ValueError:
        hours = None
    if hours is None or hours < 0.5:
        await update.message.reply_text(
            "❌ Please send a number of hours, 0.5 or higher (e.g. 1 or 1.5), or tap Cancel.",
            reply_markup=_cancel_kb(),
        )
        return ADMIN_MON_INTERVAL
    value = round(hours * 3600)
    bot_settings.set_health_interval_seconds(value)
    svm_health.reschedule_job(context.job_queue, value)
    logger.info(f"Admin {update.effective_user.id} set health_interval_seconds={value} ({hours}h)")
    await update.message.reply_text(f"✅ Check interval set to {hours:.1f}h.", reply_markup=get_main_menu())
    return ConversationHandler.END


async def admin_mon_timeout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return ConversationHandler.END
    await _edit_then_prompt_cancel(
        query,
        f"⌛ Send the new per-server SSH timeout in seconds (current: {svm_health.get_check_timeout()}s).\n"
        f"Range 3-120.",
    )
    return ADMIN_MON_TIMEOUT


async def admin_mon_timeout_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or not (3 <= int(text) <= 120):
        await update.message.reply_text(
            "❌ Please send a whole number of seconds between 3 and 120, or tap Cancel.", reply_markup=_cancel_kb()
        )
        return ADMIN_MON_TIMEOUT
    value = int(text)
    bot_settings.set_health_check_timeout(value)
    logger.info(f"Admin {update.effective_user.id} set health_check_timeout={value}")
    await update.message.reply_text(f"✅ SSH timeout set to {value}s.", reply_markup=get_main_menu())
    return ConversationHandler.END


async def admin_mon_diskpct_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return ConversationHandler.END
    await _edit_then_prompt_cancel(
        query,
        f"💾 Send the new disk-full alert threshold as a percent (current: {svm_health.get_disk_alert_percent()}%).\n"
        f"Range 1-100.",
    )
    return ADMIN_MON_DISKPCT


async def admin_mon_diskpct_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 100):
        await update.message.reply_text(
            "❌ Please send a whole number between 1 and 100, or tap Cancel.", reply_markup=_cancel_kb()
        )
        return ADMIN_MON_DISKPCT
    value = int(text)
    bot_settings.set_disk_alert_percent(value)
    logger.info(f"Admin {update.effective_user.id} set disk_alert_percent={value}")
    await update.message.reply_text(f"✅ Disk alert threshold set to {value}%.", reply_markup=get_main_menu())
    return ConversationHandler.END


async def admin_mon_hysteresis_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔️ You do not have admin access.")
        return ConversationHandler.END
    await _edit_then_prompt_cancel(
        query,
        f"📉 Send the new disk alert hysteresis in points (current: {svm_health.get_disk_alert_hysteresis()}).\n"
        f"This is how far disk usage must drop below the threshold before a future "
        f"crossing can alert again - keeps a server hovering near the threshold from "
        f"spamming alerts. Range 0-50.",
    )
    return ADMIN_MON_HYSTERESIS


async def admin_mon_hysteresis_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or not (0 <= int(text) <= 50):
        await update.message.reply_text(
            "❌ Please send a whole number between 0 and 50, or tap Cancel.", reply_markup=_cancel_kb()
        )
        return ADMIN_MON_HYSTERESIS
    value = int(text)
    bot_settings.set_disk_alert_hysteresis(value)
    logger.info(f"Admin {update.effective_user.id} set disk_alert_hysteresis={value}")
    await update.message.reply_text(f"✅ Disk alert hysteresis set to {value} points.", reply_markup=get_main_menu())
    return ConversationHandler.END
