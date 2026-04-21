"""SBC decoder backed by ffmpeg as a streaming subprocess.

Why ffmpeg and not libsbc via ctypes? Homebrew doesn't package libsbc as a
standalone formula (the old ``brew install sbc`` was removed). ffmpeg, on the
other hand, is ubiquitous and ships an SBC decoder in its default build.

We spawn one long-lived ffmpeg process with stdin = SBC byte stream and
stdout = signed-16-bit LE PCM at 32 kHz mono. A background reader thread
drains stdout and invokes the supplied ``on_pcm`` callback. A second thread
drains stderr so the pipe doesn't fill up and deadlock. Latency in
practice is ~20 ms which is fine for radio monitor audio.

A later pass could swap the backend for an in-process decoder (either a
bundled libsbc built from source, or a Python port of the Dart SBC decoder
in the parent project). The callback interface stays the same, so that
replacement wouldn't ripple through the CLI or the audio pipeline.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from typing import Callable

log = logging.getLogger(__name__)


class SbcUnavailable(RuntimeError):
    pass


# ffmpeg defaults to ~1 second of input buffering and probes the stream for
# several hundred ms before starting to decode — fine for file playback, deadly
# for live radio audio. The flags below tell ffmpeg to treat the input as a
# live stream: no probe, no analyze, no demuxer buffer, flush output per packet.
FFMPEG_PIPE_ARGS = [
    "-loglevel", "error",
    "-hide_banner",
    "-nostdin",
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-probesize", "32",
    "-analyzeduration", "0",
    "-f", "sbc",
    "-i", "pipe:0",
    "-f", "s16le",
    "-ac", "1",
    "-ar", "32000",
    "-flush_packets", "1",
    "pipe:1",
]


class SbcStream:
    """Streaming SBC → PCM decoder.

    Feed SBC frame bytes to :meth:`feed`; receive PCM (signed 16-bit LE,
    32 kHz, mono) through the ``on_pcm`` callback passed at construction.

    Thread-safety: ``feed`` may be called from any single thread at a
    time. ``on_pcm`` is invoked on our internal reader thread, not the
    caller's thread.
    """

    def __init__(self, on_pcm: Callable[[bytes], None]) -> None:
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise SbcUnavailable(
                "ffmpeg not found on PATH. Install with: brew install ffmpeg"
            )
        self._on_pcm = on_pcm
        self._proc = subprocess.Popen(
            [ffmpeg_path, *FFMPEG_PIPE_ARGS],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._closed = False
        self._reader = threading.Thread(
            target=self._read_loop, name="sbc-stdout", daemon=True
        )
        self._err_reader = threading.Thread(
            target=self._err_loop, name="sbc-stderr", daemon=True
        )
        self._reader.start()
        self._err_reader.start()

    # ------------------------------------------------------------------ feed

    def feed(self, sbc_bytes: bytes) -> None:
        """Push SBC frame bytes into the decoder. Safe to call with multiple
        concatenated frames at once."""
        if self._closed or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(sbc_bytes)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            log.warning("ffmpeg stdin closed early: %r", exc)
            self._closed = True

    # ---------------------------------------------------------------- internal

    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        while True:
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                return
            try:
                self._on_pcm(chunk)
            except Exception:
                log.exception("on_pcm callback raised")

    def _err_loop(self) -> None:
        assert self._proc.stderr is not None
        for line in iter(self._proc.stderr.readline, b""):
            try:
                s = line.decode("utf-8", "replace").rstrip()
            except Exception:
                s = repr(line)
            if s:
                log.warning("ffmpeg: %s", s)

    # ------------------------------------------------------------------ close

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

    def __enter__(self) -> "SbcStream":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# Back-compat name for the earlier ctypes-based class. Not wired up
# anywhere now that we pivoted to ffmpeg, but kept so external code that
# imports ``Sbc`` doesn't break outright.
Sbc = SbcStream


class SbcEncodeStream:
    """Streaming PCM → SBC encoder. Symmetric to :class:`SbcStream`.

    Feed raw s16le PCM to :meth:`feed`; receive one 44-byte SBC frame at a
    time through the ``on_frame`` callback. Internally the same ffmpeg
    binary and low-latency flags; output is sliced into frames on the fly
    so downstream code doesn't have to resync on the 0x9C sync byte.

    Threading: ``on_frame`` is invoked on our internal stdout reader
    thread, not the caller's thread.
    """

    # For this radio's fixed codec config (32 kHz / 16 blocks / mono /
    # loudness / 8 subbands / bitpool 18) every frame is exactly 44 bytes.
    FRAME_LEN = 44

    def __init__(
        self,
        on_frame: Callable[[bytes], None],
        *,
        sample_rate: int = 32000,
        channels: int = 1,
        bitrate: str = "88k",
    ) -> None:
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise SbcUnavailable(
                "ffmpeg not found on PATH. Install with: brew install ffmpeg"
            )
        self._on_frame = on_frame
        self._proc = subprocess.Popen(
            [
                ffmpeg_path,
                "-loglevel", "error",
                "-hide_banner",
                "-nostdin",
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-probesize", "32",
                "-analyzeduration", "0",
                "-f", "s16le",
                "-ar", str(sample_rate),
                "-ac", str(channels),
                "-i", "pipe:0",
                "-c:a", "sbc",
                "-b:a", bitrate,
                "-f", "sbc",
                "-flush_packets", "1",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._buf = bytearray()
        self._closed = False
        self._reader = threading.Thread(
            target=self._read_loop, name="sbc-enc-stdout", daemon=True
        )
        self._err_reader = threading.Thread(
            target=self._err_loop, name="sbc-enc-stderr", daemon=True
        )
        self._reader.start()
        self._err_reader.start()

    def feed(self, pcm_bytes: bytes) -> None:
        if self._closed or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(pcm_bytes)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            log.warning("ffmpeg stdin closed early: %r", exc)
            self._closed = True

    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        while True:
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                # Drain anything left in the buffer as long as it's a
                # complete frame.
                while len(self._buf) >= self.FRAME_LEN:
                    self._emit_one()
                return
            self._buf.extend(chunk)
            while len(self._buf) >= self.FRAME_LEN:
                if self._buf[0] != 0x9C:
                    # Shouldn't happen — ffmpeg emits back-to-back frames —
                    # but resync just in case.
                    idx = self._buf.find(0x9C, 1)
                    if idx < 0:
                        self._buf.clear()
                        break
                    del self._buf[:idx]
                    continue
                self._emit_one()

    def _emit_one(self) -> None:
        frame = bytes(self._buf[: self.FRAME_LEN])
        del self._buf[: self.FRAME_LEN]
        try:
            self._on_frame(frame)
        except Exception:
            log.exception("on_frame callback raised")

    def _err_loop(self) -> None:
        assert self._proc.stderr is not None
        for line in iter(self._proc.stderr.readline, b""):
            try:
                s = line.decode("utf-8", "replace").rstrip()
            except Exception:
                s = repr(line)
            if s:
                log.warning("ffmpeg: %s", s)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

    def __enter__(self) -> "SbcEncodeStream":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def encode_pcm_to_sbc(
    pcm_bytes: bytes,
    *,
    sample_rate: int = 32000,
    channels: int = 1,
    bitrate: str = "88k",
) -> bytes:
    """One-shot synchronous PCM → SBC encode via ffmpeg.

    Input must be signed 16-bit little-endian interleaved PCM at
    ``sample_rate`` Hz with ``channels`` channels.

    Default parameters target the radio's codec config exactly:
    32 kHz mono at ~88 kbps ≈ bitpool 18, which combined with ffmpeg's
    SBC encoder defaults (16 blocks, 8 subbands, loudness allocation)
    produces the ``9C 71 12`` header the radio expects.

    Returns the raw back-to-back SBC frame stream. Caller is responsible
    for splitting into fixed-size frames and wrapping in the HDLC envelope.
    """
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise SbcUnavailable(
            "ffmpeg not found on PATH. Install with: brew install ffmpeg"
        )
    result = subprocess.run(
        [
            ffmpeg_path,
            "-loglevel", "error",
            "-hide_banner",
            "-nostdin",
            "-f", "s16le",
            "-ar", str(sample_rate),
            "-ac", str(channels),
            "-i", "pipe:0",
            "-c:a", "sbc",
            "-b:a", bitrate,
            "-f", "sbc",
            "pipe:1",
        ],
        input=pcm_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"ffmpeg SBC encode failed (exit {result.returncode}): {err}"
        )
    return result.stdout
