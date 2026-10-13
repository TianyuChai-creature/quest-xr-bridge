"""Explicit XR grants. Tombstones last for the entire server epoch."""
import threading
from uuid import UUID, uuid4
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DrivingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["driving_event"]
    event: Literal["enter", "exit"]
    session_id: UUID
    authorization_id: UUID
    server_epoch: UUID
    client_token: UUID | None = None
    attempt_id: int | None = Field(default=None, strict=True, ge=1, le=2**53-1)
    seq: int = Field(strict=True, ge=1, le=2**53-1)
    reason: Literal["enter_click", "xr_end", "page_hidden", "operator_stop"]

    @model_validator(mode="after")
    def claim_pair(self):
        if (self.client_token is None) != (self.attempt_id is None):
            raise ValueError("page claim requires both token and attempt")
        return self


class DrivingAuthority:
    def __init__(self, notifier):
        self._lock = threading.RLock()
        self._notifier = notifier
        self._owner = None
        # No TTL: HTTP may arrive arbitrarily late. Reset only with a new epoch;
        # events from the old epoch can never grant authority in the new one.
        self._closed = set()
        self._page_seq = {}
        self._state = dict(type="driving_authority", server_epoch=str(uuid4()), generation=0,
                           active=False, session_id=None, authorization_id=None, reason="not_entered")
        self._seq = 0

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def receive(self, event, owner):
        if owner is None or str(event.client_token) != str(owner):
            return False
        if (event.event == "enter") != (event.reason == "enter_click"):
            return False
        key = (str(event.session_id), str(event.authorization_id))
        with self._lock:
            if str(event.server_epoch) != self._state["server_epoch"]:
                return False
            if event.event == "enter" and event.seq <= self._page_seq.get(str(owner), 0):
                return False
            previous = (self._state["session_id"], self._state["authorization_id"])
            if key in self._closed or (key == previous and (str(owner) != self._owner or event.seq <= self._seq)):
                return False
            self._page_seq[str(owner)] = max(event.seq, self._page_seq.get(str(owner), 0))
            if event.event == "exit":
                self._closed.add(key)
                # An early exit must not replace another active grant.
                if key != previous and self._state["active"]:
                    return True
            elif key != previous and self._state["active"]:
                self._closed.add(previous)
            self._owner, self._seq = str(owner), event.seq
            self._state.update(session_id=key[0], authorization_id=key[1], active=event.event == "enter",
                               reason=event.reason, generation=self._state["generation"] + 1)
        self._notifier.notify()
        return True

    def revoke(self, session_id, authorization_id, *, reason="operator_stop"):
        # Trusted host API is negative-only, including before the first enter.
        key = (str(UUID(str(session_id))), str(UUID(str(authorization_id))))
        if reason not in ("operator_stop", "xr_end", "page_hidden"):
            raise ValueError("invalid revocation reason")
        with self._lock:
            if key in self._closed:
                return False
            self._closed.add(key)
            if key != (self._state["session_id"], self._state["authorization_id"]):
                return True
            self._state.update(active=False, reason=reason, generation=self._state["generation"] + 1)
        self._notifier.notify()
        return True
