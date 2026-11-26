"""Root compatibility shim aliasing ``printops.storage.contract``."""
from __future__ import annotations

import sys
from printops.storage import contract as _impl

sys.modules[__name__] = _impl
