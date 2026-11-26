"""Root compatibility shim aliasing ``printops.storage.policy``."""
from __future__ import annotations

import sys
from printops.storage import policy as _impl

sys.modules[__name__] = _impl
