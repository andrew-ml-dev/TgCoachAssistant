"""Голосовые: OGG из Telegram → PCM через ffmpeg → локальный faster-whisper на Pi (протокол Wyoming)."""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient

from .. import config

RATE, WIDTH, CHANNELS = 16000, 2, 1


class VoiceError(Exception):
    """Сообщение пригодно для показа пользователю."""


async def _to_pcm(data: bytes) -> bytes:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ac", str(CHANNELS),
            "-ar", str(RATE), "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise VoiceError("На сервере нет ffmpeg — голосовые недоступны.")
    pcm, _ = await proc.communicate(data)
    if proc.returncode or not pcm:
        raise VoiceError("Не удалось прочитать голосовое.")
    return pcm


async def _whisper(pcm: bytes) -> str:
    u = urlparse(config.WHISPER_URI)
    async with AsyncTcpClient(u.hostname, u.port) as client:
        await client.write_event(Transcribe(language="ru").event())
        await client.write_event(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
        step = RATE * WIDTH  # секунда звука на кусок
        for i in range(0, len(pcm), step):
            await client.write_event(AudioChunk(rate=RATE, width=WIDTH, channels=CHANNELS,
                                                audio=pcm[i:i + step]).event())
        await client.write_event(AudioStop().event())
        while True:
            event = await client.read_event()
            if event is None:
                raise VoiceError("Распознавание речи оборвалось, попробуйте ещё раз.")
            if Transcript.is_type(event.type):
                return Transcript.from_event(event).text.strip()


async def transcribe(data: bytes) -> str:
    pcm = await _to_pcm(data)
    try:
        return await asyncio.wait_for(_whisper(pcm), timeout=90)
    except (OSError, asyncio.TimeoutError):
        raise VoiceError("Распознавание речи на Pi не отвечает. Напишите текстом.")
