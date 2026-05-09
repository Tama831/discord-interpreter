# discord-interpreter

Discord ボイスチャンネルの発話を **日本語 ↔ 英語** で双方向リアルタイム翻訳し、
同名のテキストチャンネル (もしくは VC built-in chat) に投稿する on-demand bot。

> Real-time bidirectional Japanese ↔ English voice translator for Discord.
> Speak in Japanese, see English in chat. Speak in English, see Japanese in chat.
> Toggle on/off with slash commands. Cheap (~$0.006/min) thanks to Gemini Flash.

MIT licensed. PRs welcome.

## 特徴 / Features

- 🎙️ **音声 → 翻訳テキスト 1 API call**: Gemini 2.5 Flash の audio input で言語自動判定 + 翻訳をワンショット (Whisper API 不要)
- 🪙 **激安**: 約 **$0.006/分** (5 人 2 時間で ≒ ¥270)
- 🤖 **シェア型**: 1 VC = 1 通訳セッション。`/interpret on` で召喚、`/interpret off` で退室
- 🧭 **同名ペア検出**: `🔊voice-A` ↔ `💬voice-A` のような絵文字 prefix を正規化して照合
- 🛡️ **セキュリティ**: ギルド allowlist で許可外サーバー即退室、AllowedMentions.none() で `@everyone` 注入防止、日次予算超過で自動 OFF
- ⏱️ **発話単位の切出し**: user 別 PCM buffer + 0.8 秒無音検出で自然な区切り

## アーキテクチャ

```
[Discord VC] ─ voice-recv ─→ StreamingTranslatorSink
                              │ user 別 PCM buffer
                              │ 0.8 秒無音 or 8 秒上限で発話切出
                              ▼
                     Translator (Gemini Flash audio)
                              │ 言語自動判定 + ja↔en 翻訳
                              ▼
                  find_paired_text_channel()
                              │ 同名ペア / 同カテゴリ / VC chat
                              ▼
                     text channel.send(...)
```

| ファイル | 役割 |
|---|---|
| `bot.py` | エントリ、slash commands (`/interpret on/off/status`)、セッション管理、自動退室 |
| `translator_sink.py` | `voice_recv.AudioSink` 継承、user 別 PCM buffer + 無音検出 |
| `translator.py` | Gemini 2.5 Flash 呼び出しラッパ |
| `channel_mapper.py` | voice ↔ text の同名ペア検出 (絵文字正規化) |
| `config.py` | `.env` 読み込み |
| `dave_patch.py` | DAVE (E2E voice encryption) 復号 monkey-patch (詳細は「既知の制約」) |

## セットアップ

### 1. Discord 側

1. https://discord.com/developers/applications で **New Application** → 名前任意 → Create
2. 左メニュー **Bot**:
   - **Reset Token** で **Bot Token** をコピー (`.env` の `DISCORD_BOT_TOKEN` へ)
   - **Privileged Gateway Intents** で `SERVER MEMBERS INTENT` を ON → Save
3. 🔒 **Public Bot を OFF にする (推奨)** — このままだと誰でも招待 URL を作って自分のサーバーに入れてしまう。
   - 先に **Installation** タブの "Install Link" を **None** にして Save
   - その後 **Bot** タブで **Public Bot** を OFF にして Save
4. 左メニュー **OAuth2 → URL Generator**:
   - SCOPES: `bot` + `applications.commands`
   - BOT PERMISSIONS: View Channels / Send Messages / Embed Links / Connect / Speak / Use Voice Activity
5. 生成 URL を開いて自分のサーバーへ Authorize

### 2. サーバー側 (任意の Linux サーバーで)

```bash
git clone https://github.com/Tama831/discord-interpreter.git
cd discord-interpreter

# 依存
sudo apt-get install -y ffmpeg libopus0     # Debian/Ubuntu
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 設定
cp .env.example .env
# .env を編集:
#   DISCORD_BOT_TOKEN   ... 必須
#   GEMINI_API_KEY      ... 必須 (https://aistudio.google.com/app/apikey)
#   ALLOWED_GUILD_IDS   ... 推奨 (CSV: 招待許可するサーバー ID)
#   DISCORD_GUILD_ID    ... 任意 (slash commands を即時 sync したい場合)

# 起動
.venv/bin/python bot.py
```

`bot ready as YourBot#xxxx (guilds=N)` が出れば OK。

### 3. systemd 常駐化 (任意)

```bash
sudo cp systemd/discord-interpreter.service /etc/systemd/system/
sudoedit /etc/systemd/system/discord-interpreter.service  # User= と path を環境に合わせる
sudo systemctl daemon-reload
sudo systemctl enable --now discord-interpreter.service
journalctl -u discord-interpreter -f
```

## 使い方

| コマンド | 動作 |
|---|---|
| `/interpret on` | 自分が今いる VC に bot を召喚、翻訳開始 |
| `/interpret off` | その VC の翻訳を停止 (bot 退室) |
| `/interpret status` | 現在翻訳中の全 VC を表示 |

出力例:
```
🤖 通訳者が参加しました — voice-A の発話を翻訳してこのチャンネルに流します

🎙️ alice 🇯🇵 → 🇺🇸
> Good morning, the weather's nice today.

🎙️ bob 🇺🇸 → 🇯🇵
> こんにちは、今日もよろしくね。
```

## コスト

Gemini 2.5 Flash audio: 約 $0.0001/sec ≒ **$0.006/分**

| シナリオ | 推定コスト |
|---|---|
| 1 人 30 分 (発話半分) | $0.09 ≒ ¥14 |
| 5 人 2 時間 (発話半分) | $1.80 ≒ ¥270 |
| 月 4 回ミートアップ | ~¥1,100 |

`DAILY_BUDGET_USD` で日次上限を設定可能 (デフォルト $2)。上限到達で自動 OFF + チャンネル通知。

## 環境変数

| 名前 | 必須 | 既定 | 説明 |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | — | Discord Developer Portal で発行 |
| `GEMINI_API_KEY` | ✅ | — | https://aistudio.google.com/app/apikey で発行 |
| `ALLOWED_GUILD_IDS` | ⚠️ 推奨 | (DISCORD_GUILD_ID) | CSV のギルド ID。許可外への招待は即退室。空だと「全許可」モード |
| `DISCORD_GUILD_ID` | — | (全 server) | 指定するとそのギルドだけで slash commands を即時 sync |
| `GEMINI_MODEL` | — | `gemini-2.5-flash` | audio 対応モデル必須 |
| `CHUNK_MAX_SECONDS` | — | `8.0` | 1 チャンクの最大長 |
| `SILENCE_TIMEOUT_SECONDS` | — | `0.8` | 無音判定 |
| `DAILY_BUDGET_USD` | — | `2.0` | 日次予算上限 |
| `LOG_LEVEL` | — | `INFO` | |

## セキュリティ

このリポジトリは小規模 friend server / コミュニティでの利用を想定。デフォルト設定で以下を満たします:

- **Public Bot OFF** (Developer Portal 側で手動設定 — セットアップ手順 §1.3)
- **コード側 ギルド allowlist** (`ALLOWED_GUILD_IDS`) — 万が一 Public のままでも、許可外サーバーに招待されたら即退室
- **`@everyone` / `@here` / メンション無効化** — 翻訳テキストにそれらの文字列が紛れても Discord は通知に展開しない
- **日次コスト上限** — 暴走しても財布は守る
- **Token は `.env` のみ** (gitignore 済み、commit されない)
- **VAD 風 silent skip** — 無音は Gemini に送らない (コスト & プライバシー)

## 既知の制約

- **DAVE 復号は monkey-patch 依存**: Discord は 2026-03-02 に DAVE (E2E voice encryption) を全 VC に強制適用。`discord-ext-voice-recv` 本体はまだ DAVE 復号未対応のため、`dave_patch.py` で [PR #54](https://github.com/imayhaveborkedit/discord-ext-voice-recv/pull/54) 相当を runtime patch している。upstream merge 後は `dave_patch.py` を削除可能。
- **同時複数 VC 対応はしているが、bot 1 台で 5 VC 以上掛け持つと CPU 負荷が辛い** (小型 VPS 想定で 3 VC まで)
- **言語判定は出力 ASCII 比率での簡易判定** — 将来 Gemini 出力に lang マーカーを足して厳密化予定。韓国語・中国語等の他言語対応も同方針で対応可能 (現状は ja/en 専用)
- **笑い声 / 相槌 / 雑音は Gemini プロンプトで「空文字列を返せ」と指示済**だが、たまにすり抜ける

## トラブルシュート

| 症状 | 原因 / 対処 |
|---|---|
| `Improper token has been passed` | `.env` の `DISCORD_BOT_TOKEN` が空 or 古い |
| `PrivilegedIntentsRequired` | Developer Portal で SERVER MEMBERS INTENT を ON |
| `Could not find Opus library` | `sudo apt-get install libopus0` |
| VC に入れない | bot に Connect/Speak 権限なし → サーバーロール確認 |
| 翻訳が出ない (0 発話のまま) | `journalctl -u discord-interpreter` で `OpusError corrupted` を確認 → DAVE patch が効いてない可能性。`dave_patch.py` の有無と `davey` パッケージのインストール状況を確認 |
| WebSocket closed with 4017 | discord.py を 2.7.0 以上にアップグレード (`pip install -U "discord.py[voice]"`) |
| Slash commands が出てこない | Discord クライアント再起動 or `DISCORD_GUILD_ID` を設定して即時 sync |

## ライセンス

MIT — 自由に fork / 改変 / 商用利用 OK。

## 謝辞

- [discord.py](https://github.com/Rapptz/discord.py) — Discord ライブラリ本体 (DAVE 送信対応)
- [discord-ext-voice-recv](https://github.com/imayhaveborkedit/discord-ext-voice-recv) — voice receive 拡張
- [davey](https://pypi.org/project/davey/) — DAVE protocol Python 実装
- [Google Gemini](https://ai.google.dev/) — 音声理解 + 翻訳
- DAVE 復号 monkey-patch のロジックは [@rdphillips7 さんの PR #54](https://github.com/imayhaveborkedit/discord-ext-voice-recv/pull/54) を参考
