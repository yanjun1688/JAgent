from harness.storage.event_store import EventStore
from harness.storage.scoped import ScopedEventStore

EventStoreLike = EventStore | ScopedEventStore

__all__ = ["EventStore", "EventStoreLike", "ScopedEventStore"]
