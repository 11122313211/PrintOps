"""Root compatibility shim aliasing ``printops.storage.backup``."""
from __future__ import annotations

import sys
from printops.storage import backup as _impl

sys.modules[__name__] = _impl
