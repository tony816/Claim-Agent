"""Compatibility alias for the pre-rename package path (``claim_copa``).

Saved commands, reports and worker processes created before the rename keep
working: ``python -m claim_copa.cli`` resolves to the same files as
``claim_agent`` without copying runtime code.
"""

from claim_agent import EXECUTION_PROFILE, PROTOCOL_VERSION, __path__, __version__

__all__ = ["EXECUTION_PROFILE", "PROTOCOL_VERSION", "__version__"]
