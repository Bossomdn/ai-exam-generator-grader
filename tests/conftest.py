import pytest

from src import config


@pytest.fixture(autouse=True)
def temp_database(tmp_path, monkeypatch):
    """ทุกเทสต์ใช้ฐานข้อมูลชั่วคราว ไม่แตะ data/school.db จริง"""
    path = tmp_path / "data" / "school.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    return path
