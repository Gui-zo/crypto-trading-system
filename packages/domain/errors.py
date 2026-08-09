"""Base domain exceptions.

Domain code raises these rather than generic exceptions so that application and
API layers can map them to structured error codes without string matching.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain-level errors."""


class InvalidModeTransition(DomainError):
    """Raised when a trading-mode transition violates the safety state machine."""

    def __init__(self, from_mode: object, to_mode: object, reason: str) -> None:
        self.from_mode = from_mode
        self.to_mode = to_mode
        self.reason = reason
        super().__init__(f"Illegal transition {from_mode} -> {to_mode}: {reason}")
