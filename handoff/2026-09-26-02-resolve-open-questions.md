# 引き継ぎ: 未決事項の解決

- 作成日時: 2026-09-26 14:55:15 +09:00
- リポジトリ: `26_pdf-editor`
- 所在: `C:\Users\littl\app-dev\26_pdf-editor`
- remote: なし（`origin` は未設定）

## 目的と次のセッションでの重点

`docs/spec.md` の「未決事項・矛盾と影響する範囲」に残っている判断を、実装の前に解消する。次のセッションでは実装を始めない。仮の数値や方式で埋めず、1問ずつ決めて、仕様とアーキテクチャの該当箇所へ残す。戻しにくく、文脈がないと意外で、本当の代替がある判断だけ ADR にする。

作業の実装順は変えない。先に 1 つの PDF の編集（選択と表示、その次に回転・削除・並べ替えと Undo）を完成させ、複数 PDF の末尾追加とトレイ／exe はその後である。未決を決めることと、その実装を今始めることは別である。

## 現在地

読み込み、表示、全ページ書き出しの縦切りは `7c3d1506e34690328619166870958138154a11ee` にある。編集状態の正本は Python。画面は Vite で組み立てた React（TypeScript）で、FastAPI が配信する。加工は pikepdf、表示は PDF.js。成功した `POST /api/documents` は作業領域をその 1 件で置き換える。末尾追加は未実装である。

選択、ズームの段階、サムネイルのサイズと解像度、表示の記憶、コマンド API、Undo の形は文章で合意済みである。コードにはまだ無い。2 つの実装計画は `ready-for-agent` で、実装開始日時は未着手である。

## 完了したこと

2026-09-26 のグリルで決まり、仕様とアーキテクチャへ未コミットで反映したこと。

- ズームは 25% から 400% まで 25 ポイント刻み。ページ全体が 25% 未満のときはその倍率のままにし、+/- で段階へ移ったときだけ 25% 以上にする。
- サムネイル枠の高さは小 96px、中 150px、大 220px。描画は CSS サイズに `devicePixelRatio` を掛け、長辺 400px まで。大プレビューのビットマップは現在と前後 1 ページの最大 3 枚。
- 表示の記憶は `%LOCALAPPDATA%\pdf-editor\settings.json`。選択の更新は `PATCH /api/session/selection`。編集操作は `POST /api/session/commands`。Undo の 1 件は逆コマンドと操作前の選択。上限は 100 件。
- ドラッグ開始は 8px。端の自動スクロールは 480px/秒。
- プレビューとサムネイルの境界の最低高さは、大プレビュー 240px、サムネイル領域 160px として仕様に書いた。この 2 つだけは質問を分けずに入れた。利用者へ訂正できると伝え、この引き継ぎ時点で訂正は来ていない。

## 未完了・未決・妨げ

仕様の未決表に残っているもの。実装中に仮置きしない。

- exe 化、ランチャー、システムトレイの実装方式。役割（トレイの「開く」「終了」、ログイン時の自動起動はしない）だけが合意済み。
- 大容量警告のファイルサイズとページ数のしきい値。確認ダイアログを出す条件。
- ログの保存期間と最大容量。期間と容量の両方に上限がある、という要求自体は合意済み。
- 更新確認先。配布設計が無く、URL を作らない。決められないなら未決のまま残し、架空の通信先を書かない。
- 余白、アイコン、フォントサイズと、「...」へ格納する操作の固定順。狭い幅ではアイコン中心、低優先の操作を格納する、という要求自体は合意済み。
- Edge / Chrome 以外を開いたときの警告文。警告して利用は禁止しない、という要求自体は合意済み。
- 分割線と PDF の追加・取り除きのコマンド本文。口と Undo の形は合意済み。コマンドの中身は、その作業単位まで未定義。

縦切りの人手確認は未了のままである。実保存ダイアログ（初期フォルダがドキュメントで、保存まで終わる）と、既定ブラウザを開く経路。製品機能の追加や未決の決定と混ぜない。

`docs/diagrams/` と `.cursor/` は未追跡である。未決の解消とは別であり、このセッションでは中身を仕様の根拠にしていない。

## 次に行う具体的な作業

1. `docs/spec.md` の未決表と `docs/architecture.md` の未決を読む。表に無い項目を、未決として開き直さない。
2. 1 問ずつ決める。先に、後続の計画を止めるものから扱う。分割線と PDF の追加・取り除きのコマンド本文、大容量警告のしきい値、ログの期間と容量、Edge / Chrome 以外の警告文、余白と「...」の順、exe 化とトレイの方式、の順が候補である。更新確認先は配布設計が無いので最後にし、無ければ未決のまま残す。
3. 決まった数値と文言は仕様本文へ書き、未決表から外す。アーキテクチャに影響するものだけそちらも更新する。ADR は、戻しにくいこと、文脈がないと意外であること、本当の代替があること、の 3 つが揃ったときだけ作る。
4. 選択と表示の実装、および回転・削除・並べ替えと Undo の実装は、この未決のセッションでは始めない。
5. 最低高さ 240px / 160px を変える指示が利用者からあれば、仕様の「ページを選び、見る」と選択と表示の計画の両方を直す。指示が無ければそのままにする。

## 推奨スキル

`grill-with-docs`。用語の矛盾と ADR の採否は、そのスキルが呼ぶ domain-modeling に従う。

## 参照

リポジトリ `26_pdf-editor` からの相対パス。

- `docs/spec.md` の「未決事項・矛盾と影響する範囲」（`#unresolved`）。
- `docs/architecture.md` の「編集状態」と「未決事項」。
- `docs/adr/0004-view-preferences-in-backend-file.md`
- `docs/adr/0005-edit-commands-one-endpoint.md`
- `docs/adr/0006-selection-lives-in-session.md`
- `docs/adr/0007-undo-record-is-inverse-command.md`
- `docs/work/2026-09-26-01-select-and-view/select-and-view-implementation-plan.md`
- `docs/work/2026-09-26-02-edit-commands-undo/edit-commands-undo-implementation-plan.md`
- `docs/work/2026-09-25-01-load-preview-export/load-preview-export-implementation-plan.md`。実ダイアログの確認は未チェック。
- `CONTEXT.md`
- `handoff/2026-09-26-01-full-spec.md`。このファイルより前の、仕様全体への実装の引き継ぎ。
- コミット: `7c3d1506e34690328619166870958138154a11ee`（縦切り）、`f77a5e9c6e89f908da4e73604d7166eb44b19bc9`（直前の引き継ぎファイル）。

## Git

- ブランチ: `feat-other-function`
- HEAD: `f77a5e9c6e89f908da4e73604d7166eb44b19bc9`
- remote: なし
- 未コミット: `docs/spec.md`、`docs/architecture.md`（変更）。`docs/adr/0004-view-preferences-in-backend-file.md`、`docs/adr/0005-edit-commands-one-endpoint.md`、`docs/adr/0006-selection-lives-in-session.md`、`docs/adr/0007-undo-record-is-inverse-command.md`、`docs/work/2026-09-26-01-select-and-view/`、`docs/work/2026-09-26-02-edit-commands-undo/`、`docs/diagrams/`、`.cursor/`（未追跡）。
- このファイルは未コミットである。自動ではコミットしない。`handoff/` は Git の除外対象ではない。
