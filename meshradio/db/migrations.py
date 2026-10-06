"""The schema, as the ordered list of scripts that built it.

Each entry is one migration; ``Core._migrate`` runs the ones past the
database's ``user_version``, each as a single transaction.
"""

from __future__ import annotations

MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE themes(
        id          INTEGER PRIMARY KEY,
        date        TEXT NOT NULL,               -- YYYY-MM-DD, channel-local (America/Chicago)
        title       TEXT NOT NULL,
        set_by      TEXT,
        raw_message TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE(date, title)
    );
    CREATE TABLE tracks(
        id           INTEGER PRIMARY KEY,
        video_id     TEXT NOT NULL,
        url          TEXT NOT NULL,
        title        TEXT,
        artist       TEXT,
        duration     REAL,
        theme_id     INTEGER REFERENCES themes(id),
        sender       TEXT,
        mesh_ts      REAL,                       -- unix seconds, message time on the mesh
        ingested_at  TEXT NOT NULL,
        source       TEXT NOT NULL CHECK(source IN ('mesh','corescope')),
        cache_path   TEXT,
        cache_status TEXT NOT NULL DEFAULT 'pending'
                     CHECK(cache_status IN ('pending','ready','failed')),
        dedupe_hash  TEXT NOT NULL UNIQUE
    );
    CREATE INDEX idx_tracks_theme ON tracks(theme_id);
    CREATE INDEX idx_tracks_status ON tracks(cache_status);
    CREATE TABLE plays(
        id        INTEGER PRIMARY KEY,
        track_id  INTEGER NOT NULL REFERENCES tracks(id),
        played_at TEXT NOT NULL,
        output    TEXT,
        completed INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_plays_track ON plays(track_id);
    CREATE TABLE settings(
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """,
    # v2 — 'radio' track source (YouTube Mix continuations; not channel posts,
    # theme_id stays NULL so they never appear in the archive). SQLite can't
    # alter a CHECK, so rebuild the table.
    """
    PRAGMA foreign_keys=OFF;
    CREATE TABLE tracks_v2(
        id           INTEGER PRIMARY KEY,
        video_id     TEXT NOT NULL,
        url          TEXT NOT NULL,
        title        TEXT,
        artist       TEXT,
        duration     REAL,
        theme_id     INTEGER REFERENCES themes(id),
        sender       TEXT,
        mesh_ts      REAL,
        ingested_at  TEXT NOT NULL,
        source       TEXT NOT NULL CHECK(source IN ('mesh','corescope','radio')),
        cache_path   TEXT,
        cache_status TEXT NOT NULL DEFAULT 'pending'
                     CHECK(cache_status IN ('pending','ready','failed')),
        dedupe_hash  TEXT NOT NULL UNIQUE
    );
    INSERT INTO tracks_v2 SELECT * FROM tracks;
    DROP TABLE tracks;
    ALTER TABLE tracks_v2 RENAME TO tracks;
    CREATE INDEX idx_tracks_theme ON tracks(theme_id);
    CREATE INDEX idx_tracks_status ON tracks(cache_status);
    PRAGMA foreign_keys=ON;
    """,
    # v3 — per-visitor web session snapshots (embed hosting): survive deploys.
    """
    CREATE TABLE web_sessions(
        sid        TEXT PRIMARY KEY,
        updated_at TEXT NOT NULL,
        state      TEXT NOT NULL              -- JSON snapshot
    );
    """,
    # v4 — one playlist per day: lockable themes + merge existing duplicates.
    #
    # A theme *is* the day's playlist. Because UNIQUE is on (date, title), a
    # second "Theme: …" message with a different title used to insert a rival
    # theme row for the same date — splitting the day's tracks across two
    # playlists and "resetting" which theme new links attached to. Themes now
    # carry a `locked` flag: once a real theme is set for the day it is locked,
    # and later theme messages are ignored (enforced in IngestService).
    #
    # Backfill for days that already split: reassign every track to its day's
    # canonical theme (prefer a real title over an "Untitled —" placeholder,
    # then the earliest row), drop the now-empty rivals, and lock every
    # surviving real theme so it can't be reset either.
    """
    ALTER TABLE themes ADD COLUMN locked INTEGER NOT NULL DEFAULT 0;

    UPDATE tracks
    SET theme_id = (
        SELECT t.id FROM themes t
        WHERE t.date = (SELECT d.date FROM themes d WHERE d.id = tracks.theme_id)
        ORDER BY (t.title LIKE 'Untitled — %') ASC, t.id ASC
        LIMIT 1
    )
    WHERE theme_id IS NOT NULL;

    DELETE FROM themes
    WHERE id NOT IN (
        SELECT (
            SELECT t2.id FROM themes t2
            WHERE t2.date = dates.date
            ORDER BY (t2.title LIKE 'Untitled — %') ASC, t2.id ASC
            LIMIT 1
        )
        FROM (SELECT DISTINCT date FROM themes) dates
    );

    UPDATE themes SET locked = 1 WHERE title NOT LIKE 'Untitled — %';
    """,
    # v5 — 'letsmesh' track source: a since-retired backup analyzer feed
    # (analyzer.letsmesh.net; the host moved its API behind a Cloudflare
    # challenge a headless poller can't clear). The value stays in the CHECK
    # because existing rows may carry it — nothing writes it now. SQLite can't
    # alter a CHECK, so rebuild the table (as v2 did for 'radio').
    """
    PRAGMA foreign_keys=OFF;
    CREATE TABLE tracks_v5(
        id           INTEGER PRIMARY KEY,
        video_id     TEXT NOT NULL,
        url          TEXT NOT NULL,
        title        TEXT,
        artist       TEXT,
        duration     REAL,
        theme_id     INTEGER REFERENCES themes(id),
        sender       TEXT,
        mesh_ts      REAL,
        ingested_at  TEXT NOT NULL,
        source       TEXT NOT NULL CHECK(source IN ('mesh','corescope','radio','letsmesh')),
        cache_path   TEXT,
        cache_status TEXT NOT NULL DEFAULT 'pending'
                     CHECK(cache_status IN ('pending','ready','failed')),
        dedupe_hash  TEXT NOT NULL UNIQUE
    );
    INSERT INTO tracks_v5 SELECT * FROM tracks;
    DROP TABLE tracks;
    ALTER TABLE tracks_v5 RENAME TO tracks;
    CREATE INDEX idx_tracks_theme ON tracks(theme_id);
    CREATE INDEX idx_tracks_status ON tracks(cache_status);
    PRAGMA foreign_keys=ON;
    """,
    # v6 — one song per playlist. A song reposted to the same day used to
    # insert a second track row (dedupe_hash only catches the *same message*
    # arriving twice), so the playlist listed it twice. add_track now refuses a
    # video already present under a theme; collapse the dupes that already
    # accumulated and add a partial unique index as a hard backstop.
    #
    # Keep the earliest row per (theme_id, video_id) — by mesh time, then id —
    # repoint any plays at the survivor, then drop the rest. Radio filler
    # (theme_id NULL) is left alone: a mix legitimately echoes videos across
    # days, and SQLite's partial index treats NULL theme rows as distinct.
    """
    CREATE TEMP TABLE _keep AS
        SELECT t.id AS keep_id, t.theme_id AS theme_id, t.video_id AS video_id
        FROM tracks t
        WHERE t.theme_id IS NOT NULL
          AND t.id = (
              SELECT t2.id FROM tracks t2
              WHERE t2.theme_id = t.theme_id AND t2.video_id = t.video_id
              ORDER BY t2.mesh_ts, t2.id LIMIT 1
          );

    UPDATE plays SET track_id = (
        SELECT k.keep_id FROM tracks d JOIN _keep k
          ON k.theme_id = d.theme_id AND k.video_id = d.video_id
        WHERE d.id = plays.track_id
    )
    WHERE track_id IN (
        SELECT d.id FROM tracks d JOIN _keep k
          ON k.theme_id = d.theme_id AND k.video_id = d.video_id
        WHERE d.id <> k.keep_id
    );

    DELETE FROM tracks
    WHERE theme_id IS NOT NULL
      AND id NOT IN (SELECT keep_id FROM _keep);

    DROP TABLE _keep;

    CREATE UNIQUE INDEX idx_tracks_theme_video
        ON tracks(theme_id, video_id) WHERE theme_id IS NOT NULL;
    """,
    # v7 — index tracks.video_id. cached_track_for_video / tracks_for_video key
    # on it, and the cacher runs the reuse-existing-file check on every non-embed
    # download; without this it was a full table scan per download.
    """
    CREATE INDEX idx_tracks_video ON tracks(video_id);
    """,
    # v8 — index tracks.ingested_at. The relay pusher calls tracks_since on
    # every push interval (default 120s); its cursor predicate and ORDER BY
    # both key on ingested_at, which was a full table scan per push.
    """
    CREATE INDEX idx_tracks_ingested ON tracks(ingested_at);
    """,
    # v9 — themes.updated_at, so theme *adoptions* reach the relay receiver.
    #
    # adopt_theme renames a placeholder in place, leaving created_at alone. The
    # relay's themes cursor is keyed on created_at, and the placeholder was
    # already skipped-but-passed on an earlier push — so the adopted row never
    # came back from themes_since and the receiver kept its own "Untitled —"
    # for that day forever. themes_since now keys on COALESCE(updated_at,
    # created_at), which an adoption moves forward. NULL on existing rows means
    # "never adopted since this migration" and reads as created_at.
    """
    ALTER TABLE themes ADD COLUMN updated_at TEXT;
    """,
    # v10 — 'comchan' track source: the backup analyzer feed
    # (analyzer.comchan.net), standing in while the primary CoreScope
    # instance is unreachable. It is its own source rather than more
    # 'corescope' rows so provenance stays readable when the two feeds
    # disagree — and so a repeat of the letsmesh retirement (v5) is one
    # query to find. SQLite can't alter a CHECK, so rebuild the table
    # (as v2 and v5 did).
    """
    PRAGMA foreign_keys=OFF;
    CREATE TABLE tracks_v10(
        id           INTEGER PRIMARY KEY,
        video_id     TEXT NOT NULL,
        url          TEXT NOT NULL,
        title        TEXT,
        artist       TEXT,
        duration     REAL,
        theme_id     INTEGER REFERENCES themes(id),
        sender       TEXT,
        mesh_ts      REAL,
        ingested_at  TEXT NOT NULL,
        source       TEXT NOT NULL
                     CHECK(source IN ('mesh','corescope','radio','letsmesh','comchan')),
        cache_path   TEXT,
        cache_status TEXT NOT NULL DEFAULT 'pending'
                     CHECK(cache_status IN ('pending','ready','failed')),
        dedupe_hash  TEXT NOT NULL UNIQUE
    );
    INSERT INTO tracks_v10 SELECT * FROM tracks;
    DROP TABLE tracks;
    ALTER TABLE tracks_v10 RENAME TO tracks;
    CREATE INDEX idx_tracks_theme ON tracks(theme_id);
    CREATE INDEX idx_tracks_status ON tracks(cache_status);
    CREATE INDEX idx_tracks_video ON tracks(video_id);
    CREATE INDEX idx_tracks_ingested ON tracks(ingested_at);
    CREATE UNIQUE INDEX idx_tracks_theme_video
        ON tracks(theme_id, video_id) WHERE theme_id IS NOT NULL;
    PRAGMA foreign_keys=ON;
    """,
    # v11 — tombstones for songs the operator removed by hand
    # (``meshradio --delete-track``). Deleting the row alone isn't enough: the
    # message is still on the channel, so any re-backfill (the relay's
    # self-healing one, a restore, a cursor reset) would insert it right back.
    # A tombstone is the same idea as a locked theme — the archive remembers
    # the out-of-band decision and ignores the replayed message.
    """
    CREATE TABLE deleted_tracks(
        id         INTEGER PRIMARY KEY,
        date       TEXT NOT NULL,          -- channel-local day it was removed from
        video_id   TEXT NOT NULL,
        title      TEXT,
        sender     TEXT,
        deleted_at TEXT NOT NULL,
        UNIQUE(date, video_id)
    );
    """,
    # v12 — index tracks.sender, case-insensitively. The member pages look a
    # name up with ``sender = ? COLLATE NOCASE`` four times per visit
    # (member_name, member_profile, member_tracks, member_artists), and every
    # one was a full scan of tracks; an index declared with the same
    # collation serves them. Any future rebuild of tracks (v2/v5/v10 style)
    # must recreate this one along with the five before it.
    """
    CREATE INDEX idx_tracks_sender ON tracks(sender COLLATE NOCASE);
    """,
    # v13 — two things the request path was paying for on every call.
    #
    # tracks.last_played_at: the cache pruner orders cached tracks least-
    # recently-played first, which was a GROUP BY over tracks×plays returning
    # every cached row (416 ms at 50k tracks) each time a download finished
    # over the cap. record_play now stamps the track and the pruner walks an
    # index a few rows at a time. Backfilled from plays.
    #
    # tracks_fts / themes_fts: search was four LIKE scans over the join. An
    # FTS5 external-content index with the trigram tokenizer keeps LIKE's
    # substring, case-insensitive match (and adds the Unicode case folding
    # LIKE never had) but answers from an index. Triggers keep it in step
    # with the rows. Any future rebuild of tracks (v2/v5/v10 style) must
    # recreate the three tracks_fts triggers along with the seven indexes
    # before them, and a rebuild of themes its three. The trigram tokenizer
    # needs SQLite 3.34 (December 2020) or later.
    """
    ALTER TABLE tracks ADD COLUMN last_played_at TEXT;
    UPDATE tracks SET last_played_at = (
        SELECT MAX(p.played_at) FROM plays p WHERE p.track_id = tracks.id
    );
    CREATE INDEX idx_tracks_lru ON tracks(cache_status, last_played_at, ingested_at);

    CREATE VIRTUAL TABLE tracks_fts USING fts5(
        title, artist, sender,
        content='tracks', content_rowid='id', tokenize='trigram'
    );
    INSERT INTO tracks_fts(rowid, title, artist, sender)
        SELECT id, title, artist, sender FROM tracks;
    CREATE TRIGGER tracks_fts_ai AFTER INSERT ON tracks BEGIN
        INSERT INTO tracks_fts(rowid, title, artist, sender)
            VALUES (new.id, new.title, new.artist, new.sender);
    END;
    CREATE TRIGGER tracks_fts_ad AFTER DELETE ON tracks BEGIN
        INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, sender)
            VALUES ('delete', old.id, old.title, old.artist, old.sender);
    END;
    CREATE TRIGGER tracks_fts_au AFTER UPDATE OF title, artist, sender ON tracks BEGIN
        INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, sender)
            VALUES ('delete', old.id, old.title, old.artist, old.sender);
        INSERT INTO tracks_fts(rowid, title, artist, sender)
            VALUES (new.id, new.title, new.artist, new.sender);
    END;

    CREATE VIRTUAL TABLE themes_fts USING fts5(
        title, content='themes', content_rowid='id', tokenize='trigram'
    );
    INSERT INTO themes_fts(rowid, title) SELECT id, title FROM themes;
    CREATE TRIGGER themes_fts_ai AFTER INSERT ON themes BEGIN
        INSERT INTO themes_fts(rowid, title) VALUES (new.id, new.title);
    END;
    CREATE TRIGGER themes_fts_ad AFTER DELETE ON themes BEGIN
        INSERT INTO themes_fts(themes_fts, rowid, title) VALUES ('delete', old.id, old.title);
    END;
    CREATE TRIGGER themes_fts_au AFTER UPDATE OF title ON themes BEGIN
        INSERT INTO themes_fts(themes_fts, rowid, title) VALUES ('delete', old.id, old.title);
        INSERT INTO themes_fts(rowid, title) VALUES (new.id, new.title);
    END;
    """,
    # v14 — the admin page (web/routes_admin.py).
    #
    # admin_log: every sign-in and every change made from /admin or the
    # operator CLI, with the value before and after, so the archive's hand
    # edits read straight through and the undoable ones can be undone.
    # admin_sessions: signed-in admin browsers, by the hash of their cookie
    # (the cookie itself is never stored). artist_aliases: spellings the
    # operator merged into one; ingest maps a new song's artist through it.
    # tracks.meta_edited_at: set when the operator corrects a title or artist
    # by hand, so a late oEmbed answer or a relay re-push can't put the wrong
    # one back. deleted_tracks.track_json: the removed track as it was, so a removal
    # can be put back exactly instead of waiting for the channel to replay it.
    """
    CREATE TABLE admin_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        ip TEXT,
        action TEXT NOT NULL,
        target TEXT,
        before TEXT,
        after TEXT,
        undo TEXT,
        undone_by INTEGER
    );
    CREATE INDEX idx_admin_log_at ON admin_log(at);
    CREATE TABLE admin_sessions (
        token_hash TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        seen_at REAL NOT NULL,
        ip TEXT,
        key_fp TEXT NOT NULL
    );
    CREATE TABLE artist_aliases (
        alias TEXT PRIMARY KEY COLLATE NOCASE,
        canonical TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    ALTER TABLE tracks ADD COLUMN meta_edited_at TEXT;
    ALTER TABLE deleted_tracks ADD COLUMN track_json TEXT;
    """,
]
