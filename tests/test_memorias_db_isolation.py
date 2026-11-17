"""T6(c): the suite must never write the developer's real memorias.db.

Root cause of the `test-profile` / `p1` / `bench` rows in
data/memorias/memorias.db: tests built a real MotorVocalIA, set a profile id and
committed a capturable pair without redirecting `llm_engine.MEMORIAS_DB` (e.g.
test_context_budget_blind_window.py's `motor_instance`), so capture-at-commit
upserted drafts into USER_DATA_DIR. conftest now pins every bound copy of the
path to tmp for every test; a test may still override it explicitly.
"""

from pathlib import Path

import opencohost.api.main as main_mod
import opencohost.config.settings as settings_mod
import opencohost.core.llm_engine as llm_engine


def _real():
    return Path(settings_mod.MEMORIAS_DB).resolve()


def test_engine_memorias_path_is_not_the_real_store():
    assert Path(llm_engine.MEMORIAS_DB).resolve() != _real()


def test_api_memorias_path_is_not_the_real_store():
    assert Path(main_mod.MEMORIAS_DB).resolve() != _real()

