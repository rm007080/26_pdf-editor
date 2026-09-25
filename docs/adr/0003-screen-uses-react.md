---
status: accepted
---

# 画面は Vite と React と TypeScript で書く

ページの順番、削除、回転の正本は Python が持つ。画面は、その記録が変わるたびにサムネイル、大プレビュー、操作の可否をまとめて更新する。この更新を React が担う。受付は FastAPI のままである。

## 検討した選択肢

- 素の HTML / CSS / JavaScript。最初の読み込みと書き出しだけなら足る。仕様全体では、一つの操作で選択、プレビュー、進捗の表示を同時に変える。その更新を手で揃えるので不採用。
- Next.js。React に加え、Node.js のサーバーとウェブサイト向けのページ生成を持つ。正本の受付が Python と Node.js の二つになるので不採用。
- Vite と React と TypeScript。採用。組み立ては開発時に行い、できたファイルを FastAPI が配信する。実行中の受付は Python のままである。

## 結果

- 画面の更新は React が行う。ページの順番、削除、回転は React が正本にしない。
- TypeScript は、画面が Python から受け取るページ記録の形を、実行前に確認する。
- PDF の描画は PDF.js、ページの組み立ては pikepdf のままである。
- Next.js は使わない。
