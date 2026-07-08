"""Sintesi vocale (TTS) per le chiamate di emergenza linphone.

Genera un WAV in formato telefonico (8 kHz, mono, PCM 16-bit) — l'unico che
linphone riproduce in modo intelligibile sulla chiamata SIP.

Motore primario: gTTS (voce Google, naturale) → richiede internet.
Se gTTS/pydub falliscono (es. rete assente), ritorna False: lo script di
chiamata ricade su espeak-ng offline (voce robotica ma sempre disponibile).
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Formato narrowband telefonico (G.711): la conversione a 8 kHz mono è ciò che
# rende l'audio comprensibile in chiamata.
_RATE = 8000
_CHANNELS = 1
_SAMPLE_WIDTH = 2  # 16-bit


def synthesize_wav(message: str, out_path: str | Path, *, lang: str = "it",
                   uid: int | None = None, gid: int | None = None) -> bool:
    """Genera `out_path` (WAV 8 kHz mono) dal testo `message` con gTTS.

    Ritorna True se il file è stato creato, False su qualsiasi errore
    (rete assente, dipendenze mancanti, ...). Best-effort, non solleva.
    """
    out_path = Path(out_path)
    try:
        from gtts import gTTS
        from pydub import AudioSegment
    except ImportError as exc:
        logger.warning("TTS gTTS/pydub non disponibili: %s", exc)
        return False

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_mp3 = tmp.name
        try:
            gTTS(text=message, lang=lang, slow=False).save(tmp_mp3)
            audio = (
                AudioSegment.from_mp3(tmp_mp3)
                .set_channels(_CHANNELS)
                .set_frame_rate(_RATE)
                .set_sample_width(_SAMPLE_WIDTH)
            )
            audio.export(str(out_path), format="wav", codec="pcm_s16le")
        finally:
            try:
                os.unlink(tmp_mp3)
            except OSError:
                pass

        if uid is not None and gid is not None:
            try:
                os.chown(out_path, uid, gid)
            except (PermissionError, OSError) as exc:
                logger.warning("chown WAV TTS fallito: %s", exc)
        logger.info("TTS gTTS generato: %s (%d byte)", out_path, out_path.stat().st_size)
        return True
    except Exception as exc:  # rete assente o errore gTTS: fallback a espeak
        logger.warning("TTS gTTS fallito (fallback espeak-ng): %s", exc)
        return False
