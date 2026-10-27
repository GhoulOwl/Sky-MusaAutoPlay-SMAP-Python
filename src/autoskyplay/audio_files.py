"""Cancellable, atomic compression for oversized audio sources."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

import imageio_ffmpeg
import soundfile as sf

MAX_AUDIO_BYTES = 100 * 1024 * 1024
log = logging.getLogger(__name__)


def compress_audio(source, target, cancelled=lambda: False, progress=lambda value: None,
                   max_bytes=MAX_AUDIO_BYTES):
    """Keep the whole recording, lowering the bitrate only as required by its duration."""
    source, target = Path(source), Path(target)

    def check_cancelled():
        if cancelled():
            raise ValueError("音频压缩已取消")

    check_cancelled()
    try:
        info = sf.info(source)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError("无法读取音频，请重新获取歌曲或选择有效的音频文件") from exc
    if not .1 <= info.duration <= 86400:
        raise ValueError("音频时长无效")
    # Leave room for MP3 headers/padding, including long recordings near the size limit.
    budget = max_bytes * 8 * .9 / info.duration / 1000
    bitrate = next((rate for rate in (192, 160, 128, 112, 96, 80, 64, 56, 48, 40, 32, 24, 16, 8)
                    if rate <= budget), None)
    if bitrate is None:
        raise ValueError("音频过长，无法在保留完整时长的情况下压缩到大小限制内")
    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("音频超过大小限制，自动压缩完整音频：%.1f 秒，%s kbps", info.duration, bitrate)
    with tempfile.TemporaryDirectory(prefix=".compress-", dir=target.parent) as directory:
        encoded = Path(directory) / "audio.mp3"
        try:
            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except (OSError, RuntimeError) as exc:
            raise ValueError("音频压缩组件不可用，请重新安装应用") from exc
        command = [executable, "-nostdin", "-v", "error", "-y", "-i", str(source),
                   "-map", "0:a:0", "-vn", "-map_metadata", "-1", "-c:a", "libmp3lame",
                   "-b:a", f"{bitrate}k", "-ar", "22050" if bitrate < 32 else "44100",
                   "-ac", str(1 if bitrate < 64 else min(2, info.channels)), str(encoded)]
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=errors,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                while process.poll() is None:
                    check_cancelled()
                    size = encoded.stat().st_size if encoded.exists() else 0
                    progress(min(99, int(size * 800 / (bitrate * info.duration * 1000))))
                    time.sleep(.05)
                check_cancelled()
                if process.returncode:
                    errors.seek(max(0, errors.tell() - 2000))
                    log.warning("音频自动压缩失败：%s", errors.read().decode("utf-8", errors="replace"))
                    raise ValueError("音频自动压缩失败，请重新获取歌曲或选择其他音频文件")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        if not encoded.is_file() or not 0 < encoded.stat().st_size <= max_bytes:
            raise ValueError("音频压缩后的大小仍超出限制，请重新获取歌曲")
        compressed = sf.info(encoded)
        if abs(compressed.duration - info.duration) > .1:
            raise ValueError("音频压缩不完整，请重试")
        check_cancelled()
        encoded.replace(target)
    log.info("音频自动压缩完成：%.2f MB", target.stat().st_size / 1024**2)
    progress(100)
    return target
