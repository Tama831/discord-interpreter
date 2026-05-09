"""discord-ext-voice-recv (0.5.2a179) に DAVE 復号サポートを足すパッチ。

Discord は 2026-03-02 に DAVE (E2E voice encryption) を全 VC に強制適用。
discord.py 2.7.x は DAVE 対応済 (送信)、voice-recv 拡張は未対応 (受信時に opus
decoder が "corrupted stream" エラー or 雑音化)。

rdphillips7 さんの PR https://github.com/imayhaveborkedit/discord-ext-voice-recv/pull/54
を monkey patch として実装。upstream merge されたら本ファイル削除可。

import 順序が重要: bot.py が voice_recv を import する**前に**この apply() を呼ぶこと。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def apply() -> None:
    """voice_recv の OpusDecoder と PacketRouter を DAVE 対応に置換する。"""
    try:
        from davey import MediaType
    except ImportError:
        logger.warning("davey が無いので DAVE patch をスキップ (受信音声が崩れます)")
        return

    from discord.ext.voice_recv import opus as _opus
    from discord.ext.voice_recv.opus import VoiceData

    _orig_init = _opus.PacketDecoder.__init__
    _orig_decode_packet = _opus.PacketDecoder._decode_packet

    def _patched_init(self, router, ssrc):
        _orig_init(self, router, ssrc)
        # DAVE passthrough: 暗号化済み packet を decoder にそのまま渡してもらう
        try:
            vc = self.sink.voice_client
            if vc is not None and getattr(vc._connection, "dave_session", None) is not None:
                vc._connection.dave_session.set_passthrough_mode(True, 10)
                self._dave_vc = vc
            else:
                self._dave_vc = None
        except Exception:
            logger.exception("DAVE passthrough 設定失敗")
            self._dave_vc = None

    def _patched_process_packet(self, packet):  # type: ignore[no-untyped-def]
        pcm = None
        member = self._get_cached_member()
        if member is None:
            self._cached_id = self.sink.voice_client._get_id_from_ssrc(self.ssrc)
            member = self._get_cached_member()

        # DAVE 復号 (member 確定後でないと decrypt 用 user_id が取れない)
        vc = getattr(self, "_dave_vc", None)
        if (
            vc is not None
            and member is not None
            and not packet.is_silence()
            and packet.decrypted_data is not None
            and vc._connection.dave_session is not None
            and vc._connection.dave_session.ready
        ):
            try:
                packet.decrypted_data = vc._connection.dave_session.decrypt(
                    member.id, MediaType.audio, bytes(packet.decrypted_data)
                )
            except Exception:
                self._last_seq = packet.sequence
                self._last_ts = packet.timestamp
                return VoiceData(packet, None, pcm=b"")

        if not self.sink.wants_opus():
            packet, pcm = self._decode_packet(packet)

        data = VoiceData(packet, member, pcm=pcm)
        self._last_seq = packet.sequence
        self._last_ts = packet.timestamp
        return data

    def _patched_decode_packet(self, packet):  # type: ignore[no-untyped-def]
        if packet:
            try:
                pcm = self._decoder.decode(packet.decrypted_data, fec=False)
            except Exception:
                # corrupted packet → silent frame で埋める
                pcm = self._decoder.decode(None, fec=False)
            return packet, pcm
        return _orig_decode_packet(self, packet)

    _opus.PacketDecoder.__init__ = _patched_init
    _opus.PacketDecoder._process_packet = _patched_process_packet
    _opus.PacketDecoder._decode_packet = _patched_decode_packet

    # 注: PR #54 の router 修正 (data.source is None skip) は本実装では不要。
    # 我々の StreamingTranslatorSink.write() が `if user is None: return` で
    # 弾くので、router 側で同じことをしなくても害はない。

    logger.info("DAVE decryption patch applied to discord-ext-voice-recv")
