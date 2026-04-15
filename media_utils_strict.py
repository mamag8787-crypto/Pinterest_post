import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")
PINTEREST_MAX_UPLOAD_MB = int(os.getenv("PINTEREST_MAX_UPLOAD_MB", "180"))
FFMPEG_CRF = int(os.getenv("FFMPEG_CRF", "23"))
FFMPEG_PRESET = os.getenv("FFMPEG_PRESET", "medium")


class MediaError(RuntimeError):
    pass


async def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", errors="ignore"), stderr.decode("utf-8", errors="ignore")


def _mb(path: str | Path) -> float:
    return Path(path).stat().st_size / 1024 / 1024


async def _probe(path: str) -> dict:
    if shutil.which(FFPROBE_BIN) is None:
        return {}
    cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        path,
    ]
    rc, stdout, _stderr = await _run(cmd)
    if rc != 0 or not stdout.strip():
        return {}
    try:
        return json.loads(stdout)
    except Exception:
        return {}


async def transcode_for_pinterest(input_path: str) -> tuple[str, dict]:
    input_file = Path(input_path)
    if not input_file.exists():
        raise MediaError(f"Входной файл не найден: {input_path}")

    if shutil.which(FFMPEG_BIN) is None:
        raise MediaError("ffmpeg не найден в контейнере")

    fd, output_path = tempfile.mkstemp(prefix="pinterest_ready_", suffix=".mp4")
    os.close(fd)

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i", str(input_file),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-vf", "fps=30,scale='if(gt(iw,1080),1080,trunc(iw/2)*2)':'if(gt(iw,1080),-2,trunc(ih/2)*2)',format=yuv420p",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level", "4.1",
        "-pix_fmt", "yuv420p",
        "-preset", FFMPEG_PRESET,
        "-crf", str(FFMPEG_CRF),
        "-tag:v", "avc1",
        "-movflags", "+faststart",
        "-r", "30",
        "-vsync", "cfr",
        "-video_track_timescale", "90000",
        "-c:a", "aac",
        "-ac", "2",
        "-ar", "44100",
        "-b:a", "128k",
        "-max_muxing_queue_size", "1024",
        output_path,
    ]

    logger.info("Pinterest transcode input=%s", input_file)
    rc, _stdout, stderr = await _run(cmd)
    if rc != 0:
        Path(output_path).unlink(missing_ok=True)
        raise MediaError(f"ffmpeg завершился с ошибкой: {stderr[-2500:]}")

    out_mb = _mb(output_path)
    if out_mb > PINTEREST_MAX_UPLOAD_MB:
        Path(output_path).unlink(missing_ok=True)
        raise MediaError(
            f"После обработки видео слишком большое: {out_mb:.1f} MB > {PINTEREST_MAX_UPLOAD_MB} MB"
        )

    probe = await _probe(output_path)
    info = {
        "input_mb": round(_mb(input_file), 1),
        "output_mb": round(out_mb, 1),
        "output_path": output_path,
        "probe": probe,
    }

    streams = probe.get("streams", []) if isinstance(probe, dict) else []
    v0 = next((s for s in streams if s.get("codec_type") == "video"), {})
    codec_name = v0.get("codec_name")
    pix_fmt = v0.get("pix_fmt")
    if codec_name != "h264" or pix_fmt != "yuv420p":
        Path(output_path).unlink(missing_ok=True)
        raise MediaError(
            f"После ffmpeg получился невалидный видео-поток: codec={codec_name}, pix_fmt={pix_fmt}"
        )

    return output_path, info
