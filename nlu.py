"""Root compatibility shim aliasing ``printops.domain.nlu``."""
from __future__ import annotations

import sys
from printops.domain import nlu as _impl

sys.modules[__name__] = _impl
