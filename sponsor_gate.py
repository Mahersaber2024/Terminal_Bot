import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

import config

logger = logging.getLogger(__name__)

SPONSOR_CHECK_CALLBACK = "sponsor_check_membership"

# Telegram chat_member statuses that count as "joined".
_JOINED_STATUSES = {"member", "administrator", "creator"}


def _channel_link(channel: dict) -> str:
    link = config.SPONSOR_CHANNEL_LINKS.get(str(channel["id"]))
    if link:
        return link
    cid = channel["id"]
    if isinstance(cid, str) and cid.startswith("@"):
        return f"https://t.me/{cid.lstrip('@')}"
    return channel["title"]


async def _is_member(bot, channel_id, user_id) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in _JOINED_STATUSES
    except TelegramError as e:
        logger.warning(f"Sponsor check failed for channel {channel_id}: {e}")
        return False


async def _missing_channels(bot, user_id):
    missing = []
    for channel in config.SPONSOR_CHANNELS:
        if not await _is_member(bot, channel["id"], user_id):
            missing.append(channel)
    return missing


def _prompt_keyboard(missing):
    rows = [[InlineKeyboardButton(f"➕ {ch['title']}", url=_channel_link(ch))] for ch in missing]
    rows.append([InlineKeyboardButton("✅ I've joined", callback_data=SPONSOR_CHECK_CALLBACK)])
    return InlineKeyboardMarkup(rows)


def _prompt_text(missing):
    lines = ["🔒 To use this bot, please join the following channel(s) first:", ""]
    lines += [f"• {ch['title']}" for ch in missing]
    lines += ["", "Then tap \"✅ I've joined\" below."]
    return "\n".join(lines)


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config.SPONSOR_CHANNELS:
        return  # gate disabled

    user = update.effective_user
    if user is None:
        return

    missing = await _missing_channels(context.bot, user.id)
    if not missing:
        return  # all good, let the real handlers run

    text = _prompt_text(missing)
    markup = _prompt_keyboard(missing)

    if update.callback_query:
        try:
            await update.callback_query.answer("Please join the required channel(s) first.", show_alert=True)
        except TelegramError:
            pass
        try:
            await update.callback_query.message.reply_text(text, reply_markup=markup)
        except TelegramError:
            pass
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup)

    raise ApplicationHandlerStop


async def sponsor_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    missing = await _missing_channels(context.bot, user.id)

    if missing:
        await query.answer("Still missing at least one channel.", show_alert=True)
        try:
            await query.edit_message_text(_prompt_text(missing), reply_markup=_prompt_keyboard(missing))
        except TelegramError:
            pass
        return

    await query.answer("✅ Thanks! Access granted.")
    try:
        await query.edit_message_text("✅ Membership confirmed - you're all set!")
    except TelegramError:
        pass
    import handlers
    await handlers.start(update, context)
