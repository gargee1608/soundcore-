"""Stream-capacity decisions. The numeric cap is configuration, not a guess baked into code paths."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapacityDecision:
    active: int
    cap: int
    level: str  # ok | warning | blocked
    message: str

    @property
    def can_start(self) -> bool:
        return self.level != "blocked"

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "cap": self.cap,
            "level": self.level,
            "message": self.message,
            "can_start": self.can_start,
        }


def evaluate_capacity(active: int, cap: int, *, warn_ratio: float = 0.8) -> CapacityDecision:
    if cap <= 0:
        return CapacityDecision(
            active=active,
            cap=cap,
            level="blocked",
            message="Stream cap is zero, so new streams stay off.",
        )
    if active >= cap:
        return CapacityDecision(
            active=active,
            cap=cap,
            level="blocked",
            message=f"Stream cap reached ({active} of {cap}). New channels stay off until one is stopped. This Pi will not silently add another stream.",
        )
    ratio = active / cap
    if ratio >= warn_ratio:
        return CapacityDecision(
            active=active,
            cap=cap,
            level="warning",
            message=f"Performance warning: {active} of {cap} streams are active. Another one still fits, and the next after that will be refused.",
        )
    return CapacityDecision(
        active=active,
        cap=cap,
        level="ok",
        message=f"{active} of {cap} streams active.",
    )


def can_start_another(active: int, cap: int) -> bool:
    return evaluate_capacity(active, cap).can_start and active < cap
