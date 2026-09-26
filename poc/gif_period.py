#!/usr/bin/env python3
"""GIFの「動きの周期」を推定する PoC 解析器。

使い方:
  python gif_period.py foo.gif                 # JSON を標準出力
  python gif_period.py foo.gif --plot out.png  # 信号のプロットも出力

出力 (主要キー):
  loop_duration_s      ループ1周の長さ（ブラウザ風にディレイ正規化済み）
  seamless_loop        末尾→先頭が滑らかに繋がるか（True なら周期は T/n に限定できる）
  cycle_period_s       動きの基本周期（位置・符号付き速度 = 「1往復」の周期）
  native_bpm           60 / cycle_period_s
  cycles_per_loop      seamless のとき、ループ1周に含まれる周期数 n
  impact_period_s      「視覚ビート」(方向転換=インパクト) の周期
  impact_bpm           60 / impact_period_s（振り子のような左右対称の動きでは native_bpm の2倍になる）
  impact_offsets_s     ループ内での視覚ビート時刻 [0, T)。最強の flux ピークを基準に impact_period 間隔で並べたもの。
                       音のビートへの位相合わせに使う（flux_peaks_s は生のピーク、デバッグ用）
  periodicity_score    0..1。周期性の確信度。0.5 未満は「リズム不明」扱いが妥当
  motion_energy        平均フロー速度 (px/s, 長辺160px換算)。極小なら静止GIF

アルゴリズム概要:
  1. Pillow で全フレームをRGB合成デコード、ディレイ<=10ms は100ms（ブラウザ仕様）
  2. 長辺160pxへ縮小、グレー化、ぼかし（ディザノイズ低減）
  3. 隣接フレーム間の密オプティカルフロー (DIS)。末尾→先頭も計算（ループ継ぎ目判定）
  4. 中央値フローを引いてカメラ揺れ除去。速度[px/s]に正規化し、均一時間軸へ再サンプル
  5. 信号: vx, vy（大きさ重み付き平均の符号付き速度）, cx, cy（変化領域の重心=位置）,
           mag（平均速さ）, flux（方向ヒストグラムの正の変化量 = 視覚インパクト。
           Davis & Agrawala, "Visual Rhythm and Beat", SIGGRAPH 2018 の directogram に基づく）
  6. 周期推定:
       seamless → 周期は T/n に限られる。循環自己相関 R(T/n) の局所最大のうち、
                  最大値の80%以上で最短周期（最大 n）を採用（音高推定の定石）
       非seamless → 正規化自己相関の「最初の落ち込み後の最初の強いピーク」
     位置/速度系 (vx,vy,cx,cy) から cycle、インパクト系 (flux,mag) から impact を別々に推定
  7. flux のピーク → 視覚ビート時刻（位相）
  補助: シーンカット（差分が中央値の4倍超）は等間隔の偽周期を作るので無害化し、信頼度を下げる。
        n=1（ループ1周=1周期）はループが長いほど信頼度を下げる（1周期だけではドリフトと区別不能）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageSequence

MAX_SIDE = 160
TARGET_FPS = 30.0
MIN_PERIOD_S = 0.25          # 240 BPM 上限
ACF_THRESHOLD = 0.4          # 自己相関ピークの絶対下限
ACF_REL = 0.8                # 最大ピークの 80% 以上なら、より短い周期を優先
ACF_DIP = 0.25               # 短ラグ側でこの値未満に一度落ちるまではピーク探索しない
SEAM_JUMP_RATIO = 2.5        # 末尾→先頭の動き量が中央値の何倍で「継ぎ目あり」とするか
FLOW_METHOD = "dis"          # "dis" (大きな動きに強い・速い) or "farneback"
CUT_RATIO = 4.0              # 差分が中央値の何倍で「シーンカット」とみなすか
N1_PRIOR_SHORT_S = 2.0       # ループ1周=1周期 (n=1) の信頼度: この長さ以下なら満点
N1_PRIOR_LONG_S = 6.0        #   ...この長さ以上なら 0.3 倍（長い1周期は単なるドリフトと区別できない）


# ---------------------------------------------------------------- decode

def browser_delay_s(ms) -> float:
    """ブラウザ実装に倣い、<=10ms のディレイは 100ms として扱う。"""
    return 0.1 if ms is None or ms <= 10 else ms / 1000.0


def decode_gif(path: str):
    im = Image.open(path)
    frames, delays = [], []
    for fr in ImageSequence.Iterator(im):
        frames.append(np.asarray(fr.convert("RGB")))
        delays.append(browser_delay_s(fr.info.get("duration", 100)))
    return frames, np.array(delays, dtype=np.float64), im.size


def to_gray_small(rgb: np.ndarray) -> np.ndarray:
    h, w = rgb.shape[:2]
    s = MAX_SIDE / max(h, w)
    if s < 1.0:
        rgb = cv2.resize(rgb, (max(1, int(round(w * s))), max(1, int(round(h * s)))), interpolation=cv2.INTER_AREA)
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return cv2.GaussianBlur(g, (0, 0), 1.0)


# ---------------------------------------------------------------- motion signals

@dataclass
class MotionSignals:
    t_mid: np.ndarray      # 各遷移の中央時刻
    dt: np.ndarray
    vx: np.ndarray         # px/s, 大きさ重み付き平均（カメラ補正後）
    vy: np.ndarray
    mag: np.ndarray        # px/s, 平均速さ
    flux: np.ndarray       # 方向ヒストグラム正変化量（視覚インパクト）
    diff: np.ndarray       # 平均絶対差分 / s（安価なベースライン）
    cx: np.ndarray         # 変化領域の重心 x（位置信号。フローが破綻する高速動作の保険）
    cy: np.ndarray
    wrap_jump_ratio: float
    cuts: list         # シーンカットと判定した遷移 index


_dis = None


def compute_flow(a: np.ndarray, b: np.ndarray, method: str = FLOW_METHOD) -> np.ndarray:
    global _dis
    if method == "dis":
        if _dis is None:
            _dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        return _dis.calc(a, b, None)
    return cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 5, 21, 3, 5, 1.2, 0)


def flow_signals(gray: list[np.ndarray], delays: np.ndarray, camera_comp: bool = True, n_dir_bins: int = 12,
                 method: str = FLOW_METHOD) -> MotionSignals:
    n = len(gray)
    vx, vy, mag, flux, diff, cx, cy, t_mid = [], [], [], [], [], [], [], []
    prev_hist = None
    t = 0.0
    h_img, w_img = gray[0].shape
    yy, xx = np.mgrid[0:h_img, 0:w_img]
    for i in range(n):                       # 遷移 i -> i+1、最後は n-1 -> 0（ラップ）
        a, b = gray[i], gray[(i + 1) % n]
        dt = float(delays[i])
        flow = compute_flow(a, b, method)
        if camera_comp:
            flow = flow - np.median(flow.reshape(-1, 2), axis=0)
        fx, fy = flow[..., 0], flow[..., 1]
        m = np.sqrt(fx * fx + fy * fy)
        wsum = m.sum() + 1e-9
        vx.append(float((m * fx).sum() / wsum) / dt)
        vy.append(float((m * fy).sum() / wsum) / dt)
        mag.append(float(m.mean()) / dt)
        d = np.abs(a.astype(np.float32) - b.astype(np.float32))
        diff.append(float(d.mean()) / dt)
        dsum = d.sum() + 1e-9
        cx.append(float((d * xx).sum() / dsum))
        cy.append(float((d * yy).sum() / dsum))
        ang = np.arctan2(fy, fx)
        hist, _ = np.histogram(ang, bins=n_dir_bins, range=(-math.pi, math.pi), weights=m)
        hist = hist / m.size                  # 画素数で正規化
        flux.append(0.0 if prev_hist is None else float(np.clip(hist - prev_hist, 0, None).sum()) / dt)
        prev_hist = hist
        t_mid.append(t + dt / 2)
        t += dt
    flux[0] = flux[-1] if n > 1 else 0.0      # 先頭は前ヒストが無いのでラップ遷移の値で埋める
    mag_arr, diff_arr = np.array(mag), np.array(diff)
    flux_arr = np.array(flux)
    # シーンカット検出: 差分が中央値の CUT_RATIO 倍を超える遷移（ラップ遷移は除く）。
    # カットは「等間隔の偽周期」を作るので、該当遷移の動き量を近傍中央値で置換して無害化する。
    med_diff = np.median(diff_arr[:-1]) + 1e-9
    cuts = [i for i in range(n - 1) if diff_arr[i] > CUT_RATIO * med_diff]
    for i in cuts:
        for arr in (mag_arr, diff_arr, flux_arr):
            arr[i] = np.median(arr[:-1])
    # 継ぎ目判定: ラップ遷移の動き量が通常遷移の中央値の何倍か（フローは大ジャンプを見逃すので差分も見る）
    wrap_ratio = max(float(mag_arr[-1] / (np.median(mag_arr[:-1]) + 1e-9)),
                     float(diff_arr[-1] / (np.median(diff_arr[:-1]) + 1e-9)))
    return MotionSignals(np.array(t_mid), delays.copy(), np.array(vx), np.array(vy), mag_arr,
                         flux_arr, diff_arr, np.array(cx), np.array(cy), wrap_ratio, cuts)


def winsorize(x: np.ndarray, lo: float = 2.0, hi: float = 98.0) -> np.ndarray:
    """シーンカット等の単発スパイクが自己相関を支配しないよう、上下パーセンタイルでクリップ。"""
    a, b = np.percentile(x, [lo, hi])
    return np.clip(x, a, b)


def resample_periodic(t: np.ndarray, y: np.ndarray, T: float, fs: float) -> np.ndarray:
    """[0,T) の均一グリッドへ線形補間（周期境界を跨いで補間）。"""
    n = max(8, int(round(T * fs)))
    grid = np.arange(n) / fs
    tt = np.concatenate([t - T, t, t + T])
    yy = np.concatenate([y, y, y])
    return np.interp(grid, tt, yy)


# ---------------------------------------------------------------- period estimation

def _dipped_before(r_fine: np.ndarray, lag: float) -> bool:
    """ラグ 1..lag-1 のどこかで自己相関が ACF_DIP 未満に落ちたか（ラグ0の主ローブを除外するため）。"""
    upto = int(math.floor(lag))
    return upto >= 2 and bool((r_fine[1:upto] < ACF_DIP).any())


def _choose_peak(cands: list[tuple[float, float, bool, bool]]):
    """cands: (lag, r, is_local_max, dipped) を短ラグ→長ラグ順で。
    条件を満たすピークのうち、最大値の ACF_REL 倍以上で最短ラグのものの index を返す。"""
    ok = [i for i, (lag, r, lm, dp) in enumerate(cands) if lm and dp and r >= ACF_THRESHOLD]
    if not ok:
        return None
    rmax = max(cands[i][1] for i in ok)
    for i in ok:
        if cands[i][1] >= ACF_REL * rmax:
            return i
    return None


def circular_period_seamless(x: np.ndarray, T: float, min_period: float):
    """seamless ループ: 周期は T/n (n=1,2,...) に限られる。
    パワースペクトルから循環自己相関 R(T/n) を厳密評価（非整数ラグ可）し、
    ラグ0の主ローブを抜けた後の局所最大のうち、最大値の ACF_REL 倍以上で最短周期（最大 n）を採用。
    該当なしなら n=1（ループ1周が1周期）。
    スコア: n>=2 は調和エネルギー比 h(n) と R の平均、n=1 は低周波集中度。
    戻り値: (n, score, {n: R})"""
    x = x - x.mean()
    N = len(x)
    P = np.abs(np.fft.rfft(x)) ** 2
    P[0] = 0.0
    total = P.sum() + 1e-12
    k = np.arange(len(P))
    r_fine = np.fft.irfft(P, n=N) / (P.sum() * 2 / N + 1e-12)  # 整数ラグの循環自己相関（正規化）
    r_fine = r_fine / (r_fine[0] + 1e-12)
    n_max = min(max(1, int(math.floor(T / min_period + 1e-6))), N // 2)
    ns = list(range(n_max, 1, -1))            # 短ラグ(大n) → 長ラグ(小n)
    R = {n: float((P * np.cos(2 * math.pi * k * (N / n) / N)).sum() / total) for n in range(2, n_max + 2)}
    cands = []
    for n in ns:
        r = R[n]
        lm = r >= R.get(n + 1, -1.0) and r >= R.get(n - 1, -1.0)   # n=2 は最長ラグ側の端なので右隣なし
        cands.append((N / n, r, lm, _dipped_before(r_fine, N / n)))
    i = _choose_peak(cands)
    rs = {n: R[n] for n in ns}
    if i is None:
        conc = P[1:4].sum() / total
        return 1, float(conc), rs
    n = ns[i]
    h = P[n::n].sum() / total                 # 基本波 + 全高調波のエネルギー比
    return n, float(0.5 * R[n] + 0.5 * h), rs


def autocorr_period(x: np.ndarray, fs: float, min_period: float):
    """非seamless: 正規化自己相関（重なり区間で正規化）の、主ローブを抜けた後の最初の強いピーク。
    戻り値 (period_s or None, score)。"""
    x = x - x.mean()
    N = len(x)
    lag_min = max(2, int(round(min_period * fs)))
    lag_max = N // 2
    if lag_max <= lag_min + 2:
        return None, 0.0
    r = np.ones(lag_max + 1)
    for lag in range(1, lag_max + 1):
        a, b = x[:-lag], x[lag:]
        den = math.sqrt((a * a).sum() * (b * b).sum()) + 1e-12
        r[lag] = (a * b).sum() / den
    cands = []
    for lag in range(lag_min, lag_max + 1):
        lm = r[lag] >= r[lag - 1] and (lag == lag_max or r[lag] >= r[lag + 1])
        cands.append((float(lag), float(r[lag]), lm, _dipped_before(r, lag)))
    i = _choose_peak(cands)
    if i is None:
        return None, 0.0
    lag = lag_min + i
    off = 0.0
    if 0 < lag < lag_max:                     # 放物線補間
        y0, y1, y2 = r[lag - 1], r[lag], r[lag + 1]
        denom = (y0 - 2 * y1 + y2)
        off = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-12 else 0.0
    return float((lag + off) / fs), float(np.clip(r[lag], 0, 1))


def pick_peaks(x: np.ndarray, fs: float, min_sep_s: float, circular: bool) -> list[int]:
    """単純ピーク検出: 局所最大かつ (mean + 0.5*std) 以上、最小間隔 min_sep_s。"""
    N = len(x)
    thr = x.mean() + 0.5 * x.std()
    sep = max(1, int(round(min_sep_s * fs)))
    cand = []
    for i in range(N):
        if circular:
            l, r = x[(i - 1) % N], x[(i + 1) % N]
        elif i == 0 or i == N - 1:
            continue
        else:
            l, r = x[i - 1], x[i + 1]
        if x[i] >= thr and x[i] >= l and x[i] > r:
            cand.append(i)
    cand.sort(key=lambda i: -x[i])
    chosen = []
    for i in cand:
        if all((min(abs(i - j), N - abs(i - j)) if circular else abs(i - j)) >= sep for j in chosen):
            chosen.append(i)
    return sorted(chosen)


# ---------------------------------------------------------------- main analysis

def analyze(path: str, target_fps: float = TARGET_FPS, camera_comp: bool = True, return_signals: bool = False,
            method: str = FLOW_METHOD) -> dict:
    frames, delays, size = decode_gif(path)
    n_frames = len(frames)
    T = float(delays.sum())
    if n_frames < 4 or T <= 0:
        return {"path": path, "error": "too few frames", "n_frames": n_frames}
    gray = [to_gray_small(f) for f in frames]
    sig = flow_signals(gray, delays, camera_comp=camera_comp, method=method)
    seamless = sig.wrap_jump_ratio < SEAM_JUMP_RATIO

    native_fps = n_frames / T
    fs = min(target_fps, max(10.0, 2.0 * native_fps))     # 元fpsの2倍まで（それ以上は無意味）
    signals = {name: winsorize(resample_periodic(sig.t_mid, getattr(sig, name), T, fs))
               for name in ("vx", "vy", "mag", "flux", "diff", "cx", "cy")}
    N = len(signals["vx"])
    motion_energy = float(sig.mag[:-1].mean()) if n_frames > 1 else 0.0
    min_period = max(MIN_PERIOD_S, 2.0 / native_fps)      # 元fpsのナイキスト制限

    result = {
        "path": path, "width": size[0], "height": size[1], "n_frames": n_frames,
        "native_fps": round(native_fps, 2), "loop_duration_s": round(T, 4),
        "seamless_loop": bool(seamless), "wrap_jump_ratio": round(sig.wrap_jump_ratio, 2),
        "motion_energy": round(motion_energy, 2), "analysis_fps": fs,
        "n_scene_cuts": len(sig.cuts),
    }
    if return_signals:
        result["_signals"] = {k: v.tolist() for k, v in signals.items()}
        result["_signals"]["t"] = (np.arange(N) / fs).tolist()
    if motion_energy < 1.0:
        result.update({"periodicity_score": 0.0, "note": "static or nearly static"})
        return result

    def estimate(names, groups=()):
        """複数信号から最も周期性の高いものを選ぶ。戻り値 (period, score, n, name)
        groups: 同じ単位の信号ペア。ペア内で振幅(std)が相手の10%未満の信号は候補から外す。"""
        drop = set()
        for g in groups:
            stds = {nm: signals[nm].std() for nm in g}
            mx = max(stds.values())
            drop |= {nm for nm, sd in stds.items() if sd < 0.1 * mx}
        best = (None, -1.0, None, None)
        for name in names:
            x = signals[name]
            if name in drop or x.std() < 1e-9:
                continue
            if seamless:
                n, score, _ = circular_period_seamless(x, T, min_period)
                if n == 1:                                  # 1周期しか観測できない → ループ長が長いほど信用しない
                    score *= float(np.interp(T, [N1_PRIOR_SHORT_S, N1_PRIOR_LONG_S], [1.0, 0.3]))
                period = T / n
            else:
                period, score = autocorr_period(x, fs, min_period)
                n = None
                if period is None:
                    continue
            if score > best[1]:
                best = (period, score, n, name)
        return best

    cyc_period, cyc_score, cyc_n, cyc_name = estimate(["vy", "vx", "cy", "cx"], groups=(("vx", "vy"), ("cx", "cy")))
    imp_period, imp_score, imp_n, imp_name = estimate(["flux", "mag"])

    impacts, flux_peaks = [], []                            # 視覚ビート（flux のピーク）
    if imp_period:
        idx = pick_peaks(signals["flux"], fs, min_sep_s=0.6 * imp_period, circular=seamless)
        flux_peaks = [round(i / fs, 3) for i in idx]
        if idx:
            # 最強ピークを基準(anchor)にし、impact_period 間隔のグリッドとして並べる。
            # 弱い反転（バウンドの頂点など）を拾って位相が半周期ずれるのを防ぐ。
            anchor = max(idx, key=lambda i: signals["flux"][i]) / fs
            k0 = int(math.floor(anchor / imp_period))
            impacts = sorted(round((anchor - k0 * imp_period) + k * imp_period, 3)
                             for k in range(int(math.ceil(T / imp_period)))
                             if (anchor - k0 * imp_period) + k * imp_period < T)

    score = float(np.clip(max(cyc_score, imp_score), 0, 1))
    if not seamless and native_fps < 8:
        score *= 0.8                                        # 低fpsクリップは信頼度を下げる
    if sig.cuts:
        score *= 0.7                                        # 複数クリップの寄せ集めは信頼度を下げる
    result.update({
        "cycle_period_s": round(cyc_period, 4) if cyc_period else None,
        "native_bpm": round(60.0 / cyc_period, 1) if cyc_period else None,
        "cycles_per_loop": cyc_n,
        "cycle_signal": cyc_name,
        "impact_period_s": round(imp_period, 4) if imp_period else None,
        "impact_bpm": round(60.0 / imp_period, 1) if imp_period else None,
        "impacts_per_loop": imp_n,
        "impact_signal": imp_name,
        "impact_offsets_s": impacts,
        "flux_peaks_s": flux_peaks,
        "periodicity_score": round(score, 3),
        "cycle_score": round(float(max(cyc_score, 0)), 3),
        "impact_score": round(float(max(imp_score, 0)), 3),
    })
    return result


def plot(result: dict, out_png: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    s = result["_signals"]
    t = np.array(s["t"])
    fig, axes = plt.subplots(3, 1, figsize=(10, 7.5), sharex=True)
    axes[0].plot(t, s["vy"], label="vy"); axes[0].plot(t, s["vx"], label="vx")
    ax0b = axes[0].twinx()
    ax0b.plot(t, s["cy"], label="cy (centroid)", color="C2", alpha=.5); ax0b.plot(t, s["cx"], label="cx", color="C4", alpha=.5)
    axes[0].legend(loc="upper left"); ax0b.legend(loc="upper right"); axes[0].set_ylabel("px/s")
    axes[1].plot(t, s["mag"], label="mag"); axes[1].plot(t, s["diff"], label="diff", alpha=.6); axes[1].legend()
    axes[2].plot(t, s["flux"], label="flux (visual impact)", color="C3"); axes[2].legend(); axes[2].set_xlabel("s")
    for o in result.get("impact_offsets_s", []):
        axes[2].axvline(o, color="k", alpha=.3)
    if result.get("cycle_period_s"):
        p = result["cycle_period_s"]
        for k in range(int(result["loop_duration_s"] / p) + 1):
            axes[0].axvline(k * p, color="gray", alpha=.25, ls="--")
    fig.suptitle(f"{result['path']}\nseamless={result['seamless_loop']} native_bpm={result.get('native_bpm')} "
                 f"(n={result.get('cycles_per_loop')}, {result.get('cycle_signal')}) impact_bpm={result.get('impact_bpm')} "
                 f"({result.get('impact_signal')}) score={result.get('periodicity_score')}", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_png, dpi=110)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gif")
    ap.add_argument("--plot", help="信号のプロットPNGを出力")
    ap.add_argument("--no-camera-comp", action="store_true", help="カメラ揺れ補正を無効化")
    ap.add_argument("--flow", default=FLOW_METHOD, choices=["dis", "farneback"])
    a = ap.parse_args()
    r = analyze(a.gif, camera_comp=not a.no_camera_comp, return_signals=bool(a.plot), method=a.flow)
    if a.plot:
        plot(r, a.plot)
    r.pop("_signals", None)
    json.dump(r, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
