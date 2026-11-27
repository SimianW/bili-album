from bili_album.catalog import CatAlbum, CatTrack, drop_full_length, parse_lrc, score_album
from bili_album.textnorm import norm, similarity


def test_norm_folds_scripts_and_widths():
    assert norm("萬能青年旅店") == norm("万能青年旅店")
    assert norm("Ｈｉ－Ｒｅｓ") == norm("hi-res")
    assert norm("𝐇𝐢-𝐑𝐞𝐬") == "hires"


def test_similarity():
    assert similarity("平等雲霧", "平等云雾") == 1.0
    assert similarity("Starboy (feat. Daft Punk)", "Starboy") >= 0.9
    assert similarity("上海1943", "上海一九四三") >= 0.9
    assert similarity("爸，我回来了", "爸我回来了") == 1.0
    assert similarity("忍者", "双截棍") < 0.3


def test_drop_full_length_track():
    tracks = [CatTrack("冀西南林路行 (专辑全长)", "x", 2662.0, 1)] + [
        CatTrack(f"t{i}", "x", d, i + 1) for i, d in enumerate([83.6, 348.3, 86.3, 536.2, 198.0, 163.5, 688.0, 580.5])]
    kept = drop_full_length(tracks)
    assert len(kept) == 8 and kept[0].title == "t0" and kept[0].number == 1


def test_parse_lrc():
    lrc = "[00:00.000] 作词 : 姬赓\n[00:00.667] 作曲 : 董亚千/姬赓\n[00:02.669]《早》\n[01:22.715]骤雨重山 将甘苦注入他\n"
    text, composer = parse_lrc(lrc)
    assert composer == "董亚千/姬赓"
    assert text == "《早》\n骤雨重山 将甘苦注入他"


def _album(title, artist, durs):
    return CatAlbum("itunes", "1", title, artist, tracks=[CatTrack(f"t{i}", artist, d, i + 1)
                                                         for i, d in enumerate(durs)])


def test_score_album_prefers_duration_fit():
    std = _album("Starboy", "The Weeknd", [228.0] * 18)
    deluxe = _album("Starboy (Deluxe)", "The Weeknd", [228.0] * 21)
    D = 228.0 * 18
    a = score_album(std, duration=D, works=["Starboy"])
    b = score_album(deluxe, duration=D, works=["Starboy"])
    assert a.score > b.score


def test_score_album_prefers_uploader_script():
    simp = _album("要得", "张杰", [240.0] * 10)
    trad = _album("要得", "張杰", [240.0] * 10)
    a = score_album(simp, duration=2400, artists=["张杰"], works=["要得"])
    b = score_album(trad, duration=2400, artists=["张杰"], works=["要得"])
    assert a.score > b.score
