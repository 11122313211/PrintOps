"""Root compatibility shim aliasing ``printops.runtime.deadline``."""
from __future__ import annotations

import sys
from printops.runtime import deadline as _impl

sys.modules[__name__] = _impl
