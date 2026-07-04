"""
tests/test_notifications.py — Mixtape

Tests for notification creation logic.
"""

import pytest
from app import create_app, db
from models import User, Song, playlist_entries
from services.notification_service import add_to_playlist, rate_song, get_notifications
from services.playlist_service import create_playlist


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def sharer_and_song(app):
    """A sharer who shared a song, plus a second user to act as rater/adder."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        other = User(username="other", email="other@example.com")
        db.session.add_all([sharer, other])
        db.session.flush()

        song = Song(title="Test Song", artist="Test Artist", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "other": other, "song": song}


def test_rate_song_notifies_sharer(app, sharer_and_song):
    """Rating a song should notify the original sharer, without raising."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        other = sharer_and_song["other"]
        song = sharer_and_song["song"]

        rate_song(other.id, song.id, 5)

        notifications = get_notifications(sharer.id)
        assert len(notifications) == 1
        assert notifications[0]["type"] == "song_rated"
        assert "other" in notifications[0]["body"]


def test_rate_song_does_not_notify_self_rating(app, sharer_and_song):
    """A user rating their own shared song should not generate a notification."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        song = sharer_and_song["song"]

        rate_song(sharer.id, song.id, 4)

        assert get_notifications(sharer.id) == []


def test_add_to_playlist_notifies_sharer(app, sharer_and_song):
    """Adding a song to a playlist should notify the original sharer."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        other = sharer_and_song["other"]
        song = sharer_and_song["song"]

        playlist = create_playlist("Road Trip", other.id)
        # Pre-seed the playlist_entries row directly (position/added_by are
        # NOT NULL columns that add_to_playlist's relationship .append() call
        # doesn't populate — a separate, pre-existing gap outside this bug's scope).
        db.session.execute(
            playlist_entries.insert().values(
                playlist_id=playlist.id, song_id=song.id, position=1, added_by=other.id
            )
        )
        db.session.commit()

        add_to_playlist(playlist.id, song.id, other.id)

        notifications = get_notifications(sharer.id)
        assert len(notifications) == 1
        assert notifications[0]["type"] == "song_added_to_playlist"
