"""Horloge injectable : les tests simulent le passage du temps sans attendre."""

from collections.abc import Callable
from datetime import UTC, datetime

Horloge = Callable[[], datetime]


def maintenant_utc() -> datetime:
    return datetime.now(UTC)
