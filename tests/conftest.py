import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated state dir and project dir for one test."""
    home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("OPENSITERM_HOME", str(home))
    monkeypatch.setenv("OPENSITERM_PROJECT", str(project))
    monkeypatch.setenv("PYTHONPATH", str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.chdir(project)
    yield project
    from opensiterm import api
    try:
        api.close(all=True)
    except Exception:
        pass
