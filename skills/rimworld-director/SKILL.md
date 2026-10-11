---
name: rimworld-director
description: 人間の自然言語の指示に従い、起動済みRimWorldのマップと状態を確認してRimBridgeで建築・作業・研究などを設定する。通常は人間が結果を観察する。明示された自律運転では、上限内で進捗・警告を監視し、判断を繰り返す。
---

# RimWorld 指示プレイ

Codex自身が判断し、RimBridgeのローカルHTTP経由で操作する。既存RunnerやAssist、Lunaなどの別LLMを起動しない。人間が読み込んだコロニーを対象とする。対象はこのrimagentチェックアウト、Bridgeは `http://127.0.0.1:8765`。

## 指示の範囲

今回の目的を満たす、資材・動線・安全性を考慮した小さな操作集合を選ぶ。「最適」を無制限のコロニー改造と解釈しない。指示に必要な場所・素材の選択は任されているので通常は進める。用途・対象など結果が大きく変わる不足情報だけ質問し、回答まで停止を維持する。調査やスキル作成だけの依頼ではゲームを操作しない。

通常のプレイ指示は、その範囲の操作と停止・正常終了後の再開を含む。攻撃、追放、臓器摘出、大量破壊などは指示に含まれる場合だけ扱う。dev/engine操作、新規ゲーム、ロード、ゲーム終了、ソース編集、watchdog、brain編集、Git操作は範囲外。

## 実行

1. `health` と `game.status` を読む。メインスレッドが応答しなければ操作を止め、ゲームを前面に出す／Run in backgroundを有効にすることを案内する。コロニー未読込なら人間の読込を待つ。
2. Assist／playと競合させない。補助スクリプトは変更時に同じ `runs/controller.lock` を取得する。使用中なら操作を始めず、`script/stop.ps1` で既存コントローラーの終了が必要だと伝える。勝手に強制終了しない。
3. `game.pause {paused:true}` を送り、`game.status` で停止を確認する。`state.summary` と必要な範囲の資材・入植者情報を読む。対象コロニーの識別情報とtickを記憶し、途中でロード・巻き戻し・接続異常があれば中止する。
4. 空間判断ではマップ画像を保存し、`view_image` で実際に見る。画像だけで座標を決めず、`map.detail` や `map.find` で地形・障害物・通路を照合する。
5. `methods --filter <RPC名>` で実際の引数仕様を確認する。建物・素材・研究のdefNameと対象IDは実データから選ぶ。不明なら `mod/Source` の該当RPCを読む。名前やキーを推測しない。
6. 小さな単位で操作し、応答と局所状態を確認する。`ok:false`、非空の `failed`、空の `placed` は成功ではない。変更タイムアウトは結果不明として停止し、自動再送しない。まず読み取りで実際の結果を調べる。
7. 設定がすべて成功したら `game.speed` で再開し、日本語で設定内容とキャラの作業待ちを報告する。初回は1倍速、以後は指示開始前に確認した人間の速度1〜3。停止維持の指示、部分失敗、質問、結果不明では再開しない。

建築予定と完成、製造指示受付と製品完成を区別する。SleepingSpotなど即時設置される対象は実状態に合わせて報告する。通常モードではゲーム進行中・待機中に定期監視や自動思考を始めず、人間の次の指示を待つ。明示された自律モードのみ下記を使う。別LLMのAPI要求や費用台帳の初期化をしない。Codex自体の利用枠は通常どおり消費する。

## 自律モード（明示された場合だけ）

「自律的に」「完成まで任せる」等の指示では、実行中のCodexが判断→操作→待機→検証を繰り返す。別LLMや常駐Runnerを起動しない。チャット終了後の動作は保証しない。

### 平時と緊急時

平時は `--mode routine`（既定）で120秒ごとにまとめて判断する。この間、ゲームは進行を続け、Pythonだけで2秒ごとに安全監視する。作業待ち人数、普通の通知、目標の進捗では途中停止しない。完成判定と詳しい状態取得は開始・終了時に行い、終了時に停止してCodexが次の設定を考える。

緊急時は `--mode emergency` で1秒ごとに確認し、目標達成・新しい警告・新しい通知でも停止する。既定の待機上限は30秒。両モードとも、敵・倒れた入植者・ゲームのHigh危険度・Critical警告・新たなThreatBig/ThreatSmall/Death通知・接続異常は即座に中断する。ゲームが人間に停止された場合も終了し、自動再開しない。検知はポーリングで、RPCの通信時間や観測間の短命イベントには限界がある。重大な警告を確認したら、対処または人間の判断まで再開しない。

1. 目標・操作範囲・完了条件を定める。既定上限は10待機サイクル、実時間15分（判断・操作時間も含む）。各サイクル前に残時間を確認し、最後の待機を残時間に収める。
2. 通常の実行手順で停止・調査・操作を行う。手順7の直接再開を行わず、waitに再開と停止を任せる。既存警告も調査する。
3. `<repo>/runs/codex-goal.json`にUTF-8 JSONの完了条件を書く。現対応はbuilding_count（完成建物数）、stock_count（保管在庫数）のみ。実データのdefNameを使い、任意コード評価はしない。

```json
{"kind":"building_count","def":"Bed","at_least":3}
```

在庫例は `{"kind":"stock_count","def":"WoodLog","at_least":100}`。既存建物も数えるため「新しく1台」は既存数+1を指定する。完成数は割当・到達可能性・通電・使用可能性を保証しない。目的達成の報告前に必要な追加条件を通常RPCで検証する。在庫数は保管分であり全地面資材ではない。

4. 観測・待機する。1回の待機は最大120秒。既存RPCの15秒タイムアウトにより、取得・停止を含む時間は指定時間を超え得る。コントローラーロックは待機終了まで保持する。Codexのツール呼出はバックグラウンド実行にして60秒以内に進捗を伝え、120秒のブロッキング待機をしない。

```powershell
python <skill-dir>/scripts/bridge_watch.py --repo <repo> observe --goal-file <repo>/runs/codex-goal.json
python <skill-dir>/scripts/bridge_watch.py --repo <repo> wait --goal-file <repo>/runs/codex-goal.json --write --mode routine
python <skill-dir>/scripts/bridge_watch.py --repo <repo> wait --goal-file <repo>/runs/codex-goal.json --write --mode emergency
```

5. paused:trueを確認する。completedなら用途を再検証して終了する。routine_check/timeout/game_time_limitなら進捗とまとめられたnew_alerts/worsened_alerts/new_lettersとobserved_eventsを読み、次の作業または待機を選ぶ。普通の警告をすべて緊急扱いにしない。明示した `--max-ticks` は両モードの早期上限、`--stall-ticks` は緊急時に早期判定、平時は120秒の判断時に判定する。既定では両制限を使わず、建築中に完成数が変わらないことだけで停滞と断定しない。
6. eventなら内容を読む。danger/game_paused/error/paused:falseなら中止し報告する。goal_met:trueでも危険・エラーを優先する。終了・上限到達時は停止して成果・未達事項・警告を報告する。変更失敗を自動再送しない。

警告・通知は開始時を基準に、新規・優先度上昇・解消を返す。既存警告はactive_alertsに残る。説明文だけの変化は悪化判定しない。通知はIDで重複を抑える。観測で確認した途中の変化はobserved_eventsに重複を抑えて残す（区切りまでに解消しても残す）。停止後の最終観測でも停止状態を検証する。

異常時は一度だけ停止を試み、通信断で停止確認できなければ手動停止を依頼する。seed・map_size・tick・seqの逆行で切替を検出するが、同一seed/サイズで時刻が進むロードは識別できない。実行中に人間がロード・マップ切替しないよう伝える。

## Steward

導入されていれば `steward.status` と `steward.orders` を読む。人間主導の開始時は scorer・stock・standing ordersを無効にし、検証する。その後は人間が明示した処理だけ有効化する。stockの全体有効化は既存ジョブも動かすので、対象外を停止できることを確認する。限定できなければ通常の製造指示等を使う。停止しても既存の予定・指示は残るので勝手に消さない。未導入なら省く。

## 補助スクリプト

`scripts/bridge_control.py` はPython標準ライブラリだけで動く。`--help` を参照する。既定は読み取りで、変更には `--write` が必要。フラグは人間の承認の代わりではなく、指示範囲の操作を送るためのもの。

```powershell
python <skill-dir>/scripts/bridge_control.py health
python <skill-dir>/scripts/bridge_control.py methods --filter ui.build
python <skill-dir>/scripts/bridge_control.py rpc game.status
python <skill-dir>/scripts/bridge_control.py screenshot --output <repo>/runs/codex-map.png --x 130 --z 160 --w 40
python <skill-dir>/scripts/bridge_control.py rpc game.pause --params-file <repo>/runs/codex-params.json --write
```

`<skill-dir>` はこのファイルの親ディレクトリ。引数はUTF-8 JSONファイルで渡し、PowerShellの引用符問題を避ける。停止は `{"paused":true}`。建築位置は `at:[x,z]`、向きは `N/E/S/W`。`map.detail` は `x,z,w,h`、`defs.buildable` は `category`（`pos`、`center/radius`、`search` は使わない）。最新の `/methods` を優先する。

変更は `runs/codex-play.jsonl` に送信前・応答後を記録する。補助スクリプトはローカルHTTPのみ、通常操作のみ、停止中の変更のみ許可し、失敗を再送しない。画像は保存された実ファイルを見てから説明する。

## Codexへのインストール

リポジトリ直下のPowerShellで、スキルを個人スキルフォルダへコピーする。既に同名スキルがあれば内容を比較して更新する。

```powershell
$skillRoot = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path $env:USERPROFILE '.codex/skills' }
New-Item -ItemType Directory -Path $skillRoot -Force | Out-Null
Copy-Item -LiteralPath ./skills/rimworld-director -Destination $skillRoot -Recurse
```

Codexで `$rimworld-director` と指示する。一覧に反映されない場合は新しいチャットで確認する。
`<repo>` は実際のチェックアウトの絶対パス。スクリプトは `RIMAGENT_ROOT`、現在のフォルダ、スクリプト配置場所からリポジトリを探す。見つからなければ `--repo <repo>` をサブコマンドより前に指定する。
