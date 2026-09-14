"""Public Claim-Agent namespace, sharing the existing runtime implementation.

Keep the implementation path stable for workers started before the rename.
Both module entry points remain usable without copying runtime code.
"""

from claim_copa import EXECUTION_PROFILE, PROTOCOL_VERSION, __path__, __version__

__all__ = ["EXECUTION_PROFILE", "PROTOCOL_VERSION", "__version__"]
