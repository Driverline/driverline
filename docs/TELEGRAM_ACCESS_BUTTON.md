# Telegram: pinned "Open TradeLens App" button

A one-off, idempotent script that publishes and pins the TradeLens access message (with the button) in the Telegram channel and its linked
discussion group. It is **not** part of the web app: the server has no Telegram code and no bot token (signals are posted by the Driverline
Companion EA on the admin's MT5 terminal). The script only calls Telegram's Bot API, adds no server process, webhook, polling, table or
dependency, and does not touch signal publishing.

## Permissions the bot (@TradeLensagent) needs
- Channel: administrator with **Post messages** and **Edit messages of others** (Telegram requires the latter to pin in a channel).
- Linked group: administrator with **Pin messages**. The bot must be in the group to post.
The signal posting only needs "Post messages", so these extra rights are what you may need to add. Missing rights are named in the output.

## Run it (Termux or any computer with Python 3; nothing to install)
```
pkg install python          # Termux only, once
cd ~/driverline
read -s TELEGRAM_BOT_TOKEN; export TELEGRAM_BOT_TOKEN      # paste the token, press Enter (nothing is shown or saved)
python3 scripts/telegram_access_button.py --channel @YourChannelUsername            # check only
python3 scripts/telegram_access_button.py --channel @YourChannelUsername --apply    # publish + pin
```
Use the same token as the EA's `InpTgToken`. A private channel can use its numeric id (-100...) instead of @username.

## How it works
- The linked group is found from Telegram (`linked_chat_id` of the channel). If it cannot be found it says so; give `--group <id or @name>`. It never guesses.
- Message ids are stored in `~/.tradelens_telegram_access.json` (no token inside). Running again never duplicates: it confirms the stored
  message; if that file is lost it recognises its own pinned message; if a message was deleted it publishes it again once.
- A normal run pins only newly created messages and leaves pins other admins made later alone. `--repin` pins ours again. Changing `--url`
  edits the existing message in place.
- Pinning is silent (no notification). Exit code 0 = all done, 1 = something needs attention (printed).

## Rollback
Unpin or delete the two messages in Telegram. Nothing else was changed.
