"""Root compatibility entrypoint for ``printops.mcp.server``."""
from __future__ import annotations

import sys
from printops.mcp import server as _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())
else:
    sys.modules[__name__] = _impl
