import json

from timmy import config, paths


def test_reads_bom_and_keeps_unknown_keys(app_dir):
    p = paths.config_path()
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"owner_name": "דנה", "custom_key": 1}, ensure_ascii=False).encode("utf-8"))
    cfg = config.load()
    assert cfg["owner_name"] == "דנה"
    new = config.save({"accent_color": "#3B82F6"})
    assert new["custom_key"] == 1 and new["owner_name"] == "דנה"
    assert json.loads(p.read_text(encoding="utf-8"))["accent_color"] == "#3B82F6"
    assert not [x for x in app_dir.iterdir() if x.name.startswith(".config-")]


def test_broken_or_missing_config_is_empty(app_dir):
    assert config.load() == {}
    paths.config_path().write_text("{not json", encoding="utf-8")
    assert config.load() == {}
    assert config.get({}, "summary_model") == "sonnet"
    assert config.get({"summary_model": "opus"}, "summary_model") == "opus"


def test_output_dir_is_never_overwritten(app_dir, tmp_path):
    chosen = str(tmp_path / "Drive" / "calls")
    assert config.ensure_output_dir({"output_dir": chosen}, default=tmp_path / "default") == chosen
    assert config.ensure_output_dir({"output_dir": "  "}, default=tmp_path / "default") == str(tmp_path / "default")
    assert config.ensure_output_dir({"output_dir": chosen}, explicit=str(tmp_path / "x")) == str(tmp_path / "x")


def test_default_recordings_folder_name():
    assert paths.default_output_dir().name == "טימי - הקלטות"


def test_known_keys_match_mac_installer(repo_root):
    src = (repo_root / "mac" / "install.sh").read_text(encoding="utf-8")
    for k in ("owner_name", "owner_description", "output_dir", "claude_path", "ffmpeg_path", "people", "clients_dir", "meetings_dir"):
        assert f'"{k}"' in src and k in config.KNOWN_KEYS, k
