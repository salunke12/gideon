# Gideon

A voice study companion. Talk to Gideon while you study, or type to it and hear the answer. Gideon runs timers for you, and remembers earlier chats.

Browser (mic + speakers) <-> `server.py` <-> Gemini Live API, with MySQL for accounts, chat history and timers.

## Set up (Windows, PowerShell)

1. Start **MySQL** in the XAMPP control panel (Apache isn't needed).
2. In your venv:

```powershell
cd gideon
pip install -r requirements.txt
copy .env.example .env
notepad .env          # paste GEMINI_API_KEY (get a free key in Google AI Studio)
python server.py
```

3. On the very first start the console prints the password for the user `Shreyas_12`, once. Copy it.
4. Open http://localhost:8000, log in, and press **Start talking**.

The database `gideon_db` (change it with `DB_NAME` in `.env`) and its tables are created for you. An older Gideon database is upgraded automatically, and its saved chats are given to `Shreyas_12`.

## Users

```powershell
python manage_users.py list
python manage_users.py add NAME
python manage_users.py passwd Shreyas_12     # choose your own password
python manage_users.py delete NAME           # also deletes their chats and timers
```

Passwords are stored as plain text in the `password` column, so keep this database local. Each user only sees and talks about their own chats and timers. After 5 wrong passwords in 5 minutes, that username is paused for a few minutes.

## Timers

Say things like "start a 25 minute focus timer" or "how long is left?". Python counts the time, so it is exact. Timers appear under the orb, chime when they end, and survive a page refresh.

## Reading your data in phpMyAdmin

Open the `gideon_db` database and browse these views:

- `v_chat_history`: user, conversation, time, who spoke, voice or text, message
- `v_timers`: user, label, duration, start, end, status
- `v_users`: usernames, created and last login (never the password)

## Tips

- Use headphones, or the mic hears the reply and Gideon interrupts itself.
- Chrome and Edge work best.
- Free tier data may be used by Google, so don't share private information.
- Models: `gemini-3.8-live` (default) or `gemini-3.1-flash-live-preview`, chosen in Settings.

## Files

| File | Purpose |
|---|---|
| `server.py` | Web server, login, Gemini Live bridge |
| `auth.py` | Password check and login tokens |
| `db.py` | MySQL access and upgrade of older databases |
| `timers.py` | Timers run in Python |
| `manage_users.py` | Add, list, delete users and set passwords |
| `database/schema.sql` | Tables and readable views |
| `static/` | Interface: `index.html`, `login.html`, `style.css`, `app.js`, `login.js`, `worklets/` |