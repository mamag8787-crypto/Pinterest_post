import asyncio
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


def _mb(path: str | Path) -> float:
    return Path(path).stat().st_size / 1024 / 1024


async def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", errors="ignore"), stderr.decode("utf-8", errors="ignore")


async def transcode_for_pinterest(input_path: str) -> tuple[str, dict]:
    """Преобразует любое обычное видео в универсальный MP4 для Pinterest."""
    input_file = Path(input_path)
    if not input_file.exists():
        raise MediaError(f"Входной файл не найден: {input_path}")

    if shutil.which(FFMPEG_BIN) is None:
        raise MediaError("ffmpeg не найден в контейнере")

    fd, output_path = tempfile.mkstemp(prefix="pinterest_ready_", suffix=".mp4")
    os.close(fd)

    # Делаем чётные размеры кадра, H.264, yuv420p, AAC.
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i",
        str(input_file),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v",
        "libx264",
        "-preset",
        FFMPEG_PRESET,
        "-crf",
        str(FFMPEG_CRF),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        output_path,
    ]

    logger.info("Транскодирую видео под Pinterest: %s", input_file.name)
    rc, _stdout, stderr = await _run(cmd)
    if rc != 0:
        Path(output_path).unlink(missing_ok=True)
        tail = stderr[-2000:] if stderr else ""
        raise MediaError(f"ffmpeg завершился с ошибкой. {tail}")

    output_mb = _mb(output_path)
    input_mb = _mb(input_file)
    if output_mb > PINTEREST_MAX_UPLOAD_MB:
        Path(output_path).unlink(missing_ok=True)
        raise MediaError(
            f"После обработки видео получилось слишком большим: {output_mb:.1f} MB > {PINTEREST_MAX_UPLOAD_MB} MB"
        )

    info = {
        "input_mb": round(input_mb, 1),
        "output_mb": round(output_mb, 1),
    }

    # Быстрая техническая проверка через ffprobe, если он есть.
    if shutil.which(FFPROBE_BIN):
        probe_cmd = [
            FFPROBE_BIN,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,pix_fmt,width,height",
            "-of",
            "default=noprint_wrappers=1:nokey=0",
            output_path,
        ]
        probe_rc, probe_out, _probe_err = await _run(probe_cmd)
        if probe_rc == 0:
            info["probe"] = probe_out.strip()

    return output_path, info
