# Wiring the Web Terminal button into the existing bot

Three small, additive changes. Nothing existing is removed - the inline
button terminal keeps working exactly as before; this just adds a second
door into the same server.

## 1. `config.py` - add the Mini App base URL

```python
# The HTTPS URL where webterminal/frontend/index.html is served from
# (see webterminal/README.md for deployment). Must be https:// - Telegram
# refuses to open a Mini App otherwise.
WEBTERMINAL_URL = os.getenv("WEBTERMINAL_URL", "")
```

## 2. `ServerManager/handlers.py` - add the button

At the top of the file:

```python
from telegram import WebAppInfo
import config
```

In `_terminal_keyboard()`, right next to the existing `⚡ Quick` button:

```python
            rows.append([
                InlineKeyboardButton("✕ Cancel", callback_data=f"{CMD_CANCEL_CALLBACK}_{session_id}"),
                InlineKeyboardButton("⚡ Quick", callback_data=f"{QUICK_MENU_CB_PREFIX}{session_id}"),
            ])
            if config.WEBTERMINAL_URL and session:
                rows.append([
                    InlineKeyboardButton(
                        "🌐 Open Web Terminal",
                        web_app=WebAppInfo(
                            url=f"{config.WEBTERMINAL_URL}?server_id={session['server_id']}",
                        ),
                    ),
                ])
```

`web_app=WebAppInfo(url=...)` is what makes Telegram open it as a Mini App
(in-app WebView) instead of an external browser tab.

## 3. `.env` - point it at your deployed backend

```
WEBTERMINAL_URL=https://terminal.yourdomain.com
```

That's it - no changes to `main.py`'s `ConversationHandler` are needed for
this button itself, since `web_app` buttons don't produce a normal
`callback_query` your bot has to route; Telegram handles the tap by opening
the WebView directly.
