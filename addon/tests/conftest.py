"""Make the add-on package importable as ``ci_buddy`` without a running Anki.

The add-on lives in ``addon/ci_buddy``; add ``addon/`` to ``sys.path`` so tests
can ``import ci_buddy.core`` / ``ci_buddy.provisioners`` directly. Neither of
those import paths touches ``aqt`` (the provisioners import it lazily), so the
suite runs with stdlib + pytest only.
"""

import sys
from pathlib import Path

ADDON_ROOT = Path(__file__).resolve().parent.parent  # .../addon
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))
