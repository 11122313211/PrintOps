"""Root compatibility shim aliasing ``printops.llm.adapter``."""
from __future__ import annotations

import sys
from printops.llm import adapter as _impl

sys.modules[__name__] = _impl
