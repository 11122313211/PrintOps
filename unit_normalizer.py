"""Root compatibility shim aliasing ``printops.domain.units``."""
from __future__ import annotations

import sys
from printops.domain import units as _impl

sys.modules[__name__] = _impl
