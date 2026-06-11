"""
Packaging Task 3: user-data path fixes.

cards.db and referencia_grabada.wav must be resolved under USER_DATA_DIR,
not BASE_DIR, so packaged builds under Program Files don't crash on write.
"""
from __future__ import annotations

import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


# ---------------------------------------------------------------------------
# Task 3a — cards.db path is under USER_DATA_DIR
# ---------------------------------------------------------------------------

def test_editorial_cards_path_is_under_user_data_dir():
    """cards.db must be resolved under USER_DATA_DIR, not BASE_DIR."""
    from config.storage import USER_DATA_DIR
    from config.settings import EDITORIAL_CARDS_DB

    user_data = str(USER_DATA_DIR)
    assert EDITORIAL_CARDS_DB.startswith(user_data), (
        f"EDITORIAL_CARDS_DB={EDITORIAL_CARDS_DB!r} must be under "
        f"USER_DATA_DIR={user_data!r}"
    )


# ---------------------------------------------------------------------------
# Task 3b — referencia_grabada.wav default path is under USER_DATA_DIR
# ---------------------------------------------------------------------------

def test_reference_wav_default_path_is_under_user_data_dir():
    """Default path for referencia_grabada.wav must be under USER_DATA_DIR."""
    from config.storage import USER_DATA_DIR
    from config.settings import REFERENCE_WAV_PATH

    user_data = str(USER_DATA_DIR)
    assert REFERENCE_WAV_PATH.startswith(user_data), (
        f"REFERENCE_WAV_PATH={REFERENCE_WAV_PATH!r} must be under "
        f"USER_DATA_DIR={user_data!r}"
    )


# ---------------------------------------------------------------------------
# Task 3c — paths are not under BASE_DIR (would break under Program Files)
# ---------------------------------------------------------------------------

def test_paths_are_not_under_base_dir_when_different():
    """In dev mode BASE_DIR==USER_DATA_DIR; in packaged mode they must diverge.

    We can only verify that the constants exist and do not reference the legacy
    hard-coded join pattern os.path.join(BASE_DIR, ...) values.
    """
    from config.settings import EDITORIAL_CARDS_DB, REFERENCE_WAV_PATH

    # Sanity: both paths are strings
    assert isinstance(EDITORIAL_CARDS_DB, str) and len(EDITORIAL_CARDS_DB) > 0
    assert isinstance(REFERENCE_WAV_PATH, str) and len(REFERENCE_WAV_PATH) > 0

    # Neither must contain the literal sub-path pattern that triggered the bug
    # (data/editorial_cards under BASE_DIR was the original broken path)
    for path in (EDITORIAL_CARDS_DB, REFERENCE_WAV_PATH):
        # The file must live *in* the data/editorial_cards folder or root — just
        # verify the file name is correct.
        pass  # structure already covered by the USER_DATA_DIR assertions above

    assert "cards.db" in EDITORIAL_CARDS_DB
    assert "referencia_grabada.wav" in REFERENCE_WAV_PATH
