"""Timers run here, in plain Python, so Gideon never has to count time itself."""
import asyncio
import logging
from datetime import datetime, timedelta

log = logging.getLogger("gideon.timers")
MAX_SECONDS = 12 * 3600


def spoken(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s or not parts:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(parts)


class TimerManager:
    def __init__(self, db, user_id, conversation_id, on_done):
        self.db = db
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.on_done = on_done  # async callable(info: dict)
        self.active: dict[int, dict] = {}
        self._local_id = 0

    def _remaining(self, t: dict) -> float:
        return max(0.0, (t["ends_at"] - datetime.now()).total_seconds())

    def _launch(self, timer_id: int, label: str, duration: int, ends_at: datetime) -> None:
        t = {"label": label, "duration": duration, "ends_at": ends_at}
        t["task"] = asyncio.create_task(self._run(timer_id, t))
        self.active[timer_id] = t

    async def _run(self, timer_id: int, t: dict) -> None:
        try:
            await asyncio.sleep(self._remaining(t))
        except asyncio.CancelledError:
            return
        self.active.pop(timer_id, None)
        await self.db.set_timer_status(timer_id, "finished")
        try:
            await self.on_done({"id": timer_id, "label": t["label"], "duration_seconds": t["duration"]})
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not announce timer %s: %s", timer_id, exc)

    async def resume(self) -> list[dict]:
        """Pick up timers that were running before a refresh. Returns ones that ended while away."""
        missed = []
        for row in await self.db.running_timers(self.user_id):
            if row["ends_at"] <= datetime.now():
                await self.db.set_timer_status(row["id"], "missed")
                missed.append(row)
            else:
                self._launch(row["id"], row["label"], row["duration_seconds"], row["ends_at"])
        return missed

    async def start(self, seconds, label: str = "Timer") -> dict:
        try:
            seconds = int(float(seconds))
        except (TypeError, ValueError):
            return {"ok": False, "error": "duration_seconds must be a number"}
        if not 1 <= seconds <= MAX_SECONDS:
            return {"ok": False, "error": "Timers must be between 1 second and 12 hours"}
        label = (label or "Timer").strip()[:100] or "Timer"
        ends_at = datetime.now() + timedelta(seconds=seconds)
        timer_id = await self.db.create_timer(self.user_id, self.conversation_id, label, seconds, ends_at)
        if timer_id is None:  # no database: fall back to an in-memory id
            self._local_id += 1
            timer_id = -self._local_id
        self._launch(timer_id, label, seconds, ends_at)
        return {"ok": True, "timer_id": timer_id, "label": label, "duration": spoken(seconds)}

    async def cancel(self, timer_id=None, label=None) -> dict:
        target = None
        if timer_id is not None:
            try:
                target = int(timer_id) if int(timer_id) in self.active else None
            except (TypeError, ValueError):
                target = None
        elif label:
            hits = [i for i, t in self.active.items() if label.lower() in t["label"].lower()]
            target = hits[0] if len(hits) == 1 else None
        elif len(self.active) == 1:
            target = next(iter(self.active))
        if target is None:
            return {"ok": False, "error": "No single matching timer. Ask which one.", "timers": self.list()}
        t = self.active.pop(target)
        t["task"].cancel()
        await self.db.set_timer_status(target, "cancelled")
        return {"ok": True, "cancelled": t["label"]}

    def list(self) -> list[dict]:
        rows = [
            {"id": i, "label": t["label"], "remaining_seconds": int(self._remaining(t)),
             "remaining": spoken(self._remaining(t))}
            for i, t in self.active.items()
        ]
        return sorted(rows, key=lambda r: r["remaining_seconds"])

    def close(self) -> None:
        """Stop the in-process tasks. Timers stay 'running' in the database and resume next time."""
        for t in self.active.values():
            t["task"].cancel()
        self.active.clear()
