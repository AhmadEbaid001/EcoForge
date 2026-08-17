"""Dependencies shared by every router.

`get_session` lives here rather than in `main.py` for a mechanical reason worth
recording: FastAPI captures the dependency CALLABLE when the decorator runs, so a
router that defines its own placeholder and expects the application to reassign the
module attribute later ends up holding the placeholder forever. One object, imported
by everyone, is the version that works - and it is also the object the test suite
overrides, so overriding it covers every router at once.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from gemp.db import get_sessionmaker


def get_session() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
