#!/usr/bin/env python3
"""ディレクトリ内の GIF を一括解析し、正解BPMと比較する。

正解BPM = ファイル名の拍数 (_4b) × 60 / 実測ループ長。拍数が無いファイルは名目BPM (120bpm) をそのまま使う。

判定:
  exact  : |推定/正解 - 1| <= tol
  octave : 推定が正解の 1/2, 2, 1/4, 4 倍のいずれかに tol 内で一致（マッチングでは許容）
  miss   : それ以外
"""
import argparse
import glob
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gif_period import analyze  # noqa: E402


def classify(est, truth, tol):
    if est is None or truth is None:
        return "n/a"
    for mult, label in ((1, "exact"), (2, "octave"), (0.5, "octave"), (4, "octave"), (0.25, "octave"), (3, "x3"), (1 / 3, "x3")):
        if abs(est / (truth * mult) - 1) <= tol:
            return label
    return "miss"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--tol", type=float, default=0.04)
    ap.add_argument("--flow", default="dis", choices=["dis", "farneback"])
    a = ap.parse_args()
    rows = []
    for p in sorted(glob.glob(os.path.join(a.dir, "*.gif"))):
        m = re.search(r"(\d+)bpm", os.path.basename(p))
        mb = re.search(r"_(\d+)b[_.]", os.path.basename(p))
        t0 = time.time()
        r = analyze(p, method=a.flow)
        dt = time.time() - t0
        truth = None
        if mb and r.get("loop_duration_s"):
            truth = round(int(mb.group(1)) * 60.0 / r["loop_duration_s"], 1)
        elif m:
            truth = float(m.group(1))
        rows.append((os.path.basename(p), truth, r.get("native_bpm"), r.get("impact_bpm"), r.get("periodicity_score"),
                     r.get("seamless_loop"), r.get("cycles_per_loop"), r.get("cycle_signal"), dt,
                     classify(r.get("native_bpm"), truth, a.tol), classify(r.get("impact_bpm"), truth, a.tol)))
    print(f"{'file':32} {'truth':>6} {'native':>7} {'impact':>7} {'score':>6} {'seam':>5} {'n':>3} {'sig':>4} {'sec':>5}  native/impact")
    for row in rows:
        f, tr, nb, ib, sc, sm, n, sg, dt, c1, c2 = row
        print(f"{f:32} {tr if tr else '-':>6} {nb if nb else '-':>7} {ib if ib else '-':>7} {sc if sc is not None else '-':>6} "
              f"{str(sm):>5} {n if n else '-':>3} {sg or '-':>4} {dt:5.2f}  {c1}/{c2}")
    judged = [r for r in rows if r[1]]
    if judged:
        ex = sum(1 for r in judged if r[9] == "exact")
        oc = sum(1 for r in judged if r[9] in ("exact", "octave"))
        print(f"\nnative_bpm: exact {ex}/{len(judged)}  within-octave {oc}/{len(judged)}")
        ex2 = sum(1 for r in judged if r[10] == "exact")
        oc2 = sum(1 for r in judged if r[10] in ("exact", "octave"))
        print(f"impact_bpm: exact {ex2}/{len(judged)}  within-octave {oc2}/{len(judged)}")


if __name__ == "__main__":
    main()
