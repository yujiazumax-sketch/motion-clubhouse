# PoC コード

企画の核心「GIF の動きの周期を推定し、音楽のビートに同期させる」を最小構成で検証するスクリプト群。
設計上の位置づけは [docs/02_poc_plan.md](../docs/02_poc_plan.md) を参照。

## セットアップ

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r poc/requirements.txt
```
Python 3.10 以上。GPU 不要。mp3 を読む場合は ffmpeg があると確実。

## スクリプト

| ファイル | 役割 | 入出力 |
|---|---|---|
| `make_synthetic.py OUT_DIR` | BPM 既知の合成 GIF を生成（バウンド、揺れ＋ノイズ付きバウンド、ヘッドバング、振り子、非周期） | GIF ファイル。名前の `_4b` はループ 1 周の拍数 |
| `gif_period.py X.gif [--plot X.png]` | GIF の動きの周期・視覚ビート・信頼度を推定 | JSON（下記） |
| `evaluate.py DIR [--tol 0.04] [--flow dis\|farneback]` | ディレクトリ一括評価。ファイル名の拍数 × 60 / 実ループ長 を正解にする | 表と正解率 |
| `audio_beats.py track.wav` / `--synth 128 out.wav` | 曲の BPM・ビート時刻・グリッド位相（librosa）。`--synth` で検証用ドラムを生成 | JSON |
| `match.py --music-bpm B a.json b.json ...` | 曲 BPM × GIF 解析結果 → 倍率・再生速度・位相・スコア | JSON（スコア順） |
| `web/sync_player.html` | ブラウザで音と GIF を同期再生。デモ素材入り。TAP テンポ、同期／伸縮のみ／元速度の A/B、ズレ表示 | デモは全ブラウザ。自分の GIF は Chrome / Edge（`ImageDecoder` 使用） |

## `gif_period.py` の出力

```json
{
  "loop_duration_s": 2.0,        "seamless_loop": true,   "n_scene_cuts": 0,
  "cycle_period_s": 0.5,         "native_bpm": 120.0,     "cycles_per_loop": 4,  "cycle_signal": "vy",
  "impact_period_s": 0.5,        "impact_bpm": 120.0,     "impact_offsets_s": [0.033, 0.533, 1.033, 1.533],
  "periodicity_score": 1.0,      "motion_energy": 42.1
}
```
- `cycle_*`: 位置・符号付き速度から見た「1 往復」の周期。
- `impact_*`: 方向転換（視覚ビート）の周期。左右対称の振りでは cycle の 2 倍になる。
- `impact_offsets_s`: ループ内で拍に合わせるべき瞬間。`sync_player.html` の位相オフセットに使う。
- `periodicity_score`: 0.5 未満は「リズム不明」扱いが妥当。
- 周期の手法は seamless なら「循環自己相関を T/n の点で評価」、そうでなければ通常の自己相関。詳細は docstring。

## 結果（合成 24 本、`evaluate.py`）

| 指標 | 結果 |
|---|---|
| native_bpm オクターブ内正解 | 24 / 24 |
| native_bpm 完全一致（±4%） | 17 / 24（外れは振り子 6 本＝設計上 1 往復 2 拍、ノイズ付き 174 BPM 1 本） |
| impact_bpm 完全一致 | 20 / 24 |
| 非周期（ランダムウォーク） | スコア 0（棄却） |
| 処理時間 | 0.1〜0.8 秒 / GIF（CPU 1 コア） |

実 GIF（GitHub から入手できた範囲）: ニュートンのゆりかごは往復 73 BPM ／ 衝突 146 BPM（物理的に正しい）。
4 クリップ連結の姿勢推定デモは、シーンカット検出を入れる前は「1.63 秒周期」と誤認し、入れた後は正しく「周期なし」。

## 同期プレイヤーの使い方

開くとデモ素材（120 BPM で跳ねるボールの GIF と、128 BPM の合成ドラム）が入った状態で始まる。ファイルなしで同期の効果を確かめられる。

1. `web/sync_player.html` をブラウザで開く（ローカルファイルのままで可）。
2. ▶ 再生を押し、モード（同期／伸縮のみ／元速度）を切り替えて見比べる。黄色のランプが音の拍、水色が動きの拍。「ズレ」に動きの拍と音の拍の差が ms で出る。
3. 自分の素材を試すときは、音楽ファイルと GIF を選ぶ。GIF の読み込みは Chrome / Edge のみ（ImageDecoder を使用）。
4. 曲の BPM を入力するか、再生中に TAP ボタン（またはスペースキー）を拍に合わせて叩く。BPM と拍の位置が入る。
5. `gif_period.py` の JSON を「解析結果（JSON）を貼る」に貼ると、周期と位相オフセットが入る。
6. 「デモ素材に戻す」で最初の状態に戻る。

再生の式: GIF 内時刻 g(τ) = (τ − 拍位相) × rate + 位相オフセット（mod ループ長）、rate = 周期 / (拍数 × 拍間隔)。

## 既知の限界

- 全画面集計。複数被写体や強い背景の動きには弱い。
- 5 fps 以下の GIF は実質解析不能（150 BPM 以上を扱えない、位相が粗い）。
- 「1 往復 = 1 拍」か「2 拍」かは GIF 単体では決まらない。両方出してマッチングと投票で解決する。
- 位相精度は ±1 フレーム。
- `sync_player.html` で自分の GIF を読み込めるのは Chrome / Edge のみ（Safari / Firefox は `gifuct-js` 等でデコードするか、配信を WebM 化する）。デモ素材はどのブラウザでも動く。
