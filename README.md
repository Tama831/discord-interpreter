# Discord Interpreter Bot

Discord ボイスチャンネルの発話を **日本語 ↔ 英語** で双方向リアルタイム翻訳し、
同名のテキストチャンネル (もしくは VC built-in chat) に投稿する bot。

- 音声→翻訳テキスト1コール: **Gemini 2.5 Flash audio input** (Whisper API 不要)
- 必要な時だけ `/interpret on` で召喚、`/interpret off` で退室
- 同 VC の全員の発話を捌くシェア型 (個人ごとセッションは持たない)
- 同名ペア検出: `🔊voice-A` ↔ `💬voice-A` のような絵文字prefixを正規化して照合
- 5分無音 / VC 全員退出で自動退室
- 日次予算超過で自動 OFF (`DAILY_BUDGET_USD`)

---

## セットアップ手順

### 1. Discord 側 (たまさん作業)

1. https://discord.com/developers/applications で **New Application** → 名前: `Interpreter` (任意)
2. 左メニュー **Bot** → **Reset Token** で **Bot Token** をコピーして `.env` の `DISCORD_BOT_TOKEN` に貼る
3. **Privileged Gateway Intents** で以下を ON:
   - SERVER MEMBERS INTENT
   - VOICE STATES (デフォルトで ON のはず)
4. 左メニュー **OAuth2 → URL Generator**:
   - scopes: `bot` + `applications.commands`
   - Bot Permissions: `View Channels` / `Send Messages` / `Embed Links` / `Connect` / `Speak` / `Use Voice Activity`
5. 生成された URL を開いて **対象サーバー (ID: 505735702923706371) に招待**
6. 念のためサーバー側のロール設定で bot が各カテゴリの voice/text channel を見られることを確認

### 2. サーバー側 (Hetzner, Claude が実行)

```bash
cd /home/tama/ai-agent-team/discord-interpreter
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

# ffmpeg 必須 (pycord voice receive が利用)
sudo apt-get install -y ffmpeg libopus0

cp .env.example .env
# .env を編集して以下を埋める:
#   DISCORD_BOT_TOKEN  ... Developer Portal で発行したやつ
#   DISCORD_GUILD_ID   ... 505735702923706371  (たまさんサーバー)
#   GEMINI_API_KEY     ... ai-agent-team の親 .env から流用可

# テスト起動
.venv/bin/python bot.py
```

エラーなく `bot ready as Interpreter#xxxx` が出れば OK。

### 3. systemd 常駐化

```bash
sudo cp systemd/discord-interpreter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discord-interpreter.service
sudo systemctl status discord-interpreter.service
journalctl -u discord-interpreter -f  # ライブログ
```

---

## 使い方 (Discord 側)

| コマンド | 動作 |
|---|---|
| `/interpret on` | 自分が今いる VC に bot を召喚、翻訳開始 |
| `/interpret off` | その VC の翻訳を停止 (bot 退室) |
| `/interpret status` | 現在翻訳中の全 VC を表示 |

出力例 (text-A チャンネル):
```
🤖 通訳者が参加しました — voice-A の発話を翻訳してこのチャンネルに流します

🎙️ alice 🇯🇵 → 🇺🇸
> Good morning, the weather's nice today.

🎙️ bob 🇺🇸 → 🇯🇵
> こんにちは、今日もよろしくね。
```

---

## コスト感

Gemini 2.5 Flash audio: 約 $0.0001/sec ≒ **$0.006/分**

- 5人 / 各人2時間中 半分発話 = 5h 実発話 → **$1.8/会**
- VAD 風の無音切り出しで silent packet は送らない → 体感さらに半減
- 安全網: `DAILY_BUDGET_USD=2.0` で上限到達したら自動 OFF + 警告投稿

---

## アーキテクチャ

```
[Discord VC] ─ pycord voice receive ─→ StreamingTranslatorSink
                                          │ user別 PCM buffer
                                          │ 0.8秒無音 or 8秒上限で発話切出
                                          ▼
                                 Translator (Gemini Flash audio)
                                          │ 言語自動判定 + ja↔en 翻訳
                                          ▼
                              find_paired_text_channel()
                                          │ 同名ペア / 同カテゴリ / VC chat
                                          ▼
                                 text channel.send(...)
```

ファイル構成:
- `bot.py` — エントリ、スラッシュコマンド、セッション管理、自動退室
- `translator_sink.py` — pycord `Sink` 継承、user別 PCM buffer + 無音検出
- `translator.py` — Gemini 2.5 Flash 呼び出しラッパ
- `channel_mapper.py` — voice↔text の同名ペア検出 (絵文字正規化)
- `config.py` — `.env` 読み込み

---

## 既知の制約

- 同時複数 VC 対応はしているが、bot 1台で重い VC を 5つ以上掛け持つと CPU が辛い (CX23 想定で 3 VC まで)
- 言語判定は出力 ASCII 比率での雑判定 (将来 Gemini 出力に `[lang=ja]` 等のマーカーを足して厳密化予定)
- 笑い声・相槌・雑音は Gemini 側に「空文字列を返せ」とプロンプトで指示済だが、たまにすり抜ける可能性あり

## トラブルシュート

- `Improper token has been passed` → `.env` の `DISCORD_BOT_TOKEN` が空 or 古い
- `Privileged Intents` エラー → Developer Portal で SERVER MEMBERS INTENT を ON
- `Could not find Opus library` → `sudo apt-get install libopus0`
- VC に入れない → bot に Connect/Speak 権限が無い、サーバー側ロールを確認
