"""
handlers.py
===========
Core, non-Server-Manager command handlers: /start, /help, and the shared
main reply-keyboard menu. ServerManager.server_manager_handlers calls
get_main_menu() (wired up via set_get_main_menu in main.py) whenever it
needs to send the user "back" to this same keyboard.
"""
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ServerManager import server_manager_handlers as svm

HELP_TEXT = (
    "🤖 *Terminal Bot*\n\n"
    "Register your own server(s) and run SSH commands on them right from this chat.\n\n"
    "• Tap *🖥 Server Manager* to add a server or open a live terminal session.\n"
    "• Inside a session, just type commands like you would in a normal terminal - "
    "output streams back live.\n"
    "• Use the *❌ Cancel* / *🔚 End Session* button any time to stop.\n\n"
    "Your server credentials are stored encrypted and only you can see or use them."
)


def get_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[svm.MENU_BUTTON_TEXT]],
        resize_keyboard=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Welcome to *Terminal Bot*!\n\n"
        "Add a server and run SSH commands on it, right here in the chat.\n"
        "Tap *🖥 Server Manager* below to get started, or /help for details."
    )
    chat = update.effective_chat
    await context.bot.send_message(
        chat_id=chat.id,
        text=text,
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=get_main_menu())
