from pathlib import Path

from bili_album.config import DEFAULT_JELLYFIN_LIBRARY_PATH, load_config, parse_env_file

FAKE_KEY = "test-key-not-real-0001"
KEY_NAME = "BILI_ALBUM_LLM_API_KEY"


def test_file_then_env_override(tmp_path: Path):
    f = tmp_path / "env"
    f.write_text("# comment\nBILI_ALBUM_LLM_BASE_URL=http://x:1/v1/\nBILI_ALBUM_LLM_MODEL='m1'\n"
                 f"{KEY_NAME}={FAKE_KEY}\nBILI_ALBUM_LIBRARY_DIR=/srv/music\n"
                 "export BILI_ALBUM_JELLYFIN_URL=http://j:8096\n", encoding="utf-8")
    cfg = load_config(f, environ={"BILI_ALBUM_LLM_MODEL": "m2"})
    assert cfg.llm_base_url == "http://x:1/v1"
    assert cfg.llm_model == "m2"
    assert cfg.llm_api_key == FAKE_KEY
    assert cfg.library_dir == Path("/srv/music")
    assert cfg.jellyfin_url == "http://j:8096"
    assert cfg.jellyfin_library_path == DEFAULT_JELLYFIN_LIBRARY_PATH
    assert cfg.llm_ready and not cfg.jellyfin_ready


def test_keys_hidden_from_repr_and_scrubbed(tmp_path: Path):
    cfg = load_config(tmp_path / "missing", environ={"BILI_ALBUM_LLM_API_KEY": FAKE_KEY,
                                                     "BILI_ALBUM_JELLYFIN_API_KEY": FAKE_KEY + "j"})
    assert FAKE_KEY not in repr(cfg)
    assert cfg.scrub(f"bad {FAKE_KEY}j and {FAKE_KEY}") == "bad *** and ***"


def test_parse_quotes_and_junk():
    assert parse_env_file('A="1"\nnot a line\nB = two \n') == {"A": "1", "B": "two"}
