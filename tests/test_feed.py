"""
tests/test_feed.py — Mixtape

Tests for the "Friends Listening Now" feed logic.
"""

import pytest
from datetime import datetime, time, timedelta, timezone
from app import create_app, db
from models import User, Song, ListeningEvent, friendships
from services.feed_service import get_friends_listening_now


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def user_and_friend(app):
    """Two users, friends with each other."""
    with app.app_context():
        user = User(username="me", email="me@example.com")
        friend = User(username="friend", email="friend@example.com")
        db.session.add_all([user, friend])
        db.session.flush()

        db.session.execute(friendships.insert().values(user_id=user.id, friend_id=friend.id))
        db.session.execute(friendships.insert().values(user_id=friend.id, friend_id=user.id))
        db.session.commit()

        yield {"user": user, "friend": friend}


def _song(owner_id, title="Song"):
    song = Song(title=title, artist="Artist", shared_by=owner_id)
    db.session.add(song)
    db.session.flush()
    return song


def test_friend_listening_today_shows_up(app, user_and_friend):
    """A friend who listened earlier today should appear in the feed."""
    with app.app_context():
        user = user_and_friend["user"]
        friend = user_and_friend["friend"]
        song = _song(friend.id, "Fresh Track")

        now = datetime.now(timezone.utc)
        db.session.add(ListeningEvent(user_id=friend.id, song_id=song.id, listened_at=now))
        db.session.commit()

        result = get_friends_listening_now(user.id)
        assert len(result) == 1
        assert result[0]["friend"]["username"] == "friend"
        assert result[0]["song"]["title"] == "Fresh Track"


def test_friend_listening_yesterday_within_24h_is_excluded(app, user_and_friend):
    """
    A friend who last listened yesterday (calendar day) should NOT show up,
    even if under 24 hours have passed — this was the reported bug
    ("shows people from yesterday").
    """
    with app.app_context():
        user = user_and_friend["user"]
        friend = user_and_friend["friend"]
        song = _song(friend.id, "Late Night Track")

        # Yesterday at 23:59:59 is always < 24h before "now", regardless of
        # what time the test happens to run, and always a different calendar date.
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        listened_at = datetime.combine(yesterday, time(23, 59, 59), tzinfo=timezone.utc)
        db.session.add(ListeningEvent(user_id=friend.id, song_id=song.id, listened_at=listened_at))
        db.session.commit()

        result = get_friends_listening_now(user.id)
        assert result == []


def test_friend_listening_over_24h_ago_is_excluded(app, user_and_friend):
    """A friend who listened more than 24 hours ago should not show up."""
    with app.app_context():
        user = user_and_friend["user"]
        friend = user_and_friend["friend"]
        song = _song(friend.id, "Old Track")

        listened_at = datetime.now(timezone.utc) - timedelta(hours=30)
        db.session.add(ListeningEvent(user_id=friend.id, song_id=song.id, listened_at=listened_at))
        db.session.commit()

        result = get_friends_listening_now(user.id)
        assert result == []


def test_only_most_recent_song_per_friend_shown(app, user_and_friend):
    """If a friend listened to two songs today, only the most recent should appear."""
    with app.app_context():
        user = user_and_friend["user"]
        friend = user_and_friend["friend"]
        older_song = _song(friend.id, "Older Track")
        newer_song = _song(friend.id, "Newer Track")

        now = datetime.now(timezone.utc)
        db.session.add(ListeningEvent(user_id=friend.id, song_id=older_song.id, listened_at=now - timedelta(minutes=10)))
        db.session.add(ListeningEvent(user_id=friend.id, song_id=newer_song.id, listened_at=now - timedelta(minutes=1)))
        db.session.commit()

        result = get_friends_listening_now(user.id)
        assert len(result) == 1
        assert result[0]["song"]["title"] == "Newer Track"


def test_non_friend_listening_is_excluded(app):
    """A user with no friends gets an empty feed even if others are listening."""
    with app.app_context():
        user = User(username="loner", email="loner@example.com")
        stranger = User(username="stranger", email="stranger@example.com")
        db.session.add_all([user, stranger])
        db.session.flush()

        song = _song(stranger.id, "Not My Friend's Track")
        db.session.add(ListeningEvent(user_id=stranger.id, song_id=song.id, listened_at=datetime.now(timezone.utc)))
        db.session.commit()

        result = get_friends_listening_now(user.id)
        assert result == []
