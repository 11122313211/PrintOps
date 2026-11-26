"""Root compatibility shim aliasing ``printops.supplier.contract``."""
from __future__ import annotations

import sys
from printops.supplier import contract as _impl

sys.modules[__name__] = _impl
