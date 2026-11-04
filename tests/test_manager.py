from src.media import NullBackend
from src.media.base import NowPlaying, PlaybackStatus as S, classify_player, detect_ad
from src.media.linux_mpris import normalize_art_url, player_id_from_bus_name
from src.media.manager import PlayerManager, select_session

from .fakes import Clock, FakeBackend, snap


def make(selected="spotify"):
    b, c = FakeBackend(), Clock()
    return b, c, PlayerManager(b, selected, clock=c)


# ------------------------------------------------------------ selection

def test_default_is_spotify():
    b, _, m = make(selected="")
    assert m.selected_player == "spotify"
    b.set(snap("Spotify.exe"))
    assert m.active().info.player_id == "Spotify.exe"


def test_browser_never_shown_unless_chosen():
    b, _, m = make()
    b.set(snap("308046B0AF4A39CB"), snap("Chrome"))  # YouTube etc. playing
    assert m.active() is None
    assert m.state()["running"] is False


def test_paused_spotify_shown_over_playing_browser():
    b, _, m = make()
    b.set(snap("Spotify.exe", S.PAUSED), snap("Chrome", S.PLAYING))
    assert m.active().info.key == "spotify"
    assert m.now_playing().status == S.PAUSED


def test_not_running_shows_nothing_then_appears():
    b, _, m = make("qobuz")
    b.set(snap("Spotify.exe"))
    assert m.active() is None
    b.set(snap("Spotify.exe"), snap("com.squirrel.Qobuz.Qobuz"))
    assert m.active().info.key == "qobuz"
    b.set(snap("Spotify.exe"))  # Qobuz closed / crashed
    assert m.active() is None


def test_store_and_classic_spotify_both_match():
    for pid in ("Spotify.exe", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "spotify"):
        b, _, m = make()
        b.set(snap(pid))
        assert m.active() is not None, pid


def test_user_can_choose_browser():
    b, _, m = make()
    b.set(snap("Spotify.exe"), snap("308046B0AF4A39CB"))
    m.set_selected("firefox")
    assert m.active().info.player_id == "308046B0AF4A39CB"


def test_raw_player_id_selection_for_unknown_apps():
    b, _, m = make("SomeVendor.CoolPlayer")
    b.set(snap("SomeVendor.CoolPlayer"))
    assert m.active().info.player_id == "SomeVendor.CoolPlayer"


def test_multiple_sessions_prefers_playing_then_sticks():
    b, c, m = make("chrome")
    b.set(snap("Chrome", S.PAUSED), snap("Chrome#2", S.PLAYING))
    assert m.active().info.player_id == "Chrome#2"
    c.t = 5
    b.set(snap("Chrome", S.PLAYING), snap("Chrome#2", S.PLAYING))
    assert m.active().info.player_id == "Chrome#2"  # no flipping to the newcomer
    c.t = 6
    b.set(snap("Chrome", S.PLAYING), snap("Chrome#2", S.PAUSED))
    assert m.active().info.player_id == "Chrome"


def test_select_session_pure_paused_prefers_last_played():
    snaps = [snap("Chrome", S.PAUSED), snap("Chrome#2", S.PAUSED)]
    got = select_session(snaps, "chrome", None, {}, {"Chrome": 1.0, "Chrome#2": 9.0})
    assert got.info.player_id == "Chrome#2"


def test_survives_backend_snapshot_errors():
    b, _, m = make()
    b.set(snap("Spotify.exe"))
    b.snapshot = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    m.refresh()
    assert m.active() is None


def test_players_list_groups_and_includes_missing_selection():
    b, _, m = make("qobuz")
    b.set(snap("Spotify.exe"), snap("Chrome"), snap("Chrome#2", S.PAUSED))
    rows = {r.key: r for r in m.players()}
    assert rows["chrome"].sessions == 2 and rows["chrome"].is_browser
    assert rows["qobuz"].running is False and rows["qobuz"].selected
    assert m.players()[0].key == "qobuz"  # selected first


def test_listeners_notified():
    b, _, m = make()
    hits = []
    m.add_listener(lambda: hits.append(1))
    b.set(snap("Spotify.exe"))
    assert hits


# ------------------------------------------------------------ helpers

def test_classify():
    assert classify_player("Spotify.exe")[0] == "spotify"
    assert classify_player("AppleInc.AppleMusicWin_nzyj5cx40ttyp!App")[0] == "apple-music"
    assert classify_player("MSEdge")[0:3:2] == ("edge", True)
    assert classify_player("308046B0AF4A39CB")[0] == "firefox"
    assert classify_player("weird.app", "Weird Player") == ("weird.app", "Weird Player", False)


def test_detect_ad():
    assert detect_ad("spotify", "Advertisement", "")
    assert detect_ad("spotify", "Spotify", "")
    assert not detect_ad("spotify", "Song", "Artist")
    assert not detect_ad("firefox", "Some video", "")


def test_mpris_helpers():
    assert player_id_from_bus_name("org.mpris.MediaPlayer2.firefox.instance_1_42") == "firefox"
    assert player_id_from_bus_name("org.mpris.MediaPlayer2.spotify") == "spotify"
    assert normalize_art_url("https://open.spotify.com/image/abc") == "https://i.scdn.co/image/abc"


def test_null_backend():
    assert NullBackend("x").snapshot() == []


def test_position_extrapolation():
    np = NowPlaying("p", "P", status=S.PLAYING, position_ms=1000, duration_ms=5000, position_at_ms=10_000)
    assert np.position_now_ms(11_500) == 2500
    assert np.position_now_ms(99_000) == 5000
    np.status = S.PAUSED
    assert np.position_now_ms(99_000) == 1000
