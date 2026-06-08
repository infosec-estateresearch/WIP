"""Feature flags for the WIP web app.

WO-27: manual WIP writes are built but GATED until the Mastercopy import is
formally cut over as the authoritative WIP source. Keeping the flag in one place
means flipping it later (WO-27B) enables the already-built write path with no
redesign — set WIP_MANUAL_WRITES_ENABLED=true (or 1/yes/on) in the environment.

Default is OFF: forms and validation render, but POST writes are refused
server-side and the Submit button is disabled in the UI.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}


def manual_writes_enabled() -> bool:
    """True only when manual WIP writes have been explicitly enabled."""
    return os.environ.get("WIP_MANUAL_WRITES_ENABLED", "false").strip().lower() in _TRUTHY


# Plain-English reason shown on the form when writes are gated off. Kept here so
# the wording is consistent across the add and edit screens.
CUTOVER_MESSAGE = (
    "Manual WIP entry is prepared but not yet enabled. The Mastercopy import is "
    "still the active source for WIP. This form will be enabled when WIP is cut "
    "over as the live source."
)
SUBMIT_DISABLED_HELP = "Available after WIP cutover"
