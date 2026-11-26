"""Root compatibility shim aliasing ``printops.domain.order_contract``."""
from __future__ import annotations

import sys
from printops.domain import order_contract as _impl

sys.modules[__name__] = _impl
