# Telegram Approval Setup

As an alternative to Discord, `schwab-mcp` can require approval for account-modifying tools (placing orders, cancelling orders, etc.) through a Telegram chat. This prevents an LLM from executing trades without your explicit confirmation. Configure either Discord or Telegram, not both.

## 1. Create a Telegram Bot

1.  Open a chat with [@BotFather](https://t.me/BotFather) in Telegram.
2.  Send `/newbot` and follow the prompts to choose a name and username for your bot.
3.  BotFather will reply with a bot token (looks like `123456789:AAExampleTokenValue`). **Copy this token**; you will need it for the `SCHWAB_MCP_TELEGRAM_TOKEN` configuration.

## 2. Get Your Chat ID

You need the ID of the chat where approval requests will be posted. This can be your personal DM with the bot, or a group chat.

1.  Start a conversation with your new bot (search for its username and send it any message, e.g. `/start`), or add it to a group.
2.  Fetch updates from the Bot API to find the chat ID:

    ```bash
    curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates"
    ```

3.  In the JSON response, find `message.chat.id`. For a personal DM this is your own numeric user ID; for a group it's typically a negative number. This is your `SCHWAB_MCP_TELEGRAM_CHAT_ID`.

## 3. Get Your User ID (Approver)

If you used your personal DM in step 2, `message.chat.id` is also your user ID and can be reused as the approver ID. Otherwise, message [@userinfobot](https://t.me/userinfobot) to get your numeric Telegram user ID. This is your `SCHWAB_MCP_TELEGRAM_APPROVERS` ID.

Only users whose IDs are listed as approvers can tap the Approve/Deny buttons; button presses from anyone else are rejected.

## Configuration

Use these values when running the server:

```bash
uv run schwab-mcp server \
  --telegram-token "YOUR_BOT_TOKEN" \
  --telegram-chat-id "YOUR_CHAT_ID" \
  --telegram-approver "YOUR_USER_ID" \
  ...
```

Or set them as environment variables:

```bash
export SCHWAB_MCP_TELEGRAM_TOKEN="YOUR_BOT_TOKEN"
export SCHWAB_MCP_TELEGRAM_CHAT_ID="YOUR_CHAT_ID"
export SCHWAB_MCP_TELEGRAM_APPROVERS="YOUR_USER_ID"
```

`SCHWAB_MCP_TELEGRAM_APPROVERS` accepts a comma-separated list if you want multiple reviewers.
