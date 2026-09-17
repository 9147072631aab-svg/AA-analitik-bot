import os
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


class ScanScheduler:
    """Single-flight automatic scanner with a real MOEX calendar."""

    def __init__(self, bot_module, interval_minutes=60):
        self.bot = bot_module
        self.interval = timedelta(minutes=max(1, int(interval_minutes)))
        self.lock = threading.RLock()
        self.next_scan_at = None
        self.enabled = True
        self.thread = None
        self.stop_event = threading.Event()
        self._orig_enqueue = bot_module.enqueue_scan
        self._orig_target = bot_module._scan_thread_target
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
        """Find the next actual trading minute, including holidays."""
        probe = dt.astimezone(MSK).replace(second=0, microsecond=0)
        if self._allowed(probe):
            return probe.astimezone(timezone.utc)

        # 370 days covers the complete explicit 2026 calendar.
        for _ in range(370 * 24 * 60):
            probe += timedelta(minutes=1)
            if self._allowed(probe):
                return probe.astimezone(timezone.utc)
        return None

    def _reset_after_finish(self, finished_at):
        with self.lock:
            self.next_scan_at = self._next_allowed(
                finished_at + self.interval
            )

    def _install_wrappers(self):
        scheduler = self
        original_enqueue = self._orig_enqueue

        def enqueue(chat_id=None, _automatic=False):
            with scheduler.bot.STATE_LOCK:
                if scheduler.bot.SCAN_RUNNING:
                    return False
            return original_enqueue(chat_id)

        self.bot.enqueue_scan = enqueue

        def target(chat_id=None):
            try:
                return scheduler._orig_target(chat_id)
            finally:
                # This is intentionally after the worker finishes.
                # Manual scans therefore restart the countdown here.
                scheduler._reset_after_finish(scheduler._now())

        self.bot._scan_thread_target = target

    def status_lines(self):
        with self.lock:
            nxt = self.next_scan_at
            enabled = self.enabled

        now = self._now()

        if nxt and nxt <= now:
            due = "сейчас"
        elif nxt:
            total = max(0, int((nxt - now).total_seconds()))
            hours, rem = divmod(total, 3600)
            minutes, seconds = divmod(rem, 60)
            due = (
                f"{hours} ч {minutes} мин {seconds} сек"
                if hours else f"{minutes} мин {seconds} сек"
            )
        else:
            due = "не запланирован"

        market_open = self._allowed(now)

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
            # Restarting the service creates a fresh schedule, but the
            # candidate is always moved to a real trading minute.
            self.next_scan_at = self._next_allowed(
                self._now() + self.interval
            )

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
                    self.next_scan_at = self._next_allowed(
                        now + self.interval
                    )
                continue

            if now < nxt:
                continue

            # Never launch outside a real trading session.
            if not self._allowed(now):
                with self.lock:
                    self.next_scan_at = self._next_allowed(
                        now + timedelta(minutes=1)
                    )
                continue

            with self.bot.STATE_LOCK:
                if self.bot.SCAN_RUNNING:
                    continue
                chat_id = self.bot.LAST_CHAT_ID

            started = self.bot.enqueue_scan(chat_id)
            if started:
                # Completion wrapper will calculate the next run.
                with self.lock:
                    self.next_scan_at = None

    def stop(self):
        self.stop_event.set()

    def status_text(self):
        return "\n".join(self.status_lines())


def install(bot_module):
    interval = int(os.getenv("AUTO_SCAN_INTERVAL_MIN", "60"))
    scheduler = ScanScheduler(bot_module, interval_minutes=interval)

    original_status = bot_module.scan_status_text

    def scan_status_text():
        return original_status() + "\n" + scheduler.status_text()

    bot_module.scan_status_text = scan_status_text
    bot_module.AUTO_SCAN_SCHEDULER = scheduler
    scheduler.start()
    return scheduler
