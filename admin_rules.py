"""Checks used by the admin dashboard. Plain functions, so they are easy to test."""
import re

import auth

ROLES = ("user", "admin")
STATUSES = ("active", "disabled")


def mask_key(key: str | None) -> str:
    """Show just enough of an API key to recognise it: 'AIza...x9Ab'."""
    key = (key or "").strip()
    if not key:
        return ""
    return "\u2022\u2022\u2022\u2022" if len(key) <= 8 else f"{key[:4]}\u2026{key[-4:]}"


def clean_input(data: dict, creating: bool) -> tuple[dict, str | None]:
    """Validate what the dashboard sent. Returns (fields to save, error message or None)."""
    fields: dict = {}

    if creating:
        username = str(data.get("username", "")).strip()
        if not auth.valid_username(username):
            return {}, "Usernames use 3 to 50 letters, numbers, dots, dashes or underscores."
        fields["username"] = username

    if "first_name" in data:
        first_name = str(data.get("first_name") or "").strip()
        if first_name and not auth.valid_first_name(first_name):
            return {}, "First names use letters, spaces, apostrophes or dashes (up to 50 characters)."
        fields["first_name"] = first_name  # blank means: guess it from the username

    if "role" in data:
        if data["role"] not in ROLES:
            return {}, "Role must be user or admin."
        fields["role"] = data["role"]

    if "status" in data:
        if data["status"] not in STATUSES:
            return {}, "Status must be active or disabled."
        fields["status"] = data["status"]

    if data.get("clear_api_key"):
        fields["api_key"] = None
    elif str(data.get("api_key") or "").strip():
        key = str(data["api_key"]).strip()
        if len(key) > 255 or re.search(r"\s", key):
            return {}, "That doesn't look like an API key."
        fields["api_key"] = key

    password = data.get("password")
    if creating or (password is not None and str(password) != ""):
        password = str(password or "")
        if len(password) < 8:
            return {}, "Passwords need at least 8 characters."
        if len(password) > 200:
            return {}, "That password is too long."
        fields["password"] = password

    if data.get("reset_attempts"):
        fields["reset_attempts"] = True

    return fields, None


def safety_error(actor_id: int, target: dict, changes: dict, active_admins: int) -> str | None:
    """Stop an admin from locking everyone out of the dashboard."""
    new_role = changes.get("role", target["role"])
    new_status = changes.get("status", target["status"])
    if target["id"] == actor_id:
        if new_role != "admin":
            return "You can't remove your own admin role."
        if new_status != "active":
            return "You can't disable your own account."
    was_active_admin = target["role"] == "admin" and target["status"] == "active"
    still_active_admin = new_role == "admin" and new_status == "active"
    if was_active_admin and not still_active_admin and active_admins <= 1:
        return "Gideon needs at least one active admin."
    return None


def delete_error(actor_id: int, target: dict, active_admins: int) -> str | None:
    if target["id"] == actor_id:
        return "You can't delete your own account."
    if target["role"] == "admin" and target["status"] == "active" and active_admins <= 1:
        return "Gideon needs at least one active admin."
    return None