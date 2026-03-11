"""Server-Sent Events bus for real-time UI updates.

Thread-safe queue connecting background workers to the SSE endpoint.
"""

import json
import queue
import threading
import time


class SSEBus:
    """Broadcast SSE events to all connected clients."""

    def __init__(self):
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        """Register a new client. Returns a queue to read events from."""
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Remove a client."""
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def publish(self, event: str, data: dict) -> None:
        """Send an event to all connected clients."""
        payload = json.dumps(data)
        msg = f"event: {event}\ndata: {payload}\n\n"
        with self._lock:
            dead = []
            for q in self._clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self._clients.remove(q)
                except ValueError:
                    pass

    def stream(self, client_queue: queue.Queue):
        """Generator yielding SSE messages for a single client."""
        try:
            while True:
                try:
                    msg = client_queue.get(timeout=15)
                    yield msg
                except queue.Empty:
                    # Heartbeat to keep connection alive
                    yield ": heartbeat\n\n"
        finally:
            self.unsubscribe(client_queue)


# Singleton bus instance
bus = SSEBus()
