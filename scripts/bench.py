"""Scale benchmark (run: .venv/bin/python scripts/bench.py): seed ~50k tracks over ~400 days and time the hot queries,
then measure read latency while a backfill batch is writing on the one
shared connection."""
import asyncio, hashlib, random, statistics, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meshradio.db import Database, utcnow

DAYS, PER_DAY = 400, 125           # 50k tracks
WORDS = "rain sun river road night day love heart fire blue moon star car train home".split()


async def seed(db):
    random.seed(1)
    base = time.time() - DAYS * 86400
    themes, tracks, plays = [], [], []
    for d in range(DAYS):
        date = time.strftime("%Y-%m-%d", time.gmtime(base + d * 86400))
        themes.append((d + 1, date, f"theme {random.choice(WORDS)} {random.choice(WORDS)}", "alice", utcnow(), 1))
        for i in range(PER_DAY):
            vid = "".join(random.choices("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-", k=11))
            ts = base + d * 86400 + i * 300
            sender = f"member{random.randrange(80)}"
            dh = hashlib.sha256(f"#music|{sender}|{vid}|{int(ts // 60)}".encode()).hexdigest()
            tid = d * PER_DAY + i + 1
            tracks.append((tid, vid, f"https://www.youtube.com/watch?v={vid}",
                           f"{random.choice(WORDS)} {random.choice(WORDS)} song {tid}",
                           f"artist {random.randrange(2000)}", 180.0 + random.randrange(200), d + 1,
                           sender, ts, utcnow(), "corescope", f"/cache/{vid}.opus", "ready", dh))
            if random.random() < 0.3:
                plays.append((tid, utcnow(), "speaker", 1))
    async with db.transaction():
        await db.db.executemany("INSERT INTO themes(id,date,title,set_by,created_at,locked) VALUES(?,?,?,?,?,?)", themes)
        await db.db.executemany(
            "INSERT INTO tracks(id,video_id,url,title,artist,duration,theme_id,sender,mesh_ts,ingested_at,source,cache_path,cache_status,dedupe_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tracks)
        await db.db.executemany("INSERT INTO plays(track_id,played_at,output,completed) VALUES(?,?,?,?)", plays)
    await db.db.execute("ANALYZE")


async def timed(label, coro_fn, n=5):
    ts = []
    for _ in range(n):
        t = time.perf_counter(); await coro_fn(); ts.append((time.perf_counter() - t) * 1000)
    print(f"  {label:<48} median {statistics.median(ts):7.1f} ms")


async def main(tmp):
    db = Database(f"{tmp}/bench.db"); await db.connect()
    t = time.perf_counter(); await seed(db); print(f"seeded {DAYS*PER_DAY} tracks in {time.perf_counter()-t:.1f}s\n")
    print("hot queries at 50k tracks / 400 days:")
    await timed("archive_days() (whole-archive aggregate)", db.archive_days)
    await timed("all_themes()", db.all_themes)
    await timed("overall_stats()", db.overall_stats)
    await timed("top_songs()", db.top_songs)
    await timed("top_sharers()", db.top_sharers)
    await timed("busiest_themes()", db.busiest_themes)
    await timed("cached_tracks_lru() (prune ordering)", db.cached_tracks_lru)
    await timed("random_channel_tracks(10) ORDER BY RANDOM()", lambda: db.random_channel_tracks(10))
    await timed("search_tracks('rain') (trigram FTS)", lambda: db.search_tracks("rain"))
    await timed("search_tracks('zzzz') (no hits)", lambda: db.search_tracks("zzzz"))
    await timed("search_tracks('zz') (2 chars: LIKE path)", lambda: db.search_tracks("zz"))
    await timed("recent_days_tracks(30) (feed)", lambda: db.recent_days_tracks(30))
    await timed("tracks_for_day (per live partial)", lambda: db.tracks_for_day("2026-09-01"))
    await timed("newest_day_with_tracks()", db.newest_day_with_tracks)
    await timed("member_profile('member7')", lambda: db.member_profile("member7"))

    # read latency while a backfill batch writes on the shared connection
    print("\nread latency for track_by_id() while a 2000-row write batch runs on the same connection:")
    async def writer():
        async with db.transaction():
            for i in range(2000):
                await db.db.execute("UPDATE tracks SET title=title WHERE id=?", (i + 1,))
                await db._fetchone("SELECT 1 FROM tracks WHERE theme_id=? AND video_id=? LIMIT 1", (1, "x"))
                await db._fetchone("SELECT * FROM themes WHERE date=? ORDER BY created_at DESC, id DESC LIMIT 1", ("2026-01-01",))
    async def reader():
        lat = []
        end = time.perf_counter() + 2.0
        while time.perf_counter() < end:
            t = time.perf_counter(); await db.track_by_id(5); lat.append((time.perf_counter() - t) * 1000)
            await asyncio.sleep(0.005)
        return lat
    idle = await reader()
    w = asyncio.create_task(writer()); busy = await reader(); await w
    for label, lat in (("idle", idle), ("during batch", busy)):
        lat.sort()
        print(f"  {label:<14} p50 {lat[len(lat)//2]:5.2f} ms   p99 {lat[int(len(lat)*0.99)]:6.2f} ms   max {lat[-1]:6.2f} ms")
    await db.close()

with tempfile.TemporaryDirectory() as tmp:
    asyncio.run(main(tmp))
