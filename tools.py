"""Root compatibility shim aliasing ``printops.tools.registry``."""
from __future__ import annotations

import sys
from printops.tools import registry as _impl

sys.modules[__name__] = _impl
