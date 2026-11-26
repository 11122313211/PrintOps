"""Root compatibility shim aliasing ``printops.domain.perception_candidates``."""
from __future__ import annotations

import sys
from printops.domain import perception_candidates as _impl

sys.modules[__name__] = _impl
