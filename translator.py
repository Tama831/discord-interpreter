"""音声バイト列 → 翻訳済みテキスト (言語自動判定 ja↔en) 。

Gemini 2.5 Flash の audio input を使い、1 API call で
「言語判定 + 翻訳」を済ませる。Whisper API は使わない。
"""
from __future__ import annotations

import asyncio
import io
import logging
import wave
from dataclasses import dataclass

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Discord pycord sink が吐く PCM の規格
DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2
DISCORD_SAMPLE_WIDTH = 2  # 16-bit


SYSTEM_PROMPT = """あなたは Discord ボイスチャットの同時通訳アシスタントです。

入力音声に対して以下を出力してください:
1. 音声の言語を自動判定 (日本語 or 英語)
2. 日本語なら自然な英語に、英語なら自然な日本語に翻訳
3. 翻訳結果のみを 1〜2 行で出力 (前置き・説明・引用符は禁止)
4. 音声が短すぎる/聞き取れない/翻訳不能なら空文字列のみ返す
5. 雑音・笑い声・相槌のみなら空文字列のみ返す

出力は翻訳テキスト単体。"原文: ..." や "翻訳: ..." は付けない。"""


@dataclass
class TranslationResult:
    """翻訳結果。空文字列なら投稿しない。"""
    detected_lang: str  # "ja" / "en" / "unknown"
    original_text: str
    translated_text: str

    @property
    def empty(self) -> bool:
        return not self.translated_text.strip()


def pcm_to_wav_bytes(pcm: bytes) -> bytes:
    """生 PCM (48kHz/stereo/16bit) を WAV コンテナでくるむ。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(DISCORD_CHANNELS)
        wav.setsampwidth(DISCORD_SAMPLE_WIDTH)
        wav.setframerate(DISCORD_SAMPLE_RATE)
        wav.writeframes(pcm)
    return buf.getvalue()


class Translator:
    """Gemini への薄いラッパ。Bot 側からは translate(pcm) だけ呼べばよい。"""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def translate(self, pcm: bytes) -> TranslationResult:
        """PCM チャンク → 翻訳結果。

        失敗時は (空文字列のResult) を返してログに残す。
        """
        if len(pcm) < DISCORD_SAMPLE_RATE * DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH * 0.3:
            # 0.3 秒未満は無視 (相槌・雑音)
            return TranslationResult("unknown", "", "")
        wav_bytes = pcm_to_wav_bytes(pcm)
        try:
            # google-genai は同期 API なので executor で逃がす
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self._model,
                contents=[
                    types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
                    SYSTEM_PROMPT,
                ],
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=300,
                ),
            )
            text = (response.text or "").strip()
        except Exception as exc:
            logger.warning("Gemini 翻訳失敗: %s", exc)
            return TranslationResult("unknown", "", "")
        # 雑な言語判定 (出力テキストに ASCII の英単語が多ければ ja→en だった)
        ascii_ratio = (
            sum(1 for c in text if ord(c) < 128) / max(1, len(text))
        )
        detected = "ja" if ascii_ratio > 0.7 else "en"
        return TranslationResult(detected, "", text)
