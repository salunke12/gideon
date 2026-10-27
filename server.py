"""Gideon: a voice study companion built on the Gemini Live API.

Browser  <--WebSocket-->  this server  <--WebSocket-->  Gemini Live API
                              |
                              +-- MySQL (chat history) and Python timers
"""
import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types

import admin_rules
import auth
import chat_rules
from db import Database
from timers import TimerManager, spoken

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gideon")

STATIC = Path(__file__).parent / "static"
DEFAULT_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live").strip()
MODELS = {"gemini-3.8-live", "gemini-3.1-flash-live-preview", DEFAULT_MODEL}
DEFAULT_VOICE = "Puck"
CONTEXT_MESSAGES = 30      # how many earlier messages Gideon is reminded of
CONTEXT_CHARS = 6000       # upper bound on the reminder size
SESSION_COOKIE = "gideon_session"
SESSION_DAYS = 30
FIRST_USER = "Shreyas_12"  # created on the very first start, when there are no users yet
FIRST_USER_NAME = "Shreyas"
ADMIN_USER = "admin"  # created on start when no admin exists yet

BASE_PROMPT = (
    "You are Gideon, a calm and encouraging study companion. The student talks to you out loud "
    "while they study: answer questions, explain ideas simply, quiz them when asked, and keep "
    "them motivated. Speak naturally in one to three short sentences unless they ask for more "
    "detail. Always speak and write in English.\n\n"
    "You cannot measure time yourself. For any timer, countdown, study block or break, call "
    "start_timer (convert minutes and hours to seconds). To say how much time is left, call "
    "list_timers. To stop a timer, call cancel_timer. After starting a timer, confirm it in one "
    "short sentence. When you receive a system notice that a timer finished, tell the student in "
    "one short sentence."
)

TOOLS = [{"function_declarations": [
    {
        "name": "start_timer",
        "description": "Start a countdown timer that the app's code runs. Use it for every timer, "
                       "study block or break. Never count time yourself.",
        "parameters": {
            "type": "object",
            "properties": {
                "duration_seconds": {"type": "integer", "description": "Length of the timer in seconds"},
                "label": {"type": "string", "description": "Short name, for example 'Focus' or 'Break'"},
            },
            "required": ["duration_seconds"],
        },
    },
    {
        "name": "cancel_timer",
        "description": "Cancel a running timer by its id or label. With no arguments it cancels the "
                       "only running timer.",
        "parameters": {
            "type": "object",
            "properties": {
                "timer_id": {"type": "integer", "description": "Id returned by start_timer or list_timers"},
                "label": {"type": "string", "description": "Part of the timer's label"},
            },
        },
    },
    {
        "name": "list_timers",
        "description": "Get every running timer with the exact time left. Always call this to answer "
                       "'how much time is left'.",
    },
]}]

db = Database()


async def seed_first_user() -> None:
    if not db.ok:
        return
    await db.backfill_first_names()  # fills in first names for users made before they existed
    if await db.count_users():
        return
    password = secrets.token_urlsafe(9)
    user_id = await db.create_user(FIRST_USER, password, FIRST_USER_NAME)
    if user_id:
        await db.adopt_orphans(user_id)  # chats saved before logins existed
        print(
            "\n" + "=" * 58
            + f"\n  First start: created the user  {FIRST_USER}"
            + f"\n  Password (shown only once):    {password}"
            + f"\n  Choose your own:  python manage_users.py passwd {FIRST_USER}"
            + "\n" + "=" * 58 + "\n",
            flush=True,
        )


async def seed_admin() -> None:
    if not db.ok or await db.count_admins():
        return
    name = ADMIN_USER if not await db.get_user(ADMIN_USER) else "gideon_admin"
    if await db.get_user(name):
        return  # both names are taken: promote someone with  python manage_users.py setrole NAME admin
    password = secrets.token_urlsafe(9)
    if await db.create_user(name, password, "Admin", "admin"):
        print(
            "\n" + "=" * 58
            + f"\n  No admin existed, so one was created:  {name}"
            + f"\n  Password (shown only once):         {password}"
            + f"\n  Choose your own:  python manage_users.py passwd {name}"
            + "\n" + "=" * 58 + "\n",
            flush=True,
        )


async def prepare_data() -> None:
    await seed_first_user()
    await seed_admin()
    await db.adopt_conversations_into_chats()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.connect()
    await prepare_data()
    yield
    await db.close()


app = FastAPI(lifespan=lifespan)


def first_name_of(user: dict) -> str:
    return user.get("first_name") or auth.derive_first_name(user["username"])


# Added last, after the student's own extra instructions and the remembered chat, so nothing can override it.
LANGUAGE_RULE = (
    "Language rule: always reply in English only, every single time. This applies even if the student "
    "speaks or types in another language, or asks you to switch languages. If their words are not in "
    "English, still answer in English and, if it helps, tell them in English that you only reply in English."
)


def build_instruction(extra: str, history: list[dict], first_name: str, chat_title: str) -> str:
    parts = [
        BASE_PROMPT,
        f"The student's first name is {first_name}. Use it now and then, naturally, "
        "and not in every reply.",
        f'This chat is called "{chat_title}". Keep the conversation on that subject '
        "unless the student changes it.",
    ]
    if extra:
        parts.append("Extra instructions from the student:\n" + extra)
    lines, used = [], 0
    for row in reversed(history):  # newest first, so the freshest context wins when trimmed
        line = f"{'Student' if row['role'] == 'user' else 'Gideon'}: {row['content'][:400]}"
        if used + len(line) > CONTEXT_CHARS:
            break
        lines.append(line)
        used += len(line)
    if lines:
        parts.append(
            "What you remember from earlier in this chat with the student (oldest first). "
            "Use it only when relevant and don't recite it:\n" + "\n".join(reversed(lines))
        )
    parts.append(LANGUAGE_RULE)
    return "\n\n".join(parts)


NO_KEY_MESSAGE = (
    "Your account doesn't have a Gemini API key yet. Ask the admin to add one, then press Start talking."
)


def friendly_error(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    if "api key" in low or "api_key" in low or "permission" in low or "401" in low or "403" in low:
        return "Google rejected the API key saved for your account. Ask the admin to check it."
    if "429" in text or "quota" in low or "resource_exhausted" in low:
        return "The free tier limit was reached. Wait a few minutes, then start again."
    if "not found" in low or "404" in text:
        return "That model isn't available for your key. Pick another model in Settings."
    return f"The Gemini session ended: {text[:200]}"


class GideonSession:
    """Everything that belongs to one live conversation."""

    def __init__(self, ws: WebSocket, session, user_id: int, conversation_id):
        self.ws = ws
        self.session = session
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.send_lock = asyncio.Lock()
        self.role = None        # who the buffered transcript belongs to
        self.buffer: list[str] = []
        self.timers = TimerManager(db, user_id, conversation_id, self.timer_done)

    # sending to the browser
    async def send_json(self, obj: dict) -> None:
        async with self.send_lock:
            await self.ws.send_json(obj)

    async def send_audio(self, data: bytes) -> None:
        async with self.send_lock:
            await self.ws.send_bytes(data)

    async def push_timers(self) -> None:
        await self.send_json({"type": "timers", "timers": self.timers.list()})

    # saving the transcript: one row per turn, written when the speaker changes
    async def add_chunk(self, role: str, text: str) -> None:
        if self.role != role:
            await self.flush()
            self.role = role
        self.buffer.append(text)

    async def flush(self) -> None:
        text = "".join(self.buffer).strip()
        if text and self.role:
            await db.add_message(self.conversation_id, self.role, "voice", text)
        self.buffer, self.role = [], None

    # timers
    async def timer_done(self, info: dict) -> None:
        await self.send_json({"type": "timer_done", **info})
        await self.push_timers()
        notice = (
            f'[System notice from the timer: the timer "{info["label"]}" '
            f'({spoken(info["duration_seconds"])}) has just finished. Tell the student it is up.]'
        )
        await self.session.send_realtime_input(text=notice)

    async def handle_tool_call(self, tool_call) -> None:
        responses = []
        for fc in tool_call.function_calls:
            args = dict(fc.args or {})
            if fc.name == "start_timer":
                result = await self.timers.start(args.get("duration_seconds"), args.get("label") or "Timer")
            elif fc.name == "cancel_timer":
                result = await self.timers.cancel(args.get("timer_id"), args.get("label"))
            elif fc.name == "list_timers":
                result = {"timers": self.timers.list()}
            else:
                result = {"error": f"Unknown tool {fc.name}"}
            responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
        await self.session.send_tool_response(function_responses=responses)
        await self.push_timers()

    # browser -> Gemini
    async def upstream(self) -> None:
        while True:
            msg = await self.ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes"):
                await self.session.send_realtime_input(
                    audio=types.Blob(data=msg["bytes"], mime_type="audio/pcm;rate=16000")
                )
            elif msg.get("text"):
                data = json.loads(msg["text"])
                if data.get("type") == "text":
                    text = (data.get("text") or "").strip()[:2000]
                    if text:
                        await self.flush()
                        await db.add_message(self.conversation_id, "user", "text", text)
                        await self.session.send_realtime_input(text=text)
                elif data.get("type") == "cancel_timer":
                    await self.timers.cancel(timer_id=data.get("id"))
                    await self.push_timers()

    # Gemini -> browser
    async def downstream(self) -> None:
        while True:  # session.receive() finishes after every turn, so keep looping
            async for resp in self.session.receive():
                if resp.data:
                    await self.send_audio(resp.data)
                if resp.tool_call:
                    await self.handle_tool_call(resp.tool_call)
                sc = resp.server_content
                if not sc:
                    continue
                if sc.interrupted:
                    await self.send_json({"type": "interrupted"})
                if sc.input_transcription and sc.input_transcription.text:
                    await self.add_chunk("user", sc.input_transcription.text)
                    await self.send_json({"type": "user_text", "text": sc.input_transcription.text})
                if sc.output_transcription and sc.output_transcription.text:
                    await self.add_chunk("gideon", sc.output_transcription.text)
                    await self.send_json({"type": "ai_text", "text": sc.output_transcription.text})
                if sc.turn_complete:
                    await self.flush()
                    await self.send_json({"type": "turn_complete"})


@app.websocket("/ws")
async def voice_socket(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin and urlparse(origin).netloc != ws.headers.get("host"):
        await ws.close(code=1008)  # another website is trying to use your session
        return
    user = await current_user(ws.cookies.get(SESSION_COOKIE))
    await ws.accept()
    if user is None:
        await ws.send_json({"type": "unauthorized"})
        await ws.close()
        return
    api_key = (user.get("api_key") or "").strip()  # only the key the admin saved for this user is used
    if not api_key:
        await ws.send_json({"type": "error", "code": "no_key", "message": NO_KEY_MESSAGE})
        await ws.close()
        return
    live_client = genai.Client(api_key=api_key)

    try:
        init = json.loads(await ws.receive_text())
    except Exception:
        await ws.close()
        return

    model = init.get("model") if init.get("model") in MODELS else DEFAULT_MODEL
    voice = (init.get("voice") or DEFAULT_VOICE).strip()
    extra = (init.get("prompt") or "").strip()[:2000]

    chat = await chat_for(user, init.get("chat_id"))
    if chat is None:
        await ws.send_json({"type": "error", "message": "Couldn't open that chat. Refresh the page and try again."})
        await ws.close()
        return
    history = await db.recent_messages(user["id"], chat["id"], CONTEXT_MESSAGES)
    conversation_id = await db.start_conversation(user["id"], chat["id"], model, voice)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=build_instruction(extra, history, first_name_of(user), chat["title"]),
        tools=TOOLS,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    gideon = None
    try:
        async with live_client.aio.live.connect(model=model, config=config) as session:
            gideon = GideonSession(ws, session, user["id"], conversation_id)
            missed = await gideon.timers.resume()
            await gideon.send_json({"type": "ready", "model": model, "memory": db.ok})
            for row in missed:
                await gideon.send_json({"type": "timer_missed", "label": row["label"]})
            await gideon.push_timers()

            up = asyncio.create_task(gideon.upstream())
            down = asyncio.create_task(gideon.downstream())
            done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()  # re-raise any error
    except Exception as exc:  # noqa: BLE001
        log.warning("Session ended: %s", exc)
        try:
            await ws.send_json({"type": "error", "message": friendly_error(exc)})
        except Exception:  # browser already gone
            pass
    finally:
        if gideon:
            gideon.timers.close()
            await gideon.flush()
        await db.end_conversation(conversation_id)
        try:
            await ws.close()
        except Exception:
            pass


NO_CACHE = {"Cache-Control": "no-store"}


async def current_user(token: str | None):
    if not token:
        return None
    return await db.get_session_user(auth.hash_token(token))


@app.post("/api/login")
async def login(request: Request):
    was_down = not db.ok
    if not await db.ensure():  # retries, so starting MySQL later works without restarting server.py
        return JSONResponse(
            {
                "error": f"Gideon can't reach MySQL ({db.last_error}). Start MySQL in XAMPP, "
                         "check the DB_ lines in .env, then try again."
            },
            status_code=503,
        )
    if was_down:
        await prepare_data()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Send a username and password."}, status_code=400)
    username = str(body.get("username", "")).strip()[:50]
    password = str(body.get("password", ""))[:200]
    user = await db.get_user(username)
    if user and user["status"] == "disabled":
        return JSONResponse(
            {"error": "This account is disabled because all 5 login attempts were used. "
                      "Ask the admin to turn it back on.", "attempts_left": 0, "disabled": True},
            status_code=403,
        )
    valid = bool(user) and auth.verify_password(password, user["password"])
    if not (user and valid):
        if not user:
            return JSONResponse({"error": "Wrong username or password."}, status_code=401)
        left = await db.record_failed_login(user["id"])  # each wrong password uses up one attempt
        if left == 0:
            return JSONResponse(
                {"error": "Wrong password. That was your last attempt, so this account is now disabled. "
                          "Ask the admin to turn it back on.", "attempts_left": 0, "disabled": True},
                status_code=403,
            )
        return JSONResponse(
            {"error": f"Wrong password. You have {left} login attempt{'' if left == 1 else 's'} left.",
             "attempts_left": left},
            status_code=401,
        )

    await db.reset_login_attempts(user["id"])  # a correct password gives all 5 attempts back
    token = auth.new_token()
    await db.purge_expired_sessions()
    await db.create_login_session(user["id"], auth.hash_token(token), datetime.now() + timedelta(days=SESSION_DAYS))
    await db.touch_login(user["id"])
    response = JSONResponse({"ok": True, "username": user["username"]})
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/"
    )
    return response


@app.post("/api/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await db.delete_login_session(auth.hash_token(token))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/me")
async def me(request: Request):
    user = await current_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    return {
        "username": user["username"],
        "first_name": first_name_of(user),
        "role": user["role"],
        "has_key": bool((user.get("api_key") or "").strip()),
    }


@app.get("/api/history")
async def history(request: Request):
    """The logged-in user's saved chat, so the transcript is still there after a refresh or a new login."""
    user = await current_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    try:
        limit = min(max(int(request.query_params.get("limit", 200)), 1), 1000)
    except ValueError:
        limit = 200
    chat = await chat_for(user, request.query_params.get("chat"))
    if chat is None:
        return JSONResponse({"messages": []}, headers=NO_CACHE)
    rows = await db.chat_history(user["id"], chat["id"], limit)
    return JSONResponse(
        {
            "messages": [
                {
                    "id": r["id"],
                    "conversation": r["conversation_id"],
                    "role": "user" if r["role"] == "user" else "ai",
                    "text": r["content"],
                    "at": r["created_at"].isoformat(),
                }
                for r in rows
            ]
        },
        headers=NO_CACHE,
    )


@app.post("/api/history/delete")
async def history_delete(request: Request):
    """Delete the chosen messages from this user's saved chat."""
    user = await current_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    try:
        ids = [int(i) for i in (await request.json())["ids"]]
    except Exception:
        return JSONResponse({"error": "Choose which messages to delete."}, status_code=400)
    return {"deleted": await db.delete_messages(user["id"], ids)}


@app.post("/api/history/clear")
async def history_clear(request: Request):
    """Delete every saved message in one chat (the chat itself stays)."""
    user = await current_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    data = await read_json_object(request) or {}
    chat = await chat_for(user, data.get("chat_id"))
    if chat is None:
        return {"deleted": 0}
    return {"deleted": await db.clear_history(user["id"], chat["id"])}


async def chat_for(user: dict, raw_id):
    """The chat with this id if it belongs to the user, otherwise their most recent chat."""
    try:
        chat_id = int(raw_id)
    except (TypeError, ValueError):
        chat_id = 0
    chat = await db.get_chat(user["id"], chat_id) if chat_id else None
    if chat:
        return chat
    default_id = await db.default_chat_id(user["id"])
    return await db.get_chat(user["id"], default_id) if default_id else None


def chat_view(c: dict) -> dict:
    return {
        "id": c["id"],
        "title": c["title"],
        "messages": c["messages"],
        "updated_at": c["updated_at"].isoformat() if c["updated_at"] else None,
    }


@app.get("/api/chats")
async def chats_list(request: Request):
    user = await current_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    chats = await db.list_chats(user["id"])
    if not chats:  # everyone always has at least one chat
        await db.default_chat_id(user["id"])
        chats = await db.list_chats(user["id"])
    return JSONResponse({"chats": [chat_view(c) for c in chats]}, headers=NO_CACHE)


@app.post("/api/chats")
async def chats_create(request: Request):
    user = await current_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    if await db.count_chats(user["id"]) >= chat_rules.MAX_CHATS:
        return JSONResponse(
            {"error": f"You can have up to {chat_rules.MAX_CHATS} chats. Delete one you don't need first."},
            status_code=400,
        )
    data = await read_json_object(request) or {}
    title = chat_rules.clean_title(data.get("title"))
    new_id = await db.create_chat(user["id"], title)
    if not new_id:
        return JSONResponse({"error": "Couldn't create the chat. Check the server console."}, status_code=500)
    return JSONResponse({"id": new_id, "title": title}, status_code=201)


@app.put("/api/chats/{chat_id}")
async def chats_rename(chat_id: int, request: Request):
    user = await current_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    if await db.get_chat(user["id"], chat_id) is None:
        return JSONResponse({"error": "That chat no longer exists."}, status_code=404)
    data = await read_json_object(request) or {}
    title = chat_rules.clean_title(data.get("title"), default=None)
    if not title:
        return JSONResponse({"error": "Give the chat a name."}, status_code=400)
    await db.rename_chat(user["id"], chat_id, title)
    return {"id": chat_id, "title": title}


@app.delete("/api/chats/{chat_id}")
async def chats_delete(chat_id: int, request: Request):
    user = await current_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    if await db.get_chat(user["id"], chat_id) is None:
        return JSONResponse({"error": "That chat no longer exists."}, status_code=404)
    await db.delete_chat(user["id"], chat_id)  # its conversations and messages go with it
    return {"deleted": True}


def admin_view(u: dict) -> dict:
    key = (u.get("api_key") or "").strip()
    when = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "id": u["id"],
        "username": u["username"],
        "first_name": u["first_name"] or auth.derive_first_name(u["username"]),
        "role": u["role"],
        "status": u["status"],
        "attempts_left": u["login_attempts_left"],
        "has_key": bool(key),
        "key_hint": admin_rules.mask_key(key),
        "created_at": when(u["created_at"]),
        "last_login_at": when(u["last_login_at"]),
    }


async def admin_user(request: Request):
    """Returns (admin, None), or (None, error response) for anyone who isn't a logged-in admin."""
    user = await current_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return None, JSONResponse({"error": "Not logged in."}, status_code=401)
    if user["role"] != "admin":
        return None, JSONResponse({"error": "Admins only."}, status_code=403)
    return user, None


async def read_json_object(request: Request):
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


@app.get("/api/admin/users")
async def admin_list(request: Request):
    admin, error = await admin_user(request)
    if error:
        return error
    users = await db.list_users_admin()
    return JSONResponse(
        {"users": [admin_view(u) for u in users], "me": admin["id"]},
        headers=NO_CACHE,
    )


@app.post("/api/admin/users")
async def admin_create(request: Request):
    admin, error = await admin_user(request)
    if error:
        return error
    data = await read_json_object(request)
    if data is None:
        return JSONResponse({"error": "Fill in the form and try again."}, status_code=400)
    fields, problem = admin_rules.clean_input(data, creating=True)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    if await db.get_user(fields["username"]):
        return JSONResponse({"error": "That username is already taken."}, status_code=409)
    new_id = await db.create_user(
        fields["username"],
        fields["password"],
        fields.get("first_name") or auth.derive_first_name(fields["username"]),
        fields.get("role", "user"),
        fields.get("api_key"),
    )
    if not new_id:
        return JSONResponse({"error": "Couldn't save the user. Check the server console."}, status_code=500)
    return JSONResponse(admin_view(await db.get_user_by_id(new_id)), status_code=201)


@app.put("/api/admin/users/{user_id}")
async def admin_update(user_id: int, request: Request):
    admin, error = await admin_user(request)
    if error:
        return error
    target = await db.get_user_by_id(user_id)
    if target is None:
        return JSONResponse({"error": "That user no longer exists."}, status_code=404)
    data = await read_json_object(request)
    if data is None:
        return JSONResponse({"error": "Fill in the form and try again."}, status_code=400)
    fields, problem = admin_rules.clean_input(data, creating=False)
    problem = problem or admin_rules.safety_error(admin["id"], target, fields, await db.count_active_admins())
    if problem:
        return JSONResponse({"error": problem}, status_code=400)

    password = fields.pop("password", None)
    status = fields.pop("status", None)
    reset = fields.pop("reset_attempts", False)
    if "first_name" in fields and not fields["first_name"]:
        fields["first_name"] = auth.derive_first_name(target["username"])
    await db.update_user(user_id, fields)
    if password:
        await db.set_password(user_id, password)  # also signs that user out everywhere
    if status == "active" and target["status"] != "active":
        await db.enable_user(user_id)
    elif status == "disabled" and target["status"] != "disabled":
        await db.disable_user(user_id)
    elif reset:
        await db.reset_login_attempts(user_id)
    return admin_view(await db.get_user_by_id(user_id))


@app.delete("/api/admin/users/{user_id}")
async def admin_delete(user_id: int, request: Request):
    admin, error = await admin_user(request)
    if error:
        return error
    target = await db.get_user_by_id(user_id)
    if target is None:
        return JSONResponse({"error": "That user no longer exists."}, status_code=404)
    problem = admin_rules.delete_error(admin["id"], target, await db.count_active_admins())
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    await db.delete_user(user_id)  # their chats and timers go with them
    return {"deleted": True}


@app.get("/")
async def index(request: Request):
    user = await current_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return RedirectResponse("/login", headers=NO_CACHE)
    if user["role"] == "admin":
        return RedirectResponse("/admin", headers=NO_CACHE)
    return FileResponse(STATIC / "index.html", headers=NO_CACHE)


@app.get("/admin")
async def admin_page(request: Request):
    user = await current_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return RedirectResponse("/login", headers=NO_CACHE)
    if user["role"] != "admin":
        return RedirectResponse("/", headers=NO_CACHE)
    return FileResponse(STATIC / "admin.html", headers=NO_CACHE)


@app.get("/login")
async def login_page(request: Request):
    if await current_user(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse("/", headers=NO_CACHE)
    return FileResponse(STATIC / "login.html", headers=NO_CACHE)


app.mount("/static", StaticFiles(directory=STATIC), name="static")

if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=8000)  