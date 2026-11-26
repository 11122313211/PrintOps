"""Root compatibility shim aliasing ``printops.runtime.stdio_utf8``."""
from __future__ import annotations

import sys
from printops.runtime import stdio_utf8 as _impl

sys.modules[__name__] = _impl
