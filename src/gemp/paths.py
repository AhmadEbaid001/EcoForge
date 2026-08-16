"""Where `data/` lives.

Resolving it relative to the package source works in a src-layout checkout and
breaks the moment the package is installed: in the container the code sits under
site-packages, so walking up three parents lands in the Python installation rather
than the project. That failure only appears once the image actually runs, which is
exactly the class of bug worth removing with an explicit rule.

Resolution order:

1. `GEMP_DATA_DIR`, if set. The container sets it; anything unusual can too.
2. `./data` relative to the working directory. Covers the container's /app and a
   developer running from the project root.
3. The checkout-relative path. Covers a developer running from a subdirectory.
"""

from __future__ import annotations

import os
from pathlib import Path

_CHECKOUT_DATA = Path(__file__).resolve().parents[2] / "data"


def data_dir() -> Path:
    override = os.environ.get("GEMP_DATA_DIR")
    if override:
        return Path(override)

    cwd_data = Path.cwd() / "data"
    if cwd_data.is_dir():
        return cwd_data

    return _CHECKOUT_DATA
