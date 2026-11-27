"""Offline tests for the evaluation fix rules: release choice, derivatives, original-script
titles, edition subsets, name cleaning, reorder by recognition, cut placement, sampling,
provider fallback and the slow-download switch."""

from pathlib import Path

import numpy as np
import pytest

from bili_album.analyze import Candidate, Features, candidates
from bili_album.bilibili import RangedDownloader, mirror_urls
from bili_album.catalog import (CatAlbum, Catalog, CatTrack, NetEase, Provider, edition_subsets, original_script_better,
                                rank_releases, release_kind, score_album)
from bili_album.hints import derivative_cues, guess_from_title, name_like
from bili_album.net import HttpError
from bili_album.plan import CUT_MARGIN, Planner, PlanOptions, Seg, place_cut, sample_indices
from bili_album.recognize import Recognition, Recognizer
from bili_album.textnorm import strip_translation


# ----------------------------------------------------------------------------- fakes

class FakeProvider(Provider):
    def __init__(self, name="fake", tracks=(), albums=(), fail=False):
        self.name = name
        self.tracks = list(tracks)       # [(CatTrack, CatAlbum)]
        self.albums = {a.id: a for a in albums}
        self.fail = fail
        self.queries: list[str] = []

    def search_tracks(self, q):
        self.queries.append(q)
        if self.fail:
            raise HttpError("boom")
        words = q.casefold().split()
        return [(t, a) for t, a in self.tracks
                if any(w in (t.title + " " + t.artist).casefold() for w in words)]

    def search_albums(self, q):
        if self.fail:
            raise HttpError("boom")
        return [a for a in self.albums.values() if a.title.casefold() in q.casefold()]

    def album(self, a):
        return self.albums.get(a.id, a)


class FakeRecognizer(Recognizer):
    """Names whatever song covers the clip in a synthetic timeline."""

    available = True

    def __init__(self, timeline):
        self.timeline = timeline  # [(start, end, title, artist)]
        self.calls = 0

    def recognize(self, audio, start, length=12.0):
        self.calls += 1
        for s, e, title, artist in self.timeline:
            if s <= start < e:
                return Recognition(start, length, title, artist, offset=start - s)
        return Recognition(start, length)


def planner(text_hints=False, providers=()):
    p = Planner(PlanOptions(text_hints=text_hints))
    p.catalog = Catalog(None, list(providers))
    p.feats = None
    return p


def album(title, artist, items, provider="itunes", aid="1", date="2001-01-01"):
    tracks = [CatTrack(t, artist, d, i + 1, provider=provider, id=f"{aid}-{i}") for i, (t, d) in enumerate(items)]
    return CatAlbum(provider, aid, title, artist, release_date=date, year=date[:4], track_count=len(tracks),
                    tracks=tracks)


def feats_from_levels(levels_db: list[tuple[float, float]], fps=10) -> Features:
    """[(seconds, dB)] -> Features with a flat spectrum."""
    rms = np.concatenate([np.full(int(round(sec * fps)), db, dtype=np.float32) for sec, db in levels_db])
    return Features(rms, np.zeros((len(rms), 32), np.float32), len(rms) / fps)


# ----------------------------------------------------------------------------- release choice (item 2)

def test_release_kind_and_ranking_prefer_own_studio_album():
    song = CatTrack("Yellow", "Coldplay", 269.0)
    comp = CatAlbum("netease", "1", "Best 00s Pop Ever", "Various Artists", release_date="2021-01-08", track_count=80)
    single = CatAlbum("netease", "2", "Yellow", "Coldplay", release_date="2000-06-26", track_count=3)
    studio = CatAlbum("itunes", "3", "Parachutes", "Coldplay", release_date="2000-07-10", track_count=10)
    reissue = CatAlbum("itunes", "4", "Parachutes (Remastered)", "Coldplay", release_date="2020-07-10", track_count=10)
    hits = CatAlbum("itunes", "5", "Greatest Hits", "Coldplay", release_date="2010-01-01", track_count=14)
    assert release_kind(comp, song) == "compilation"
    assert release_kind(single, song) == "single"
    assert release_kind(studio, song) == "album"
    assert release_kind(hits, song) == "compilation"
    order = rank_releases([(song, a) for a in (comp, single, reissue, hits, studio)])
    assert [a.title for _, _, a in order][:3] == ["Parachutes", "Parachutes (Remastered)", "Yellow"]


def test_preferred_release_skips_compilation_after_fetching_album_artist():
    song = CatTrack("Yellow", "Coldplay", 269.0, provider="netease", id="a")
    # The search summary carries the song artist; the fetched album reveals Various Artists.
    comp_summary = CatAlbum("netease", "c", "Pop Anthems", "Coldplay", release_date="1999-01-01", track_count=12)
    comp_full = album("Pop Anthems", "Various Artists", [("Yellow", 269.0), ("Other", 200.0)], "netease", "c",
                      "1999-01-01")
    studio = album("Parachutes", "Coldplay", [("Don't Panic", 137.0), ("Yellow", 266.8)], "itunes", "p", "2000-07-10")
    prov = FakeProvider("netease", albums=[comp_full])
    prov2 = FakeProvider("itunes", albums=[studio])
    p = planner(providers=[prov, prov2])
    seg = Seg(0, 270)
    pool = [(song, comp_summary), (studio.tracks[1], CatAlbum("itunes", "p", "Parachutes", "Coldplay",
                                                               release_date="2000-07-10", track_count=10))]
    t, al = p._preferred_release(seg, song, comp_summary, pool)
    assert al.title == "Parachutes" and t.number == 2
    assert not p.review


def test_preferred_release_flags_compilation_only():
    song = CatTrack("Yellow", "Coldplay", 269.0, provider="netease", id="a")
    comp = album("Best 00s Pop Ever", "Various Artists", [("Yellow", 269.0)], "netease", "c", "2021-01-08")
    p = planner(providers=[FakeProvider("netease", albums=[comp])])
    t, al = p._preferred_release(Seg(0, 270), song, comp, [(song, comp)])
    assert al.title == "Best 00s Pop Ever"
    assert any("compilation" in r for r in p.review)


# ----------------------------------------------------------------------------- derivatives (O1)

def test_derivative_cues():
    assert {"司凤版", "演奏", "AI"} <= set(derivative_cues("《520AM》司凤版⚡思罢吗乐队激情演奏！小时候这集听哭了😭【AI MV大赛】"))
    assert "翻唱" in derivative_cues("《晴天》女生翻唱")
    assert derivative_cues("周杰伦《晴天》钢琴版") == ["钢琴版"]
    assert derivative_cues("百万级装备听《Yellow》- Coldplay【Hi-Res】") == []
    assert derivative_cues("张杰《要得》完整版 无损版 录音室版") == []
    assert derivative_cues("Daft Punk - Around the World (Live)") == []


def test_derivative_picks_earliest_same_titled_original_not_nearest_length():
    a_new = CatAlbum("netease", "n", "520am", "苏也莉", release_date="2026-11-25", track_count=1)
    a_orig = CatAlbum("netease", "o", "5:20AM", "soldier", release_date="2024-01-18", track_count=1)
    a_cover = CatAlbum("netease", "c", "5:20AM (翻唱)", "某人", release_date="2023-01-01", track_count=1)
    pool = [(CatTrack("520am", "苏也莉", 121.0), a_new),          # same length as the video
            (CatTrack("5:20AM", "soldier", 172.0), a_orig),
            (CatTrack("5:20AM(翻唱)", "某人", 160.0), a_cover)]  # marked as a cover: not an original
    prov = FakeProvider("netease", tracks=pool)
    p = planner(providers=[prov])
    p.rec = Recognizer()
    p.derivative = {"cues": ["司凤版"]}
    segs, al = p._single_derivative(Seg(0, 121), "520AM", "", ["司凤版"], pool, None, 121.0)
    assert (segs[0].title, segs[0].artist) == ("5:20AM", "soldier")
    assert al.release_date == "2024-01-18"
    assert any("derivative version (cue: 司凤版)" in r for r in p.review)


def test_derivative_without_believable_original_says_so():
    p = planner()
    p.derivative = {"cues": ["翻唱"]}
    segs, al = p._single_derivative(Seg(0, 200), "Some Song", "", ["翻唱"], [], None, 200.0)
    assert segs[0].title == "Some Song" and al is None
    assert any("no catalog song" in r for r in p.review)


# ----------------------------------------------------------------------------- original script (O2)

def test_strip_translation():
    assert strip_translation("괴물(Monster)") == "괴물"
    assert strip_translation("처음이자 마지막으로") == "처음이자 마지막으로"
    assert strip_translation("敬自己不为谁 (feat. GAI周延)") == "敬自己不为谁 (feat. GAI周延)"
    assert strip_translation("Lucky Charm") == "Lucky Charm"
    assert strip_translation("夜に駆ける (English Ver.)") == "夜に駆ける (English Ver.)"
    assert strip_translation("Sunday (周日)") == "Sunday (周日)"


def test_original_script_better():
    assert original_script_better("First and Last", "처음이자 마지막으로")
    assert not original_script_better("처음이자 마지막으로", "First and Last")
    assert not original_script_better("Yellow", "黄色")                 # Han for a Latin-named artist
    assert original_script_better("Sunny Day", "晴天", artist="周杰伦")


def test_catalog_takes_original_titles_from_other_provider():
    us = album("SIX HEARTS", "WE GO-6", [("Monster", 191.0), ("Lucky Charm", 126.0), ("First and Last", 171.0)],
               "itunes", "u")
    ne = album("SIX HEARTS", "WE GO-6", [("괴물(Monster)", 191.5), ("Lucky Charm", 126.0),
                                         ("처음이자 마지막으로", 171.2)], "netease", "n")
    cat = Catalog(None, [FakeProvider("itunes", albums=[us]), FakeProvider("netease", albums=[ne])])
    notes = cat.original_titles(us)
    assert [t.title for t in us.tracks] == ["괴물(Monster)", "Lucky Charm", "처음이자 마지막으로"]
    assert len(notes) == 2


def test_translated_recognition_is_not_a_disagreement():
    al = album("SIX HEARTS", "WE GO-6", [("괴물(Monster)", 191.0), ("Lucky Charm", 126.0),
                                         ("처음이자 마지막으로", 171.0)], "netease")
    p = planner(providers=[FakeProvider()])
    r = Recognition(340, 12, "First and Last", "WE GO-6")
    assert p._match_kind(r, "처음이자 마지막으로", "", 171, al, "WE GO-6") == "translation"
    # Another track of the same album is a real disagreement even with the same artist.
    r2 = Recognition(340, 12, "Lucky Charm", "WE GO-6")
    assert p._match_kind(r2, "처음이자 마지막으로", "", 171, al, "WE GO-6") == ""
    assert p._match_kind(Recognition(5, 12, "Monster", "WE GO-6"), "괴물(Monster)", "", 191, al, "WE GO-6")


# ----------------------------------------------------------------------------- edition subsets (item 4)

def test_edition_with_instrumental_block_matches_its_vocal_subset():
    songs = [(f"歌{i}", 270.0) for i in range(10)]
    vocal_and_inst = songs + [(f"歌{i} (伴奏)", 270.0) for i in range(10)]
    ed = album("没语季节", "黄霄雲", vocal_and_inst, "netease")
    m = score_album(ed, duration=2700.0, artists=["黄霄云"], works=["没语季节"])
    assert len(m.album.tracks) == 10 and m.duration_score > 0.9 and m.score >= 0.55
    assert "10 of 20 edition tracks" in m.reason
    # Same, with the instrumentals listed under the same titles (a repeated block).
    rep = album("没语季节", "黄霄雲", songs + songs, "itunes")
    m2 = score_album(rep, duration=2700.0, artists=["黄霄云"], works=["没语季节"])
    assert len(m2.album.tracks) == 10


def test_edition_subsets_keep_full_album_and_ignore_unmarked_tails():
    std = album("Starboy", "The Weeknd", [(f"t{i}", 228.0) for i in range(18)])
    deluxe = album("Starboy (Deluxe)", "The Weeknd", [(f"t{i}", 228.0) for i in range(21)])
    D = 228.0 * 18
    assert score_album(std, duration=D, works=["Starboy"]).score > score_album(deluxe, duration=D,
                                                                              works=["Starboy"]).score
    assert [lbl for lbl, _ in edition_subsets(deluxe.tracks)] == [""]


# ----------------------------------------------------------------------------- name cleaning (items 3, 4)

def test_title_noise_and_descriptive_phrases_leave_the_name():
    g = guess_from_title("在专业录音棚听 周杰伦《搁浅》｜Hi-Res 无损")
    assert g.artists == ["周杰伦"] and g.works == ["搁浅"]
    g = guess_from_title("百万级装备听《Yellow》- Coldplay【Hi-Res】")
    assert g.artists == ["Coldplay"] and g.works == ["Yellow"]
    g = guess_from_title("Hi·Res | 黄霄雲 原创专辑《没语季节》超高音质+歌词纯享")
    assert g.artists == ["黄霄云"] and g.works[0] == "没语季节"
    assert not name_like("在专业录音棚听 周杰伦") and not name_like("小时候这集听哭了") and name_like("WE GO-6")


# ----------------------------------------------------------------------------- reorder by recognition (item 1)

def _vinyl_case():
    # Catalog (digital) order: A B C W P D E F X ; the video (vinyl) plays A B C P W D E F, no X.
    digital = [("A", 193.4), ("B", 203.6), ("C", 170.9), ("W", 178.0), ("P", 204.8), ("D", 179.2),
               ("E", 200.4), ("F", 229.4), ("X", 179.0)]
    lens = dict(digital)
    video = ["A", "B", "C", "P", "W", "D", "E", "F"]
    starts, t = [], 0.0
    for name in video:
        starts.append(t)
        t += lens[name] + 1.5                       # vinyl runs a little long
    duration = t
    cands = [Candidate(s, 0.9, "silence", s - 1.0, s + 0.2, -60.0, s - 1.0, s + 0.2) for s in starts[1:]]
    timeline = [(s, (starts[i + 1] if i + 1 < len(starts) else duration), video[i], "Artist")
                for i, s in enumerate(starts)]
    return album("dsam", "Artist", digital), video, starts, duration, cands, timeline


def test_recognition_reorders_and_drops_absent_catalog_tracks():
    al, video, starts, duration, cands, timeline = _vinyl_case()
    p = planner(providers=[FakeProvider()])
    p.rec = FakeRecognizer(timeline)
    p.duration = duration
    p.album_match = score_album(al, duration=duration, works=["dsam"])
    p._align_mark = 0
    p._from_catalog_alignment = True
    segs = p._from_album(al, duration, cands)
    segs = p._refine_with_recognition(Path("x.m4a"), segs, cands, al)
    assert [s.title for s in segs] == video
    for s, t in zip(segs[1:], starts[1:]):
        assert abs(s.start - t) < 3.0
    assert p.reordered and p.recog_info["policy"] == "full"
    assert any("track order follows recognition" in r and "'X'" in r for r in p.review)
    assert not any("plan says" in r for r in p.review)


# ----------------------------------------------------------------------------- sampling (item 6)

def test_sample_indices_cover_each_third():
    assert sample_indices(10) == [1, 5, 8]
    assert sample_indices(3) == [0, 1, 2]
    for n in (6, 8, 18):
        idx = sample_indices(n)
        assert len(idx) == 3 and idx[0] < n / 3 <= idx[1] < 2 * n / 3 <= idx[2]


def _clean_case(swap=False):
    items = [(f"T{i}", 200.0 + 7 * i) for i in range(10)]
    if swap:  # equal lengths around the swap, so the catalog lengths cannot reveal it
        items[7] = ("T7", 214.0)
        items[8] = ("T8", 214.0)
    al = album("Clean", "Artist", items)
    order = [t for t, _ in items]
    if swap:
        order[7], order[8] = order[8], order[7]
    starts, t = [], 0.0
    for name in order:
        starts.append(t)
        t += dict(items)[name]
    duration = t
    cands = [Candidate(s, 1.0, "silence", s - 1.5, s + 0.2, -120.0, s - 1.5, s + 0.2) for s in starts[1:]]
    timeline = [(s, (starts[i + 1] if i + 1 < len(starts) else duration), order[i], "Artist")
                for i, s in enumerate(starts)]
    return al, duration, cands, timeline


def test_clean_alignment_is_sampled_with_few_requests():
    al, duration, cands, timeline = _clean_case()
    p = planner(providers=[FakeProvider()])
    p.rec = FakeRecognizer(timeline)
    p.duration = duration
    p.album_match = score_album(al, duration=duration, works=["Clean"])
    p._from_catalog_alignment = True
    segs = p._from_album(al, duration, cands)
    segs = p._refine_with_recognition(Path("x.m4a"), segs, cands, al)
    assert p.rec.calls <= 5
    assert p.recog_info["policy"] == "sampled" and p.recog_info["tracks_checked"] == 3
    assert [s.title for s in segs] == [f"T{i}" for i in range(10)]
    assert not p.review


def test_sample_disagreement_triggers_full_check():
    # Two adjacent tracks swapped where lengths cannot tell (equal lengths around the swap).
    al, duration, cands, timeline = _clean_case(swap=True)
    p = planner(providers=[FakeProvider()])
    p.rec = FakeRecognizer(timeline)
    p.duration = duration
    p.album_match = score_album(al, duration=duration, works=["Clean"])
    p._from_catalog_alignment = True
    segs = p._from_album(al, duration, cands)
    segs = p._refine_with_recognition(Path("x.m4a"), segs, cands, al)
    assert p.recog_info["policy"] == "full"
    assert [s.title for s in segs][7:9] == ["T8", "T7"]


# ----------------------------------------------------------------------------- cut placement (item 5)

def test_silence_candidate_cuts_at_deepest_block_not_before_a_soft_intro():
    # loud song, fade, 1.3 s digital zero, 3.7 s soft intro (-45 dB), loud song
    f = feats_from_levels([(30, -10), (3, -55), (1.3, -120), (3.7, -50), (30, -10)])
    c = [c for c in candidates(f) if c.kind == "silence"]
    assert len(c) == 1
    assert c[0].digital and abs(c[0].time - 34.1) < 0.25          # end of the zero block (34.3)
    # A uniformly quiet gap: the cut stays near its end, before the onset.
    g = feats_from_levels([(30, -10), (2.0, -90), (30, -10)])
    c2 = [c for c in candidates(g) if c.kind == "silence"][0]
    assert abs(c2.time - 31.8) < 0.15


def test_place_cut_never_after_recognised_start_unless_silent():
    # Quiet (not silent) region 100-106 s, recording recognised to start at 101.0.
    f = feats_from_levels([(100, -10), (6, -50), (30, -10)])
    quiet = Candidate(105.8, 1.0, "silence", 100.0, 106.0, -50.0, 100.0, 106.0)
    t, note = place_cut(101.0, [quiet], f)
    assert t == pytest.approx(101.0 - CUT_MARGIN) and "held" in note
    # Digital silence between the recognised start and the gap's end: cutting at the end is fine.
    g = feats_from_levels([(100, -10), (6, -120), (30, -10)])
    silent = Candidate(105.8, 1.0, "silence", 100.0, 106.0, -120.0, 100.0, 106.0)
    t2, note2 = place_cut(101.0, [silent], g)
    assert t2 == pytest.approx(105.8) and "digital silence" in note2
    # No gap at the recognised start: cut just before it.
    t3, _ = place_cut(150.0, [quiet], f)
    assert t3 == pytest.approx(150.0 - CUT_MARGIN)


# ----------------------------------------------------------------------------- provider fallback (item 3)

def test_failing_provider_falls_back_to_the_other():
    t = CatTrack("搁浅", "周杰伦", 238.1, provider="itunes", id="1")
    a = CatAlbum("itunes", "q", "七里香", "周杰伦", release_date="2004-08-03", track_count=10)
    cat = Catalog(None, [FakeProvider("netease", fail=True), FakeProvider("itunes", tracks=[(t, a)])])
    hits = cat.find_track("搁浅", "周杰伦", 240.0)
    assert hits and hits[0][1].title == "搁浅" and cat.errors


class _Resp:
    def __init__(self, obj):
        self.obj = obj


class FakeHttp:
    cache_dir = None

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def get_json(self, url, params=None, headers=None, validate=None, stateless=False, cache=True):
        self.calls.append(cache)
        return self.answers.pop(0)

    def store_json(self, *a):
        pass


def test_netease_unrelated_answer_is_retried_then_reported_clearly():
    junk = {"code": 200, "result": {"songs": [{"name": "Popular Hit", "artists": [{"name": "Someone"}]}]}}
    ne = NetEase(FakeHttp([junk, junk]))
    ne.retry_pause = 0
    with pytest.raises(HttpError, match="unrelated to the query"):
        ne.search_tracks("搁浅")
    good = {"code": 200, "result": {"songs": [{"name": "搁浅", "artists": [{"name": "x"}], "id": 1}]}}
    ne2 = NetEase(FakeHttp([junk, good]))
    ne2.retry_pause = 0
    assert ne2.search_tracks("搁浅")[0][0].title == "搁浅"
    assert ne2.http.calls == [True, False]


def test_single_searches_title_alone_when_artist_guess_hides_the_song():
    t = CatTrack("搁浅", "周杰倫", 238.1, provider="itunes", id="1")
    a = CatAlbum("itunes", "q", "七里香", "周杰倫", release_date="2004-08-03", track_count=10)

    class Picky(FakeProvider):
        def search_tracks(self, q):
            self.queries.append(q)
            return [(t, a)] if q == "搁浅" else []

    prov = Picky("itunes", albums=[album("七里香", "周杰倫", [("我的地盘", 200.0), ("搁浅", 238.1)], "itunes", "q",
                                         "2004-08-03")])
    p = planner(providers=[prov])
    p.rec = Recognizer()
    from bili_album.hints import TitleGuess
    from bili_album.bilibili import VideoInfo
    vi = VideoInfo("", "x", 0, "《搁浅》", "", 0, "", 240.0, "", 0)
    segs, al = p._single(vi, Path("x"), 240.0, TitleGuess(works=["搁浅"], artists=["某个错的名字"]), None)
    assert segs[0].title == "搁浅" and al.title == "七里香"
    assert "搁浅" in prov.queries


# ----------------------------------------------------------------------------- slow download switch (item 7)

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeResp:
    def __init__(self, data, start, total, clock, rate, status=206):
        self.data, self.start, self.total, self.clock, self.rate = data, start, total, clock, rate
        self.status_code = status
        self.headers = {"Content-Range": f"bytes {start}-{start + len(data) - 1}/{total}"}

    def iter_content(self, n):
        for i in range(0, len(self.data), n):
            buf = self.data[i:i + n]
            self.clock.t += len(buf) / self.rate
            yield buf

    def close(self):
        pass


class FakeSession:
    def __init__(self, blob, rates, clock):
        self.blob, self.rates, self.clock = blob, rates, clock
        self.requests = []

    def get(self, url, headers=None, stream=True, timeout=None):
        a, b = map(int, headers["Range"].split("=")[1].split("-"))
        self.requests.append((url, a))
        if self.rates[url] is None:
            return FakeResp(b"", a, len(self.blob), self.clock, 1, status=403)
        return FakeResp(self.blob[a:b + 1], a, len(self.blob), self.clock, self.rates[url])


def test_slow_host_is_switched_and_file_is_complete(tmp_path):
    blob = bytes(range(256)) * 40000            # ~10 MB
    clock = FakeClock()
    sess = FakeSession(blob, {"https://slow/a": 100_000, "https://dead/a": None, "https://fast/a": 20_000_000}, clock)
    dl = RangedDownloader(sess, {}, chunk=4 * 1024 * 1024, clock=clock, read_size=64 * 1024)
    out = dl.download(["https://slow/a", "https://dead/a", "https://fast/a"], tmp_path / "x")
    assert out.read_bytes() == blob
    assert any("far below normal" in e for e in dl.log)
    assert clock.t < 10                          # the slow host alone would need ~100 s
    assert sess.requests[-1][0] == "https://fast/a"


def test_all_hosts_slow_finishes_on_fastest(tmp_path):
    blob = b"x" * 3_000_000
    clock = FakeClock()
    sess = FakeSession(blob, {"https://a/": 200_000, "https://b/": 400_000}, clock)
    dl = RangedDownloader(sess, {}, chunk=1_000_000, clock=clock, read_size=50_000)
    assert dl.download(["https://a/", "https://b/"], tmp_path / "y").read_bytes() == blob
    assert sess.requests[-1][0] == "https://b/"


def test_mirror_urls_only_for_object_storage_paths():
    u = "https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/12/34/x-30280.m4s?deadline=1&upsig=z"
    ms = mirror_urls(u)
    assert len(ms) == 2 and all("/upgcxcode/12/34/x-30280.m4s?deadline=1&upsig=z" in m for m in ms)
    assert mirror_urls("https://cn-x.mcdn.bilivideo.cn/v1/resource/x.m4s") == []


# ----------------------------------------------------------------------------- refinements found live

def test_original_is_the_release_other_versions_followed_not_an_older_namesake():
    from bili_album.plan import pick_original
    namesake = (CatTrack("520Am", "Some Band", 188.0), CatAlbum("itunes", "x", "Siege Engine", "Some Band",
                                                                 release_date="2014-02-08"))
    orig = (CatTrack("5:20AM", "soldier", 160.0), CatAlbum("netease", "o", "5:20AM", "soldier",
                                                            release_date="2024-01-18"))
    later = [(CatTrack("5:20AM", f"singer{i}", 150.0 + i), CatAlbum("itunes", f"l{i}", "5:20AM - Single", f"singer{i}",
                                                                     release_date=f"2024-0{i + 2}-01"))
             for i in range(3)]
    dj = (CatTrack("5:20AM（DJ版）", "dj", 156.0), CatAlbum("netease", "d", "x", "dj", release_date="2024-03-01"))
    same = [namesake, orig] + later
    ranked, why = pick_original(same, same + [dj])
    assert ranked[0] is orig and "followed" in why
    # An explicit credit wins over dates.
    credit = (CatTrack("5:20AM(原唱：singer1)", "fan", 150.0), CatAlbum("netease", "c", "x", "fan",
                                                                        release_date="2025-01-01"))
    ranked, why = pick_original(same, same + [credit])
    assert ranked[0][0].artist == "singer1" and "credit" in why
    # No release drew versions: the earliest.
    ranked, why = pick_original([namesake, orig], [namesake, orig])
    assert ranked[0] is namesake and "earliest" in why


def test_faint_noise_after_digital_zero_still_counts_as_silence():
    # fade, 0.6 s of digital zero, 0.8 s of faint noise at -90 dB, then the song
    f = feats_from_levels([(30, -10), (2.0, -60), (0.6, -120), (0.8, -90), (30, -10)])
    c = [c for c in candidates(f) if c.kind == "silence"][0]
    assert c.digital and abs(c.time - 33.2) < 0.15            # just before the onset at 33.4


def test_weak_start_is_flagged_unless_recognition_pins_it():
    p = planner()
    a = Seg(0, 200, title="A", start_kind="start")
    b = Seg(200, 400, title="B", start_kind="grid", confidence=0.7)
    c = Seg(400, 600, title="C", start_kind="novelty", confidence=0.7)
    c.start_strength = 0.3
    d = Seg(600, 800, title="D", start_kind="silence", confidence=0.7)
    p._flag_weak_starts([a, b, c, d])
    assert [r.split(":")[0] for r in p.review] == ["track 2", "track 3"]
    assert b.confidence == pytest.approx(0.5) and d.confidence == pytest.approx(0.7)
    p2 = planner()
    b.pinned = True
    p2._flag_weak_starts([a, b])
    assert not p2.review
