"""Root compatibility shim aliasing ``printops.agent.core``."""
from __future__ import annotations

import sys
from printops.agent import core as _impl

sys.modules[__name__] = _impl
