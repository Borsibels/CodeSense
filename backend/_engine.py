"""Make the Phase 3-4.5 analysis engine (``backend/app``) importable from the session host.

The engine's modules import each other as the top-level package ``app`` (``from app.config import ...``),
because it is normally started from ``backend/`` as ``app.main:app``. The session host is started from the
repository root as ``backend.main:app``, where ``app`` does not exist. Aliasing ``sys.modules['app']`` to
``backend.app`` fixes that without putting ``backend/`` on ``sys.path`` (which would also expose
``backend/main.py``, ``backend/models.py`` and friends as top-level modules).

If ``app`` is already imported (for example when running the engine's own tests from ``backend/``) the
existing package is kept.
"""

import sys

from . import app as _engine_package

sys.modules.setdefault("app", _engine_package)
