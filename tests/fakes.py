from src.media.base import MediaBackend, NowPlaying, PlaybackStatus as S, PlayerInfo, PlayerSnapshot, classify_player


def snap(pid, status=S.PLAYING, title="Song", artist="Artist", art_version=""):
    key, name, is_browser = classify_player(pid.split("#")[0])
    return PlayerSnapshot(
        PlayerInfo(pid, name, status, is_browser, key),
        NowPlaying(pid, name, title, artist, status=status, duration_ms=200_000, art_version=art_version),
    )


class FakeBackend(MediaBackend):
    name = "fake"

    def __init__(self):
        super().__init__()
        self.snaps = []
        self.art = {}
        self.status = "ok"

    def set(self, *snaps):
        self.snaps = list(snaps)
        self._emit()

    async def start(self): pass
    async def stop(self): pass
    def snapshot(self): return list(self.snaps)
    async def get_art(self, pid): return self.art.get(pid)


class Clock:
    t = 0.0
    def __call__(self): return self.t
