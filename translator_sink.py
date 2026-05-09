"""pycord の Sink を継承し、ユーザーごとに PCM をバッファリングして
"無音 N 秒" で発話単位に切り出すカスタム sink。

Discord の audio packet は 20ms 単位で来るが、pycord は デコード済 PCM
(48kHz/stereo/16bit) を Sink.write(data, user) 経由で同期スレッドから渡してくる。

- write() は pycord の voice receiver スレッドから呼ばれる (asyncio ループ外)
- なのでバッファ操作は lock で保護
- 無音検出と Gemini 呼び出しは asyncio タスクで非同期に実行
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import defaultdict
from typing import Awaitable, Callable

import discord
from discord.sinks import Sink

logger = logging.getLogger(__name__)


# user_id, pcm_bytes を受け取って投稿まで責任を持つコルーチン
ChunkHandler = Callable[[int, bytes], Awaitable[None]]


class StreamingTranslatorSink(Sink):
    """発話単位で PCM チャンクを切り出すリアルタイム sink。"""

    # discord.sinks.Sink は filters に container_type を要求しない
    # WaveSink 等のファイル sink と違って、自分で write() を捌く

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

    # --- pycord Sink overrides --------------------------------------------

    def write(self, data: bytes, user: int) -> None:  # noqa: D401 - sink contract
        """voice receiver スレッドから 20ms PCM が届く。バッファに足すだけ。"""
        if self._closed or not user:
            return
        with self._lock:
            buf = self._buffers[user]
            buf.extend(data)
            self._last_write[user] = time.monotonic()
            if len(buf) >= self._max_chunk_bytes:
                # 上限到達 → スレッドセーフに即フラッシュ
                pcm = bytes(buf)
                buf.clear()
                self._dispatch(user, pcm)

    def cleanup(self) -> None:  # pycord が stop_recording 時に呼ぶ
        # 同期的なので flush は次の機会に任せる
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
                            # last_write はクリアしておく (再発話時に再記録)
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
