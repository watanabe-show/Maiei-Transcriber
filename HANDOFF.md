# このプロジェクトの引き継ぎ（Claude Codeに貼り付けて使う）

> 使い方：PowerShell で `cd C:\Users\user\Documents\毎映transcriber` してから `claude` を起動し、
> 下の「==== コピペするプロンプト ====」の中身をそのまま貼り付けてEnter。

==== コピペするプロンプト ====

あなたはこのリポジトリ（C:\Users\user\Documents\毎映transcriber）の開発を引き継ぎます。
私は非エンジニアで、日本語でやり取りします。専門用語は最小限にして、手順は「コピペで実行できる形」で出してください。

## このアプリの概要
音声・動画ファイルを文字起こしするローカルWebアプリ。Python + FastAPI 単体（ビルド不要・npm不要）。
- エンジン: Groq Whisper（whisper-large-v3 既定）。話者分離は無し（仕様確定）。
- ffmpeg は imageio-ffmpeg 同梱バイナリ（手動インストール不要）。16kHz mono mp3 に変換 → 10分ごと分割 → 順次Groq → タイムスタンプ連結。
- 認証: パスワード（.env の APP_PASSWORD / 複数は APP_PASSWORDS）。Starlette署名Cookieセッション。
- 出力: 結果画面の「切り方」プルダウンで表示単位を選択（一文ごと / 5秒 / 10秒 / 30秒 / 1分 / 段落(短) / 段落(長) / TimeCodeなし）。
  DLは txt / Word(.docx) / Excel(.xlsx) / srt。txt・Word・Excelは選んだ切り方で保存（srtは字幕用の細かい単位で固定）。
  切り方ロジックは formats.build_blocks / build_views、画面側は static/app.js の renderView。
- UIは昭和初期レトロ（毎日映画社ニュース映画調、images/ の素材使用）。
- 起動: run.bat（既定 HOST=127.0.0.1=このPCのみ。LAN共有時は 0.0.0.0）。Renderデプロイ用 render.yaml も同梱。

## ファイル構成
- app/ : main.py(ルート全部), config.py(.env読込), media.py(ffmpeg変換分割), groq_client.py(Groq呼出), jobs.py(非同期ジョブ+リトライ), formats.py(段落化/10秒TCブロック/srt/vtt), documents.py(docx/xlsx)
- static/ : login.html, index.html, help.html, app.js, styles.css
- scripts/smoke_test.py : スモークテスト（最新 28/28 OK。section6 が切り方の検証）
- .env.example → .env にコピーして GROQ_API_KEY と APP_PASSWORD を記入

## 絶対に退行させてはいけない注意点（過去に実際ハマった）
1. run.bat は必ずASCIIのみ。日本語を入れるとcmdが文字化けして誤動作する。Web UI / .env は日本語OK。
2. .env は load_dotenv(encoding="utf-8") で読む。run.bat は PYTHONUTF8=1 を設定。
3. Groq呼び出しは「同期httpx + asyncio.to_thread」を維持する。async httpx に戻さないこと
   （WindowsのasyncIO+SSLでアップロード時に SSLV3_ALERT_BAD_RECORD_MAC が出るため）。
   SSL/通信エラーは jobs._transcribe_with_retry が最大5回自動リトライ＋日本語で原因案内。
4. StaticFiles（/images, /static）は必ずAPIルートの後にマウント（パストラバーサル対策）。
5. Excel出力は documents._safe_cell で数式インジェクション対策済み（= + - @ 始まりを無害化）。

## 現在の状態
- ローカルで実APIキーを使い、文字起こし → 段落 → txt/srt/docx/xlsx のDLまで全て動作確認済み。
- smoke_test は 28/28 PASS。
- 文字起こし結果の「切り方」を8種類から選べる（一文/5秒/10秒/30秒/1分/段落短/段落長/TimeCodeなし）。保存も選んだ切り方に連動。

## 最初にやってほしいこと
1. `python scripts/smoke_test.py`（または .venv\Scripts\python.exe scripts\smoke_test.py）を実行して、今も21項目PASSするか確認し、結果を報告してください。
2. その後で、私が次にやりたいことを伝えます。まだコードは変更しないでください。

==== ここまで ====
