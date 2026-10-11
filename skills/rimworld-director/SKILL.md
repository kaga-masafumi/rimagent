---
name: rimworld-director
description: 人間の自然言語の指示に従い、起動済みRimWorldのマップと状態を確認してRimBridgeで建築・作業・研究などを設定する。結果は人間が観察し、次の行動を決める。
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

建築予定と完成、製造指示受付と製品完成を区別する。SleepingSpotなど即時設置される対象は実状態に合わせて報告する。ゲーム進行中・待機中は定期監視や自動思考を始めず、人間の次の指示を待つ。別LLMのAPI要求や費用台帳の初期化をしない。Codex自体の利用枠は通常どおり消費する。

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
