import threading
from collections.abc import Callable

from app.session import Activity, Session
from app.settings import SettingsStore

SECOND_TAB_DETAIL = "編集画面は別のタブで開いています。このタブでは編集できません。開いているタブに戻ってください。"
LEASE_LIMIT_SECONDS = 5.0
WARNING_REMAINING_SECONDS = 60.0
RESETTING_SOURCES = {"click", "key", "scroll", "settings", "continue"}


class EditorLease:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._holder: str | None = None
        self._seen: float | None = None

    def _expire(self, now: float) -> None:
        if self._holder is not None and self._seen is not None and now - self._seen > LEASE_LIMIT_SECONDS:
            self._holder = None
            self._seen = None

    def claim(self, tab_id: str) -> tuple[int, dict]:
        now = self._clock()
        with self._lock:
            self._expire(now)
            if self._holder is None:
                self._holder = tab_id
                self._seen = now
                return 200, {}
            if self._holder == tab_id:
                return 200, {}
            return 409, {"detail": SECOND_TAB_DETAIL}

    def release(self, tab_id: str) -> tuple[int, dict]:
        now = self._clock()
        with self._lock:
            self._expire(now)
            if self._holder == tab_id:
                self._holder = None
                self._seen = None
            return 200, {}

    def heartbeat(self, tab_id: str) -> bool:
        now = self._clock()
        with self._lock:
            self._expire(now)
            if self._holder != tab_id:
                return False
            self._seen = now
            return True

    def holds(self, tab_id: str | None) -> bool:
        if not tab_id:
            return False
        now = self._clock()
        with self._lock:
            self._expire(now)
            return self._holder == tab_id


class IdleWatch:
    def __init__(
        self,
        clock: Callable[[], float],
        settings: SettingsStore,
        session: Session,
        stop: Callable[[], None],
    ) -> None:
        self._clock = clock
        self._settings = settings
        self._session = session
        self._stop = stop
        self._lock = threading.Lock()
        self._origin: float | None = None
        self._budget: float | None = None
        self._frozen: float | None = None
        self._fired = False
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, *, threaded: bool) -> None:
        now = self._clock()
        budget = self._budget_seconds()
        with self._lock:
            self._origin = now
            self._budget = budget
            self._frozen = None
            self._fired = False
        if not threaded:
            return
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name="pdf-editor-idle", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def on_activity(self, previous: Activity, new: Activity) -> None:
        now = self._clock()
        with self._lock:
            if self._origin is None or self._budget is None:
                return
            if new is Activity.EXPORTING and previous is not Activity.EXPORTING and self._frozen is None:
                self._frozen = self._budget - (now - self._origin)
            elif previous is Activity.EXPORTING and new is not Activity.EXPORTING and self._frozen is not None:
                self._budget = self._frozen
                self._origin = now
                self._frozen = None

    def reset(self) -> None:
        now = self._clock()
        budget = self._budget_seconds()
        with self._lock:
            self._origin = now
            self._budget = budget
            if self._frozen is not None:
                self._frozen = budget

    def remaining(self) -> float:
        now = self._clock()
        with self._lock:
            if self._origin is None or self._budget is None:
                return float("inf")
            if self._frozen is not None:
                return self._frozen
            return self._budget - (now - self._origin)

    def warning(self) -> bool:
        return self.remaining() <= WARNING_REMAINING_SECONDS

    def tick(self) -> None:
        if self._session.current_activity() is Activity.EXPORTING:
            return
        if self.remaining() <= 0:
            self.shutdown_now()

    def shutdown_now(self) -> bool:
        with self._lock:
            if self._fired:
                return False
            self._fired = True
        self._session.shutdown()
        self._stop()
        return True

    def _loop(self) -> None:
        while not self._wake.is_set():
            self.tick()
            self._wake.wait(0.5)

    def _budget_seconds(self) -> float:
        minutes = self._settings.read()["idle_minutes"]
        return float(minutes) * 60
