# 引き継ぎ：大容量動画対応（方式B / クラウドストレージ直アップロード）

> 他のAI（Claude Code等）に渡す用の引き継ぎ。**まだ実装は1行も入っていない**（設計合意＋技術検証まで完了）。
> 基盤アプリ自体の概要・退行注意点は同フォルダの `HANDOFF.md` を必ず併読。
> 完全な実装プラン原本：`C:\Users\user\.claude\plans\vivid-weaving-barto.md`（本書はその要約を内包）。

---

## 0. 前提（ユーザー像）
非エンジニア・日本語でやり取り。専門用語は最小限、手順は「コピペで実行できる形」で。

## 1. 背景・課題（なぜやるか）
- 毎映transcriber は **複数人で共有するWebアプリ**、Render無料枠で公開している。
- 現状アップロード上限は数百MB（`MAX_UPLOAD_MB=300`）。
  受信時にFastAPIが一旦丸ごと一時保存→再コピーで**ファイルの約2倍のディスク**を消費し、無料枠の
  小さなディスク＋スリープにより **2GB級の動画は通らない**。
- 利用者は「ページに動画をドラッグするだけ」が前提。各自にローカル変換ツールを配るのは
  **共有Webの手軽さを壊すため不可**（この案は検討の上で却下済み）。

## 2. 検討した3案と結論
| 案 | 内容 | 判定 |
|----|------|------|
| A | ブラウザ内変換(ffmpeg.wasm)で音声化してから送る | ✕ 2-3GBはWASMメモリ上限で不安定（実用~1GB止まり） |
| **B** | **クラウドストレージへ直接アップロード** | **◎ 採用** |
| C | Renderを有料プラン化 | ✕ ¥0が崩れ、WAN越し2GBの不安定さも残る |

→ **方式Bを採用。アップロードは最初から multipart（分割・再開対応）で実装する**ことまで合意済み。

## 3. 方式Bの核心アーキテクチャ
1. ブラウザ → **Cloudflare R2（S3互換・egress無料）** へ動画を直接アップ（**Renderを経由しない**）。
2. サーバーは R2 の **presigned GET URL を ffmpeg の `-i` に渡してストリーミング変換** →
   既存パイプライン（16kHz mono mp3化 → 10分ごと分割 → Groq Whisper順次 → タイムスタンプ連結）に合流。
3. 処理後に R2 のオブジェクトを削除。

### 検証済みの重要事実（設計の土台）
- 同梱ffmpeg（`imageio-ffmpeg`, v7.1）は **https/tls 対応**を確認済み → URLから直接読める。
  よって Render のディスクには**小さな分割mp3しか置かれない**（2GBをディスク/メモリに載せない）。
- MP4の moov atom 問題は ffmpeg の HTTP **Range要求でseek**できるため解消。
- 音声は16kHz mono 32kbpsに圧縮されるため、**3時間動画でも変換後 約43MB**（Groqへ送るチャンクは常に小さい）。
- アップロードは R2→Render の読み戻し（egress）が発生するが、**R2はegress無料**なので「¥0」と相性が最良。
  コードはS3互換なので、後から Backblaze B2 / AWS S3 にもエンドポイント差し替えで切替可能。

## 4. 実装プラン（ファイル別）

### 新規 `app/storage.py`（boto3でR2操作する薄いラッパー）
- `enabled() -> bool`（必要な環境変数が揃っているか）
- 遅延生成のS3クライアント（`endpoint_url`, `region_name="auto"`, SigV4）
- `new_key(ext)` → `uploads/<uuid><ext>`
- `initiate(key, content_type) -> upload_id`（create_multipart_upload）
- `presign_parts(key, upload_id, part_numbers, expires) -> {n: url}`（upload_part の presigned PUT。再送/再開で再発行に使う）
- `presign_get(key, expires) -> url`（get_object の presigned GET。ffmpeg入力用）
- `complete(key, upload_id, parts)` / `abort(key, upload_id)` / `delete(key)`

### `app/config.py` 追加
- `S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_REGION(=auto)`
- `STORAGE_ENABLED`（上記が揃えばTrue）／`STORAGE_MAX_UPLOAD_MB`(既定3000)／`UPLOAD_PART_MB`(既定64)／`DIRECT_UPLOAD_MAX_MB`(既定200=これ以下はフロントが従来直アップを使う閾値)
- presigned有効期限：PART=6h、GET=6h（変換はジョブ開始直後のffmpeg段で完結するため十分）

### `app/main.py` 追加/変更（既存 `_require_auth` を再利用、全エンドポイント認証必須）
- `GET /api/config`：`{storage_enabled, direct_max_mb, storage_max_mb, part_mb, allowed_ext}` を返しフロントが経路判定
- `POST /api/uploads/initiate`：ext/サイズ検証 → `{key, upload_id, part_size, part_count, part_urls[]}`
- `POST /api/uploads/parts`：失効/再開時に該当パートのpresigned URLを再発行
- `POST /api/uploads/complete`：`{key, upload_id, parts[{PartNumber,ETag}]}` → complete_multipart_upload
- `POST /api/uploads/abort`：キャンセル時に abort_multipart_upload
- `POST /api/transcribe` を拡張：従来の `file`（multipart）に加え、`key`（ストレージ経路）を受理。
  `key` 指定時は presigned GET を発行してジョブ作成

### `app/jobs.py` 変更（本体はほぼ流用）
- `create_job(..., key=None)` を追加。`_process` の入力を「ローカルパス or presigned GET URL」に一般化
  （ffmpegは `-i` にローカル/URLどちらも同様に渡せるため分岐は最小）
- 完了/失敗時の `finally` で `key` があれば `storage.delete(key)`
- 既存の `_SEMAPHORE`（同時2件）・`_transcribe_with_retry`・TTL掃除はそのまま維持

### `app/media.py` 変更（小）
- 入力がURL（`http(s)://`）のとき `-i` の前に `-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5` を付与。
  `transcode_and_segment`（変換→`-f segment`分割）本体は不変

### フロント `static/app.js` 変更
- 起動時に `GET /api/config` 取得
- ファイル送信時：`storage_enabled && file.size > direct_max_mb` なら multipart経路:
  1. initiate → 2. `file.slice()` で分割し各パートを XHR PUT（`upload.onprogress` で合算進捗、並列3、各パート指数バックオフ再試行、ETag回収）→ 3. complete → 4. `/api/transcribe`(`key`) → 既存 `poll()`
  - 失効/失敗時は `uploads/parts` でURL再発行。キャンセルで `uploads/abort`。`localStorage` に進捗保存し再読込で残りパートから再開
- それ以外（小ファイル・マイク録音）は**従来の直アップ経路のまま**
- 「アップロード中…」フェーズUIを既存プログレスバー流用で追加

### 設定・ドキュメント
- `requirements.txt`：`boto3` 追加
- `render.yaml`：`S3_*` 環境変数（秘密は `sync:false`）追記
- `README.md` / `static/help.html`（line64 の「300MB」記述）/ `.env.example`：R2手順・新上限(〜3GB)に更新

## 5. 後方互換・安全性（重要）
- **ストレージ未設定なら `/api/config` が `storage_enabled:false` を返し、完全に従来動作**（大ファイルは従来通り413）。R2は完全オプション。
- 新エンドポイントは全て `_require_auth` 必須。キーはuuidで非推測。
- presigned短期失効 ＋ 処理後 `delete` ＋ R2ライフサイクル(1日)で**二重に後始末**。
- ffmpegはR2からストリーミング読込でRender無料枠のディスク/メモリを圧迫しない。同時実行は既存セマフォで2件に制限。

## 6. 退行させてはいけない既存の注意点（HANDOFF.md より、必ず維持）
1. `run.bat` は必ずASCIIのみ（日本語禁止）。Web UI / .env は日本語OK。
2. `.env` は `load_dotenv(encoding="utf-8")`、`run.bat` は `PYTHONUTF8=1`。
3. **Groq呼び出しは「同期httpx + asyncio.to_thread」を維持**（async httpxに戻すとWindowsで SSLV3_ALERT_BAD_RECORD_MAC）。
4. StaticFiles（/images, /static）は必ずAPIルートの**後**にマウント。
5. Excel出力は `documents._safe_cell` の数式インジェクション対策を維持。

## 7. 利用者（管理者）が手動でやるクラウド設定（README/.env.example に手順記載予定）
1. Cloudflare アカウント → **R2** → バケット作成（例 `transcriber-uploads`）
2. **R2 APIトークン**（Object Read & Write）発行 → Access Key ID / Secret Access Key 取得。
   アカウントIDから エンドポイント `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
3. バケット **CORS**（必須）:
   - AllowedOrigins: 本番URL（`https://xxx.onrender.com`）＋ `http://localhost:8000`
   - AllowedMethods: `PUT, GET` / AllowedHeaders: `*` / **ExposeHeaders: `ETag`（multipart必須）**
4. **ライフサイクル**: 未完了multipartを1日で中断、オブジェクトを1日で削除（安全網）
5. 環境変数 `S3_ENDPOINT / S3_BUCKET / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY /（S3_REGION=auto）` を
   Renderダッシュボード＋ローカル `.env` に設定

## 8. 検証手順（エンドツーエンド）
1. `pip install -r requirements.txt`（boto3導入）
2. `.env` にR2情報を設定し、バケットCORS（ExposeHeaders: ETag）を適用
3. 接続確認：`ffmpeg -i "<presigned GET URL>" -t 5 -f null -`（URLから読めるか）
4. ローカル起動 → ブラウザで **>200MBの動画** をドラッグ:
   - multipartアップ進捗が伸びる → complete → 文字起こしが走りTC付きで出力
   - 途中で回線を一瞬切る → 該当パートが自動再送（再開動作）
   - 処理後にR2バケットからオブジェクトが消えている
5. 小さい音声＋マイク録音が**従来通り**動く（直アップ経路の非回帰）
6. （任意）`scripts/` に R2疎通スモークテスト追加（initiate→小パート送信→complete→presign GET→abort）

## 9. 現在の状態 / 次の一歩
- **実装完了（2026-06-12）。§9.5 の4点修正も反映済み。** ローカルで py_compile / app import / routes登録 / smoke_test **28/28 PASS** / app.js構文OK を確認済み。
  R2未設定のため `storage.enabled()=False`＝**従来動作のまま（非回帰）**。
- 実装ファイル：新規 `app/storage.py`／`config.py`(S3_*)／`main.py`(/api/config・/api/uploads/{initiate,parts,complete,abort}・/api/transcribe-key、既存 /api/transcribe は不変)／
  `jobs.py`(create_jobにkey、変換ステージ文言、finallyでR2削除)／`media.py`(URL入力時reconnect)／`static/app.js`(multipart直アップ)／`requirements.txt`(boto3)／`render.yaml`／`.env.example`／`README.md`／`static/help.html`。
- boto3 1.36+ のチェックサム署名不一致対策として `storage._client()` で `request_checksum_calculation="when_required"` を設定済み（古いbotocoreはtry/exceptでフォールバック）。
- **残: 実機検証（未実施）** — ① `pip install -r requirements.txt`（boto3導入）② R2バケット作成＋CORS(ExposeHeaders:ETag)＋環境変数 ③ >200MB動画で multipart→complete→文字起こし→処理後R2削除 を通しで確認（§8）。

## 9.5 再検討の結果（2026-06-12 / Fable 5 で現行コードと突き合わせ済み）

**結論：方式Bのまま進めてよい。** 切り方8種リファクタ後のコード（formats.build_views 化）とも矛盾なし。
切り方の整形は文字起こし「後」、本改修は文字起こし「前」の入口なので互いに干渉しない。
ffmpeg入力は jobs._process → media.transcode_and_segment → `-i` の一本道で、URL差し替えは最小変更で済むことをコードで確認済み。

ただし §4 のプランに以下4点の修正を加えること：

1. **既存 `/api/transcribe` は変更しない。** R2経路は別エンドポイント `POST /api/transcribe-key`
   （Form: key, filename, language）として新設する。実績経路（smoke 28/28）への退行リスクをゼロにするため。
   §4 main.py の「`/api/transcribe` を拡張」は本項で上書き。
2. **元ファイル名の引き回しを忘れない。** R2経由ではサーバーに元のファイル名が届かない。
   `/api/transcribe-key` の filename をジョブの `filename` に保存する
   （ダウンロード時の `base`＝保存ファイル名の元になるため必須）。
3. **変換中ステージの文言改善。** 大容量では「音声に変換中…」(progress=5)が数分続く。
   key経路のジョブではステージ文言を「動画から音声を取り出しています（大きい動画は数分かかります）…」にする。
   ffmpeg `-progress` 解析による実進捗は任意（後回しでよい）。
4. **R2無料枠10GB/月の注意書き。** 1本最大3GB設定のため、同日に複数人が大型動画を上げると
   無料枠に達しうる。処理後即削除＋ライフサイクル1日の二重掃除で実用上は問題ない見込みだが、
   help.html の料金欄に一言追記する。
   （補足：presigned PUT はパートの実サイズを強制できないが、利用者はパスワードを知る身内のみなので許容。）

## 10. この実装と無関係な雑談（無視してよい）
- 会話中に出た「モデル切替(Fable 5)」「`aws sso login`」は本実装と無関係の脱線。
  特に **AWS SSO は R2 では使わない**（R2はSSOではなく静的なAccess Key/Secretで署名する）。
