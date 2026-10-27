"""Manage Gideon users.

    python manage_users.py list
    python manage_users.py add NAME [FIRST_NAME]   (asks for a password)
    python manage_users.py passwd NAME             (sets a new password and signs that user out everywhere)
    python manage_users.py setname NAME FIRST_NAME (the name Gideon uses for that user)
    python manage_users.py enable NAME             (turns a disabled account back on, with 5 attempts)
    python manage_users.py setrole NAME user|admin (admins get the admin dashboard)
    python manage_users.py setkey NAME             (saves that user's Gemini API key)
    python manage_users.py delete NAME             (also deletes that user's chats and timers)
"""
import asyncio
import getpass
import sys

from dotenv import load_dotenv

load_dotenv()

import auth  # noqa: E402
from db import Database  # noqa: E402


def ask_password() -> str | None:
    first = getpass.getpass("New password (at least 8 characters): ")
    if len(first) < 8:
        print("That password is too short.")
        return None
    if first != getpass.getpass("Type it again: "):
        print("The two passwords don't match.")
        return None
    return first


async def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"list", "add", "passwd", "setname", "enable", "setrole", "setkey", "delete"}:
        print(__doc__)
        return 1
    cmd = argv[0]
    name = argv[1] if len(argv) > 1 else ""
    if cmd != "list" and not name:
        print(f"Usage: python manage_users.py {cmd} NAME")
        return 1
    if cmd == "setname" and len(argv) < 3:
        print("Usage: python manage_users.py setname NAME FIRST_NAME")
        return 1
    if cmd == "setrole" and (len(argv) < 3 or argv[2] not in ("user", "admin")):
        print("Usage: python manage_users.py setrole NAME user|admin")
        return 1

    db = Database()
    await db.connect()
    if not db.ok:
        print("Can't reach MySQL. Start it in XAMPP and check the DB_ lines in .env.")
        return 1
    try:
        if cmd == "list":
            for u in await db.list_users():
                print(
                    f"{u['username']:<20} {u['first_name'] or '-':<14} {u['role']:<6} {u['status']:<9} "
                    f"attempts left {u['login_attempts_left']}  "
                    f"key {'set' if u['has_api_key'] else 'none':<5} last login {u['last_login_at'] or 'never'}"
                )
            return 0

        user = await db.get_user(name)
        if cmd == "add":
            if not auth.valid_username(name):
                print("Usernames use 3 to 50 letters, numbers, dots, dashes or underscores.")
                return 1
            if user:
                print(f"{user['username']} already exists. Use: python manage_users.py passwd {name}")
                return 1
            first_name = argv[2] if len(argv) > 2 else input(
                f"First name Gideon should use (Enter for {auth.derive_first_name(name)}): "
            ).strip() or auth.derive_first_name(name)
            if not auth.valid_first_name(first_name):
                print("First names use letters, spaces, apostrophes or dashes (up to 50 characters).")
                return 1
            password = ask_password()
            if not password:
                return 1
            await db.create_user(name, password, first_name)
            print(f"Created {name}. Gideon will call them {first_name}.")
        elif cmd == "passwd":
            if not user:
                print(f"No user called {name}.")
                return 1
            password = ask_password()
            if not password:
                return 1
            await db.set_password(user["id"], password)
            print(f"Password changed for {user['username']}.")
        elif cmd == "setname":
            if not user:
                print(f"No user called {name}.")
                return 1
            if not auth.valid_first_name(argv[2]):
                print("First names use letters, spaces, apostrophes or dashes (up to 50 characters).")
                return 1
            await db.set_first_name(user["id"], argv[2])
            print(f"Gideon will call {user['username']} {argv[2]}.")
        elif cmd == "setrole":
            if not user:
                print(f"No user called {name}.")
                return 1
            await db.update_user(user["id"], {"role": argv[2]})
            print(f"{user['username']} is now {argv[2]}.")
        elif cmd == "setkey":
            if not user:
                print(f"No user called {name}.")
                return 1
            key = getpass.getpass("Gemini API key (leave empty to remove it): ").strip()
            await db.update_user(user["id"], {"api_key": key or None})
            print("Key saved." if key else "Key removed.")
        elif cmd == "enable":
            if not user:
                print(f"No user called {name}.")
                return 1
            await db.enable_user(user["id"])
            print(f"{user['username']} is enabled again with 5 login attempts.")
        elif cmd == "delete":
            if not user:
                print(f"No user called {name}.")
                return 1
            if input(f"This deletes {user['username']} and all their chats. Type the username to confirm: ") != user["username"]:
                print("Cancelled.")
                return 1
            await db.delete_user(user["id"])
            print(f"Deleted {user['username']}.")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))