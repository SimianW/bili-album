"""Live-service checks. Opt in with ``uv run pytest -m network``."""

import pytest

from bili_album.catalog import Catalog
from bili_album.hints import guess_from_title
from bili_album.net import Http

pytestmark = pytest.mark.network


def test_catalog_finds_album(tmp_path):
    g = guess_from_title("【HiRes】  万能青年旅店  |  2020 - 冀西南林路行  |  HiRes臻品整轨专辑无损专辑")
    ms = Catalog(Http(tmp_path)).find_album(g.queries(), duration=2662, artists=g.artists, works=g.works)
    assert ms and similarity_ok(ms[0].album.title, "冀西南林路行")
    assert len(ms[0].album.tracks) == 8


def test_bilibili_info(tmp_path):
    from bili_album.bilibili import Bilibili

    vi = Bilibili(Http(tmp_path)).info("https://www.bilibili.com/video/BV14F4m1M7Lx")
    assert vi.duration > 4000 and len(vi.parts) == 1


def similarity_ok(a, b):
    from bili_album.textnorm import similarity

    return similarity(a, b) > 0.9
