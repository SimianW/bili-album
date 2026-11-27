from bili_album.hints import (guess_from_hint, guess_from_title, names_in_prose, parse_timestamps,
                              parse_tracklist_file, part_title, plausible_list)


def starts_titles(entries):
    return [(e.start, e.title) for e in entries]


def test_fullwidth_colon_time_glued_to_title():
    text = ("00：01爱在西元前\n03：59爸，我回来了\n07：45简单爱\n12：11忍者\n"
            "给想单独听某一首的朋友们提供个方便[呲牙]")
    got = parse_timestamps(text)
    assert starts_titles(got) == [(1.0, "爱在西元前"), (239.0, "爸,我回来了"), (465.0, "简单爱"), (731.0, "忍者")]


def test_numbered_title_then_time_in_brackets():
    text = "1.《Starboy》 00:00\n2.《Party  Monster》 03:50\n16.《All I Konw》 54:24\n18.《I Feel It Coming》 1:04:03"
    got = parse_timestamps(text)
    assert starts_titles(got) == [(0.0, "Starboy"), (230.0, "Party  Monster"), (3264.0, "All I Konw"),
                                  (3843.0, "I Feel It Coming")]


def test_title_with_time_in_parentheses():
    text = "曲序：\n早（0:00）\n泥河（1:22）\n平等云雾（7:05）\n郊眠寺（34:37）"
    assert starts_titles(parse_timestamps(text)) == [(0.0, "早"), (82.0, "泥河"), (425.0, "平等云雾"),
                                                     (2077.0, "郊眠寺")]


def test_hh_mm_ss_and_title_artist():
    text = "[00:00:00] 01. Song One - Artist A\n[00:04:10] 02. Song Two - Artist B\n[01:02:03] 03 Third"
    got = parse_timestamps(text)
    assert [(e.start, e.title, e.artist) for e in got] == [(0.0, "Song One", "Artist A"),
                                                          (250.0, "Song Two", "Artist B"),
                                                          (3723.0, "Third", "")]


def test_many_timestamps_on_one_line_time_first():
    got = parse_timestamps("00:00 A 03:50 B Song 07:59 C")
    assert starts_titles(got) == [(0.0, "A"), (230.0, "B Song"), (479.0, "C")]


def test_one_line_comment_title_first():
    got = parse_timestamps("早 0:00 泥河 1:22 平等云雾 7:05")
    assert starts_titles(got) == [(0.0, "早"), (82.0, "泥河"), (425.0, "平等云雾")]


def test_ranges():
    got = parse_timestamps("0:00-3:50 Song one\n3:50 ~ 7:00 Song two")
    assert [(e.start, e.end, e.title) for e in got] == [(0.0, 230.0, "Song one"), (230.0, 420.0, "Song two")]


def test_fractional_seconds():
    got = parse_timestamps("00:00.000 Intro\n01:23.450 Next")
    assert [e.start for e in got] == [0.0, 83.45]


def test_plausible_list_rejects_chatter_and_out_of_range():
    chatter = parse_timestamps("03:12 这里好听\n03:15 这里也是\n03:20 哈哈")
    assert plausible_list(chatter, 600) is None
    single = parse_timestamps("1:23 nice")
    assert plausible_list(single, 600) is None
    beyond = parse_timestamps("0:00 a\n12:00 b")
    assert plausible_list(beyond, 300) is None
    ok = parse_timestamps("0:00 a\n3:00 b\n6:10 c")
    assert len(plausible_list(ok, 600)) == 3


def test_tracklist_file_formats():
    text = "# my list\n爱在西元前\n3:59 爸，我回来了\nSidewalks - The Weeknd\n\n"
    got = parse_tracklist_file(text)
    assert [(e.start, e.title, e.artist) for e in got] == [(-1.0, "爱在西元前", ""), (239.0, "爸,我回来了", ""),
                                                          (-1.0, "Sidewalks", "The Weeknd")]


def test_part_titles():
    assert part_title("p01 01.回") == "回"
    assert part_title("03.宿命之眼") == "宿命之眼"
    vt = "WE GO-6  出道专辑《SIX HEARTS》全专音源"
    assert part_title(vt + " p02 Lucky Charm", vt) == "Lucky Charm"
    assert part_title("1004") == "1004"


def test_guess_bracketed_album_and_artist():
    g = guess_from_title("【经典专辑】周杰伦《范特西》华语乐坛巅峰专辑！祝大家节日快乐")
    assert g.works[0] == "范特西" and g.artists[0] == "周杰伦"
    assert g.queries()[0] == "周杰伦 范特西"


def test_guess_pipe_layout_with_year_and_noise():
    g = guess_from_title("【HiRes】  万能青年旅店  |  2020 - 冀西南林路行  |  HiRes臻品整轨专辑无损专辑")
    assert g.artists == ["万能青年旅店"] and g.works == ["冀西南林路行"] and g.year == "2020"


def test_guess_fancy_unicode_and_quote():
    g = guess_from_title("【𝐇𝐢-𝐑𝐞𝐬无损臻享】石衍恺《5th》|“Though it’s far , I’ll never be gone”")
    assert g.works[0] == "5th" and g.artists[0] == "石衍恺"


def test_guess_noise_words_split_fragments():
    g = guess_from_title("【盆栽】Starboy全专翻译（无缝） 另类R&B经典之作")
    assert g.works[0] == "Starboy"
    assert "Starboy" in g.queries()


def test_guess_artist_before_noise():
    g = guess_from_title("【Hi-Res无损】张杰最新专辑《要得》全专分P纯享")
    assert g.artists[0] == "张杰" and g.works[0] == "要得"
    g = guess_from_title("WE GO-6  出道专辑《SIX HEARTS》全专音源")
    assert g.artists[0] == "WE GO-6" and g.works[0] == "SIX HEARTS"


def test_album_hint():
    g = guess_from_hint("The Weeknd - Starboy")
    assert g.artists == ["The Weeknd"] and g.works == ["Starboy"]


def test_names_in_prose():
    d = "官方MV\n专辑里的《爱在西元前》《简单爱》还有《双截棍》，以及再说一次《简单爱》"
    assert names_in_prose(d) == ["爱在西元前", "简单爱", "双截棍"]
