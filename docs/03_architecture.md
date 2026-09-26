# ③ システム・アーキテクチャ設計

## 設計原則
1. **音源は端末から出さない。** BPM・ビート位相は端末内（Web Audio）で解析し、サーバには数値だけ送る。権利・プライバシー・帯域の三方良し。
2. **フレームは送らない。イベントを送る。** リアルタイム共有は「共有ビートグリッド ＋ GIF ID ＋ 開始拍番号」の小さなメッセージで行い、各端末が自分の時計で描画する。
3. **解析はオフラインで前計算。** GIF の周期メタデータは取り込み時に計算してインデックス化。リクエスト時は範囲検索だけ。
4. **アルゴリズムはバージョン管理。** `analyzer_version` を持ち、改良時に再解析できるようにする。

## 全体構成

```mermaid
flowchart LR
  subgraph Client["ブラウザ (Next.js / React / TS)"]
    A[音源: ローカルファイル / マイク / ストリーム] --> B[Web Audio<br/>ビート検出 essentia.js 等]
    B --> C[同期エンジン<br/>AudioContext 時計 + Canvas/WebGL 描画]
    C <--> D[WS クライアント<br/>クロック同期]
  end
  D <--> E[Realtime Gateway<br/>WebSocket / Socket.IO or Ably]
  Client --> F[API (FastAPI or Node)<br/>検索・マッチング・投票]
  F --> G[(PostgreSQL<br/>gifs / analysis / rooms / votes)]
  F --> H[(Redis<br/>キャッシュ・Pub/Sub・presence)]
  E <--> H
  subgraph Ingest["取り込み・解析（非同期ワーカー）"]
    I[GIPHY / Tenor API<br/>クローラ] --> J[重複排除 pHash] --> K[解析ワーカー<br/>gif_period.py 系]
    K --> G
    K --> L[変換: WebM/MP4 + timing JSON<br/>サムネイル]
    L --> M[(Object Storage + CDN<br/>S3 / R2)]
  end
  Client --> M
```

## コンポーネントと推奨スタック

| レイヤ | 推奨 | 代替 | 選定理由 |
|---|---|---|---|
| フロントエンド | Next.js + React + TypeScript、Tailwind | SvelteKit | エコシステム、SSR で共有ページの OGP、Vercel デプロイが楽 |
| 描画 | Canvas 2D（GIF 数枚）→ WebGL / PixiJS（GIF ウォール 20 枚以上） | | 同時再生数が増えたら GPU へ |
| GIF デコード | 配信は WebM/MP4 に事前変換 ＋ `requestVideoFrameCallback`。元 GIF が必要な場面は `ImageDecoder`（Chrome/Edge）／`gifuct-js`（他） | | GIF は重い。動画化で 5〜10 分の 1 |
| 端末内ビート検出 | `essentia.js`（RhythmExtractor）または `web-audio-beat-detector` ＋ TAP テンポ | サーバ解析（ユーザーがアップロードした曲のみ） | 音源を送らない原則 |
| API | Python **FastAPI**（解析コードと言語を揃える） | Node/NestJS | 解析ライブラリが Python |
| 解析ワーカー | Python、Celery または RQ ＋ Redis / SQS。コンテナ化、CPU スポットインスタンス | | GPU は姿勢推定・RepNet 導入時のみ |
| DB | **PostgreSQL**（＋ `pgvector`: 将来の見た目類似検索） | | 範囲検索・JSONB・全文検索が一箇所で済む |
| キャッシュ / PubSub | Redis | | presence、部屋状態、レート制限 |
| リアルタイム | 自前 WebSocket（FastAPI or Node）→ 規模が出たら **Ably / Pusher / Supabase Realtime** | LiveKit（音声も共有する場合） | 最初は自前で十分。マルチリージョンは SaaS に任せる |
| ストレージ / CDN | Cloudflare R2 ＋ CDN（エグレス無料） | S3 + CloudFront | GIF/動画配信はエグレスが支配的コスト |
| 認証 | Supabase Auth / Clerk / Auth.js | | ゲスト参加を許す（部屋 URL で入れる） |
| 監視 | OpenTelemetry + Grafana、Sentry | | 同期ズレを計測するクライアントメトリクスを送る |

## データモデル（要点）

```sql
-- 取り込んだ GIF（元データ）
gifs(id, source, source_id, source_url, width, height, n_frames, loop_duration_s,
     phash, tags text[], license_note, created_at)

-- 解析結果（バージョン付き。1 GIF に複数世代を持てる）
gif_analysis(id, gif_id, analyzer_version,
     seamless_loop bool, n_scene_cuts int, motion_energy real,
     cycle_period_s real, native_bpm real, cycles_per_loop int,
     impact_period_s real, impact_bpm real, impact_offsets_s real[],
     periodicity_score real,
     tempo_class real,          -- log2(native_bpm) の小数部（オクターブ折り畳み、後述）
     signals jsonb,             -- 64 点に圧縮した flux / vy 等（再解析なしで再マッチング可能）
     created_at)

-- 変換済み配信アセット
gif_assets(gif_id, kind /* webm|mp4|sprite|thumb */, url, frame_timing jsonb)

-- 部屋（クラブハウス）
rooms(id, slug, host_user_id, bpm real, beat_anchor_server_ms bigint, downbeat_index int, created_at)
room_events(id, room_id, server_ms, type /* gif_show|gif_hide|tempo_set|reaction */, payload jsonb)

-- フィードバック（学習データになる）
votes(id, user_id, gif_id, track_bpm real, beats_per_cycle real, rate real,
      verdict /* synced|off|half|double */, created_at)
```

## マッチングクエリ

音楽 BPM `B` に対し、GIF の周期を伸縮率 ±s（既定 15%）で `m ∈ {¼, ½, 1, 2, 4}` 拍に合わせられるものを探す。
オクターブを同一視するため、`tempo_class = frac(log2(native_bpm))` を前計算しておく。
`frac(log2(B))` との円周距離が `log2(1+s)` 以内なら、何らかの 2 のべき乗倍率で合う。

```sql
WITH q AS (SELECT (log(2, :B) - floor(log(2, :B))) AS c)
SELECT g.id, a.native_bpm, a.periodicity_score,
       least(abs(a.tempo_class - q.c), 1 - abs(a.tempo_class - q.c)) AS dist
FROM gif_analysis a JOIN gifs g ON g.id = a.gif_id, q
WHERE a.analyzer_version = :ver
  AND a.periodicity_score >= 0.5
  AND least(abs(a.tempo_class - q.c), 1 - abs(a.tempo_class - q.c)) <= log(2, 1 + :s)
ORDER BY dist ASC, a.periodicity_score DESC
LIMIT 50;
```
- `tempo_class` に B-tree インデックス。境界（0 と 1 の付近）は 2 回に分けるか `dist` 計算で吸収。
- 倍率 3 や 3/2（3 拍子系）は将来オプション。
- 最終スコアは API 側で `poc/match.py` と同じ式（伸縮の少なさ × m の好ましさ × 周期性スコア × ループが小節に揃うボーナス）。投票データで重みを学習する。
- タグ／検索語（"cat", "dance"）は Postgres 全文検索。見た目の類似は将来 `pgvector`（CLIP 埋め込み）。

## リアルタイム同期の設計

### 部屋の状態 = 共有ビートグリッド
```json
{ "bpm": 128.0, "beat_anchor_server_ms": 1727340000123, "downbeat_index": 0,
  "now_playing": [ { "gif_id": "abc", "start_beat": 512, "beats_per_cycle": 1, "rate": 1.067, "phase_offset_s": 0.53 } ] }
```
- ビート番号 k の時刻 = `anchor + k × 60000 / bpm`（サーバ時刻）。
- クライアントはサーバ時刻とのオフセットを NTP 風に推定（往復 5〜10 回、中央値）。誤差 ±10〜20ms が目安。
- GIF の切替は「次の小節頭（`start_beat` を 4 の倍数）から」とスケジュールする。ネットワーク遅延を吸収し、全員同時に切り替わる。
- 表示側は `AudioContext.currentTime` にサーバ時刻を写像して駆動。ローカル時計のドリフトは定期的に再同期。

### 音源の 3 パターン
| パターン | 音の共有 | 必要なもの |
|---|---|---|
| 同じ物理空間（パーティー、店舗） | 不要（空気） | ホストの端末でビート検出 → グリッド配信 |
| 各自が同じ曲を手元で再生 | 各自 | 曲 ID ＋ 再生位置の共有（Spotify 等の外部プレイヤー連携は API 制約が大きいので後回し） |
| ホストの音声をストリーム | WebRTC / LiveKit | 遅延 100〜300ms を加味してグリッドをずらす |

Phase 2 は 1 番目（＋ホストのローカルファイル）に絞るのが現実的。

### ホストが曲を変えたとき
1. ホスト端末でビート検出 → `tempo_set` イベント（新 bpm, anchor）。
2. サーバが部屋状態を更新、全員に配信。
3. 各端末は再マッチング結果（API から取得済みの候補）で GIF を次の小節頭から差し替え。

## 大量 GIF の前処理・インデックス化方針

### パイプライン
```
検索クローラ → 重複排除 (pHash + ループ長) → 解析 (安価パス) → [低信頼のみ] 高価パス (姿勢推定 / RepNet)
     → メタデータ保存 → 変換 (WebM/MP4/サムネ) → CDN → インデックス更新
```

### コスト試算（PoC 実測 0.1〜0.8 秒 / GIF、160px、CPU 1 コア）
| 規模 | 解析 CPU 時間 | 備考 |
|---|---|---|
| 1 万本 | 約 1 時間 | ノート PC で可 |
| 10 万本 | 約 10 時間 | スポット 8 vCPU で 1〜2 時間 |
| 100 万本 | 約 100 時間 | 数十ドル。ストレージ・エグレスの方が高い |

### 方針
- **安価パス（全件）**: `gif_period.py` 相当。周期・視覚ビート・信頼度・シーンカット・静止判定。
- **高価パス（条件付き）**: 信頼度 0.3〜0.6 の「惜しい」GIF、人気 GIF、ユーザー投稿 GIF だけに姿勢推定や TSM を適用。
- **信号の保存**: 64〜128 点に圧縮した flux / vy を JSONB で持つ。マッチング式の変更や位相の再計算を、GIF を再デコードせずに行える。
- **バージョン**: `analyzer_version` 列。改良時はバックフィルジョブで再解析し、A/B で旧版と比較してから切替。
- **品質ゲート**: `periodicity_score ≥ 0.5` かつ `motion_energy ≥ 閾値` かつ `n_scene_cuts = 0` を「同期可能」プールに。それ以外は「単発」プール（ドロップ演出用）または除外。
- **フィードバック**: 投票（synced / off / half / double）を保存。倍率の好み・閾値のチューニング、将来は学習モデルの教師データ。
- **配信形式**: 元 GIF は解析用に保持、配信は WebM（VP9）＋ MP4（H.264）＋ `frame_timing`（伸縮再生の基準）。ポスター画像も生成。

## 非機能要件（目安）
| 項目 | 目標 |
|---|---|
| 同期精度（端末間） | ±30ms（1 フレーム未満） |
| GIF 切替の一斉性 | 次の小節頭。遅延 500ms 以内のネットワークで全員同時 |
| マッチング API 応答 | p95 < 200ms（インデックス済み範囲検索） |
| 同時接続 | Phase 2: 部屋あたり 50 人、Phase 3: 数千人（SaaS リアルタイムに移行） |
| コスト | 100 万 GIF 規模でもストレージ・CDN が支配的。解析は無視できる |

## 権利・コンプライアンス（設計に影響する範囲）
- 音源: 端末内解析のみ。サーバに音声を送る機能は「ユーザー自身の音源」に限定し、保存しない。
- GIF: GIPHY / Tenor の API 規約（帰属表示、キャッシュ期間、商用利用条件）に従う。自前ホスティングする場合は各 API の再配信条件を確認。ユーザー投稿は利用規約と通報導線を用意。
- 個人情報: 投票・部屋ログは匿名 ID で保存。
