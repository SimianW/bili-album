"""One call for the whole job: plan -> review -> cut -> import.

Built for a web server that runs one job at a time in a worker thread: no
global state, no ``sys.exit``, every exception becomes a ``failed`` result with
a message. After every stage the job state is written to ``<workdir>/job.json``
so a restarted server can list finished jobs and continue held ones.

API::

    ingest(url, workdir, on_event=None, *, on_unsure="hold", review=True, do_import=True,
           replace=False, config=None, plan_options=None) -> IngestResult
    confirm(job, on_event=None, *, replace=False, config=None) -> IngestResult
    discard(job) -> IngestResult
    load_job(workdir_or_job_json) -> IngestResult
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Config, load_config
from .messages import Z, explain, hold_message, import_reason, problems_zh, rejected_reason_zh, review_error

log = logging.getLogger(__name__)

STATE_FILE = "job.json"
STAGES = ("plan", "review", "cut", "import")
# Job statuses.
RUNNING = "running"
NEEDS_CONFIRMATION = "needs_confirmation"   # held after review; confirm() or discard()
DONE = "done"                               # cut, not imported (do_import=False)
IMPORTED = "imported"
IMPORTED_UNCONFIRMED = "imported_unconfirmed"
DUPLICATE = "duplicate"                     # library already has the album; confirm(replace=True) replaces
FAILED = "failed"
DISCARDED = "discarded"

EventCallback = Callable[[dict], None]


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


@dataclass
class IngestResult:
    job_id: str
    url: str
    workdir: str
    status: str = RUNNING
    stage: str = ""                  # last stage started
    options: dict = field(default_factory=dict)
    plan_path: str = ""
    kind: str = ""
    album: dict = field(default_factory=dict)       # title, artist, year, genre, cover_path, track_total
    tracks: list[dict] = field(default_factory=list)  # index, start, end, title, artist, album, confidence, flagged
    needs_review: bool | None = None
    flagged_tracks: list[int] = field(default_factory=list)   # track indexes to look at (= review.flagged_tracks)
    reasons: list[str] = field(default_factory=list)  # plan.review_reasons (tool language, English)
    review: dict | None = None       # see read_plan(): verdict, reasons / remaining_concerns (Chinese), edits with
                                     # reason_zh and tracks, flagged_tracks, tool_notes (Chinese), error
    derivative: dict | None = None
    forced_import: bool = False      # imported although needs_review was true (on_unsure="import")
    confirmed: bool = False          # continued by confirm()
    output_dir: str = ""             # cut files (work directory staging folder)
    output_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    import_result: dict | None = None
    error: str = ""                  # plain Chinese: why it stopped / why confirm refused ("" when fine)
    error_code: str = ""             # stable key, see messages.Z (video_not_found, plan_problems, ...)
    error_detail: str = ""           # technical detail for logs (exception class and text), never shown as is
    message: str = ""                # last Chinese status line
    events: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def state_path(self) -> Path:
        return Path(self.workdir) / STATE_FILE

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "IngestResult":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def load_job(path: Path | str) -> IngestResult:
    p = Path(path)
    if p.is_dir():
        p = p / STATE_FILE
    return IngestResult.from_dict(json.loads(p.read_text(encoding="utf-8")))


def save_job(job: IngestResult) -> None:
    job.updated_at = _now()
    p = job.state_path
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


# ----------------------------------------------------------------------------- steps (injectable)

@dataclass
class Steps:
    """The four stages. Defaults call the real implementations; tests pass fakes."""
    plan: Callable[..., Path] | None = None          # (url, workdir, plan_options) -> plan path
    review: Callable[..., Any] | None = None         # (plan_path, config) -> ReviewOutcome
    cut: Callable[..., Any] | None = None            # (plan_path, outdir) -> ApplyResult
    import_: Callable[..., Any] | None = None        # (source, cover, replace, config) -> ImportResult


def _plan_default(url: str, workdir: Path, opts: dict) -> Path:
    from .plan import PlanOptions, make_plan
    po = PlanOptions(workdir=workdir, repo_root=workdir,
                     **{k: v for k, v in opts.items() if k in ("text_hints", "cookies", "album_hint", "tracklist",
                                                                "recognize")})
    return make_plan(url, po)


def _review_default(plan_path: Path, config: Config):
    from .review import review_plan
    return review_plan(plan_path, config=config)


def _cut_default(plan_path: Path, outdir: Path):
    from .apply import apply_plan
    return apply_plan(plan_path, outdir)


def _import_default(source: Path, cover: Path | None, replace: bool, config: Config):
    from .library import import_album
    return import_album(source, config=config, replace=replace, cover=cover)


# ----------------------------------------------------------------------------- runner

MSG = {
    ("plan", "started"): "正在下载并分析视频",
    ("review", "started"): "正在由模型复核方案",
    ("cut", "started"): "正在切分音频并写入标签",
    ("import", "started"): "正在导入 Jellyfin 音乐库",
}
KIND_ZH = {"single": "单曲", "compilation": "整张专辑", "multipart": "分P专辑"}


class _Run:
    def __init__(self, job: IngestResult, on_event: EventCallback | None, steps: Steps | None,
                 config: Config | None):
        self.job = job
        self.on_event = on_event
        s = steps or Steps()
        self.plan_fn = s.plan or _plan_default
        self.review_fn = s.review or _review_default
        self.cut_fn = s.cut or _cut_default
        self.import_fn = s.import_ or _import_default
        self._config = config

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = load_config()
        return self._config

    def emit(self, stage: str, status: str, message: str, elapsed: float | None = None, **data) -> None:
        ev = {"job_id": self.job.job_id, "stage": stage, "status": status, "message": message,
              "elapsed_s": None if elapsed is None else round(elapsed, 2), "at": _now()}
        if data:
            ev["data"] = data
        self.job.events.append(ev)
        self.job.message = message
        if status == "started":
            self.job.stage = stage
        if self.on_event:
            try:
                self.on_event(dict(ev))
            except Exception as e:  # the page's callback must not break the job
                log.warning("on_event callback failed: %s", e)
        save_job(self.job)

    def stage(self, name: str, fn: Callable[[], str]) -> bool:
        """Run one stage; return False (job failed) on any exception."""
        t0 = time.monotonic()
        self.emit(name, "started", MSG[(name, "started")])
        try:
            msg = fn()
        except BaseException as e:  # SystemExit from deep code included; KeyboardInterrupt re-raised
            if isinstance(e, KeyboardInterrupt):
                raise
            ex = explain(e, name)
            detail = self._config.scrub(ex.detail) if self._config is not None else ex.detail
            log.warning("stage %s failed: %s", name, detail)
            self.job.status = FAILED
            self.job.error = f"{_zh_stage(name)}失败：{ex.zh}"
            self.job.error_code = ex.code
            self.job.error_detail = detail[:1000]
            self.emit(name, "failed", self.job.error, time.monotonic() - t0, code=ex.code)
            return False
        self.emit(name, "finished", msg, time.monotonic() - t0)
        return True

    # -- stages --------------------------------------------------------------------

    def read_plan(self) -> dict:
        plan = json.loads(Path(self.job.plan_path).read_text(encoding="utf-8"))
        j = self.job
        al = plan.get("album") or {}
        j.kind = plan.get("kind", "")
        j.album = {k: al.get(k) for k in ("title", "artist", "year", "genre", "cover_path", "track_total")}
        j.tracks = [{k: t.get(k) for k in ("index", "start", "end", "title", "artist", "album", "album_artist",
                                           "track_number", "confidence")} for t in plan.get("tracks") or []]
        j.needs_review = bool(plan.get("needs_review"))
        j.reasons = list(plan.get("review_reasons") or [])
        j.derivative = plan.get("derivative")
        rv = plan.get("review")
        if rv:
            j.review = {k: rv.get(k) for k in ("model", "verdict", "confidence", "reasons", "remaining_concerns",
                                                "edits_applied", "edits_rejected", "edits_skipped", "tool_notes",
                                                "flagged_tracks", "error", "timing", "replan")
                        if k in rv}
            # Rejected edits: a fully Chinese reason (the plan's own text ends in the English "why").
            rej = [dict(r, reason_zh=rejected_reason_zh(r)) for r in rv.get("edits_rejected") or []]
            j.review["edits_rejected"] = rej
            if rv.get("tool_notes") is not None:
                skipped = [n for n in rv.get("tool_notes") or [] if not n.startswith("工具无法核实")]
                j.review["tool_notes"] = skipped + [r["reason_zh"] for r in rej]
            if rv.get("error"):
                j.review["error_zh"] = review_error(rv["error"]).zh
        flagged = set(rv.get("flagged_tracks") or []) if rv and j.needs_review else set()
        if j.needs_review and not rv:
            # Not reviewed by the model (review=False): fall back to the tool's own low-confidence marks.
            flagged = {t.get("index") for t in plan.get("tracks") or [] if float(t.get("confidence") or 0) < 0.6}
        j.flagged_tracks = sorted(i for i in flagged if isinstance(i, int))
        for t in j.tracks:
            t["flagged"] = t.get("index") in flagged
        return plan

    def do_plan(self, url: str) -> str:
        path = self.plan_fn(url, Path(self.job.workdir), self.job.options.get("plan_options") or {})
        self.job.plan_path = str(path)
        self.read_plan()
        j = self.job
        return f"分析完成：{KIND_ZH.get(j.kind, j.kind)}《{j.album.get('title') or '?'}》，共 {len(j.tracks)} 首"

    def do_review(self) -> str:
        out = self.review_fn(Path(self.job.plan_path), self.config)
        self.read_plan()
        rv = getattr(out, "review", None) or self.job.review or {}
        if rv.get("error"):
            return review_error(rv["error"]).zh
        v = rv.get("verdict")
        n = len(rv.get("edits_applied") or [])
        base = {"approved": "复核通过", "edited": f"复核完成，已修正 {n} 处",
                "needs_human": "模型认为需要人工确认"}.get(v, f"复核完成（{v}）")
        if self.job.needs_review and v != "needs_human":
            base += "，仍有待确认的问题"
        return base

    def do_cut(self) -> str:
        outdir = Path(self.job.workdir) / "out"
        if outdir.exists():
            shutil.rmtree(outdir)   # our own staging folder: start clean so stale files never get imported
        res = self.cut_fn(Path(self.job.plan_path), outdir)
        self.job.output_dir = str(res.folder or outdir)
        self.job.output_files = [str(f) for f in res.files]
        self.job.warnings = list(res.warnings)
        if res.warnings:
            raise RuntimeError("apply reported problems: " + "; ".join(res.warnings))
        return f"已生成 {len(res.files)} 个文件"

    def do_import(self, replace: bool) -> str:
        plan = json.loads(Path(self.job.plan_path).read_text(encoding="utf-8"))
        files = [Path(f) for f in self.job.output_files]
        source = Path(self.job.output_dir) if plan.get("kind") != "single" else files[0]
        cover = Path(plan["album"].get("cover_path") or "") if plan.get("kind") == "single" else None
        res = self.import_fn(source, cover if cover and cover.is_file() else None, replace, self.config)
        d = res.to_dict() if hasattr(res, "to_dict") else dict(res)
        self.job.import_result = d
        st = d.get("status")
        if d.get("warnings"):
            self.job.warnings = list(self.job.warnings) + list(d["warnings"])
        if st == "imported":
            self.job.status = IMPORTED
            msg = f"已导入 Jellyfin：《{d.get('album')}》{d.get('jellyfin_track_count')} 首"
            fields = {n.get("field") for n in d.get("normalized") or []}
            if fields & {"ArtistItems", "AlbumArtists"}:
                msg += "；已把 Jellyfin 里的繁体歌手名改回简体"
            elif fields:
                msg += "；已把 Jellyfin 里的繁体名称改回简体"
            if d.get("normalize_error") or d.get("normalize_verified") is False:
                msg += "（但 Jellyfin 里仍可能显示繁体名称，可在 Jellyfin 里手动修改）"
            return msg
        if st == "duplicate":
            self.job.status = DUPLICATE
            return (f"音乐库里已有《{d.get('album')}》（{len(d.get('existing') or [])} 个文件），没有覆盖。"
                    "如要替换，请选择替换后再导入。")
        if st == "imported_unconfirmed":
            self.job.status = IMPORTED_UNCONFIRMED
            self.job.error_code = d.get("reason_code") or "not_listed_in_time"
            self.job.error_detail = str(d.get("reason") or "")
            return import_reason(self.job.error_code)
        raise RuntimeError(f"import ended with status {st}: {d.get('reason')}")

    # -- flows ---------------------------------------------------------------------

    def after_review(self, on_unsure: str, do_import: bool, replace: bool, confirmed: bool) -> IngestResult:
        j = self.job
        if j.needs_review and not confirmed:
            if on_unsure == "hold":
                j.status = NEEDS_CONFIRMATION
                _code, j.message = hold_message(j.reasons, bool(j.derivative))
                save_job(j)
                return j
            j.forced_import = True
        if not self.stage("cut", self.do_cut):
            return j
        if not do_import:
            j.status = DONE
            j.message = f"已完成切分（未导入）：{j.output_dir}"
            save_job(j)
            return j
        self.stage("import", lambda: self.do_import(replace))
        save_job(j)
        return j


def ingest(url: str, workdir: Path | str, on_event: EventCallback | None = None, *, on_unsure: str = "hold",
           review: bool = True, do_import: bool = True, replace: bool = False, config: Config | None = None,
           plan_options: dict | None = None, steps: Steps | None = None, job_id: str | None = None) -> IngestResult:
    """Run plan -> review -> cut -> import for ``url`` in ``workdir``.

    ``on_unsure="hold"`` stops after review when the plan still needs review (status
    ``needs_confirmation``); ``"import"`` imports anyway and sets ``forced_import``.
    ``plan_options`` may carry ``text_hints``, ``recognize``, ``album_hint``, ``tracklist``, ``cookies``.
    Never raises (except KeyboardInterrupt); failures come back as status ``failed``."""
    if on_unsure not in ("hold", "import"):
        raise ValueError("on_unsure must be 'hold' or 'import'")
    wd = Path(workdir).resolve()
    job = IngestResult(job_id=job_id or uuid.uuid4().hex[:12], url=url, workdir=str(wd),
                       options={"on_unsure": on_unsure, "review": review, "do_import": do_import,
                                "replace": replace, "plan_options": _jsonable(plan_options or {})})
    r = _Run(job, on_event, steps, config)
    try:
        save_job(job)
        if not r.stage("plan", lambda: r.do_plan(url)):
            return job
        if review and not r.stage("review", r.do_review):
            return job
        return r.after_review(on_unsure, do_import, replace, confirmed=False)
    except Exception as e:  # anything outside a stage (disk full writing job.json, ...)
        _fail(job, e)
        return job


def confirm(job: IngestResult | Path | str, on_event: EventCallback | None = None, *, replace: bool = False,
            do_import: bool | None = None, config: Config | None = None, steps: Steps | None = None) -> IngestResult:
    """Continue a held job (``needs_confirmation``), importing the plan as it now stands (the
    caller may have edited titles / artists in ``plan_path``), or retry an import that ended as
    ``duplicate`` (with ``replace=True``) or ``imported_unconfirmed``."""
    j = job if isinstance(job, IngestResult) else load_job(job)
    if j.status not in (NEEDS_CONFIRMATION, DUPLICATE, IMPORTED_UNCONFIRMED, FAILED) or not j.plan_path:
        j.error, j.error_code = Z["bad_status"], "bad_status"
        j.error_detail = f"cannot continue a job with status {j.status}"
        return j
    r = _Run(j, on_event, steps, config)
    try:
        plan = r.read_plan()
        from .review import structural_problems
        problems = structural_problems(plan)
        if problems:
            j.error = Z["plan_problems"] + problems_zh(problems)
            j.error_code = "plan_problems"
            j.error_detail = "; ".join(problems)
            j.message = j.error
            save_job(j)
            return j
        j.confirmed = True
        j.error = j.error_code = j.error_detail = ""
        j.status = RUNNING
        imp = j.options.get("do_import", True) if do_import is None else do_import
        return r.after_review("import", imp, replace, confirmed=True)
    except Exception as e:
        _fail(j, e)
        return j


def discard(job: IngestResult | Path | str) -> IngestResult:
    """Drop a held job: mark it discarded and delete its cut files (the work directory stays)."""
    j = job if isinstance(job, IngestResult) else load_job(job)
    out = Path(j.workdir) / "out"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    j.status = DISCARDED
    j.message = "已放弃"
    save_job(j)
    return j


def _fail(job: IngestResult, e: BaseException) -> None:
    ex = explain(e)
    job.status, job.error, job.error_code, job.error_detail = FAILED, ex.zh, ex.code, ex.detail
    job.message = ex.zh
    try:
        save_job(job)
    except Exception:
        pass


def _zh_stage(name: str) -> str:
    return {"plan": "分析", "review": "复核", "cut": "切分", "import": "导入"}.get(name, name)


def _jsonable(d: dict) -> dict:
    return {k: (str(v) if isinstance(v, Path) else v) for k, v in d.items()}
