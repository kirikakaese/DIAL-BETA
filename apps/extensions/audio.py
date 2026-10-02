"""Custom ringback tones: upload validation and conversion to Asterisk-friendly WAV.

Asterisk plays music-on-hold from plain files, so every upload is normalised to
8 kHz / mono / 16-bit PCM WAV (``ringback/processed/<extension id>/tone.wav``).
Conversion uses ``ffmpeg`` when it is on ``PATH``; without it only WAV input can
be handled (pure-Python downmix/resample via the ``wave`` module).
"""
from __future__ import annotations

import array
import io
import logging
import os
import shutil
import subprocess
import tempfile
import wave

from celery import shared_task
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.utils.translation import gettext_lazy as _

log = logging.getLogger("pet.extensions.audio")

MAX_RINGBACK_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {"wav", "mp3", "ogg", "flac"}
TARGET_RATE = 8000
FFMPEG_TIMEOUT = 120


class RingbackError(Exception):
    pass


# --------------------------------------------------------------------------- validation

def sniff_audio_format(head: bytes) -> str | None:
    """Return ``wav``/``mp3``/``ogg``/``flac`` from the first bytes of a file, or ``None``."""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and head[1] in (0xFB, 0xF3, 0xF2):
        return "mp3"
    return None


def file_extension(name: str) -> str:
    return os.path.splitext(name or "")[1].lower().lstrip(".")


def validate_ringback_upload(f) -> str:
    """Validate an uploaded ringback file (size, extension, magic bytes). Returns the detected format."""
    if f.size > MAX_RINGBACK_BYTES:
        raise ValidationError(_("The file is too large (max. 5 MB)."), code="too_large")
    ext = file_extension(f.name)
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(_("Unsupported file type. Please upload a WAV, MP3, OGG or FLAC file."),
                              code="bad_extension")
    f.seek(0)
    head = f.read(16)
    f.seek(0)
    fmt = sniff_audio_format(head)
    if fmt is None:
        raise ValidationError(_("This does not look like a valid audio file (WAV, MP3, OGG or FLAC)."),
                              code="bad_content")
    if fmt != ext:
        raise ValidationError(_("The file content does not match its .%(ext)s extension.") % {"ext": ext},
                              code="mismatch")
    return fmt


# --------------------------------------------------------------------------- conversion

def _ffmpeg_convert(data: bytes, suffix: str) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="pet-ringback-") as tmp:
        src = os.path.join(tmp, f"in.{suffix}")
        dst = os.path.join(tmp, "tone.wav")
        with open(src, "wb") as fh:
            fh.write(data)
        cmd = [ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-i", src,
               "-ac", "1", "-ar", str(TARGET_RATE), "-acodec", "pcm_s16le", "-f", "wav", dst]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            raise RingbackError("ffmpeg timed out")
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RingbackError(f"ffmpeg failed: {err[-1] if err else proc.returncode}")
        with open(dst, "rb") as fh:
            return fh.read()


def _samples_to_int16(frames: bytes, width: int) -> array.array:
    if width == 2:
        out = array.array("h")
        out.frombytes(frames)
        return out
    if width == 1:  # unsigned 8-bit
        return array.array("h", ((b - 128) << 8 for b in frames))
    if width == 4:
        src = array.array("i")
        src.frombytes(frames)
        return array.array("h", (s >> 16 for s in src))
    if width == 3:
        out = array.array("h")
        for i in range(0, len(frames) - 2, 3):
            out.append(int.from_bytes(frames[i:i + 3], "little", signed=True) >> 8)
        return out
    raise RingbackError(f"unsupported WAV sample width {width * 8} bit")


def _downmix(samples: array.array, channels: int) -> array.array:
    if channels == 1:
        return samples
    n = len(samples) // channels
    return array.array("h", (sum(samples[i * channels:(i + 1) * channels]) // channels for i in range(n)))


def _resample(samples: array.array, rate: int, target: int) -> array.array:
    if rate == target or not samples:
        return samples
    n_out = max(1, int(len(samples) * target / rate))
    out = array.array("h")
    step = rate / target
    last = len(samples) - 1
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        if j >= last:
            out.append(samples[last])
            continue
        frac = pos - j
        out.append(int(samples[j] * (1 - frac) + samples[j + 1] * frac))
    return out


def _wave_convert(data: bytes) -> bytes:
    """Pure-Python fallback for PCM WAV input (any rate, 8/16/24/32 bit, any channel count)."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise RingbackError(f"cannot read WAV file: {exc}")
    if not frames:
        raise RingbackError("WAV file contains no audio")
    samples = _resample(_downmix(_samples_to_int16(frames, width), channels), rate, TARGET_RATE)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(TARGET_RATE)
        out.writeframes(samples.tobytes())
    return buf.getvalue()


def convert_to_asterisk_wav(data: bytes, filename: str = "") -> bytes:
    """Convert audio bytes to 8 kHz mono 16-bit PCM WAV. Raises ``RingbackError``."""
    fmt = sniff_audio_format(data[:16]) or file_extension(filename)
    if shutil.which("ffmpeg"):
        return _ffmpeg_convert(data, fmt or "bin")
    if fmt == "wav":
        return _wave_convert(data)
    raise RingbackError("ffmpeg is not installed on the server; only WAV files can be processed. "
                        "Please upload a WAV file or ask the orga team to install ffmpeg.")


# --------------------------------------------------------------------------- task

@shared_task
def process_ringback_tone(extension_id: str):
    """Convert ``ext.ringback_tone`` into ``ext.ringback_tone_processed`` and update the status."""
    from .models import Extension

    try:
        ext = Extension.objects.select_related("event").get(pk=extension_id)
    except Extension.DoesNotExist:
        return
    if not ext.ringback_tone:
        ext.ringback_tone_status = Extension.RingbackStatus.NONE
        ext.ringback_tone_error = ""
        ext.save(update_fields=["ringback_tone_status", "ringback_tone_error", "updated_at"])
        return
    try:
        with ext.ringback_tone.open("rb") as fh:
            data = fh.read()
        wav = convert_to_asterisk_wav(data, ext.ringback_tone.name)
        if ext.ringback_tone_processed:
            ext.ringback_tone_processed.delete(save=False)
        ext.ringback_tone_processed.save("tone.wav", ContentFile(wav), save=False)
        ext.ringback_tone_status = Extension.RingbackStatus.READY
        ext.ringback_tone_error = ""
    except Exception as exc:  # noqa: BLE001 - surface the reason to the user instead of crashing
        log.warning("ringback processing failed for %s: %s", ext, exc)
        ext.ringback_tone_status = Extension.RingbackStatus.FAILED
        ext.ringback_tone_error = str(exc)[:300]
    ext.save(update_fields=["ringback_tone_processed", "ringback_tone_status", "ringback_tone_error",
                            "updated_at"])
    if ext.ringback_tone_status == Extension.RingbackStatus.READY and ext.is_active:
        from .tasks import provision_extension

        provision_extension.delay(str(ext.pk))
    return ext.ringback_tone_status
