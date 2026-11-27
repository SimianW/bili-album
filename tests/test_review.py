"""Model review against a local fake chat-completions endpoint and a fake catalog (offline)."""

from __future__ import annotations

import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bili_album.catalog import CatAlbum, Catalog, CatTrack, Provider
from bili_album.net import Http
from bili_album.review import (ChatClient, SYSTEM_PROMPT, _file_id, build_brief, gather_context, parse_reply,
                               review_plan, structural_problems)

FAKE_KEY = "sk-test-not-real-123456"


# ----------------------------------------------------------------------------- fake endpoint

class FakeEndpoint:
    """Serves queued replies on /v1/chat/completions and records the requests."""

    def __init__(self):
        self.replies: list = []
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"body": body, "auth": self.headers.get("Authorization")})
                item = outer.replies.pop(0) if outer.replies else {"status": 500, "raw": "no reply queued"}
                if item.get("sleep"):
                    time.sleep(item["sleep"])
                status = item.get("status", 200)
                if "raw" in item:
                    raw = item["raw"].replace("{AUTH}", self.headers.get("Authorization", ""))
                else:
                    content = item["content"] if isinstance(item["content"], str) else json.dumps(item["content"])
                    raw = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
                data = raw.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self, timeout: float = 10.0) -> ChatClient:
        return ChatClient(self.url, "fake-model", FAKE_KEY, timeout=timeout)

    def close(self):
        self.server.shutdown()


@pytest.fixture
def endpoint():
    ep = FakeEndpoint()
    yield ep
    ep.close()


# ----------------------------------------------------------------------------- fake catalog

def T(title, artist, dur, n=1):
    return CatTrack(title=title, artist=artist, duration=dur, number=n, id=f"t-{artist}-{title}", provider="fake")


ALBUMS = [
    CatAlbum("fake", "comp", "Best 00s Pop Ever", "Various Artists", year="2021", release_date="2021-01-08",
             tracks=[T("Crazy", "Gnarls Barkley", 178, 1), T("Yellow", "Coldplay", 270.4, 2)]),
    CatAlbum("fake", "para", "Parachutes", "Coldplay", year="2000", release_date="2000-07-10", genre="Rock",
             tracks=[T("Don't Panic", "Coldplay", 137, 1), T("Yellow", "Coldplay", 269.2, 5)]),
    CatAlbum("fake", "live", "Live 2003", "Coldplay", year="2003",
             tracks=[T("Yellow", "Coldplay", 320.0, 3)]),
    CatAlbum("fake", "fant", "范特西", "周杰伦", year="2001",
             tracks=[T("爱在西元前", "周杰伦", 234.0, 1), T("爸，我回来了", "周杰伦", 233.0, 2),
                     T("简单爱", "周杰伦", 270.0, 3)]),
    CatAlbum("fake", "s520", "5:20AM", "soldier", release_date="2024-01-18", tracks=[T("5:20AM", "soldier", 160)]),
    CatAlbum("fake", "sf", "520am(司凤版)", "礼崩乐不坏", release_date="2026-11-17",
             tracks=[T("5:20AM", "礼崩乐不坏", 107)]),
]


class FakeProvider(Provider):
    name = "fake"

    def search_albums(self, q):
        return [CatAlbum(a.provider, a.id, a.title, a.artist, year=a.year, release_date=a.release_date,
                         track_count=len(a.tracks)) for a in ALBUMS]

    def album(self, a):
        return copy.deepcopy(next(x for x in ALBUMS if x.id == a.id))

    def search_tracks(self, q):
        out = []
        for a in ALBUMS:
            summ = CatAlbum(a.provider, a.id, a.title, a.artist, year=a.year, release_date=a.release_date,
                            track_count=len(a.tracks))
            out += [(copy.deepcopy(t), summ) for t in a.tracks]
        return out


# ----------------------------------------------------------------------------- plans

def make_workdir(tmp_path: Path, recognitions=(), candidates=()) -> Path:
    wd = tmp_path / "work"
    (wd / "cache").mkdir(parents=True)
    audio = wd / "source.m4a"
    audio.write_bytes(bytes(range(256)) * 400)
    fid = _file_id(audio)
    for r in recognitions:
        rec = {"clip_start": r[0], "clip_len": 12.0, "title": r[1], "artist": r[2], "album": "", "key": "",
               "offset": r[3], "skew": 0.0}
        (wd / "cache" / f"shazam-zhCN-TW-{fid}-{r[0]:.2f}-12.0.json").write_text(json.dumps(rec), encoding="utf-8")
    (wd / "candidates.json").write_text(json.dumps([{"time": t, "strength": s, "kind": k, "gap_start": t,
                                                     "gap_end": t} for t, s, k in candidates]), encoding="utf-8")
    return wd


def track(i, start, end, title, artist, album, album_artist, cat_id="", **kw):
    return {"index": i, "start": start, "end": end, "title": title, "artist": artist, "album": album,
            "album_artist": album_artist, "track_number": i, "disc_number": 1, "year": "", "genre": "",
            "composer": "", "lyrics": "", "cover_path": "", "catalog": {"provider": "fake" if cat_id else "",
                                                                        "id": cat_id},
            "confidence": 0.8, "evidence": [], **kw}


def single_plan(wd: Path, *, text_hints=False) -> dict:
    return {
        "version": 1,
        "source": {"url": "https://www.bilibili.com/video/BV1xx411c7XX", "bvid": "BV1xx411c7XX",
                   "title": "听《Yellow》- Coldplay【Hi-Res】", "uploader": "someone", "duration": 269.952,
                   "audio_path": str(wd / "source.m4a"), "cover_path": "", "parts": 1, "trim_head": 0.0,
                   "trim_tail": 0.0, "text_hints": text_hints},
        "kind": "single", "kind_reason": "short",
        "album": {"title": "Best 00s Pop Ever", "artist": "Various Artists", "year": "2021-01-08", "genre": "",
                  "track_total": 2, "disc_total": 1, "cover_path": "",
                  "catalog": {"provider": "fake", "id": "comp", "url": ""}, "match_reason": ""},
        "tracks": [track(1, 0.0, 269.952, "Yellow", "Coldplay", "Best 00s Pop Ever", "Various Artists",
                         "t-Coldplay-Yellow")],
        "needs_review": False, "review_reasons": [], "timings": {},
    }


def comp_plan(wd: Path) -> dict:
    p = single_plan(wd)
    p["kind"] = "compilation"
    p["source"]["duration"] = 740.0
    p["source"]["title"] = "周杰伦《范特西》"
    p["album"] = {"title": "范特西", "artist": "周杰伦", "year": "2001", "genre": "", "track_total": 3,
                  "disc_total": 1, "cover_path": "", "catalog": {"provider": "fake", "id": "fant", "url": ""},
                  "match_reason": "fits"}
    p["tracks"] = [track(1, 0.0, 236.0, "爱在西元前", "周杰伦", "范特西", "周杰伦", "a"),
                   track(2, 236.0, 470.0, "爸，我回来了", "周杰伦", "范特西", "周杰伦", "b"),
                   track(3, 470.0, 740.0, "简单爱", "周杰伦", "范特西", "周杰伦", "c")]
    p["needs_review"] = True
    p["review_reasons"] = ["track 2: length disagrees"]
    return p


def write_plan(wd: Path, plan: dict) -> Path:
    path = wd / "plan.json"
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return path


def run(path: Path, endpoint: FakeEndpoint, **kw):
    cat = Catalog(Http(path.parent / "cache", offline=True), providers=[FakeProvider()])
    kw.setdefault("text_loader", None)
    return review_plan(path, client=endpoint.client(kw.pop("timeout", 10.0)), catalog=cat, **kw)


def reply(verdict="approved", edits=(), conf=0.9, reasons=("看起来没问题",), concerns=()):
    return {"content": {"verdict": verdict, "confidence": conf, "edits": list(edits), "reasons": list(reasons),
                        "remaining_concerns": list(concerns)}}


# ----------------------------------------------------------------------------- tests

def test_approve_clears_review_and_keeps_original(tmp_path, endpoint):
    wd = make_workdir(tmp_path, candidates=[(236.0, 0.8, "silence"), (470.0, 0.8, "silence")])
    path = write_plan(wd, comp_plan(wd))
    endpoint.replies.append(reply())
    out = run(path, endpoint)
    plan = json.loads(path.read_text(encoding="utf-8"))
    assert plan["needs_review"] is False and plan["review_reasons"] == []
    rv = plan["review"]
    assert rv["verdict"] == "approved" and rv["model"] == "fake-model"
    assert rv["resolved"] == ["track 2: length disagrees"]
    assert rv["reasons"] == ["看起来没问题"] and rv["usage"]["total_tokens"] == 120
    assert (wd / "plan.unreviewed.json").exists() and out.unreviewed_path == wd / "plan.unreviewed.json"
    assert json.loads((wd / "plan.unreviewed.json").read_text())["needs_review"] is True
    req = endpoint.requests[0]
    assert req["auth"] == f"Bearer {FAKE_KEY}"
    assert req["body"]["response_format"] == {"type": "json_object"}
    assert FAKE_KEY not in path.read_text(encoding="utf-8")


def test_single_moves_to_studio_album(tmp_path, endpoint):
    wd = make_workdir(tmp_path, recognitions=[(129.0, "Yellow", "Coldplay", 129.0)])
    path = write_plan(wd, single_plan(wd))
    endpoint.replies.append(None)  # placeholder, replaced once we know the candidate ids
    endpoint.replies.clear()
    ctx = gather_context(json.loads(path.read_text()), path, catalog=Catalog(Http(None, offline=True),
                                                                            providers=[FakeProvider()]),
                         text_loader=None)
    para = next(r["id"] for r in ctx.album_candidates if r["album"] == "Parachutes")
    live = next(r["id"] for r in ctx.album_candidates if r["album"] == "Live 2003")
    endpoint.replies.append(reply("edited", [{"op": "choose_album", "candidate": live},
                                             {"op": "choose_album", "candidate": para}]))
    out = run(path, endpoint)
    p = out.plan
    assert p["album"]["title"] == "Parachutes" and p["album"]["artist"] == "Coldplay"
    t = p["tracks"][0]
    assert (t["album"], t["album_artist"], t["track_number"], t["year"]) == ("Parachutes", "Coldplay", 5,
                                                                           "2000-07-10")
    assert any(e["kind"] == "llm_review" for e in t["evidence"])
    # The live album's Yellow is 320 s: a different recording, rejected -> stays flagged.
    assert len(out.review["edits_rejected"]) == 1 and "different recording" in out.review["edits_rejected"][0]["why"]
    assert p["needs_review"] is True


def test_single_has_no_boundaries_in_brief(tmp_path):
    wd = make_workdir(tmp_path, recognitions=[(129.0, "Yellow", "Coldplay", 126.7)], candidates=[(2.0, 0.9, "dip")])
    plan = single_plan(wd)
    ctx = gather_context(plan, None, catalog=Catalog(Http(None, offline=True), providers=[FakeProvider()]),
                         text_loader=None)
    b = build_brief(plan, ctx)
    assert b["boundaries"] == []
    assert b["recognition"][0]["starts_at"] == 2.3


def test_brief_is_blind_without_text_hints_and_has_no_home_paths(tmp_path):
    wd = make_workdir(tmp_path)
    plan = single_plan(wd, text_hints=False)
    called = []
    ctx = gather_context(plan, None, catalog=Catalog(Http(None, offline=True), providers=[FakeProvider()]),
                         text_loader=lambda p, h: called.append(1) or {"description": "x"})
    b = build_brief(plan, ctx)
    assert "uploader_text" not in b and not called
    s = json.dumps(b, ensure_ascii=False)
    assert str(tmp_path) not in s and "/home/" not in s
    plan["source"]["text_hints"] = True
    ctx = gather_context(plan, None, catalog=Catalog(Http(None, offline=True), providers=[FakeProvider()]),
                         text_loader=lambda p, h: {"description": "曲目：Yellow", "comments": []})
    assert build_brief(plan, ctx)["uploader_text"]["description"] == "曲目：Yellow"
    assert "untrusted" in SYSTEM_PROMPT


def test_relabel_from_memory_rejected_but_catalog_title_accepted(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    plan = comp_plan(wd)
    plan["tracks"][2]["title"] = "简单 爱"
    path = write_plan(wd, plan)
    endpoint.replies.append(reply("edited", [
        {"op": "relabel_track", "track": 1, "title": "A Song Nobody Released"},
        {"op": "relabel_track", "track": 3, "title": "简单爱"},
    ]))
    out = run(path, endpoint)
    assert out.plan["tracks"][2]["title"] == "简单爱"
    assert [r["edit"]["track"] for r in out.review["edits_rejected"]] == [1]
    assert out.plan["tracks"][0]["title"] == "爱在西元前"
    assert out.needs_review is True
    assert any("rejected" in r for r in out.plan["review_reasons"])
    applied, rejected = out.review["edits_applied"][0], out.review["edits_rejected"][0]
    assert applied["reason_zh"] == "第 3 首歌名「简单 爱」改为「简单爱」（依据：专辑曲目表）。"
    assert applied["tracks"] == [3] and rejected["tracks"] == [1]
    assert rejected["reason_zh"].startswith("工具无法核实“修改曲名/艺人（第 1 首）”")
    assert 1 in out.review["flagged_tracks"] and 2 in out.review["flagged_tracks"]   # 2: earlier "track 2" reason


def test_relabel_confirmed_by_catalog_lookup_uses_catalog_spelling(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    plan = comp_plan(wd)
    plan["album"]["catalog"] = {"provider": "", "id": "", "url": ""}   # no tracklist: lookup needed
    plan["tracks"][1]["title"] = "爸我回来了"
    path = write_plan(wd, plan)
    endpoint.replies.append(reply("edited", [{"op": "relabel_track", "track": 2, "title": "爸,我回来了",
                                              "artist": "周杰伦"}]))
    out = run(path, endpoint)
    assert out.plan["tracks"][1]["title"] == "爸，我回来了"   # the catalog's spelling, not the model's
    assert "catalog lookup" in out.review["edits_applied"][0]["detail"]


def test_boundary_moves_only_to_measured_options(tmp_path, endpoint):
    wd = make_workdir(tmp_path, candidates=[(240.0, 0.9, "silence"), (470.6, 0.7, "dip")])
    path = write_plan(wd, comp_plan(wd))
    endpoint.replies.append(reply("edited", [
        {"op": "move_boundary", "boundary": 2, "to": 239.0},     # invented -> rejected
        {"op": "move_boundary", "boundary": 2, "to": 240.0},     # measured -> applied
        {"op": "move_boundary", "boundary": 3, "to": 470.6},     # measured but 0.6 s -> skipped
    ]))
    out = run(path, endpoint)
    t = out.plan["tracks"]
    assert t[0]["end"] == 240.0 and t[1]["start"] == 240.0 and t[2]["start"] == 470.0
    assert len(out.review["edits_applied"]) == 1
    assert "not one of the measured options" in out.review["edits_rejected"][0]["why"]
    assert "noise" in out.review["edits_skipped"][0]["why"]
    notes = out.review["tool_notes"]
    assert notes[0].startswith("工具未执行“移动切点（第 3 个切点") and notes[1].startswith("工具无法核实")


def test_reorder_split_drop(tmp_path, endpoint):
    wd = make_workdir(tmp_path, recognitions=[(30.0, "爸，我回来了", "周杰伦", 0.0), (260.0, "爱在西元前", "周杰伦", 24.0),
                                               (600.0, "Ad Jingle", "Brand", 100.0)],
                      candidates=[(500.0, 0.95, "silence")])
    path = write_plan(wd, comp_plan(wd))
    endpoint.replies.append(reply("edited", [
        {"op": "reorder_tracks", "order": [2, 1, 3]},
        {"op": "split_track", "track": 3, "at": 500.0, "title": "Ad Jingle", "artist": "Brand"},
    ]))
    out = run(path, endpoint)
    ts = out.plan["tracks"]
    assert [t["title"] for t in ts] == ["爸，我回来了", "爱在西元前", "简单爱", "Ad Jingle"]
    assert [t["index"] for t in ts] == [1, 2, 3, 4]
    assert ts[2]["end"] == 500.0 and ts[3]["start"] == 500.0
    assert out.review["edits_rejected"] == []
    # Dropping the jingle: recognition disagrees with nothing (it is labelled as heard), track has no catalog id.
    path2 = write_plan(wd, out.plan | {"review": None})
    endpoint.replies.append(reply("edited", [{"op": "drop_track", "track": 4, "merge": "previous"},
                                             {"op": "drop_track", "track": 1}]))
    out2 = run(path2, endpoint)
    assert [t["title"] for t in out2.plan["tracks"]] == ["爸，我回来了", "爱在西元前", "简单爱"]
    assert out2.plan["tracks"][-1]["end"] == 740.0
    assert "nothing contradicts it" in out2.review["edits_rejected"][0]["why"]
    drop = out2.review["edits_applied"][0]
    assert drop["reason_zh"] == "删除第 4 首「Ad Jingle」（曲库里没有对应的歌），并入上一首。" and drop["tracks"] == [3]
    assert out2.review["flagged_tracks"] == [1]           # the rejected drop of track 1


def test_derivative_stays_flagged_with_original_artist(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    plan = single_plan(wd)
    plan["source"]["title"] = "《520AM》司凤版【AI MV】"
    plan["tracks"][0].update({"title": "520am", "artist": "苏也莉", "album": "520am", "album_artist": "苏也莉"})
    path = write_plan(wd, plan)
    endpoint.replies.append(reply("needs_human", [
        {"op": "mark_derivative", "version": "ai_remake", "original_title": "5:20AM",
         "original_artist": "礼崩乐不坏"},                       # a 司凤版 release is not an original
        {"op": "mark_derivative", "version": "ai_remake", "original_title": "5:20AM", "original_artist": "soldier"},
    ], concerns=("原曲归属需要确认",)))
    out = run(path, endpoint)
    p = out.plan
    assert p["derivative"]["original_artist"] == "soldier"
    t = p["tracks"][0]
    assert (t["title"], t["artist"], t["album_artist"], t["year"]) == ("5:20AM", "soldier", "soldier", "2024")
    assert "itself a version" in out.review["edits_rejected"][0]["why"]
    assert p["needs_review"] is True and any("derivative" in r for r in p["review_reasons"])
    assert out.review["flagged_tracks"] == [1]
    assert "原曲是 soldier《5:20AM》" in out.review["edits_applied"][0]["reason_zh"]
    # Even an approving reply cannot clear a derivative.
    endpoint.replies.append(reply("approved"))
    out2 = run(path, endpoint)
    assert out2.needs_review is True


def test_replan_runs_once(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    path = write_plan(wd, comp_plan(wd))
    calls = []

    def fake_replan(plan, req):
        calls.append(req)
        new = copy.deepcopy(plan)
        new["review_reasons"] = []
        new["needs_review"] = False
        new["tracks"][0]["title"] = "爱在西元前"
        return new

    endpoint.replies.append(reply("edited", [{"op": "replan", "album_hint": "周杰伦 - 范特西 (2001)"},
                                             {"op": "relabel_track", "track": 1, "title": "简单爱"}]))
    endpoint.replies.append(reply("edited", [{"op": "replan", "album_hint": "again"}]))
    out = run(path, endpoint, replan=fake_replan)
    assert calls == [{"album_hint": "周杰伦 - 范特西 (2001)"}]
    rv = out.review
    assert rv["replan"]["request"] == {"album_hint": "周杰伦 - 范特西 (2001)"}
    assert rv["replan"]["first_review"]["edits_skipped"][0]["why"] == "superseded by the re-plan"
    assert "not available" in rv["edits_rejected"][0]["why"]     # second round may not re-plan
    assert rv["timing"]["calls"] == 2
    assert out.needs_review is True


def test_invalid_reply_retried_once_with_errors(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    path = write_plan(wd, comp_plan(wd))
    endpoint.replies.append({"content": "Sure! Here is my review: looks fine"})
    endpoint.replies.append(reply())
    out = run(path, endpoint)
    assert out.review["error"] is None and out.review["timing"]["calls"] == 2
    second = endpoint.requests[1]["body"]["messages"]
    assert "rejected" in second[-1]["content"] and "not valid JSON" in second[-1]["content"]


def test_schema_invalid_twice_keeps_plan(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    plan = comp_plan(wd)
    path = write_plan(wd, plan)
    bad = {"content": {"verdict": "great", "confidence": 2, "edits": [{"op": "rm -rf"}]}}
    endpoint.replies += [bad, bad]
    out = run(path, endpoint)
    assert out.review["error"] and "invalid reply" in out.review["error"]
    assert out.plan["tracks"] == plan["tracks"]
    assert out.needs_review is True


def test_endpoint_errors_are_scrubbed_and_keep_plan(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    plan = comp_plan(wd)
    path = write_plan(wd, plan)
    endpoint.replies.append({"status": 401, "raw": '{"error": "bad token {AUTH}"}'})
    out = run(path, endpoint)
    assert out.review["error"] and "401" in out.review["error"]
    assert FAKE_KEY not in path.read_text(encoding="utf-8") and FAKE_KEY not in out.review["error"]
    assert out.plan["tracks"] == plan["tracks"] and out.needs_review is True


def test_timeout_and_endpoint_down(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    path = write_plan(wd, comp_plan(wd))
    endpoint.replies.append({"sleep": 1.5, **reply()})
    out = run(path, endpoint, timeout=0.5)
    assert out.review["error"] and out.needs_review is True
    down = ChatClient("http://127.0.0.1:9/v1", "m", FAKE_KEY, timeout=2)
    out2 = review_plan(path, client=down, catalog=Catalog(Http(None, offline=True), providers=[FakeProvider()]),
                       text_loader=None, dry_run=True)
    assert out2.review["error"] and out2.out_path is None


def test_dry_run_writes_nothing(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    path = write_plan(wd, comp_plan(wd))
    before = path.read_text(encoding="utf-8")
    endpoint.replies.append(reply())
    out = run(path, endpoint, dry_run=True)
    assert path.read_text(encoding="utf-8") == before and not (wd / "plan.unreviewed.json").exists()
    assert out.review["verdict"] == "approved"


def test_flagged_tracks_from_concerns_follow_renumbering(tmp_path, endpoint):
    wd = make_workdir(tmp_path, candidates=[(500.0, 0.95, "silence")],
                      recognitions=[(510.0, "简单爱", "周杰伦", 40.0)])
    plan = comp_plan(wd)
    plan["review_reasons"] = []
    plan["tracks"][0]["catalog"] = {"provider": "", "id": ""}
    path = write_plan(wd, plan)
    # Drop track 1 (no catalog id), then the model worries about "第 3 首" (original numbering = 简单爱).
    endpoint.replies.append(reply("needs_human", [{"op": "drop_track", "track": 1, "merge": "next"}],
                                  concerns=("第 3 首的结尾需要试听", "《爸，我回来了》的起点也请确认")))
    out = run(path, endpoint)
    assert [t["title"] for t in out.plan["tracks"]] == ["爸，我回来了", "简单爱"]
    assert out.review["flagged_tracks"] == [1, 2]


def test_low_confidence_approval_stays_flagged(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    path = write_plan(wd, comp_plan(wd))
    endpoint.replies.append(reply(conf=0.3))
    assert run(path, endpoint).needs_review is True


def test_structural_problems():
    p = {"kind": "compilation", "source": {"duration": 100},
         "tracks": [track(1, 0, 40, "a", "x", "al", "x"), track(2, 45, 100, "", "x", "al", "x")]}
    probs = structural_problems(p)
    assert any("not contiguous" in x for x in probs) and any("missing title" in x for x in probs)


def test_parse_reply_shapes():
    ok, errs = parse_reply('```json\n{"verdict":"approved","confidence":0.5,"edits":[],"reasons":[]}\n```')
    assert ok and not errs
    _, errs = parse_reply('{"verdict":"edited","confidence":0.5,"edits":[{"op":"move_boundary","boundary":2}]}')
    assert errs and "'to' must be a number" in errs[0]


# ----------------------------------------------------------------------------- owner rules: Simplified, blind

def test_script_only_edits_are_dropped_and_verdict_is_approved(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    path = write_plan(wd, comp_plan(wd))
    endpoint.replies.append(reply("edited", [
        {"op": "relabel_track", "track": 1, "title": "愛在西元前", "artist": "周杰倫"},
        {"op": "relabel_track", "track": 2, "title": "爸，我回來了"},
        {"op": "propose_album", "artist": "周杰倫", "album": "範特西"},
        {"op": "replan", "album_hint": "周杰倫 - 範特西"},
    ]))
    out = run(path, endpoint)
    rv = out.review
    assert rv["verdict"] == "approved" and rv["edits_applied"] == [] and rv["edits_rejected"] == []
    assert "edits_skipped" not in rv and out.needs_review is False
    assert [t["title"] for t in out.plan["tracks"]] == ["爱在西元前", "爸，我回来了", "简单爱"]
    assert out.plan["tracks"][0]["artist"] == "周杰伦"


def test_real_correction_kept_script_change_dropped(tmp_path, endpoint):
    wd = make_workdir(tmp_path)
    plan = comp_plan(wd)
    plan["tracks"][2]["title"] = "简单 爱"
    path = write_plan(wd, plan)
    endpoint.replies.append(reply("edited", [
        {"op": "relabel_track", "track": 3, "title": "简单爱", "artist": "周杰倫"},   # title real, artist script-only
        {"op": "relabel_track", "track": 1, "artist": "周杰倫"},                     # script-only
    ]))
    out = run(path, endpoint)
    rv = out.review
    assert rv["verdict"] == "edited" and len(rv["edits_applied"]) == 1
    applied = rv["edits_applied"][0]
    assert applied["track"] == 3 and "artist" not in applied and applied["tracks"] == [3]
    assert applied["reason_zh"] == "第 3 首歌名「简单 爱」改为「简单爱」（依据：专辑曲目表）。"
    assert [t["artist"] for t in out.plan["tracks"]] == ["周杰伦"] * 3


def test_uploader_only_in_hinted_brief(tmp_path):
    wd = make_workdir(tmp_path)
    cat = Catalog(Http(None, offline=True), providers=[FakeProvider()])
    blind = single_plan(wd, text_hints=False)
    b = build_brief(blind, gather_context(blind, None, catalog=cat, text_loader=None))
    assert "uploader" not in b["video"] and "someone" not in json.dumps(b, ensure_ascii=False)
    assert b["video"]["title"] == blind["source"]["title"]
    hinted = single_plan(wd, text_hints=True)
    b = build_brief(hinted, gather_context(hinted, None, catalog=cat, text_loader=lambda p, h: None))
    assert b["video"]["uploader"] == "someone"


def test_prompt_states_owner_rules():
    assert "ALWAYS written in Simplified" in SYSTEM_PROMPT and "only effect is a change of script" in SYSTEM_PROMPT
    assert "original artist filled in" in SYSTEM_PROMPT
