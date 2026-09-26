#!/usr/bin/env python3
"""音楽BPM と GIF解析結果(gif_period.py の JSON) を照合し、同期再生パラメータを出す PoC。

使い方:
  python match.py --music-bpm 128 a.json b.json ...   # スコア順に表示
  python match.py --music-bpm 128 --gif-bpm 60         # 数値だけで試す

考え方:
  GIFの周期 p [s] を、再生速度 rate 倍で伸縮して p' = p / rate にし、
  p' = m × (音楽の拍間隔 b) となる m ∈ {1/4, 1/2, 1, 2, 4}（1周期あたりの拍数）を探す。
  rate が 1 ± max_stretch に収まる候補だけ採用。m=1 を最優先、オクターブ違い(2, 1/2)は少し減点。
  GIF の cycle(往復) と impact(方向転換) の2つの周期を両方候補にする（振り子は impact が cycle の2倍）。
  最終スコア = 伸縮の少なさ × m の好ましさ × GIF の周期性スコア (+ ループが小節に揃うボーナス)。

再生側(sync_player.html)へ渡す値:
  rate            GIF の再生速度倍率（1.0 = 元速度）
  beats_per_cycle m
  phase_offset_s  GIF ループ内で「拍に合わせるべき時刻」（視覚ビート）。元GIFの時間軸で。
"""
import argparse
import json
import math
import sys

MULTIPLIERS = (1.0, 2.0, 0.5, 4.0, 0.25)      # 1周期あたりの拍数（優先順）
MULT_PREF = {1.0: 1.0, 2.0: 0.9, 0.5: 0.9, 4.0: 0.75, 0.25: 0.75}


def candidates(music_bpm: float, period_s: float, max_stretch: float = 0.15):
    b = 60.0 / music_bpm
    out = []
    for m in MULTIPLIERS:
        target = m * b                            # 伸縮後にこの周期にしたい
        rate = period_s / target                  # >1: 速く再生
        dev = abs(math.log(rate))
        lim = math.log(1 + max_stretch)
        if dev <= lim:
            out.append({"beats_per_cycle": m, "rate": round(rate, 4), "stretch_pct": round((rate - 1) * 100, 1),
                        "fit": round((1 - dev / lim) * MULT_PREF[m], 3)})
    return out


def match(music_bpm: float, gif: dict, max_stretch: float = 0.15) -> dict:
    """gif: gif_period.py の出力 dict（最低限 cycle_period_s / impact_period_s / periodicity_score）"""
    pscore = float(gif.get("periodicity_score") or 0.0)
    T = float(gif.get("loop_duration_s") or 0.0)
    best = None
    for kind in ("cycle", "impact"):
        p = gif.get(f"{kind}_period_s")
        if not p:
            continue
        for c in candidates(music_bpm, float(p), max_stretch):
            score = c["fit"] * (0.5 + 0.5 * pscore)
            # ループ全体が拍の整数倍(できれば 4 の倍数=小節)に揃うとさらに気持ちいい
            beats_per_loop = (T / c["rate"]) / (60.0 / music_bpm) if T else 0.0
            if beats_per_loop:
                nearest = round(beats_per_loop)
                if nearest >= 1 and abs(beats_per_loop - nearest) < 0.08:
                    score *= 1.05 if nearest % 4 == 0 else 1.02
            cand = dict(c, basis=kind, score=round(score, 3), beats_per_loop=round(beats_per_loop, 2))
            if best is None or cand["score"] > best["score"]:
                best = cand
    offsets = gif.get("impact_offsets_s") or []
    result = {"path": gif.get("path"), "music_bpm": music_bpm, "periodicity_score": pscore,
              "phase_offset_s": offsets[0] if offsets else 0.0}
    if best is None:
        result.update({"match": False, "score": 0.0, "reason": f"no multiplier within ±{int(max_stretch*100)}% stretch"})
    else:
        result.update({"match": True, **best})
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--music-bpm", type=float, required=True)
    ap.add_argument("--max-stretch", type=float, default=0.15, help="許容する再生速度の変化率 (0.15 = ±15%%)")
    ap.add_argument("--gif-bpm", type=float, help="GIFのnative BPMを直接指定（JSONなしで試す）")
    ap.add_argument("gif_json", nargs="*")
    a = ap.parse_args()
    results = []
    if a.gif_bpm:
        results.append(match(a.music_bpm, {"path": "(manual)", "cycle_period_s": 60.0 / a.gif_bpm, "periodicity_score": 1.0}, a.max_stretch))
    for p in a.gif_json:
        with open(p) as f:
            results.append(match(a.music_bpm, json.load(f), a.max_stretch))
    results.sort(key=lambda r: -r["score"])
    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
