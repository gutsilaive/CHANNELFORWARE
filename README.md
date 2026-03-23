# 🤖 AutoForward Bot

Forward videos & messages from any Telegram channel to multiple destinations — without the "Forwarded from" tag. Controlled by a private admin-only bot with a professional inline menu.

---

## ✨ Features
- 🔑 **Login with your Telegram account** (phone + OTP + optional 2FA)
- ⏩ **Forward up to 1,000 messages** per run to **multiple channels**
- ✅ **No "Forwarded from" tag** — messages appear as original
- 📢 **Public & private channel support** (via invite links)
- 🔒 **Admin-only access** — only your configured `ADMIN_ID` can use the bot
- 💬 **Custom captions** or keep original
- 📊 **Live progress bar** updates every 10 messages
- 🗄️ **Supabase** session & task storage
- ⏹ **Stop button** to cancel a running job

---

## 🚀 Setup

### 1. Clone / Download

Place all files in a folder (e.g., `E:\bot autoforward\`).

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

```bash
copy .env.example .env
```

Then fill in the values:

| Key | Where to get it |
|-----|----------------|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → create a bot |
| `ADMIN_ID` | [@userinfobot](https://t.me/userinfobot) → your user ID |
| `API_ID` | [my.telegram.org](https://my.telegram.org) → App |
| `API_HASH` | same as above |
| `SUPABASE_URL` | Supabase project → Settings → API |
| `SUPABASE_KEY` | Supabase project → Settings → API (service_role key recommended) |

### 4. Set up Supabase tables

Open your Supabase project → **SQL Editor** → paste and run `supabase_setup.sql`.

### 5. Run the bot

```bash
python bot.py
```

---

## 📖 How to Use

1. Open Telegram and message your bot.
2. `/start` → you'll see the main menu.
3. **Login** → enter your phone number → enter the OTP sent by Telegram → (2FA if enabled).
4. **Forward Messages**:
   - Pick a **source channel** from your joined list, or type a link/username.
   - Add one or more **destination channels** (public @username or private invite link).
   - Set **message range**: `<start_id> <end_id>` (e.g., `1 500`)
   - Optionally set a **custom caption** or skip to keep originals.
   - Tap **Confirm** — watch the live progress bar!
5. **Task History** — view all past and running jobs.

---

## 📁 File Structure

```
├── bot.py               ← Main entry point
├── config.py            ← Environment config
├── database.py          ← Supabase helpers
├── userbot.py           ← Pyrogram user client
├── requirements.txt
├── supabase_setup.sql   ← Run once in Supabase
├── .env.example
└── handlers/
    ├── ui.py            ← Shared UI (keyboards, progress bar)
    ├── start.py         ← /start, main menu, help, about
    ├── auth.py          ← Login/Logout conversation
    ├── channels.py      ← Channel browser
    ├── forward.py       ← Forward conversation + live progress
    └── tasks.py         ← Task history
```

---

## ⚠️ Notes

- The bot uses `copy_message()` — the original author is never shown.
- Forwarding too fast may trigger Telegram flood limits; the bot handles these automatically with retries and delays.
- Keep your `.env` file private — it contains your Telegram session credentials.
