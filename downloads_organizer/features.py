"""Switches for functions that exist in the code but are not released (R3B-5).

``CROSS_VOLUME_COPY`` (barrier 1): the cross-volume COPY of ``runner.run_plan``. Off in
every build: a plan for two volumes is refused by the preview and by the runner, also
when a caller asks for a copy. It is read at call time (``cross_volume_copy_enabled``),
so only code that sets this module attribute, as the tests do, turns it on. It is no
permission of its own: the ACL policy (barrier 2, ``runner.AclGate``) is checked too.
"""

from __future__ import annotations

CROSS_VOLUME_COPY = False  # OWNER_DECISION: stays False until the owner releases the copy


def cross_volume_copy_enabled() -> bool:
    """Whether a cross-volume copy may be planned and run at all (exactly ``True``)."""
    return CROSS_VOLUME_COPY is True
