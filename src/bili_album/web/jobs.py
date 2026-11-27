"""The job queue behind the page: one job runs at a time, every change lands in ``job.json``.

Layout of the data directory: ``<data-dir>/<job-id>/job.json`` plus ``<job-id>/work/``,
the pipeline's work directory for that job.

Statuses: ``queued -> running -> done | needs_confirmation | duplicate | failed``.
``confirm`` and ``replace`` put a held job back in the queue (ahead of jobs submitted
later); ``retry`` re-queues a failed job with the same work directory; ``keep`` ends a
duplicate as done without import; ``discard`` and ``remove`` delete the job folder.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .runner import STAGES, Outcome, Runner, clean_text
from .urls import video_key

log = logging.getLogger(__name__)

STATUSES = ("queued", "running", "needs_confirmation", "duplicate", "failed", "done")
ACTIVE = ("queued", "running")
RESTART_MESSAGE = "服务重启时中断，可以再试一次。"
GENERIC_ERROR = "处理时出错了，可以再试一次。"
ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_FIELD = 300
MAX_DONE = 20


class JobError(Exception):
    """A request the job manager refuses: ``code`` is the HTTP status."""

    def __init__(self, code: int, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _new_job(url: str, now: float) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "url": url,
        "key": video_key(url),
        "status": "queued",
        "action": "start",
        "created": now,
        "started": None,
        "finished": None,
        "run_seconds": 0.0,
        "stages": {s: "pending" for s in STAGES},
        "message": "",
        "album": None,
        "tracks": [],
        "review": None,
        "result": None,
        "cover": "",
        "error": None,
        "kept": False,
        "edited": [],
        "confirm_error": "",
        "version": 0,
    }


class JobManager:
    def __init__(self, data_dir: Path, runner: Runner, *, start_worker: bool = True):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runner = runner
        self._secrets = [s for s in runner.secrets() if s]
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self.jobs: dict[str, dict] = {}
        self.queue: list[str] = []
        self._stop = False
        self._load()
        self._worker = None
        if start_worker:
            self._worker = threading.Thread(target=self._loop, name="bili-album-jobs", daemon=True)
            self._worker.start()

    # ------------------------------------------------------------------ persistence

    def job_dir(self, job_id: str) -> Path:
        return self.data_dir / job_id

    def workdir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "work"

    def _load(self) -> None:
        for d in sorted(self.data_dir.iterdir()):
            f = d / "job.json"
            if not (d.is_dir() and ID_RE.match(d.name) and f.is_file()):
                continue
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("skipping unreadable %s: %s", f, e)
                continue
            if job.get("id") != d.name or job.get("status") not in STATUSES:
                continue
            if job["status"] in ACTIVE:
                stage = next((s for s in STAGES if job["stages"].get(s) == "running"), None)
                if stage is None:
                    stage = next((s for s in STAGES if job["stages"].get(s) not in ("done",)), "plan")
                job["stages"][stage] = "failed"
                job["status"] = "failed"
                job["error"] = {"stage": stage, "message": RESTART_MESSAGE}
                job["message"] = RESTART_MESSAGE
                job["finished"] = time.time()
                self.jobs[job["id"]] = job
                self._save(job)
            else:
                self.jobs[job["id"]] = job

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for s in self._secrets:
                value = value.replace(s, "***")
            return value
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        return value

    def _save(self, job: dict) -> None:
        job["version"] = job.get("version", 0) + 1
        d = self.job_dir(job["id"])
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(self._scrub(job), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, d / "job.json")

    # ------------------------------------------------------------------ views

    def public(self, job: dict) -> dict:
        out = {k: copy.deepcopy(v) for k, v in job.items() if k not in ("cover", "key", "action")}
        out["has_cover"] = bool(job.get("cover"))
        if job["status"] == "queued":
            out["ahead"] = self._ahead(job["id"])
        return self._scrub(out)

    def _ahead(self, job_id: str) -> int:
        running = sum(1 for j in self.jobs.values() if j["status"] == "running")
        return running + (self.queue.index(job_id) if job_id in self.queue else 0)

    def list(self) -> list[dict]:
        with self._lock:
            active = [self.jobs[i] for i in self.queue]
            running = [j for j in self.jobs.values() if j["status"] == "running"]
            held = sorted((j for j in self.jobs.values() if j["status"] in ("needs_confirmation", "duplicate",
                                                                              "failed")),
                          key=lambda j: j.get("finished") or j["created"], reverse=True)
            done = sorted((j for j in self.jobs.values() if j["status"] == "done"),
                          key=lambda j: j.get("finished") or j["created"], reverse=True)[:MAX_DONE]
            return [self.public(j) for j in held + running + active + done]

    def get(self, job_id: str) -> dict:
        with self._lock:
            return self.public(self._job(job_id))

    def _job(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not ID_RE.match(job_id) or job_id not in self.jobs:
            raise JobError(404, "没有这个任务。")
        return self.jobs[job_id]

    def cover_path(self, job_id: str) -> Path | None:
        """The job's cover file, only if it resolves to a file inside the job's own folder."""
        with self._lock:
            job = self._job(job_id)
            rel = job.get("cover") or ""
        if not rel:
            return None
        base = self.workdir(job_id).resolve()
        try:
            p = (base / rel).resolve()
            p.relative_to(base)
        except (ValueError, OSError):
            return None
        return p if p.is_file() else None

    # ------------------------------------------------------------------ actions

    def submit(self, url: str) -> dict:
        key = video_key(url)
        with self._lock:
            for j in self.jobs.values():
                if j["status"] in ACTIVE and j["key"] == key:
                    raise JobError(409, "这个视频已经在处理了。", existing_id=j["id"])
            job = _new_job(url, time.time())
            while job["id"] in self.jobs:
                job["id"] = uuid.uuid4().hex[:12]
            self.jobs[job["id"]] = job
            self.queue.append(job["id"])
            self._save(job)
            self._wake.notify_all()
            return self.public(job)

    def _require(self, job: dict, *statuses: str) -> None:
        if job["status"] not in statuses:
            raise JobError(409, f"这个任务现在不能这样操作（{job['status']}）。")

    def confirm(self, job_id: str, edits: list[dict]) -> dict:
        with self._lock:
            job = self._job(job_id)
            self._require(job, "needs_confirmation")
            clean = self._clean_edits(job, edits)
            for e in clean:
                t = next(t for t in job["tracks"] if t["index"] == e["index"])
                if (t["title"], t["artist"]) != (e["title"], e["artist"]):
                    job["edited"].append({"index": e["index"], "title": e["title"], "artist": e["artist"]})
                t["title"], t["artist"] = e["title"], e["artist"]
            job["pending_edits"] = clean
            job["confirm_error"] = ""
            self._requeue(job, "confirm", front=True)
            return self.public(job)

    @staticmethod
    def _clean_edits(job: dict, edits: Any) -> list[dict]:
        if not isinstance(edits, list) or len(edits) > 500:
            raise JobError(400, "tracks 必须是列表。")
        known = {t["index"] for t in job["tracks"]}
        seen, out = set(), []
        for e in edits:
            if not isinstance(e, dict):
                raise JobError(400, "每首曲目都要有 index、title、artist。")
            idx, title, artist = e.get("index"), e.get("title"), e.get("artist")
            if not isinstance(idx, int) or isinstance(idx, bool) or idx not in known or idx in seen:
                raise JobError(400, f"曲目编号不对：{idx!r}")
            if not isinstance(title, str) or not isinstance(artist, str):
                raise JobError(400, "歌名和歌手必须是文字。")
            title, artist = clean_text(title), clean_text(artist)
            if not title or not artist:
                raise JobError(400, "歌名和歌手不能为空。")
            if len(title) > MAX_FIELD or len(artist) > MAX_FIELD or any(ord(c) < 32 for c in title + artist):
                raise JobError(400, f"第 {idx} 首的歌名或歌手太长或含有控制字符。")
            seen.add(idx)
            out.append({"index": idx, "title": title, "artist": artist})
        return out

    def replace(self, job_id: str) -> dict:
        with self._lock:
            job = self._job(job_id)
            self._require(job, "duplicate")
            self._requeue(job, "replace", front=True)
            return self.public(job)

    def retry(self, job_id: str) -> dict:
        with self._lock:
            job = self._job(job_id)
            self._require(job, "failed")
            job["stages"] = {s: "pending" for s in STAGES}
            job["error"] = None
            job["result"] = None
            self._requeue(job, "start", front=False)
            return self.public(job)

    def keep(self, job_id: str) -> dict:
        with self._lock:
            job = self._job(job_id)
            self._require(job, "duplicate")
        self.runner.keep(self.workdir(job_id))
        with self._lock:
            job["status"] = "done"
            job["kept"] = True
            job["finished"] = time.time()
            job["message"] = "保留了音乐库里原来的专辑。"
            self._save(job)
            return self.public(job)

    def discard(self, job_id: str) -> None:
        with self._lock:
            job = self._job(job_id)
            self._require(job, "needs_confirmation")
        self.runner.discard(self.workdir(job_id))
        self._delete(job_id)

    def remove(self, job_id: str) -> None:
        with self._lock:
            job = self._job(job_id)
            self._require(job, "queued", "failed")
            if job_id in self.queue:
                self.queue.remove(job_id)
        self._delete(job_id)

    def _delete(self, job_id: str) -> None:
        with self._lock:
            self.jobs.pop(job_id, None)
            d = self.job_dir(job_id).resolve()
            if d.parent == self.data_dir and ID_RE.match(d.name):
                shutil.rmtree(d, ignore_errors=True)

    def _requeue(self, job: dict, action: str, *, front: bool) -> None:
        job["status"] = "queued"
        job["action"] = action
        job["finished"] = None
        if front:
            # A continuation was submitted before anything still waiting.
            pos = 0
            for i, jid in enumerate(self.queue):
                if self.jobs[jid]["created"] < job["created"]:
                    pos = i + 1
            self.queue.insert(pos, job["id"])
        else:
            self.queue.append(job["id"])
        self._save(job)
        self._wake.notify_all()

    # ------------------------------------------------------------------ worker

    def stop(self) -> None:
        with self._lock:
            self._stop = True
            self._wake.notify_all()

    def run_next(self) -> bool:
        """Run the first queued job to its end in this thread. Returns False if the queue is empty."""
        with self._lock:
            if not self.queue:
                return False
            job = self.jobs[self.queue.pop(0)]
            job["status"] = "running"
            job["started"] = time.time()
            if job["action"] == "start":
                job["message"] = ""
            self._save(job)
        self._run(job)
        return True

    def _loop(self) -> None:
        while True:
            with self._lock:
                while not self.queue and not self._stop:
                    self._wake.wait()
                if self._stop:
                    return
            self.run_next()

    def _emit(self, job: dict, **upd: Any) -> None:
        with self._lock:
            if job["id"] not in self.jobs:
                return
            stage, state = upd.get("stage"), upd.get("state")
            if stage in STAGES and state in ("running", "done", "held", "failed"):
                job["stages"][stage] = state
                if state == "running":
                    for s in STAGES[:STAGES.index(stage)]:
                        if job["stages"][s] in ("pending", "held"):
                            job["stages"][s] = "done"
            if isinstance(upd.get("message"), str):
                job["message"] = upd["message"][:500]
            if isinstance(upd.get("album"), dict):
                job["album"] = {k: str(upd["album"].get(k) or "")[:MAX_FIELD]
                                for k in ("title", "artist", "year", "kind", "uploader", "album_title",
                                          "album_artist")}
            if isinstance(upd.get("tracks"), list):
                job["tracks"] = [{"index": int(t.get("index", i)), "title": str(t.get("title", "")),
                                  "artist": str(t.get("artist", "")), "duration": float(t.get("duration") or 0),
                                  "flagged": bool(t.get("flagged"))}
                                 for i, t in enumerate(upd["tracks"], 1) if isinstance(t, dict)]
            if isinstance(upd.get("review"), dict):
                job["review"] = upd["review"]
            if isinstance(upd.get("result"), dict):
                job["result"] = upd["result"]
            if isinstance(upd.get("cover"), str):
                job["cover"] = upd["cover"]
            self._save(job)

    def _run(self, job: dict) -> None:
        wd = self.workdir(job["id"])
        wd.mkdir(parents=True, exist_ok=True)

        def emit(**upd: Any) -> None:
            self._emit(job, **upd)

        t0 = time.monotonic()
        try:
            if job["action"] == "confirm":
                out = self.runner.confirm(wd, job.get("pending_edits") or [], emit)
            elif job["action"] == "replace":
                out = self.runner.replace(wd, emit)
            else:
                out = self.runner.start(job["url"], wd, emit)
        except Exception as e:  # the runner's own words are scrubbed before they are stored
            log.exception("job %s failed", job["id"])
            out = Outcome("failed", str(e)[:500] or GENERIC_ERROR)
        with self._lock:
            job["run_seconds"] = round(job.get("run_seconds", 0.0) + time.monotonic() - t0, 1)
            job.pop("pending_edits", None)
            if job["id"] not in self.jobs:
                return
            job["status"] = out.status
            job["finished"] = time.time()
            if out.status == "failed":
                stage = next((s for s in STAGES if job["stages"][s] in ("running", "failed")), None) or \
                    next((s for s in STAGES if job["stages"][s] != "done"), "import")
                job["stages"][stage] = "failed"
                msg = out.error or GENERIC_ERROR
                job["error"] = {"stage": stage, "message": msg}
                job["message"] = msg
            elif out.status == "needs_confirmation":
                # confirm() refused the edited plan: say why above the buttons, keep the edits
                job["confirm_error"] = out.error
                if not any(v == "held" for v in job["stages"].values()):
                    stage = next((s for s in STAGES if job["stages"][s] != "done"), "cut")
                    job["stages"][stage] = "held"
            elif out.status == "duplicate":
                job["stages"]["import"] = "held"
            elif out.status == "done":
                job["stages"] = {s: "done" for s in STAGES}
            self._save(job)
