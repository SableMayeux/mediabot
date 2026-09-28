"""FIFO admission for the shared inference service, entirely in memory."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


class QueueError(RuntimeError):
    pass


@dataclass(eq=False)
class Ticket:
    request_id: str
    owner_id: int | None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    dispatched: bool = False

    def check(self):
        if self.cancelled:
            raise QueueError("Local generation cancelled.")


class InferenceQueue:
    def __init__(self, *, capacity=20, per_user=2, wait_seconds=3600):
        self.tickets = []
        self.capacity, self.per_user, self.wait_seconds = capacity, per_user, wait_seconds

    def notify(self):
        for ticket in self.tickets:
            ticket.changed.set()

    def cancel_waiting(self, request_id):
        for ticket in self.tickets:
            if ticket.request_id == request_id:
                ticket.cancelled = True
                if ticket.dispatched:
                    return False
                self.tickets.remove(ticket)
                ticket.changed.set()
                self.notify()
                return True
        return False

    @asynccontextmanager
    async def slot(self, request_id, *, owner_id=None, on_progress=None):
        if any(t.request_id == request_id for t in self.tickets):
            raise QueueError("That request is already queued or running.")
        if len(self.tickets) >= self.capacity:
            raise QueueError("The AI queue is full (20 requests). Try again after it clears.")
        if owner_id is not None and sum(t.owner_id == owner_id for t in self.tickets) >= self.per_user:
            raise QueueError("You already have two AI requests queued or running. Cancel one before adding another.")
        ticket = Ticket(request_id, owner_id)
        self.tickets.append(ticket)
        deadline = asyncio.get_running_loop().time() + self.wait_seconds
        previous = None
        try:
            while True:
                ticket.check()
                position = self.tickets.index(ticket)
                if position == 0:
                    break
                if position != previous and on_progress:
                    await on_progress("queued", position)
                previous = position
                ticket.changed.clear()
                # No await between observing the queue and clearing the event.
                # A cancellation during the callback must still be noticed.
                ticket.check()
                if self.tickets.index(ticket) != position:
                    continue
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise QueueError("This request waited an hour in the AI queue and expired. No inference was submitted.")
                try:
                    await asyncio.wait_for(ticket.changed.wait(), remaining)
                except asyncio.TimeoutError:
                    raise QueueError("This request waited an hour in the AI queue and expired. No inference was submitted.") from None
            ticket.check()
            yield ticket
        finally:
            if ticket in self.tickets:
                self.tickets.remove(ticket)
            self.notify()
