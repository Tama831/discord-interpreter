"""discord-ext-voice-recv の AudioSink を継承し、ユーザーごとに PCM を
バッファリングして "無音 N 秒" で発話単位に切り出すカスタム sink。

discord.py + voice_recv 拡張の場合:
- write(user, data) が voice receiver スレッドから呼ばれる
- data.pcm は デコード済 PCM (48kHz/stereo/16bit) の 20ms フレーム
- data.source は User (None の可能性あり)
- バッファ操作は lock で保護、無音検出と Gemini 呼び出しは asyncio で非同期実行
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import defaultdict
from typing import Awaitable, Callable, Optional

import discord
from discord.ext import voice_recv

logger = logging.getLogger(__name__)


# user_id, pcm_bytes を受け取って投稿まで責任を持つコルーチン
ChunkHandler = Callable[[int, bytes], Awaitable[None]]


class StreamingTranslatorSink(voice_recv.AudioSink):
    """発話単位で PCM チャンクを切り出すリアルタイム sink。"""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        chunk_handler: ChunkHandler,
        silence_timeout: float = 0.8,
        max_chunk_seconds: float = 8.0,
    ) -> None:
        super().__init__()
        self._loop = loop
        self._handler = chunk_handler
        self._silence_timeout = silence_timeout
        self._max_chunk_bytes = int(
            max_chunk_seconds * 48000 * 2 * 2  # samplerate * channels * width
        )
        self._buffers: dict[int, bytearray] = defaultdict(bytearray)
        self._last_write: dict[int, float] = {}
        self._lock = threading.Lock()
        self._watch_task: asyncio.Task | None = None
        self._closed = False

    def start_watcher(self) -> None:
        """asyncio コンテキストから呼んで無音検出ループを起動する。"""
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = self._loop.create_task(self._watch_silence())

    async def stop_watcher(self) -> None:
        self._closed = True
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        await self._flush_all()

    # --- voice_recv.AudioSink overrides -----------------------------------

    def wants_opus(self) -> bool:
        # PCM が欲しい (Gemini に WAV で渡すため)
        return False

    def write(self, user: Optional[discord.User], data: voice_recv.VoiceData) -> None:
        """voice receiver スレッドから 20ms PCM が届く。バッファに足すだけ。"""
        if self._closed or user is None or user.bot:
            return
        pcm = data.pcm
        if not pcm:
            return
        user_id = user.id
        with self._lock:
            buf = self._buffers[user_id]
            buf.extend(pcm)
            self._last_write[user_id] = time.monotonic()
            if len(buf) >= self._max_chunk_bytes:
                # 上限到達 → スレッドセーフに即フラッシュ
                pcm_chunk = bytes(buf)
                buf.clear()
                self._dispatch(user_id, pcm_chunk)

    def cleanup(self) -> None:
        # voice_recv が listen 停止時に呼ぶ。同期的なので flush は次の機会に。
        return

    # --- internal ----------------------------------------------------------

    def _dispatch(self, user_id: int, pcm: bytes) -> None:
        """ハンドラを asyncio ループ上で発火 (スレッドセーフ)。"""
        if not pcm:
            return
        asyncio.run_coroutine_threadsafe(
            self._safe_handle(user_id, pcm), self._loop
        )

    async def _safe_handle(self, user_id: int, pcm: bytes) -> None:
        try:
            await self._handler(user_id, pcm)
        except Exception:
            logger.exception("chunk handler 失敗 user=%s", user_id)

    async def _watch_silence(self) -> None:
        """N ms ごとに「最後の write から silence_timeout 経った user」をフラッシュ。"""
        try:
            while not self._closed:
                await asyncio.sleep(0.25)
                now = time.monotonic()
                to_flush: list[tuple[int, bytes]] = []
                with self._lock:
                    for user_id, last in list(self._last_write.items()):
                        if now - last >= self._silence_timeout:
                            buf = self._buffers.get(user_id)
                            if buf:
                                to_flush.append((user_id, bytes(buf)))
                                buf.clear()
                            self._last_write.pop(user_id, None)
                for user_id, pcm in to_flush:
                    self._dispatch(user_id, pcm)
        except asyncio.CancelledError:
            raise

    async def _flush_all(self) -> None:
        with self._lock:
            pending = [
                (uid, bytes(buf)) for uid, buf in self._buffers.items() if buf
            ]
            for buf in self._buffers.values():
                buf.clear()
            self._last_write.clear()
        for user_id, pcm in pending:
            await self._safe_handle(user_id, pcm)
