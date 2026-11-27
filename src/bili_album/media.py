"""ffprobe / ffmpeg helpers: probing, lossless cutting, cover handling."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

COPY_CODECS = {"aac", "alac"}
LOSSLESS_INPUT = {"flac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "wavpack", "ape", "tta"}


def probe(path: Path | str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def audio_info(path: Path | str) -> dict:
    """codec, bitrate_kbps, sample_rate, channels, duration, start_time of the first audio stream."""
    j = probe(path)
    st = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    if st is None:
        raise ValueError(f"{path}: no audio stream")
    fmt = j.get("format", {})
    dur = float(st.get("duration") or fmt.get("duration") or 0)
    br = st.get("bit_rate")
    try:
        br_k = int(br) // 1000 if br else 0
    except ValueError:
        br_k = 0
    if br_k < 8 and dur > 0 and fmt.get("size"):
        br_k = int(int(fmt["size"]) * 8 / dur / 1000)
    return {
        "codec": st.get("codec_name", ""),
        "profile": st.get("profile", ""),
        "bitrate_kbps": br_k,
        "sample_rate": int(st.get("sample_rate") or 0),
        "channels": int(st.get("channels") or 0),
        "duration": dur,
        "start_time": float(st.get("start_time") or 0.0),
    }


def prepare_offline_audio(src: Path, dst: Path) -> Path:
    """Offline mode: bring an arbitrary file into an audio-only .m4a without lossy re-encoding."""
    if dst.exists():
        return dst
    info = audio_info(src)
    tmp = dst.with_name(dst.stem + ".tmp.m4a")
    if info["codec"] in COPY_CODECS:
        codec = ["-c:a", "copy"]
    elif info["codec"] in LOSSLESS_INPUT:
        codec = ["-c:a", "alac"]
    else:
        raise ValueError(f"{src}: codec {info['codec']!r} cannot be cut losslessly into .m4a "
                         "(need AAC/ALAC, or a lossless source to encode as ALAC)")
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(src), "-map", "0:a:0", *codec,
                    "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", str(tmp)],
                   check=True)
    shutil.move(tmp, dst)
    return dst


def cut(src: Path, dst: Path, start: float, end: float, *, overwrite: bool = False) -> None:
    """Stream-copy ``[start, end)`` of the first audio stream into ``dst``.

    Input-side ``-ss`` lets the muxer write a positive edit list that skips the
    samples before ``start`` inside the first AAC frame, so the file starts at
    0 with no negative timestamps. ``-t`` is the duration, never an absolute end.
    """
    if end <= start:
        raise ValueError(f"empty range {start}-{end}")
    if dst.exists() and not overwrite:
        raise FileExistsError(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = audio_info(src)
    codec = ["-c:a", "copy"] if info["codec"] in COPY_CODECS else ["-c:a", "alac"]
    tmp = dst.with_name("." + dst.stem + ".part.m4a")
    tmp.unlink(missing_ok=True)
    args = ["ffmpeg", "-v", "error", "-nostdin", "-n"]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(src), "-t", f"{end - start:.3f}", "-map", "0:a:0", *codec, "-map_metadata", "-1",
             "-map_chapters", "-1", "-movflags", "+faststart", str(tmp)]
    subprocess.run(args, check=True)
    shutil.move(tmp, dst)


def square_cover(src: Path, dst: Path, size: int = 1200) -> Path:
    """Centre-crop to a square JPEG (used for the Bilibili cover fallback)."""
    from PIL import Image

    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        s = min(w, h)
        im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
        if s > size:
            im = im.resize((size, size), Image.LANCZOS)
        im.save(dst, "JPEG", quality=92)
    return dst


def image_kind(path: Path) -> str:
    head = path.read_bytes()[:12]
    if head.startswith(b"\x89PNG"):
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return ""
