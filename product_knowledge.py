"""Root compatibility shim aliasing ``printops.domain.knowledge``."""
from __future__ import annotations

import sys
from printops.domain import knowledge as _impl

sys.modules[__name__] = _impl
