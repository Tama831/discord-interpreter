"""Discord Interpreter Bot (entry point).

スラッシュコマンド:
- /interpret on   ... 自分が今いる VC に bot を呼んで翻訳開始
- /interpret off  ... その VC の翻訳を停止 (bot 退室)
- /interpret status ... 全 VC の現在状態
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord.ext import commands

# === py-cord 2.7.2 voice gateway v8 IDENTIFY パッチ ===
# Discord voice gateway v8 (2024-11〜) は IDENTIFY に max_dave_protocol_version を要求するが、
# py-cord 2.7.2 はまだこのフィールドを送らないため WS code 4017 で蹴られる。
# master branch には fix 入っているが pip 経由で release 待ち。
import discord.gateway as _dg

_orig_voice_identify = _dg.DiscordVoiceWebSocket.identify


async def _patched_voice_identify(self):
    state = self._connection
    payload = {
        "op": self.IDENTIFY,
        "d": {
            "server_id": str(state.server_id),
            "user_id": str(state.user.id),
            "session_id": state.session_id,
            "token": state.token,
            "max_dave_protocol_version": 0,  # DAVE 無効、最低限 v8 を満たす
        },
    }
    await self.send_as_json(payload)


_dg.DiscordVoiceWebSocket.identify = _patched_voice_identify
# === end patch ===

from channel_mapper import find_paired_text_channel, normalize_channel_name
from config import Config, setup_logging
from translator import Translator, TranslationResult
from translator_sink import StreamingTranslatorSink

logger = logging.getLogger(__name__)


# ----- セッション状態 (VC 1 つ = セッション 1 つ) ---------------------------


@dataclass
class InterpreterSession:
    voice_channel: discord.VoiceChannel
    text_channel: discord.abc.Messageable
    voice_client: discord.VoiceClient
    sink: StreamingTranslatorSink
    started_at: float = field(default_factory=time.time)
    chunks_processed: int = 0
    estimated_cost_usd: float = 0.0


class InterpreterBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        intents.members = True  # ユーザー名取得用
        super().__init__(command_prefix="!", intents=intents)

        self.config = config
        self.translator = Translator(config.gemini_api_key, config.gemini_model)
        # VC channel_id -> session
        self.sessions: dict[int, InterpreterSession] = {}
        self._sessions_lock = asyncio.Lock()
        self._daily_cost_usd = 0.0
        self._daily_reset_at = time.time()

    async def on_ready(self) -> None:
        # pycord は create_group / slash_command デコレータで登録した command を
        # 起動時に auto-sync する (discord.py の bot.tree.sync() 相当は不要)。
        # guild_ids を渡しているので guild-scoped commands として即時反映。
        logger.info(
            "bot ready as %s (id=%s, guilds=%d)",
            self.user, self.user.id if self.user else "?", len(self.guilds),
        )
        for g in self.guilds:
            logger.info("  - guild: %s (id=%s)", g.name, g.id)

    # ----- セッション操作 -------------------------------------------------

    async def start_session(
        self,
        member: discord.Member,
    ) -> tuple[bool, str]:
        if not member.voice or not member.voice.channel:
            return False, "先に音声チャンネルに入ってから実行してください。"
        vc_channel = member.voice.channel
        if not isinstance(vc_channel, discord.VoiceChannel):
            return False, "音声チャンネル (Voice Channel) でのみ動作します。"

        async with self._sessions_lock:
            if vc_channel.id in self.sessions:
                return False, f"`{vc_channel.name}` は既に翻訳中です。"

            text_channel = find_paired_text_channel(vc_channel)
            if text_channel is None:
                return False, "対応するテキストチャンネルが見つかりませんでした。"

            # 過去 session の残骸 voice_client を強制 cleanup (handshake 失敗で leak することがある)
            existing_vc = vc_channel.guild.voice_client
            if existing_vc is not None:
                logger.warning(
                    "leaked voice_client を発見、強制 disconnect: %s", existing_vc
                )
                try:
                    await existing_vc.disconnect(force=True)
                except Exception:
                    logger.exception("leaked voice_client cleanup 失敗")
                await asyncio.sleep(0.5)

            # 接続 (timeout を明示) + handshake 安定待ち
            try:
                voice_client = await vc_channel.connect(timeout=20.0, reconnect=True)
            except Exception as exc:
                logger.exception("VC 接続失敗")
                return False, f"VC 接続に失敗しました: {exc}"

            # voice handshake が落ち着くまで少し待つ (4006 race 対策)
            for _ in range(20):
                if voice_client.is_connected():
                    break
                await asyncio.sleep(0.1)
            if not voice_client.is_connected():
                logger.error("voice_client が ready にならない")
                try:
                    await voice_client.disconnect(force=True)
                except Exception:
                    pass
                return False, "voice handshake が確立しませんでした。少し待って再試行してください。"

            loop = asyncio.get_running_loop()

            async def _handle_chunk(user_id: int, pcm: bytes) -> None:
                await self._on_chunk(vc_channel.id, user_id, pcm)

            sink = StreamingTranslatorSink(
                loop=loop,
                chunk_handler=_handle_chunk,
                silence_timeout=self.config.silence_timeout_seconds,
                max_chunk_seconds=self.config.chunk_max_seconds,
            )
            sink.start_watcher()

            def _on_recording_finished(_sink, *_args):
                # pycord が stop_recording 時に呼ぶ。ここでは何もしない
                # (実際のクリーンアップは end_session 側で同期的に実行済み)
                pass

            try:
                voice_client.start_recording(sink, _on_recording_finished, text_channel)
            except Exception as exc:
                logger.exception("start_recording 失敗、voice_client を片付け")
                await sink.stop_watcher()
                try:
                    await voice_client.disconnect(force=True)
                except Exception:
                    pass
                return False, f"録音開始に失敗しました: {exc}"

            self.sessions[vc_channel.id] = InterpreterSession(
                voice_channel=vc_channel,
                text_channel=text_channel,
                voice_client=voice_client,
                sink=sink,
            )

        await text_channel.send(
            f"🤖 **通訳者が参加しました** — `{vc_channel.name}` の発話を翻訳して"
            f"このチャンネルに流します (日本語↔英語、自動判定)。\n"
            f"停止: `/interpret off`"
        )
        return True, f"`{vc_channel.name}` で翻訳を開始しました → <#{text_channel.id}>"

    async def end_session(self, vc_channel_id: int) -> tuple[bool, str]:
        async with self._sessions_lock:
            session = self.sessions.pop(vc_channel_id, None)

        if not session:
            # session が無くても、guild に bot の voice_client が残ってたら強制 disconnect
            # (前回 /interpret on で start_recording が落ちた時の残骸を救出する用)
            channel = self.get_channel(vc_channel_id)
            guild = channel.guild if channel else None
            if guild and guild.voice_client is not None:
                logger.warning("session 無しだが voice_client 残骸を発見、disconnect")
                try:
                    await guild.voice_client.disconnect(force=True)
                except Exception:
                    logger.exception("残骸 disconnect 失敗")
                return True, "残っていた接続をクリーンアップしました。再度 `/interpret on` をどうぞ。"
            return False, "このチャンネルでは翻訳が動いていません。"

        try:
            session.voice_client.stop_recording()
        except Exception:
            logger.exception("stop_recording 失敗")
        await session.sink.stop_watcher()
        try:
            await session.voice_client.disconnect(force=True)
        except Exception:
            logger.exception("VC disconnect 失敗")

        duration_min = (time.time() - session.started_at) / 60
        try:
            await session.text_channel.send(
                f"🤖 **通訳終了** — {duration_min:.1f} 分 / "
                f"{session.chunks_processed} 発話 / "
                f"≈ ${session.estimated_cost_usd:.3f}"
            )
        except Exception:
            pass
        return True, f"`{session.voice_channel.name}` の翻訳を停止しました。"

    # ----- audio chunk → 翻訳 → 投稿 -------------------------------------

    async def _on_chunk(self, vc_channel_id: int, user_id: int, pcm: bytes) -> None:
        session = self.sessions.get(vc_channel_id)
        if session is None:
            return

        # 日次予算チェック
        self._maybe_reset_daily()
        if self._daily_cost_usd >= self.config.daily_budget_usd:
            logger.warning("日次予算超過、翻訳停止: vc=%s", session.voice_channel.name)
            await session.text_channel.send(
                f"⚠️ 日次予算 ${self.config.daily_budget_usd:.2f} に到達したので"
                f" `{session.voice_channel.name}` の翻訳を自動停止します。"
            )
            await self.end_session(vc_channel_id)
            return

        result: TranslationResult = await self.translator.translate(pcm)
        if result.empty:
            return

        # コスト見積 (Gemini 2.5 Flash audio: ~$0.0001 / sec ≒ $0.006/min)
        seconds = len(pcm) / (48000 * 2 * 2)
        cost = seconds * 0.0001
        session.chunks_processed += 1
        session.estimated_cost_usd += cost
        self._daily_cost_usd += cost

        # ユーザー情報
        guild = session.voice_channel.guild
        member = guild.get_member(user_id)
        name = member.display_name if member else f"user-{user_id}"
        flag = "🇯🇵" if result.detected_lang == "ja" else "🇺🇸"
        target_flag = "🇺🇸" if result.detected_lang == "ja" else "🇯🇵"

        try:
            await session.text_channel.send(
                f"🎙️ **{name}** {flag} → {target_flag}\n"
                f"> {result.translated_text}"
            )
        except discord.HTTPException:
            logger.exception("text channel 投稿失敗")

    def _maybe_reset_daily(self) -> None:
        now = time.time()
        if now - self._daily_reset_at > 86400:
            logger.info("日次コスト集計リセット (前日: $%.3f)", self._daily_cost_usd)
            self._daily_cost_usd = 0.0
            self._daily_reset_at = now


# ----- スラッシュコマンド定義 ------------------------------------------------


def register_commands(bot: InterpreterBot) -> None:
    guild_kwargs = {}
    if bot.config.guild_id:
        guild_kwargs["guild_ids"] = [bot.config.guild_id]

    interpret = bot.create_group(
        "interpret",
        "ボイスチャンネルの発話を日本語↔英語で翻訳",
        **guild_kwargs,
    )

    @interpret.command(name="on", description="今いる VC で翻訳を開始")
    async def _on(ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        ok, msg = await bot.start_session(ctx.author)
        await ctx.followup.send(msg, ephemeral=True)

    @interpret.command(name="off", description="今いる VC で翻訳を停止")
    async def _off(ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.followup.send("VC に入った状態で実行してください。", ephemeral=True)
            return
        ok, msg = await bot.end_session(ctx.author.voice.channel.id)
        await ctx.followup.send(msg, ephemeral=True)

    @interpret.command(name="status", description="現在翻訳中の VC 一覧")
    async def _status(ctx: discord.ApplicationContext):
        if not bot.sessions:
            await ctx.respond("今は翻訳セッションは動いていません。", ephemeral=True)
            return
        lines = []
        for s in bot.sessions.values():
            mins = (time.time() - s.started_at) / 60
            lines.append(
                f"- `{s.voice_channel.name}` → <#{s.text_channel.id}> "
                f"({mins:.1f}分 / {s.chunks_processed}発話 / ≈${s.estimated_cost_usd:.3f})"
            )
        await ctx.respond("**翻訳中のセッション**\n" + "\n".join(lines), ephemeral=True)


# ----- 自動退室 (VC が空になったら) ------------------------------------------


def register_voice_state_listener(bot: InterpreterBot) -> None:
    @bot.event
    async def on_voice_state_update(member, before, after):
        if member.bot:
            return
        # bot が居る VC で人間が 0 人になったら自動退室
        for vc_id, session in list(bot.sessions.items()):
            human_count = sum(1 for m in session.voice_channel.members if not m.bot)
            if human_count == 0:
                logger.info("VC が空になったので自動停止: %s", session.voice_channel.name)
                await bot.end_session(vc_id)


# ----- main -----------------------------------------------------------------


def main() -> None:
    config = Config.from_env()
    setup_logging(config.log_level)
    bot = InterpreterBot(config)
    register_commands(bot)
    register_voice_state_listener(bot)
    bot.run(config.discord_bot_token)


if __name__ == "__main__":
    main()
