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

logger = logging.getLogger(__name__)

# ====================== Conversation states ======================
ADMIN_CHANNEL_ADD_ID, ADMIN_CHANNEL_ADD_TITLE, ADMIN_CHANNEL_ADD_LINK = range(3)

CANCEL_BUTTON_TEXT = "❌ Cancel"


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# ====================== Shared main-menu wiring ======================
# Wired up from main.py the same way handlers.get_main_menu is wired into
# ServerManager (see main.py: svm.set_get_main_menu), so this module doesn't
# need to import handlers.py directly and risk a circular import.
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

    keyboard = [[InlineKeyboardButton("📢 Sponsor Channel Settings", callback_data="admin_channel_settings")]]
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

    keyboard = [[InlineKeyboardButton("📢 Sponsor Channel Settings", callback_data="admin_channel_settings")]]
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
