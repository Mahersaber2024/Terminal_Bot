"""
Terminal Bot - main entry point
==================================
A standalone Telegram bot: users register their own server(s) and run SSH
commands on them through a live, interactive terminal in the chat. Access
is gated behind joining the configured sponsor channel(s) (see config.py).

Run:
    pip install -r requirements.txt
    cp .env.example .env      # fill in BOT_TOKEN, ADMIN_IDS, SPONSOR_CHANNELS
    python main.py
"""
import logging
import os
import sys

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import handlers
import sponsor_gate
from ServerManager import server_manager_handlers as svm

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def set_bot_commands(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Start / main menu"),
        BotCommand("help", "How this bot works"),
    ])


async def error_handler(update: object, context):
    logger.error("Unhandled exception while processing an update", exc_info=context.error)


def main():
    print("\n" + "=" * 60)
    print("🚀 Terminal Bot starting...")
    print("=" * 60)

    if not config.BOT_TOKEN:
        print("\n❌ BOT_TOKEN is missing. Set it in your environment or .env file.")
        return

    if config.SPONSOR_CHANNELS:
        print(f"🔒 Sponsor gate enabled - {len(config.SPONSOR_CHANNELS)} required channel(s):")
        for ch in config.SPONSOR_CHANNELS:
            print(f"   • {ch['title']} ({ch['id']})")
        print("   Make sure the bot is an ADMIN in each of them, or membership checks will fail.")
    else:
        print("ℹ️ SPONSOR_CHANNELS not set - sponsor gate is disabled, the bot is open to everyone.")

    # Wire ServerManager's "back to main menu" buttons to our keyboard.
    svm.set_get_main_menu(handlers.get_main_menu)

    try:
        # concurrent_updates=True is required for Server Manager's live terminal:
        # the "❌ Cancel" button is a separate update from the one currently being
        # processed (the streaming command), so without this the button tap just
        # queues up and isn't handled until the command finishes on its own.
        application = Application.builder().token(config.BOT_TOKEN).concurrent_updates(True).build()
        print("✅ Bot initialized successfully!")
    except Exception as e:
        print(f"\n❌ Failed to initialize bot: {e}")
        return

    application.post_init = set_bot_commands
    application.add_error_handler(error_handler)

    # ====================== Sponsor channel gate ======================
    # group=-1 runs before every other handler below, for every update type.
    # See sponsor_gate.py for how it decides to let an update through or not.
    application.add_handler(MessageHandler(filters.ALL, sponsor_gate.gate), group=-1)
    application.add_handler(CallbackQueryHandler(sponsor_gate.gate), group=-1)
    application.add_handler(
        CallbackQueryHandler(sponsor_gate.sponsor_check_callback, pattern=f"^{sponsor_gate.SPONSOR_CHECK_CALLBACK}$")
    )

    # ====================== Core commands ======================
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))

    # ====================== Server Manager ======================
    svm.register_handlers(application)

    # Matches the "❌ Cancel" / "🔚 End Session" reply-keyboard buttons shown while
    # a Server Manager conversation (add-server form, or an open SSH command loop)
    # is waiting for text input.
    servermgr_cancel_button = MessageHandler(
        filters.Regex(f"^{svm.CANCEL_BUTTON_TEXT}$|^{svm.CMD_DONE_TEXT}$"), svm.servermgr_cancel
    )

    servermgr_quickping_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm.servermgr_quickping_start, pattern="^servermgr_quickping_start$")],
        states={
            svm.SERVERMGR_QUICKPING_INPUT: [
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_quickping_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm.servermgr_cancel), servermgr_cancel_button],
    )
    application.add_handler(servermgr_quickping_conv)

    servermgr_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm.servermgr_srv_add_start, pattern="^servermgr_srv_add$")],
        states={
            svm.SERVERMGR_LABEL_INPUT: [
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_srv_label_input),
            ],
            svm.SERVERMGR_HOST_INPUT: [
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_srv_host_input),
            ],
            svm.SERVERMGR_PORT_INPUT: [
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_srv_port_input),
            ],
            svm.SERVERMGR_USERNAME_INPUT: [
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_srv_username_input),
            ],
            svm.SERVERMGR_PASSWORD_INPUT: [
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_srv_password_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm.servermgr_cancel), servermgr_cancel_button],
    )
    application.add_handler(servermgr_add_conv)

    servermgr_ssh_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm.servermgr_ssh_start, pattern="^servermgr_ssh_start_")],
        states={
            svm.SERVERMGR_CMD_INPUT: [
                CallbackQueryHandler(svm.servermgr_cmd_cancel_button, pattern=f"^{svm.CMD_CANCEL_CALLBACK}$"),
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_cmd_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm.servermgr_cancel), servermgr_cancel_button],
    )
    application.add_handler(servermgr_ssh_conv)

    print("✅ Handlers registered. Polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
