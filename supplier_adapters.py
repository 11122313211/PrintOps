"""Root compatibility shim aliasing ``printops.supplier.adapters``."""
from __future__ import annotations

import sys
from printops.supplier import adapters as _impl

sys.modules[__name__] = _impl
