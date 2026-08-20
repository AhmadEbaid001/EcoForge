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


def anchor_dir() -> Path:
    """Where the deployment writes what it generates.

    `data/` is shipped input - the portfolio, the catalog, the parameters - and
    docker-compose.yml mounts it READ-ONLY into both services on purpose. `anchor/`
    is the bind-mounted directory the containers may write, and it already holds the
    integrity anchor and the live simulator's ground truth.
    """
    override = os.environ.get("GEMP_ANCHOR_DIR")
    if override:
        return Path(override)
    return data_dir().parent / "anchor"


def ground_truth_write_path() -> Path:
    """Where `gemp.seed` puts the ground truth it generates.

    Always the writable directory. It used to be `data/ground_truth.csv`, which is
    generated output living among shipped inputs and worked only because it was
    always written by a developer running from a checkout. Inside a container that
    path is mounted read-only, so seeding a deployed host failed with

        OSError: [Errno 30] Read-only file system: '/app/data/ground_truth.csv'

    after the rows had already gone into the database - leaving readings with no
    ground truth to score them against.
    """
    return anchor_dir() / "ground_truth.csv"


def ground_truth_read_path() -> Path:
    """Where to look for it, newest location first.

    A checkout seeded before the move still has `data/ground_truth.csv` and it is
    still read, so an existing machine keeps working until it is next seeded. The
    writable copy wins when both exist, because that is the one a re-seed updates.
    """
    preferred = ground_truth_write_path()
    if preferred.exists():
        return preferred
    legacy = data_dir() / "ground_truth.csv"
    if legacy.exists():
        return legacy
    return preferred
