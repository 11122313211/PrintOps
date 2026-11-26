"""Root compatibility shim aliasing ``printops.pricing.schema``."""
from __future__ import annotations

import sys
from printops.pricing import schema as _impl

sys.modules[__name__] = _impl
