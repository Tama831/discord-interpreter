"""音声チャンネル → 同名テキストチャンネルの対応付け。

想定サーバー構成: 1 カテゴリに `🔊voice-X` + `💬text-X` のような同名ペアが
置かれている形。emoji prefix と全角空白を取り除いた "正規化名" で一致する
テキスト ch を探す。
カテゴリ内優先 → 見つからなければカテゴリを跨いで探す → それでも無ければ
voice channel 自体の built-in chat にフォールバック。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

import discord

logger = logging.getLogger(__name__)

# 先頭の絵文字・記号・空白を剥がす正規表現
_PREFIX_RE = re.compile(
    r"^[\s\W_]+",  # 空白 + 非英数 + アンダースコア (絵文字含む)
    re.UNICODE,
)


def normalize_channel_name(name: str) -> str:
    """`🔊voice-A` も `💬voice-A` も `voice-a` に揃える。"""
    nfkc = unicodedata.normalize("NFKC", name)
    stripped = _PREFIX_RE.sub("", nfkc)
    return stripped.strip().lower()


def find_paired_text_channel(
    voice_channel: discord.VoiceChannel,
) -> Optional[discord.TextChannel]:
    """voice_channel と同名のテキストchを探す。

    優先順位:
    1. 同カテゴリ内のテキストch (正規化名一致)
    2. ギルド全体のテキストch (正規化名一致)
    3. voice channel の built-in text chat (chat in voice)
    """
    target = normalize_channel_name(voice_channel.name)
    guild = voice_channel.guild

    # 1. 同カテゴリ
    if voice_channel.category is not None:
        for ch in voice_channel.category.text_channels:
            if normalize_channel_name(ch.name) == target:
                return ch

    # 2. ギルド全体
    for ch in guild.text_channels:
        if normalize_channel_name(ch.name) == target:
            return ch

    # 3. voice channel 自体 (Discord は VoiceChannel.send をサポート)
    #    pycord では VoiceChannel が Messageable なのでそのまま使える
    logger.info(
        "voice=%s に対応する同名テキストchが無いので VC built-in chat を使う",
        voice_channel.name,
    )
    return voice_channel  # type: ignore[return-value]
