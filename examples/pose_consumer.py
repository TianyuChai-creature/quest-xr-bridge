"""Reference availability gate for /ws poses; contains no robot control or network loop."""

from __future__ import annotations

import math
from uuid import UUID


class PoseConsumerGate:
    """Call allowed() from the control loop using the consumer's monotonic clock."""

    def __init__(self, *, warning_s: float, timeout_s: float) -> None:
        if any(isinstance(value, bool) or not math.isfinite(value) for value in (warning_s, timeout_s)):
            raise ValueError("thresholds must be finite numbers")
        if not 0 < warning_s < timeout_s:
            raise ValueError("thresholds must satisfy 0 < warning_s < timeout_s")
        self.warning_s = warning_s
        self.timeout_s = timeout_s
        self.inhibited = False
        self.source_changed()

    def _stop(self) -> None:
        self.armed = {"left": False, "right": False}
        self.received_at = None

    def source_changed(self) -> None:
        """Call on a socket disconnect/reconnect or a supervised upstream source change."""
        self._stop()
        self.session_id = self.seq = None
        self.tracked = {"left": False, "right": False}

    def set_inhibited(self, active: bool) -> None:
        """Hold a system fault (e.g. unavailable video) until supervision clears it."""
        self.inhibited = active
        if active:
            self._stop()

    def receive(self, pose: dict, now: float) -> bool:
        """Pass an already validated /ws payload and local receipt time, never source time."""
        self.allowed(now)  # A new packet must not erase a preceding receipt timeout.
        try:
            session_id = str(UUID(pose["session_id"]))
            seq = pose["seq"]
            tracked = {side: pose["hands"][side]["tracked"] for side in self.tracked}
            if type(seq) is not int or not 0 < seq <= 0xFFFFFFFF:
                raise ValueError("invalid sequence")
            if any(type(value) is not bool for value in tracked.values()):
                raise ValueError("invalid tracked state")
        except (KeyError, TypeError, ValueError, AttributeError):
            self._stop()
            return False
        if session_id != self.session_id:
            self.source_changed()
        elif self.seq is not None and seq <= self.seq:
            self._stop()
            return False
        self.session_id, self.seq, self.received_at = session_id, seq, now
        self.tracked = tracked
        for side in self.tracked:
            if not self.tracked[side]:
                self.armed[side] = False
        return True

    def warning(self, now: float) -> bool:
        """Receipt-age warning only; the hold threshold is enforced by allowed()."""
        return self.received_at is None or now - self.received_at >= self.warning_s

    def allowed(self, now: float) -> dict[str, bool]:
        if self.received_at is None or now - self.received_at >= self.timeout_s:
            self._stop()
        return {
            side: self.armed[side] and self.tracked[side] and not self.inhibited
            for side in self.tracked
        }

    def confirm(self, side: str, now: float) -> bool:
        """Call only for an explicit user action after supervision and tracking recover."""
        self.allowed(now)
        self.armed[side] = (
            self.received_at is not None and self.tracked[side] and not self.inhibited
        )
        return self.armed[side]
