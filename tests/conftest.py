import pytest


@pytest.fixture(autouse=True)
def mute_windows_notifications(monkeypatch):
    monkeypatch.setattr("app.export.show_notification", lambda title, body: None)
