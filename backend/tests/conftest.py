import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例一份临时落盘文件 + 种子数据，互不污染仓库里的运行态文件。"""
    from fastapi.testclient import TestClient

    from app import store as store_module

    state_file = tmp_path / "runtime_state.json"
    monkeypatch.setattr(store_module, "STATE_FILE", str(state_file))
    store_module.store.reset()

    from app.main import app

    return TestClient(app)
