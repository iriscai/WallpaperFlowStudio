"""In-app timer. It only runs flows whose own schedule is enabled."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from app.engine import Busy, Engine
from app.store import Store


class Scheduler:
    def __init__(self, store: Store, engine: Engine):
        self.store = store
        self.engine = engine
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="mp-content-flow-scheduler", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop.wait(15):
            try:
                self.tick()
            except Exception as exc:
                print(f"定时检查失败：{exc}")

    def tick(self) -> None:
        now = datetime.now(timezone.utc)
        for flow in self.store.list_flows():
            schedule = flow.get("schedule") or {}
            if not schedule.get("enabled") or not schedule.get("nextRunAt"):
                continue
            due = datetime.fromisoformat(schedule["nextRunAt"])
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if due > now:
                continue
            try:
                self.engine.run(flow["id"], mode="schedule")
            except Busy:
                continue
            except Exception as exc:
                print(f"定时执行失败 {flow.get('name')}：{exc}")
