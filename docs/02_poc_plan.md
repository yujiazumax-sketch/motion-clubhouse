# ② 最小限の検証プラン（PoC / プロトタイプ設計）

## 検証すべき仮説（順番が重要）

| # | 仮説 | 検証方法 | 判定基準 |
|---|---|---|---|
| H1 | **周期を合わせた GIF は、合わせていない GIF より「盛り上がる」と感じる** | 手ラベル GIF ＋ 同期プレイヤーで A/B 体感テスト | 被験者 5〜10 人の過半数が「同期あり」を選ぶ |
| H2 | GIF の周期と視覚ビートを自動推定できる | 手ラベル 100 本に対する解析器の正解率 | オクターブ内正解 ≥ 70%、位相誤差 ≤ 1/8 拍（信頼度 ≥ 0.5 のもの） |
| H3 | 実世界の GIF のうち十分な割合が使える | GIPHY / Tenor の "dance" 系 500 本の信頼度分布 | 信頼度 ≥ 0.5 が 40% 以上（少なければ人力／投票で補う設計に倒す） |

**H1 が最重要で、アルゴリズムなしで検証できる。** 最初にやること。

## Step 0: 手動フィールテスト（1〜2 日、アルゴリズム不要）

1. ダンス系 GIF を 10 本選ぶ（GIPHY: "dancing cat", "head bob", "bounce", "dance loop"）。
2. 目視で「ループ 1 周に何回バウンドするか」を数える → native BPM = 回数 × 60 / ループ秒。
3. BPM 既知の曲を 3 曲用意（例: 90 / 120 / 128 / 140 BPM）。
4. `poc/web/sync_player.html` を Chrome で開き、曲と GIF を読み込む。BPM は手入力、拍位相は TAP ボタン（スペースキー）で取る。
5. モードを「同期」「伸縮のみ」「元速度」で切り替え、同じ GIF を見比べる。
6. 被験者に「どれが一番気持ちいいか」を答えてもらう。

これで H1 が否定されたら、アルゴリズム開発に進む前に企画を見直す。

## Step 1: アルゴリズム検証（1 週間）

### データ収集
- GIPHY API（`/v1/gifs/search`, 無料キーで可）／Tenor API v2 から検索語ごとに 20〜50 本、計 100〜500 本を取得。
- 検索語: `dancing`, `dance loop`, `head bob`, `bouncing`, `cat dance`, `vibing`, `nodding`, `jumping`, `metronome`。
- 対照群: `reaction`, `facepalm`, `explosion`（非周期）を 50 本。
- 保存: `data/raw/<id>.gif` ＋ `data/meta.csv`（id, url, 検索語, 幅, 高さ, フレーム数, ループ秒）。

### 手ラベル（100 本、2〜3 時間）
- 1 本 1 分。項目: `beats_per_loop`（ループ 1 周の拍数、整数）、`is_periodic`（Y/N）、`impact_frame`（最初の「ダウン」のフレーム番号）、`subject`（human / animal / cartoon / other）。
- 簡易ラベルツール: `sync_player.html` の元速度モードでフレーム番号を表示しているので、そのまま使える。

### 評価
```
python poc/evaluate.py data/labeled      # ファイル名に _<N>b を含めれば自動判定
```
- 指標: オクターブ内正解率、完全一致率、位相誤差（推定 impact_offset と正解フレームの差、拍の何分の 1 か）、非周期の棄却率（false positive 率）、被写体種別ごとの内訳。
- 失敗例は `--plot` で信号を可視化し、原因を分類（フロー破綻／カメラ揺れ／複数被写体／カット）。

### 改善候補（結果を見てから）
- 姿勢推定（MediaPipe Pose）で人物 GIF の腰 y 座標を信号に追加。
- 前景マスク（フレーム差分の時間中央値で背景推定）でカメラ揺れ・背景の影響を除去。
- 空間分割（左右 2 分割）で複数被写体に対応。
- 時間自己相似行列（ImageNet 特徴量）で見た目ベースの周期を補助。

## Step 2: 自動マッチングの体感評価（1 週間）

1. 曲 3 曲 × GIF 500 本に `match.py` を回し、曲ごとに上位 20 本を得る。
2. 上位 20 本と、ランダム 20 本をシャッフルして提示（同期モードで再生）。
3. 被験者 5〜10 人が各 GIF を「合ってる／合ってない」で評価。
4. 成功基準: マッチ群の「合ってる」率がランダム群より 30 ポイント以上高い。
5. 併せて「伸縮率 ±15% は違和感ないか」「半拍／倍拍のどちらが好まれるか」をログから集計 → マッチングの重みに反映。

## 実装の流れ（PoC コード。すべて `poc/` に実装済み）

```
poc/
  make_synthetic.py   BPM 既知の合成 GIF を生成（バウンド／揺れ／ヘッドバング／振り子／非周期）
  gif_period.py       GIF の周期・視覚ビート・信頼度を推定 → JSON
  evaluate.py         ディレクトリ一括評価（ファイル名の正解と比較）
  audio_beats.py      楽曲の BPM・ビート時刻・位相（librosa）→ JSON。--synth で検証用ドラム生成
  match.py            音楽 BPM × GIF 解析結果 → 倍率・再生速度・位相・スコア
  web/sync_player.html  ブラウザで音とGIFを同期再生（Chrome/Edge）。TAP テンポ、A/B モード付き
```

パイプライン:
```
曲 ──audio_beats.py──▶ {bpm, beat_times, grid_offset}
                                                  ├──match.py──▶ {rate, beats_per_cycle, phase_offset, score}
GIF ──gif_period.py──▶ {cycle_period, impact_offsets, periodicity_score}   │
                                                                          ▼
                                                    sync_player.html（JSON を貼るだけで再生）
```

## ライブラリと役割

| ライブラリ | 用途 | 備考 |
|---|---|---|
| Pillow | GIF デコード（フレーム合成、ディレイ取得） | `ImageSequence` |
| OpenCV (`opencv-python-headless`) | DIS / Farneback オプティカルフロー、縮小、ぼかし | GPU 不要 |
| NumPy / SciPy | FFT、自己相関、再サンプル | |
| librosa + soundfile | ビート検出、テンポグラム | mp3 は ffmpeg / audioread 経由 |
| matplotlib | 信号の可視化（失敗解析） | |
| MediaPipe（任意） | 人物 GIF の姿勢推定 | Phase 1 後半 |
| madmom / Beat This!（任意） | ダウンビート | Phase 2 |
| ブラウザ: WebCodecs `ImageDecoder`, Web Audio API, Canvas | 同期再生 | Safari/Firefox は `gifuct-js` で代替 |

## 計測テンプレート（Step 1 の結果をここに埋める）

| 区分 | 本数 | 信頼度 ≥ 0.5 | オクターブ内正解 | 完全一致 | 位相誤差中央値 |
|---|---|---|---|---|---|
| human / dance | | | | | |
| animal | | | | | |
| cartoon | | | | | |
| 非周期（対照） | | （false positive 率） | – | – | – |

## PoC の既知の限界（設計判断に関わるもの）
- 全画面集計なので、複数の被写体や強い背景の動きには弱い。
- 5 fps 以下の GIF は事実上解析不能（ナイキスト限界で 150 BPM 以上を扱えず、位相も粗い）。
- 「1 往復 = 1 拍」か「= 2 拍」かは GIF 単体からは決められない。往復周期と視覚ビート周期の両方を出し、マッチングと投票で解決する。
- 位相精度は ±1 フレーム。10 fps GIF なら ±50ms。
