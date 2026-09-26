#!/usr/bin/env python3
"""楽曲のテンポ(BPM)とビート時刻を推定する PoC（librosa）。

使い方:
  python audio_beats.py track.mp3            # JSON を標準出力
  python audio_beats.py --synth 128 out.wav  # 検証用の合成ドラムループ(128BPM, 20秒)を生成して解析

出力:
  bpm             推定テンポ（ビート列の線形回帰から算出。beat_track の tempo より安定）
  bpm_raw         librosa.beat.beat_track が返した tempo
  beat_period_s   60 / bpm
  beat_times      ビート時刻 [s]
  grid_offset_s   等間隔ビートグリッドの位相（最初のビートの時刻 mod 周期）
  steadiness      0..1。ビート間隔のばらつきの少なさ（1 = 完全に一定）。低いとテンポ変動あり
  tempo_candidates  テンポグラム由来の候補（オクターブ誤りの確認用）

注意:
  - librosa は「ビート追跡の定番」。EDM/ポップスでは十分。ルバートや変拍子は苦手。
  - 小節頭(ダウンビート)は librosa では取れない。必要なら madmom / Beat This! (ISMIR 2024) を使う。
  - ハーフ/ダブルテンポ誤り(例: 128 ⇔ 64)は本質的に曖昧。GIF側と同じくオクターブ許容でマッチングする。
"""
import argparse
import json
import sys

import numpy as np


def synth_drum_loop(bpm: float, seconds: float = 20.0, sr: int = 22050, seed: int = 0) -> np.ndarray:
    """キック(毎拍)+スネア(2,4拍)+ハイハット(8分)の簡易ドラムループ。"""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    y = np.zeros(n, dtype=np.float32)
    beat = 60.0 / bpm
    t_kick = np.arange(0, 0.25, 1 / sr)
    kick = (np.sin(2 * np.pi * 55 * t_kick) * np.exp(-t_kick * 18)).astype(np.float32)
    t_sn = np.arange(0, 0.15, 1 / sr)
    t_hh = np.arange(0, 0.04, 1 / sr)
    i = 0
    t = 0.0
    while t < seconds:
        s = int(t * sr)
        y[s:s + len(kick)] += kick[: max(0, min(len(kick), n - s))]
        if i % 4 in (1, 3):
            snare = (rng.normal(0, 0.4, len(t_sn)) * np.exp(-t_sn * 30)).astype(np.float32)
            y[s:s + len(snare)] += snare[: max(0, min(len(snare), n - s))]
        for k in (0, 0.5):
            s2 = int((t + k * beat) * sr)
            hh = (rng.normal(0, 0.15, len(t_hh)) * np.exp(-t_hh * 120)).astype(np.float32)
            if s2 < n:
                y[s2:s2 + len(hh)] += hh[: max(0, min(len(hh), n - s2))]
        i += 1
        t += beat
    return np.clip(y / (np.abs(y).max() + 1e-9) * 0.9, -1, 1)


def analyze(path: str) -> dict:
    import librosa
    y, sr = librosa.load(path, mono=True)
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    tempo_raw, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, units="time", trim=False)
    tempo_raw = float(np.atleast_1d(tempo_raw)[0])
    beats = np.asarray(beats, dtype=float)
    result = {"path": path, "duration_s": round(len(y) / sr, 3), "bpm_raw": round(tempo_raw, 2), "n_beats": int(len(beats))}
    if len(beats) >= 4:
        # 等間隔グリッドを線形回帰でフィット → 安定した BPM と位相
        idx = np.arange(len(beats))
        period, intercept = np.polyfit(idx, beats, 1)
        resid = beats - (period * idx + intercept)
        ibi = np.diff(beats)
        steadiness = float(np.clip(1.0 - ibi.std() / (ibi.mean() + 1e-9) * 4, 0, 1))
        result.update({
            "bpm": round(60.0 / period, 2),
            "beat_period_s": round(float(period), 5),
            "grid_offset_s": round(float(intercept % period), 4),
            "grid_residual_ms": round(float(np.abs(resid).mean() * 1000), 1),
            "steadiness": round(steadiness, 3),
            "beat_times": [round(float(b), 3) for b in beats],
        })
    # テンポ候補（オクターブ確認用）
    tg = librosa.feature.tempo(onset_envelope=onset_env, sr=sr, aggregate=None)
    vals, counts = np.unique(np.round(tg), return_counts=True)
    order = np.argsort(-counts)[:3]
    result["tempo_candidates"] = [{"bpm": float(vals[i]), "share": round(float(counts[i] / counts.sum()), 2)} for i in order]
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio", nargs="?")
    ap.add_argument("--synth", type=float, metavar="BPM", help="合成ドラムループを生成して解析（出力先は audio 引数）")
    a = ap.parse_args()
    if a.synth:
        import soundfile as sf
        out = a.audio or f"synth_{int(a.synth)}bpm.wav"
        sf.write(out, synth_drum_loop(a.synth), 22050)
        a.audio = out
    if not a.audio:
        ap.error("audio file required")
    json.dump(analyze(a.audio), sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
