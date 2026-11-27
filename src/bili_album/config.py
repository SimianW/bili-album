"""Machine configuration: model endpoint, Jellyfin, library folder.

Values come from ``~/.config/bili-album/env`` (``KEY=VALUE`` lines, outside the
repo, mode 600). Environment variables with the same names override the file;
``BILI_ALBUM_CONFIG`` points at a different file. The two API keys are kept out
of ``repr`` so they never land in logs, tracebacks or job files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEFAULT_PATH = Path("~/.config/bili-album/env")
DEFAULT_JELLYFIN_LIBRARY_PATH = "/media/music-videos/music"

KEYS = (
    "BILI_ALBUM_LLM_BASE_URL",
    "BILI_ALBUM_LLM_MODEL",
    "BILI_ALBUM_LLM_API_KEY",
    "BILI_ALBUM_JELLYFIN_URL",
    "BILI_ALBUM_JELLYFIN_PUBLIC_URL",
    "BILI_ALBUM_JELLYFIN_API_KEY",
    "BILI_ALBUM_JELLYFIN_LIBRARY_PATH",
    "BILI_ALBUM_LIBRARY_DIR",
)
SECRET_KEYS = ("BILI_ALBUM_LLM_API_KEY", "BILI_ALBUM_JELLYFIN_API_KEY")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = field(default="", repr=False)
    jellyfin_url: str = ""
    jellyfin_public_url: str = ""
    jellyfin_api_key: str = field(default="", repr=False)
    jellyfin_library_path: str = DEFAULT_JELLYFIN_LIBRARY_PATH
    library_dir: Path | None = None

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_base_url and self.llm_model and self.llm_api_key)

    @property
    def jellyfin_ready(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_api_key)

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env = ", ".join("BILI_ALBUM_" + n.upper() for n in missing)
            raise ConfigError(f"missing configuration: {env} (set it in {DEFAULT_PATH} or the environment)")

    def secrets(self) -> list[str]:
        """Key values, for scrubbing error text. Never print the result."""
        return [s for s in (self.llm_api_key, self.jellyfin_api_key) if s]

    def scrub(self, text: str) -> str:
        for s in sorted(self.secrets(), key=len, reverse=True):  # longest first: no partial leftovers
            text = text.replace(s, "***")
        return text


def parse_env_file(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def load_config(path: Path | str | None = None, environ: Mapping[str, str] | None = None) -> Config:
    """Read the env file (missing file is fine) and apply environment overrides."""
    env = os.environ if environ is None else environ
    p = Path(path or env.get("BILI_ALBUM_CONFIG") or DEFAULT_PATH).expanduser()
    values: dict[str, str] = {}
    if p.is_file():
        values.update(parse_env_file(p.read_text(encoding="utf-8")))
    for k in KEYS:
        if env.get(k):
            values[k] = env[k]
    lib = values.get("BILI_ALBUM_LIBRARY_DIR", "")
    return Config(
        llm_base_url=values.get("BILI_ALBUM_LLM_BASE_URL", "").rstrip("/"),
        llm_model=values.get("BILI_ALBUM_LLM_MODEL", ""),
        llm_api_key=values.get("BILI_ALBUM_LLM_API_KEY", ""),
        jellyfin_url=values.get("BILI_ALBUM_JELLYFIN_URL", "").rstrip("/"),
        jellyfin_public_url=values.get("BILI_ALBUM_JELLYFIN_PUBLIC_URL", "").rstrip("/"),
        jellyfin_api_key=values.get("BILI_ALBUM_JELLYFIN_API_KEY", ""),
        jellyfin_library_path=(values.get("BILI_ALBUM_JELLYFIN_LIBRARY_PATH") or DEFAULT_JELLYFIN_LIBRARY_PATH
                               ).rstrip("/"),
        library_dir=Path(lib).expanduser() if lib else None,
    )
