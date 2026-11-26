"""Root compatibility shim aliasing ``printops.supplier.connectors``."""
from __future__ import annotations

import sys
from printops.supplier import connectors as _impl

sys.modules[__name__] = _impl
