"""Root compatibility shim aliasing ``printops.storage.memory``."""
from __future__ import annotations

import sys
from printops.storage import memory as _impl

sys.modules[__name__] = _impl
