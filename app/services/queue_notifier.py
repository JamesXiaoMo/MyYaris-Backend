import asyncio
from typing import Dict, List
from app.config import settings


class QueueNotifier:
    def __init__(self):
        self._waiters: Dict[str, List[asyncio.Event]] = {}
        self._lock = asyncio.Lock()

    async def register_waiter(self, device_id: str) -> asyncio.Event:
        async with self._lock:
            if device_id not in self._waiters:
                self._waiters[device_id] = []

            # Check max active waiting limit
            if len(self._waiters[device_id]) >= settings.MAX_ACTIVE_LONG_POLL_PER_DEVICE:
                # Remove oldest waiting event or refuse
                oldest = self._waiters[device_id].pop(0)
                oldest.set()

            event = asyncio.Event()
            self._waiters[device_id].append(event)
            return event

    async def remove_waiter(self, device_id: str, event: asyncio.Event) -> None:
        async with self._lock:
            if device_id in self._waiters:
                if event in self._waiters[device_id]:
                    self._waiters[device_id].remove(event)
                if not self._waiters[device_id]:
                    del self._waiters[device_id]

    async def notify(self, device_id: str) -> None:
        async with self._lock:
            waiters = self._waiters.get(device_id, [])
            for event in waiters:
                event.set()
            # Clear woken waiters
            if device_id in self._waiters:
                del self._waiters[device_id]


queue_notifier = QueueNotifier()
