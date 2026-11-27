import pytest

from bili_album.web.urls import UrlError, normalize_url, video_key

BV = "BV14PVVzNE2R"


@pytest.mark.parametrize("raw, want", [
    (BV, f"https://www.bilibili.com/video/{BV}"),
    (f"  {BV}  ", f"https://www.bilibili.com/video/{BV}"),
    (f"https://www.bilibili.com/video/{BV}", f"https://www.bilibili.com/video/{BV}"),
    (f"https://www.bilibili.com/video/{BV}/?spm_id_from=333.1007&vd_source=abc", f"https://www.bilibili.com/video/{BV}"),
    (f"https://www.bilibili.com/video/{BV}?p=3", f"https://www.bilibili.com/video/{BV}"),
    (f"http://bilibili.com/video/{BV}", f"https://www.bilibili.com/video/{BV}"),
    (f"https://m.bilibili.com/video/{BV}", f"https://www.bilibili.com/video/{BV}"),
    (f"www.bilibili.com/video/{BV}", f"https://www.bilibili.com/video/{BV}"),
    (f"WWW.BILIBILI.COM/video/{BV}", f"https://www.bilibili.com/video/{BV}"),
    ("https://b23.tv/AbC123x", "https://b23.tv/AbC123x"),
    ("b23.tv/AbC123x", "https://b23.tv/AbC123x"),
    (f"【范特西 全专】 https://www.bilibili.com/video/{BV}/?share_source=copy", f"https://www.bilibili.com/video/{BV}"),
    ("【范特西】 https://b23.tv/AbC123x", "https://b23.tv/AbC123x"),
])
def test_accepts_and_normalises(raw, want):
    assert normalize_url(raw) == want


@pytest.mark.parametrize("raw", [
    "", "   ", None, 42, ["x"],
    "BV14PVVzNE2", "BV14PVVzNE2R1", "av170001",
    "https://www.youtube.com/watch?v=x",
    f"https://evil.example/video/{BV}",
    f"https://www.bilibili.com.evil.example/video/{BV}",
    f"https://bilibili.com@evil.example/video/{BV}",
    f"https://user:pw@www.bilibili.com/video/{BV}",
    f"https://www.bilibili.com:8443/video/{BV}",
    f"javascript:alert(1)//www.bilibili.com/video/{BV}",
    f"ftp://www.bilibili.com/video/{BV}",
    "https://www.bilibili.com/",
    "https://space.bilibili.com/123",
    f"https://www.bilibili.com/bangumi/{BV}",
    "https://b23.tv/",
    "https://b23.tv/a/b",
    "hello world",
    "x" * 3000,
])
def test_rejects(raw):
    with pytest.raises(UrlError) as e:
        normalize_url(raw)
    assert str(e.value) == "这不像 B 站视频链接。"


def test_video_key():
    assert video_key(f"https://www.bilibili.com/video/{BV}") == BV
    assert video_key("https://b23.tv/AbC123x") == "https://b23.tv/AbC123x"
