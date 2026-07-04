# Mixtape Bug Hunt — Submission

## AI Usage

I used AI tools throughout this project primarily for navigating and explaining code I had already located myself, not for guessing where bugs lived before reading the code.

- **File summary / orientation.** For each service file, I gave the AI the full file contents and asked "What is this module responsible for? What are its main functions and what does each one do?" This is how I built the first draft of the codebase map below, before opening any of the five issues.
- **Data flow tracing.** I asked the AI to trace "how does a song get added to a user's feed" and separately "what happens when a user rates a song" using the routes/ and services/ files as context. This confirmed the route → service delegation pattern and pointed me at `notification_service.rate_song()` as the place to look for Issue #4.
- **Function explanation for Issue #1.** I gave the AI the original `update_listening_streak()` function and asked what edge cases could make it return the wrong value. It correctly identified that `today.weekday() != 6` excludes Sundays from the increment branch, which matched what I'd already suspected from the bug title. I verified this myself by running `update_listening_streak()` directly against a Saturday/Sunday pair of datetimes in a Python shell before touching the code.
- **Structural diff for Issue #4.** I gave the AI the `add_to_playlist()` and `rate_song()` functions side by side and asked for the structural difference between them. It pointed out that `add_to_playlist()` calls `create_notification()` after its DB commit and `rate_song()` doesn't — which is what led me to add the missing notification call.
- **Where I had to course-correct the AI/myself.** For Issue #3, my first assumption (partly AI-suggested) was that the existing `test_search_no_duplicates_multi_tag_song` test would fail on the buggy code and pass once fixed, so a passing test would be enough proof. When I actually ran the test against the untouched original file (via `git stash`), it passed on both the buggy and fixed versions. I had to dig in myself with a raw SQL query against the test database to confirm that the `outerjoin` to `song_tags` really does produce 3 duplicate rows at the SQL level for a 3-tag song — but SQLAlchemy's legacy `Query.all()` API silently collapses duplicate full-entity rows before the list is returned, which is why the existing test didn't catch it in this environment. The takeaway: I verified the root cause by reading actual SQL output, not by trusting a test's pass/fail alone, and I made the fix explicit (`.distinct()` on the query) rather than relying on that implicit, version-dependent ORM behavior.
- **Datetime semantics for Issue #2.** I asked the AI to explain the difference between comparing full `datetime` objects vs. `.date()` objects when checking "did this happen today," which confirmed that comparing `.date()` values (rather than a raw time delta) is the correct way to detect a calendar-day boundary, independent of what time of day the check runs.

## Codebase Map

*(Written from reading the app's structure and data model — not from the bug list.)*

**Main files:**

- `app.py` — Flask application factory (`create_app`) and SQLAlchemy `db` initialization. Nothing else lives here; it's pure setup.
- `models.py` — Defines all 7 SQLAlchemy entities: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`, plus three association tables: `friendships` (self-referential many-to-many on `User`, one row per direction), `song_tags` (many-to-many between `Song` and `Tag`), and `playlist_entries` (many-to-many between `Playlist` and `Song`, but with extra columns — `position` and `added_by` — so playlist membership carries ordering and provenance, not just a boolean link). Every model has a `to_dict()` method used to serialize it for API responses.
- `routes/songs.py`, `routes/playlists.py`, `routes/users.py`, `routes/feed.py` — Flask route handlers. Each one parses the request, calls exactly one service function, and formats the response. There is no business logic in this layer.
- `services/streak_service.py` — Owns listening-streak bookkeeping: given a listening event's timestamp, decides whether a user's streak increments, resets, or stays the same.
- `services/feed_service.py` — Owns two read-only feed views: `get_friends_listening_now()` (a tight recency window, meant to feel "live") and `get_activity_feed()` (an unfiltered, most-recent-N view of all friend activity).
- `services/search_service.py` — Owns song search by title/artist substring match, including attaching each song's tags to the result.
- `services/notification_service.py` — Owns notification creation and retrieval. `create_notification()` is a single shared helper; other functions in this file (and this file's functions get called from other services, e.g. `playlist_service`) call it when a friend interacts with a user's shared song.
- `services/playlist_service.py` — Owns playlist creation and retrieval, including returning a playlist's songs in position order.
- `tests/` — One test file per service (`test_streaks.py`, `test_search.py`, `test_playlists.py`, plus `test_feed.py` and `test_notifications.py` added in this project), using an in-memory SQLite DB per test via a `create_app`/`db.create_all()` fixture.

**Data flow — a user rates a song:**

`POST /songs/<song_id>/rate` (in `routes/songs.py`) parses the score from the request body and calls `notification_service.rate_song(user_id, song_id, score)`. That function:

1. Validates the score is between 1 and 5.
2. Loads the `Song` and the rating `User` by ID.
3. Checks for an existing `Rating` row for this `(user_id, song_id)` pair (there's a DB-level `UniqueConstraint` on that pair, so a user can only have one rating per song — rating again updates the existing row rather than creating a second one).
4. Commits the rating.
5. If the rater isn't the song's original sharer, calls `create_notification()` to notify `song.shared_by`.

There's no separate "ratings feed" — a rating is just a row on the `Rating` table keyed by user and song, and the only side effect of rating is this one notification.

**Patterns I noticed:**

- Routes are a thin translation layer; all business logic and all DB queries live in `services/`.
- Every service function that mutates data looks up its referenced rows first and raises `ValueError` if any are missing, before doing any writes.
- Notifications are always created via the single shared `create_notification()` helper, called from whichever service triggered the interaction — there's no central "event bus," each interaction site is responsible for calling it directly.
- Many-to-many relationships that need extra metadata (`playlist_entries` needing `position`/`added_by`, unlike the simpler `song_tags`) are modeled as explicit `db.Table` association tables rather than plain relationship-only associations.

---

## Root Cause Analysis

### Issue #1 — My listening streak keeps resetting

**How I reproduced it:** I opened a Python shell with the app context and called `update_listening_streak()` directly with two controlled datetimes: a Saturday (`2024-06-15`) followed by a Sunday (`2024-06-16`), one day apart. On the original code, the streak went from 1 back to 1 instead of incrementing to 2 — i.e., it reset even though the user listened on consecutive days. The existing `test_streak_increments_on_sunday` test encodes exactly this scenario and fails against the original file (confirmed by `git stash`-ing the fix and re-running pytest).

**How I found the root cause:** `streak_service.py` is small enough to read end to end. `update_listening_streak()` computes `days_since_last = (today - last_date).days` and branches on that value: `== 0` (no change), `== 1` (increment), else (reset to 1). The original `elif` read `days_since_last == 1 and today.weekday() != 6`. The moment I was confident this was the actual cause (not just "something about Sundays") was checking Python's `datetime.weekday()` docs/behavior directly: `weekday()` returns `6` for Sunday. So `!= 6` evaluates to `False` on every Sunday, which means the `elif` condition as a whole is `False` on Sundays regardless of `days_since_last`, and execution falls through to the `else` branch that resets the streak to 1.

**The root cause:** The increment branch required `days_since_last == 1 AND today.weekday() != 6`. That second clause has nothing to do with correctly detecting a consecutive day — it specifically excludes Sundays from ever being treated as a "next day" increment. So any user whose most recent listen was yesterday, and who listens again today when today happens to be a Sunday, gets their streak reset to 1 instead of incremented, even though by the documented rule ("listened yesterday → streak increments by 1") they should increment.

**Fix and side-effect check:** I removed the `and today.weekday() != 6` clause entirely, leaving `elif days_since_last == 1:`. This is the smallest change that satisfies the documented streak rule for every day of the week, Sunday included. To check I hadn't broken the other branches, I re-ran `test_streak_does_not_double_count_same_day` (confirms `days_since_last == 0` still short-circuits correctly) and `test_streak_resets_after_skipped_day` (confirms the `else` reset branch still fires when more than one day is skipped) — both still pass, so the fix only changes behavior on the specific Sunday-consecutive-day case it was meant to fix.

---

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it:** I called `get_friends_listening_now()` for a user with one friend, after inserting a `ListeningEvent` for that friend timestamped at `23:59:59 UTC` on the previous calendar day. Because `RECENT_THRESHOLD` is a rolling 24-hour window (`cutoff = now - timedelta(hours=24)`), that event is always less than 24 hours old relative to "now" regardless of what time the test runs — but it happened on a different calendar day. On the original code, that friend still showed up in the "listening now" feed.

**How I found the root cause:** `feed_service.py` has two similar-looking functions: `get_friends_listening_now()` and `get_activity_feed()`. The docstring on `get_activity_feed()` explicitly says "unlike `get_friends_listening_now`, this is not filtered by recency," which told me the recency filtering was supposed to live entirely in the first function. Reading `get_friends_listening_now()`, the only recency filter applied was the SQL-level `ListeningEvent.listened_at >= cutoff` in the query — a pure 24-hour lookback with no concept of "today" as a calendar day. That's the moment I was confident: the function's name and purpose ("Now") implied same-day activity, but the only filter implemented was a sliding window that happily spans two calendar days.

**The root cause:** A 24-hour rolling window is not the same thing as "today." Someone who listened at 11:58pm yesterday is still within the 24-hour window at 11pm today (nearly 23 hours later) — well within a plausible "yesterday" framing from the user's perspective, even though technically under 24 hours have elapsed. The function only checked elapsed time, never checked whether the event happened on today's calendar date.

**Fix and side-effect check:** I added `event.listened_at.date() == datetime.now(timezone.utc).date()` as an additional condition alongside the existing dedup check, so an event must be both within the 24-hour window and on today's UTC calendar date to be included. I checked that `get_activity_feed()` — which is documented as intentionally unfiltered by recency — was left untouched and still returns events regardless of date, since it doesn't share this code path. I also wrote `tests/test_feed.py` covering: a friend listening earlier today (shows up), a friend at yesterday 23:59:59 (excluded — the exact reported bug), a friend over 24h ago (excluded), only-the-most-recent-song-per-friend (dedup still works), and a non-friend (excluded). I confirmed the "yesterday" test is a real regression test by stashing the fix and re-running: it fails on the original code and passes after the fix.

---

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it:** Using the pattern already set up in `tests/test_search.py`'s `seed_songs` fixture (a song with 3 tags), I ran a raw query mirroring `search_service.search_songs()`'s SQL directly against the test database and inspected the row count. The raw SQL (`SELECT ... FROM song LEFT OUTER JOIN song_tags ...`) returned 3 rows for the one 3-tag song, one per tag — confirming the duplication happens at the database level, conditional on how many tags a song has (a 0- or 1-tag song produces 1 row; a 3-tag song produces 3).

**How I found the root cause:** `search_service.search_songs()` builds one query: it joins `Song` to `song_tags` (`outerjoin(song_tags, Song.id == song_tags.c.song_id)`) but its `.filter()` only checks `Song.title`/`Song.artist` — it never filters or selects anything from `song_tags` or `Tag`. That told me the join exists but isn't used for filtering, so it can only be there to fan out rows. I initially assumed the existing `test_search_no_duplicates_multi_tag_song` test would prove whether a fix was needed, but running it against the untouched original file (`git stash`) showed it passed even without any fix — legacy SQLAlchemy's `db.session.query(Song).all()` was silently collapsing the duplicate full-entity rows before returning the list, which is why the test never observed the raw duplication. The moment of confidence was seeing the mismatch directly: 3 rows from raw SQL, but only 1 object back from the ORM query with no fix applied — proving the *query* was the root cause, and that the app should not depend on that implicit ORM behavior to hide it.

**The root cause:** The outer join to `song_tags` is a many-to-one join relative to a multi-tag song — for a song with N tags, the join produces N matching rows in the result set (one per tag). Because the query returns `Song` (a single entity), nothing in `search_songs()` explicitly deduplicates by `Song.id`, so the number of times a song appears in the result is a side effect of how many tags it has, not a bug that shows for all songs equally — exactly the "conditional" nature described in the bug report.

**Fix and side-effect check:** I added `.distinct()` to the query chain (deduplicating on the full `Song` row, which is unique per song), and removed the leftover `set(results)` line, which computed a set but never assigned or used it — a no-op that fixed nothing. I re-ran the full `test_search.py` suite: `test_search_no_duplicates_multi_tag_song` (3-tag song → 1 result), `test_search_no_duplicates_single_tag_song` and `test_search_no_duplicates_no_tag_song` (unaffected, still 1 result each — confirming `.distinct()` doesn't accidentally merge genuinely different songs), and `test_search_returns_matching_songs` (basic match still works). All pass.

---

### Issue #4 — I got notified when a friend added my song to a playlist but not when they rated it

**How I reproduced it:** I called `notification_service.rate_song(rater_id, song_id, score)` directly in a Python shell against a seeded `Song`/`User` pair, and separately called `add_to_playlist()`. On the version of the code I inherited, `add_to_playlist()` had already been edited to remove its notification call, and `rate_song()` had a new notification block that crashed immediately: `AttributeError: 'str' object has no attribute 'username'`. So the actual state was worse than the original report — neither interaction was working correctly.

**How I found the root cause:** I read `routes/songs.py` to confirm `rate_song()` is what the rate-song route calls, then read `notification_service.py` top to bottom and compared `add_to_playlist()`'s structure line-by-line against `rate_song()`'s, since the working case (playlist-add) and the missing case (rating) live in the same file with the same shared `create_notification()` helper. That comparison is what made me confident of two separate things: (1) `add_to_playlist()`'s notification block had been deleted outright — the function no longer called `create_notification()` at all — and (2) the notification block added to `rate_song()` referenced `user_id.username`, but `user_id` in that function's signature is a string ID parameter, not a `User` object; the actual `User` object was already loaded a few lines earlier as `rater = db.session.get(User, user_id)`.

**The root cause:** Two independent defects in the same file: `add_to_playlist()` was missing its notification call entirely (regressing previously-working behavior), and `rate_song()`'s notification call referenced the wrong variable — a raw ID string instead of the loaded `User` object — which crashes any time a rating is submitted for a song the rater doesn't own, since `str` has no `.username` attribute.

**Fix and side-effect check:** I restored `add_to_playlist()`'s original notification block (create a `song_added_to_playlist` notification for `song.shared_by`, skipped if the adder is the sharer), and changed `rate_song()`'s notification body to use `rater.username` instead of `user_id.username`. I wrote `tests/test_notifications.py` covering three cases: rating a song notifies the sharer with the rater's username correctly interpolated (no crash), a user rating their own song produces no notification (confirms the "skip if it's the sharer" guard on both code paths behaves the same way), and adding a song to a playlist still notifies the sharer. All three pass. As a side-effect check specific to this fix, I confirmed the self-interaction guard (`if song.shared_by != user_id` / `if song.shared_by != added_by_user_id`) still correctly suppresses notifications in both functions, not just the one I was actively fixing — a user rating or adding their own shared song should never notify themselves, and both paths still respect that.

---

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it:** Using the existing `seed_playlist` fixture in `tests/test_playlists.py` (5 songs at positions 1–5), I called `get_playlist_songs(playlist_id)` against the original code and got back 4 songs instead of 5 — the song at the last position (`position=5`) was missing. This matches `test_playlist_returns_all_songs`'s inline comment ("Bug causes this to return 4"), which fails against the unmodified file.

**How I found the root cause:** `get_playlist_songs()` in `playlist_service.py` runs one query — songs joined to `playlist_entries`, filtered by playlist ID, ordered ascending by `position` — and returns `[song.to_dict() for song in songs[:-1]]`. Reading that return line against the function's own docstring ("Note: This function returns all songs in the playlist") was the moment of confidence: the docstring promises all songs, but `songs[:-1]` is a Python slice that explicitly drops the last element of whatever list precedes it, regardless of how many songs the playlist has.

**The root cause:** `songs[:-1]` is an off-by-one slice — it returns every element except the last one. Since `songs` was already correctly ordered by position ascending, this unconditionally discarded the final song in the playlist (the highest `position` value) on every call, regardless of playlist length.

**Fix and side-effect check:** I changed `songs[:-1]` to `songs[:]` so the full ordered list is returned. I confirmed `test_playlist_returns_all_songs` (length is now 5) and, importantly, `test_playlist_returns_songs_in_order` (the returned titles are still `["Track 1", ..., "Track 5"]` in position order) — since the fix touches the same return statement that the ordering logic depends on, I specifically wanted to confirm the fix didn't accidentally reverse or reorder anything, only stopped truncating the list. I also confirmed `test_empty_playlist_returns_empty_list` still passes, since an empty list sliced with `[:]` should still safely return `[]` rather than raising an error.
