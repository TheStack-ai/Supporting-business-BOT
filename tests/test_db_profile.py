import importlib


def test_default_profile_min_score_is_60(tmp_path, monkeypatch):
    db = importlib.import_module("src.db")
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.delenv("PROFILE_MIN_SCORE", raising=False)

    db.init_db()

    assert db.get_profile()["min_score"] == 60


def test_invalid_profile_min_score_falls_back_to_60(tmp_path, monkeypatch):
    db = importlib.import_module("src.db")
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv("PROFILE_MIN_SCORE", "not-a-number")

    db.init_db()

    assert db.get_profile()["min_score"] == 60
