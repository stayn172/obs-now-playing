import json

from src import settings as S
from src.settings import SettingsStore


def test_defaults_and_unknown_keys(tmp_path):
    st = SettingsStore(str(tmp_path / "s.json"))
    assert st.get("selected_player") == "spotify"
    _, errors, changed = st.update({"nope": 1, "scale": 0.5})
    assert errors == {} and changed == {"scale"}
    assert "nope" not in st.all()


def test_clamping_and_coercion():
    assert S.coerce("scale", 99) == 1.0
    assert S.coerce("scale", "0.1") == 0.25
    assert S.coerce("card_radius", 12.6) == 13
    assert S.coerce("port", 80) == 1024
    assert S.coerce("accent_color", "#ABC") == "#aabbcc"
    assert S.coerce("show_art", "false") is False


def test_invalid_values_rejected_and_old_value_kept(tmp_path):
    st = SettingsStore(str(tmp_path / "s.json"))
    _, errors, changed = st.update({"show_art": "yes", "font": "Comic Sans", "text_color": "red",
                                    "scale": float("nan"), "fade_ms": True, "selected_player": "  "})
    assert set(errors) == {"show_art", "font", "text_color", "scale", "fade_ms", "selected_player"}
    assert not changed
    assert st.all() == S.defaults()


def test_non_object_update():
    st = SettingsStore(None)
    _, errors, _ = st.update([1, 2])
    assert "_" in errors


def test_persistence_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    SettingsStore(str(p)).update({"selected_player": "qobuz", "title_size": 40})
    data = json.loads(p.read_text())
    assert data["schema_version"] == S.SCHEMA_VERSION
    st2 = SettingsStore(str(p))
    assert st2.get("selected_player") == "qobuz" and st2.get("title_size") == 40


def test_broken_file_falls_back_to_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    st = SettingsStore(str(p))
    assert st.all() == S.defaults()
    assert (tmp_path / "s.json.broken").exists()


def test_bad_values_in_file_are_sanitized(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"schema_version": 1, "scale": "huge", "bg_opacity": 7, "junk": 1}))
    st = SettingsStore(str(p))
    assert st.get("scale") == 1.0 and st.get("bg_opacity") == 1.0


def test_migration_from_draft_locked_mode():
    out = S.sanitize({"player_mode": "locked", "locked_player": "qobuz"})
    assert out["selected_player"] == "qobuz"


def test_reset_keeps_player_and_system(tmp_path):
    st = SettingsStore(str(tmp_path / "s.json"))
    st.update({"selected_player": "qobuz", "port": 9000, "scale": 0.5, "show_album": True})
    settings, changed = st.reset()
    assert settings["selected_player"] == "qobuz" and settings["port"] == 9000
    assert settings["scale"] == 1.0 and not settings["show_album"]
    assert changed == {"scale", "show_album"}


def test_listener_called_only_on_change():
    st = SettingsStore(None)
    calls = []
    st.add_listener(lambda s, c: calls.append(c))
    st.update({"scale": 1.0})  # same as default
    st.update({"scale": 0.5})
    assert calls == [{"scale"}]
