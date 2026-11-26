"""Root compatibility shim aliasing ``printops.storage.sqlite``."""
from __future__ import annotations

import sys
from printops.storage import sqlite as _impl

sys.modules[__name__] = _impl
