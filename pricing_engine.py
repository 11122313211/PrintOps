"""Root compatibility shim aliasing ``printops.pricing.engine``."""
from __future__ import annotations

import sys
from printops.pricing import engine as _impl

sys.modules[__name__] = _impl
