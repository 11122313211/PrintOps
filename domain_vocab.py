"""Root compatibility shim aliasing ``printops.domain.vocab``."""
from __future__ import annotations

import sys
from printops.domain import vocab as _impl

sys.modules[__name__] = _impl
