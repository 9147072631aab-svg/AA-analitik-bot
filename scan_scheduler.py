import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

class ScanScheduler:
    """Automatic scan scheduler that wraps an existing bot.enqueue_scan.

    The next run is always calculated from the moment the previous scan
    finishes. Automatic runs are suppressed outside market_calendar's
    scan_allowed() window. Manual scans therefore reset the next-run timer.
    """
    def __init__(self, bot_module, interval_minutes=60):
        self.bot = bot_module
        self.interval = timedelta(minutes=max(1, int(interval_minutes)))
        self.lock = threading.RLock()
        self.next_scan_at = None
        self.enabled = True
        self.thread = None
        self.stop_event = threading.Event()
        self._orig_enqueue = bot_module.enqueue_scan
        self._install_wrappers()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @staticmethod
    def _fmt(dt):
        if not dt:
            return "—"
        return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M:%S MSK")

    def _allowed(self, dt):
        try:
            return bool(self.bot.market_calendar.scan_allowed(dt))
        except Exception:
            return False

    def _next_allowed(self, dt):
        # Search forward in small increments until a valid trading moment.
        # This handles weekends, holidays and session gaps without duplicating
        # the market calendar rules.
        probe = dt
        if self._allowed(probe):
            return probe
        for _ in range(7 * 24 * 60):
            probe += timedelta(minutes=1)
            if self._allowed(probe):
                return probe
        return None

    def _set_next_from_finish(self, finished_at):
        candidate = finished_at + self.interval
        candidate = self._next_allowed(candidate)
        with self.lock:
            self.next_scan_at = candidate

    def _on_scan_finished(self, finished_at):
        self._set_next_from_finish(finished_at)

    def _wrapped_target(self, chat_id=None):
        # Let the original worker do all scanning, journal and Telegram work.
        # We only observe completion and then schedule the next run.
        try:
            return self._orig_target(chat_id)
        finally:
            finish = self._now()
            self._on_scan_finished(finish)

    def _install_wrappers(self):
        # enqueue_scan is replaced so both /scan and the automatic scheduler
        # use the same single-flight lock/state.
        original_enqueue = self._orig_enqueue
        scheduler = self

        def enqueue(chat_id=None, _automatic=False):
            with scheduler.lock:
                # Do not start a second scan.
                with scheduler.bot.STATE_LOCK:
                    if scheduler.bot.SCAN_RUNNING:
                        return False

                # For a manual scan, the timer is deliberately NOT reset here.
                # It is reset after the worker finishes.
                return original_enqueue(chat_id)

        self.bot.enqueue_scan = enqueue

        # Patch the worker target so completion is observed even when the
        # original worker exits through an error/market-closed path.
        self._orig_target = self.bot._scan_thread_target

        def target(chat_id=None):
            try:
                return self._orig_target(chat_id)
            finally:
                scheduler._on_scan_finished(scheduler._now())

        self.bot._scan_thread_target = target

    def status_lines(self):
        with self.lock:
            nxt = self.next_scan_at
            enabled = self.enabled
        now = self._now()

        if nxt and nxt <= now:
            due = "сейчас"
        elif nxt:
            seconds = int((nxt - now).total_seconds())
            minutes, seconds = divmod(max(0, seconds), 60)
            hours, minutes = divmod(minutes, 60)
            if hours:
                due = f"{hours} ч {minutes} мин"
            else:
                due = f"{minutes} мин {seconds} сек"
        else:
            due = "не запланирован"

        try:
            market_open = self._allowed(now)
        except Exception:
            market_open = False

        return [
            f"Автоскан: {'ВКЛ' if enabled else 'ВЫКЛ'}",
            f"Рынок для автосканирования: {'ОТКРЫТ' if market_open else 'ЗАКРЫТ'}",
            f"Следующий запуск: {self._fmt(nxt)}",
            f"До запуска: {due}",
        ]

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            # On restart, start a fresh interval from current time, but only
            # inside an allowed market window.
            self.next_scan_at = self._next_allowed(self._now() + self.interval)
            self.thread = threading.Thread(
                target=self._loop,
                name="aa-auto-scan-scheduler",
                daemon=True,
            )
            self.thread.start()

    def _loop(self):
        while not self.stop_event.wait(1.0):
            with self.lock:
                if not self.enabled:
                    continue
                nxt = self.next_scan_at

            now = self._now()
            if nxt is None:
                with self.lock:
                    self.next_scan_at = self._next_allowed(now + self.interval)
                continue

            if now < nxt:
                continue

            # Never scan while the exchange is closed.
            if not self._allowed(now):
                with self.lock:
                    self.next_scan_at = self._next_allowed(now + timedelta(minutes=1))
                continue

            with self.bot.STATE_LOCK:
                running = self.bot.SCAN_RUNNING
                chat_id = self.bot.LAST_CHAT_ID

            if running:
                # The running scan will reset the schedule when it finishes.
                continue

            started = self.bot.enqueue_scan(chat_id)
            if started:
                # Prevent repeated launches while the worker is starting.
                with self.lock:
                    self.next_scan_at = None

    def stop(self):
        self.stop_event.set()

    def status_text(self):
        return "\n".join(self.status_lines())


def install(bot_module):
    import os
    interval = int(os.getenv("AUTO_SCAN_INTERVAL_MIN", "60"))
    scheduler = ScanScheduler(bot_module, interval_minutes=interval)

    original_status = bot_module.scan_status_text

    def scan_status_text():
        base = original_status()
        return base + "\n" + scheduler.status_text()

    bot_module.scan_status_text = scan_status_text

    # Start after bot has finished defining all functions.
    scheduler.start()
    bot_module.AUTO_SCAN_SCHEDULER = scheduler
    return scheduler
