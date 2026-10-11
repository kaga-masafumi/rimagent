# Windows 11・LLM切り替え・使用量管理

RimWorld 1.6 / Steam、Python 3.12以上とuv、.NET 10 SDKを使用します。Mod本体のターゲットは既存のnet48、Verse非依存テストはnet10.0です。RimBridgeのソース・RPC・Stewardのゲーム操作ロジックは変更していません。

## Windowsの準備

リポジトリ直下のPowerShellから実行します。

```powershell
git submodule update --init --recursive
cd agent
uv sync --frozen
cd ..
& ./script/build.ps1 -Test -ModsPath 'D:\SteamLibrary\steamapps\common\RimWorld\Mods'
```

`build.ps1`はRimBridge、Stewardの順にビルドします。SDKは`runs/dotnet/dotnet.exe`があれば優先し、なければPATHのdotnetを使います。`-Dotnet 'C:\path\dotnet.exe'`で指定できます。SDK不足の場合は[Microsoft公式の.NET 10 SDK](https://dotnet.microsoft.com/download/dotnet/10.0)を導入してください。

Modsへの配置はディレクトリジャンクションです。同じリンクが既にあればそのまま使用し、他の既存フォルダは上書きしません。リンクを使えない場合は`-InstallMode Copy`で新規フォルダにAboutと1.6をコピーします。コピー配置後の更新は、既存配置を確認して別の場所へ退避してから再配置してください。

RimWorldのMod画面でHarmony → RimBridge → RimBridge: Stewardの順に有効化し、Steam経由で再起動してください。DLLは起動時に読み込まれます。配置スクリプトは有効Mod一覧を変更しません。

ゲームのオプションで「バックグラウンドで実行」(Run in background)をONにしてください。runInBackground=Falseの場合、PowerShellやブラウザへフォーカスを移すとゲームのメインスレッドが停止し、状態取得・一時停止・エージェント終了がタイムアウトします。Bridgeのhealthは過去にフレームが動いたことだけでもmainThreadAlive=trueになり得るため、health成功だけで現在のRPC実行が正常とは判断できません。RPCエラーのlast drainが長い場合はこの設定とゲームへのフォーカスを確認してください。

```powershell
& ./script/restart-game.ps1
& ./script/start.ps1 -PlayArgs '--max-days','1'
```

`start.ps1`は設定、APIキー、料金、予算停止状態、watchdog無効を確認してからSteamを起動し、Bridgeのヘルスチェックを待ってplayとダッシュボードを開始します。このコマンドは自律プレイを開始します。初回は保存済みの試験用コロニーを使用してください。終了時の振り返り後は既存Runnerの仕様に従って次のゲームへ進むため、停止するときはRunnerを終了してください。

設定確認だけなら、ゲーム起動・有料API要求なしで実行できます。

```powershell
cd agent
uv run --frozen rimagent doctor --require-watchdog-off --require-budget-active
uv run --frozen rimagent usage
```

ダッシュボードは`http://127.0.0.1:8770`、Bridgeは`http://127.0.0.1:8765`です。UTF-8で設定・記憶を読み込み、起動スクリプトはUTF-8モードも有効にします。

### 起動診断

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
& ./script/tail-log.ps1
```

Player.logは`%USERPROFILE%\AppData\LocalLow\Ludeon Studios\RimWorld by Ludeon Studios\Player.log`です。Steamが別ドライブでもModsPathを指定すれば配置できます。再起動スクリプトはプロセス終了失敗・起動失敗・タイムアウトをエラーにします。

HTTP待受がアクセス拒否になった場合だけ、実際の待受プレフィックスをログで確認し、管理者PowerShellでURL予約を行います（ユーザー名は実環境に合わせます）。常時管理者実行は不要です。

```powershell
netsh http add urlacl url=http://127.0.0.1:8765/ user='DOMAIN\username'
```

[Microsoft HttpListener](https://learn.microsoft.com/en-us/dotnet/api/system.net.httplistener)、[URL予約](https://learn.microsoft.com/en-us/windows/win32/http/add-urlacl)を参照してください。

## QwenとOpenAI

provider省略時は`qwen`です。Qwenのbase_url、api_key、max_tokens、temperature、enable_thinking、空応答時のthinking無効再試行を維持しています。料金未設定のQwenはトークンだけ記録し、費用は「未設定」と表示します。料金未設定の要求にUSD上限は適用されません。

OpenAIは`config.openai.example.yaml`を`config.local.yaml`へコピーして選択します。既存のローカル設定がある場合は内容を統合してください。APIキーはリポジトリ直下の`.env`から読み込めます。`.env.example`を`.env`へコピーし、エディターで値を設定してください。

```dotenv
OPENAI_API_KEY=your-project-api-key
```

`.env`はGit管理から除外しています。UTF-8（BOM付きも可）、引用符とコメントに対応します。実行するフォルダがagent/でも直下の.envを参照し、RIMAGENT_ROOT指定時はそのルートを使います。環境変数が既に存在する場合はそちらを優先し、.env内の変数展開は行いません。`llm.api_key_env`で別の変数名を選んだ場合は.envにも同じ名前を設定します。APIキーをログ・doctorの結果へ出力しません。キー変更後はRunnerを再起動してください。

確認コマンドは以下です（環境変数を使う場合は従来の設定方法も使用できます）。

```powershell
cd agent
uv run --frozen rimagent doctor --require-watchdog-off --require-budget-active
```

OpenAIは公式エンドポイントのChat Completionsを使い、QwenのURL・キーを引き継ぎません。`max_tokens`設定を`max_completion_tokens`として送信し、推論強度を`reasoning_effort`へ対応させます。thinking=falseはnoneです。temperatureとQwen固有パラメーターを送らず、ツールスキーマのコピーでstrict=falseと配列itemsを補います。RegistryやRPCの引数補正は維持します。

実APIのHTTP 400で、gpt-5.6-lunaのChat Completionsではfunction toolsと推論を併用できず、reasoning_effort=noneまたはResponses APIが必要と確認しました。このアダプターではLunaのツール付き要求を自動的にnoneにします。ツールなしでthinking=trueの要求は設定したlowなどを維持し、manager等のthinking=falseの要求はnoneです。推論を伴うゲーム操作には将来のResponsesアダプターが必要です。

切り替え・単価・予算変更はRunner再起動時に適用します。有料モデルへの自動フォールバックはありません。Responsesアダプターと社長の承認UIは今回の対象外です。

大量のツール定義を送るとOpenAIがarray_above_max_lengthで要求を拒否する場合があります。128定義を超える思考では、Bridgeの操作をrimagent_bridge_call(method, params)という共通窓口へまとめ、各RPCの説明・引数をシステムメッセージのカタログへ移します。役割ごとに許可されたRPCだけを扱い、実行は元のRegistryへ戻すため引数補正やゲーム操作は同じです。Qwenのツール提示は従来どおりです。

## 終了する

ダッシュボードの「Stop agent + pause game」でエージェント終了とゲーム一時停止を要求できます。「End episode」は振り返りと次ゲームへ進む操作で、終了ボタンではありません。「Pause agent」は一時的な判断停止です。

WindowsでCtrl+Cが届かない場合は、別のPowerShellから実行してください。

```powershell
cd C:\Users\kaga0\project\rimagent
& ./script/stop.ps1
```

新しいAPI要求・ツール操作を禁止し、ゲームを一時停止します。送信済みのAPI要求は結果・usageを受け取るまで終了が遅れる場合があります。30秒待っても終了しない場合は、次のコマンドでこのリポジトリのrimagent playプロセスだけを確認して終了できます。

```powershell
& ./script/stop.ps1 -Force
```

RimWorldは閉じないので、一時停止を確認してゲーム内で保存してください。強制終了時は課金結果不明の予約が残る可能性があり、次回起動前にusageの照合が必要です。カスタムポートを使う場合は-DashboardUrl、-BridgeUrlを指定します。

## 料金と台帳

2026-10-11確認の[Luna公式仕様](https://developers.openai.com/api/docs/models/gpt-5.6-luna)を組み込みました。API IDはgpt-5.6-luna。100万トークン当たりUSDで入力0.20、キャッシュ入力0.02、出力1.20、キャッシュ書き込み0.25です。272,000入力トークンを超える要求全体には入力系2倍・出力1.5倍を適用します。推論トークンは出力の内数です。

標準料金・キャッシュなしで入力10,000、出力1,000なら推定$0.0032です。1思考はツール結果の追加ごとに複数のAPI要求になります。画像、再試行、manager、改善、振り返り、CLIのllm試験、seed --distillも記録します。契約・画像・課金条件によって実請求と差があり得るため、OpenAI側の使用量・請求とも照合してください。

`runs/usage.sqlite3`が正本です。要求ごとに日時、モデル、用途・episode・試行、要求ID、raw usage、正規化した入力・キャッシュ・出力・推論、予約額、確定推定費用、当時の単価を保存します。usageは回答解析や空応答再試行より先に記録します。sftの既存captureは維持します。SQLiteトランザクションとDecimalで並列予約・金額計算を行います。

未知のOpenAIモデルは単価を設定するまで起動できません。独自料金は次の形式で設定します（例示の数値は契約に合わせて確認が必要です）。

```yaml
prices:
  openai/your-model:
    input: '0.20'
    cached_input: '0.02'
    output: '1.20'
    cache_write: '0.25'
    verified_on: '2026-10-11'
    source: 'https://your-verified-pricing-source'
    long_context_threshold: 272000
    long_input_multiplier: '2'
    long_output_multiplier: '1.5'
```

同様に`qwen/実際のモデル名`へ料金を指定するとQwenも費用予約対象になります。ローカルで無料と確認済みならゼロ単価を明示できます。

## 上限と停止・再開

既定は実時間のAsia/Tokyoで日次$1・月次$10です。日付はゲーム内日数と無関係です。要求前に会話・ツール定義のUTF-8バイト数+余裕分を入力推定量とし、画像1枚につき65,536トークン、最大出力・最も高い入力単価で予約します。推定は保守的ですが、課金額の数学的保証ではありません。

確定費用＋未解決予約＋今回予約が日次または月次上限を超える場合は送信せず、ゲームを一時停止します。usage不明・タイムアウト・サーバーエラー・台帳保存失敗も停止します。SDKの内部再試行を無効にして要求単位で記録し、不明な有料要求を自動再送しません。通常の429拒否は未課金失敗として記録します。

停止状態はSQLiteへ永続化します。日月の切り替えや再起動では解除せず、未解決予約は日月をまたいでも確保します。ゲーム再開、次ゲーム、復旧の自動再起動、watcher、managerからのworker起動、戻ってきたLLM回答のツール実行を停止経路で防ぎます。Bridgeが不通ならゲーム停止RPCは実行できないため、接続回復後に再度停止を要求します。既にゲームへ送信済みの操作やLLM要求は取り消せません。

ダッシュボードのUsage / Costで日月費用、予約、全期間トークン、直近100要求、80%・95%警告、停止理由を確認できます。`GET /api/usage`とbusのusage/budgetイベントでも参照できます。

不明要求の照合は**Runnerを終了してから**行ってください。台帳を共有する別のRunnerやCLIのLLM処理を並行起動しないでください。起動時に有料の未解決要求がある場合、安全のため停止状態になります。

```powershell
cd agent
uv run --frozen rimagent usage
# OpenAIの請求・要求ログを確認後、request IDを指定する
uv run --frozen rimagent usage --resolve REQUEST_ID --cost-usd 0.0032
# cost-usdを省略すると予約額全額を費用へ振り替える
uv run --frozen rimagent usage --resume
```

未解決要求が残る間・予算の余裕がない間は再開を拒否します。認証やAPI設定を修復した後も明示再開が必要です。設定で上限を変更する場合はRunnerを再起動し、usage --resumeまたはダッシュボードから再開します。台帳を削除して予算をリセットしないでください。保存障害はストレージを修復して再起動してください。

OpenAI側でも専用Projectと利用可能な支出制限を設定してください。ローカル上限は推定差や送信済み要求による超過を完全には防げません。[公式Spend limits](https://developers.openai.com/api/docs/guides/spend-limits)で上限の性質・反映遅延を確認してください。

## watchdog・今後の承認機能

配布設定はwatchdog.enabled=falseです。無効時は専用ツールの登録・スレッドの起動を省きます。Windowsのstart.ps1も無効を要求します。改善処理によるbrain更新は既存仕様として継続します。

Contextのexecution_check、Registryの実行前チェック、Bridgeクライアントのoperation_guardが将来の承認機能の接続点です。現行brainツール/run_pythonは任意Pythonを実行できるため、重要操作を完全に統制するには別途権限・隔離設計が必要です。今回の停止制御は通常のRunner・Registry・Bridge経路を対象とし、任意コードのサンドボックスではありません。

## 検証と変更範囲

```powershell
cd agent
uv run --frozen pytest -q
cd ..
& ./script/build.ps1 -Test
```

Windowsでリンク作成権限がない環境だけ、watchdogのsymlink逃避テストを理由付きskipにします。権限があれば従来どおり検証します。追加テストはQwen要求互換、OpenAIパラメーター、usage null項目・空応答・解析失敗、並列予算、日月境界、再起動、不明課金、再開条件、manager/worker/復旧/後処理による再開防止を対象にしています。有料API接続とゲーム内操作の受け入れ確認は別途必要です。

| ファイル | 変更の目的 |
|---|---|
| config.yaml / config.openai.example.yaml / agent/rimagent/config.py | provider・料金・予算設定、UTF-8、再帰マージ、watchdog無効 |
| agent/rimagent/llm.py / providers.py / pricing.py / usage.py | 共通LLMインターフェース、要求分離、料金、永続台帳・予約・停止 |
| agent/rimagent/loop.py / runner.py / context.py / registry.py / bridge.py / watchers.py | 用途の記録、停止・実行前チェック、再開防止。bridge.pyはPython側だけ |
| agent/rimagent/cli.py / knowledge/seed.py / reflect.py | CLI・蒸留・振り返りを共通台帳に接続、doctor・照合CLI |
| agent/rimagent/dashboard/app.py / bus.py | Usage / Cost表示、API、イベント契約 |
| agent/rimagent/annotate.py / skills.py / tracker.py / export_sft.py | Windowsフォント、日本語ファイルのUTF-8読み取り |
| script/build.ps1 / start.ps1 / restart-game.ps1 / tail-log.ps1 | Windowsビルド、配置、起動前検査、Steam起動、ログ閲覧 |
| .gitignore / README.md / docs/WINDOWS_LLM.md | 生成物除外、設定・運用・制限の説明 |
| agent/tests/test_config.py / test_llm.py / test_usage.py / test_runner_budget.py | 追加契約と停止経路の検証 |
| agent/tests/test_watchdog.py / test_steward.py | Windowsリンク権限差・UTF-8と再帰設定の期待値 |

modサブモジュールの固定コミットは維持し、mod/とmod-steward/のC#ソース・ターゲットフレームワークは変更しません。
