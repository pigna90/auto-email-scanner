"""Delimiters decide *when* to end a mail item and *when* to end a session.

This is the seam where the future GPIO button drops in. The session loop only
knows about the `Delimiter` interface; it doesn't care whether the boundary
comes from a timer (v1) or a hardware button (v2).

    Action.NONE            -> keep scanning into the current mail item
    Action.FINALIZE_EMAIL  -> close current PDF, next sheet starts a new one
    Action.END_SESSION     -> stop polling, let the scanner sleep
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class Action(Enum):
    NONE = auto()
    FINALIZE_EMAIL = auto()
    END_SESSION = auto()


@dataclass
class Context:
    pending_pages: int          # pages buffered in the current mail item
    idle_seconds: float         # time since the last sheet was scanned


class Delimiter:
    """Base interface. Subclass and override :meth:`poll`."""

    def poll(self, ctx: Context) -> Action:  # pragma: no cover - interface
        raise NotImplementedError


class TimeoutDelimiter(Delimiter):
    """v1 delimiter: purely time based.

    * Finalize the current mail item after `email_timeout` seconds of no sheet.
    * End the session after `session_idle_timeout` seconds with nothing pending
      (so the scanner can sleep).
    """

    def __init__(self, email_timeout: float, session_idle_timeout: float):
        self.email_timeout = email_timeout
        self.session_idle_timeout = session_idle_timeout

    def poll(self, ctx: Context) -> Action:
        if ctx.pending_pages > 0 and ctx.idle_seconds >= self.email_timeout:
            return Action.FINALIZE_EMAIL
        if ctx.pending_pages == 0 and ctx.idle_seconds >= self.session_idle_timeout:
            return Action.END_SESSION
        return Action.NONE


# --- v2 sketch (not wired up yet) -------------------------------------------
# A GPIO button makes the boundary explicit instead of timed. Implement a
# background listener that sets a flag on button-press, then:
#
# class GpioDelimiter(Delimiter):
#     def __init__(self, pin, session_idle_timeout):
#         self._pressed = threading.Event()
#         # ... set up gpiozero Button(pin).when_pressed = self._pressed.set
#         self.session_idle_timeout = session_idle_timeout
#     def poll(self, ctx):
#         if self._pressed.is_set():
#             self._pressed.clear()
#             return Action.FINALIZE_EMAIL      # button = "this mail is done"
#         if ctx.pending_pages == 0 and ctx.idle_seconds >= self.session_idle_timeout:
#             return Action.END_SESSION
#         return Action.NONE
#
# The session loop stays identical — only this class changes.
