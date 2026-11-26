"""Root compatibility shim aliasing ``printops.domain.measurement``."""
from __future__ import annotations

import sys
from printops.domain import measurement as _impl

sys.modules[__name__] = _impl
