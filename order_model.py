"""Root compatibility shim aliasing ``printops.domain.order_model``."""
from __future__ import annotations

import sys
from printops.domain import order_model as _impl

sys.modules[__name__] = _impl
