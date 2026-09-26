# Motion Clubhouse

音楽のビートと GIF アニメーションの「動きの周期」を解析・同期させ、曲にぴったり合う GIF を自動で選んで踊らせる Web プラットフォーム（企画・設計・PoC 段階）。

## ドキュメント

| ファイル | 内容 |
|---|---|
| [docs/01_feasibility.md](docs/01_feasibility.md) | ① 技術的実現可能性の評価。ビート検出・GIF 周期推定の手法比較、推奨手法、PoC の測定結果、エッジケースと対策 |
| [docs/02_poc_plan.md](docs/02_poc_plan.md) | ② 最小限の検証プラン。仮説の順序、手動フィールテスト、データ収集と手ラベル、評価指標、ライブラリ |
| [docs/03_architecture.md](docs/03_architecture.md) | ③ システム・アーキテクチャ。推奨スタック、データモデル、マッチングクエリ、リアルタイム同期、大量 GIF の前処理方針 |
| [docs/04_roadmap.md](docs/04_roadmap.md) | ④ 開発ロードマップ。Phase 0〜3 のタスク・成果物・Go/No-Go 基準、リスク、未決事項 |
| [poc/README.md](poc/README.md) | PoC コードの使い方と結果 |

## PoC クイックスタート

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r poc/requirements.txt

# 1. BPM 既知の合成 GIF を作って解析器を評価
python poc/make_synthetic.py /tmp/synth
python poc/evaluate.py /tmp/synth

# 2. 手持ちの GIF を解析（周期・視覚ビート・信頼度）
python poc/gif_period.py my.gif --plot my.png > my.json

# 3. 曲のビートを検出（--synth で検証用ドラムも作れる）
python poc/audio_beats.py track.mp3

# 4. 曲 BPM と GIF を照合し、再生速度・位相を得る
python poc/match.py --music-bpm 128 my.json

# 5. ブラウザで同期再生（Chrome / Edge）
#    poc/web/sync_player.html を開き、曲と GIF を読み込み、my.json を貼り付けて再生
```

## 現状（2026-09）

- 合成 GIF 24 本で、動きの周期をオクターブ内で 24 / 24 正解（完全一致 17 / 24。差分はほぼ「1 往復 = 2 拍」の振り子）
- 合成ドラムループの BPM を ±0.3% で推定（174 BPM は 87 と半分に出る＝既知のオクターブ誤り）
- Chrome で音楽とGIFを同期再生する PoC ページが動作（Playwright で自動検証済み）
- 実世界のダンス GIF での検証は未実施（この環境から GIPHY / Tenor に到達できないため）。Phase 1 の最初の仕事
