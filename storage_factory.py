"""Root compatibility shim aliasing ``printops.storage.factory``."""
from __future__ import annotations

import sys
from printops.storage import factory as _impl

sys.modules[__name__] = _impl
