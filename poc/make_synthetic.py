#!/usr/bin/env python3
"""BPM既知の合成GIFを生成する（解析器の正解データ用）。

生成物 (out_dir/):  ファイル名の _Nb = ループ1周あたりの拍数
  bounce_<bpm>bpm_4b.gif        : 1拍=1バウンド。ボールが着地する瞬間が拍。
  bounce_<bpm>bpm_4b_shaky.gif  : 同上 + カメラ揺れ + 背景ノイズ（ディザ模倣）
  headbob_<bpm>bpm_8b.gif       : 「人」風の矩形が上下に揺れる（振幅小、位置ベースの周期）
  swing_<bpm>bpm_4b.gif         : 左右振り子。1拍=片側へ振る（1往復=2拍 → 符号付き速度の周期は2拍）
  random_walk.gif               : 非周期（ランダムウォーク）→ 低スコアになるべき
注意: GIFのフレームディレイは10ms単位。fpsは 20 (50ms) / 10 (100ms) を使うこと。
"""
import argparse
import math
import os
import random

import numpy as np
from PIL import Image, ImageDraw


def save_gif(frames, path, delay_ms):
    ims = [Image.fromarray(f) for f in frames]
    ims[0].save(path, save_all=True, append_images=ims[1:], duration=delay_ms, loop=0, optimize=False)


def bounce_frames(bpm, fps=20, beats=4, size=(240, 180), shaky=False, seed=0):
    rng = random.Random(seed)
    beat = 60.0 / bpm
    total = beat * beats                       # ループ長 = beats拍
    n = int(round(total * fps))
    w, h = size
    frames = []
    for i in range(n):
        t = i / fps
        phase = (t / beat) % 1.0               # 0 = 着地（拍）
        # 放物線バウンド: 着地(0)で y 最下, 中間(0.5)で最高
        y_norm = 4 * phase * (1 - phase)       # 0..1..0
        cy = int(h * 0.75 - y_norm * h * 0.5)
        cx = w // 2
        dx = dy = 0
        img = Image.new("RGB", size, (30, 30, 40))
        d = ImageDraw.Draw(img)
        if shaky:
            dx = int(rng.gauss(0, 3))
            dy = int(rng.gauss(0, 3))
            # 背景テクスチャ（カメラ揺れで一緒に動く）
            for k in range(12):
                x0 = (k * 37 + dx) % w
                d.rectangle([x0, 0, x0 + 6, h], fill=(45, 45, 60))
        d.ellipse([cx - 22 + dx, cy - 22 + dy, cx + 22 + dx, cy + 22 + dy], fill=(240, 200, 60))
        d.rectangle([0, int(h * 0.75) + 24 + dy, w, h], fill=(70, 70, 80))
        arr = np.asarray(img).copy()
        if shaky:
            noise = np.random.default_rng(seed + i).integers(-12, 13, size=arr.shape, dtype=np.int16)
            arr = np.clip(arr.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        frames.append(arr)
    return frames, int(round(1000 / fps))


def headbob_frames(bpm, fps=20, beats=8, size=(200, 200)):
    beat = 60.0 / bpm
    n = int(round(beat * beats * fps))
    w, h = size
    frames = []
    for i in range(n):
        t = i / fps
        # 頭の上下: 拍で最下点 (cos が 1 → 下)
        bob = 6 * math.cos(2 * math.pi * t / beat)
        img = Image.new("RGB", size, (20, 24, 32))
        d = ImageDraw.Draw(img)
        # 体
        d.rectangle([w // 2 - 30, h // 2 + 10 + bob * 0.5, w // 2 + 30, h - 10], fill=(90, 120, 200))
        # 頭
        d.ellipse([w // 2 - 24, h // 2 - 50 + bob, w // 2 + 24, h // 2 - 2 + bob], fill=(230, 190, 160))
        frames.append(np.asarray(img).copy())
    return frames, int(round(1000 / fps))


def swing_frames(bpm, fps=20, beats=4, size=(240, 160)):
    beat = 60.0 / bpm
    # 1拍 = 片側へ振る → 1往復 = 2拍
    n = int(round(beat * beats * fps))
    w, h = size
    frames = []
    for i in range(n):
        t = i / fps
        x = w / 2 + (w * 0.35) * math.sin(math.pi * t / beat)
        img = Image.new("RGB", size, (25, 25, 25))
        d = ImageDraw.Draw(img)
        d.line([w / 2, 0, x, h * 0.7], fill=(200, 200, 200), width=3)
        d.ellipse([x - 16, h * 0.7 - 16, x + 16, h * 0.7 + 16], fill=(200, 80, 80))
        frames.append(np.asarray(img).copy())
    return frames, int(round(1000 / fps))


def random_walk_frames(fps=20, seconds=3.0, size=(200, 200), seed=1):
    rng = np.random.default_rng(seed)
    n = int(seconds * fps)
    w, h = size
    x, y = w / 2, h / 2
    frames = []
    for _ in range(n):
        x = float(np.clip(x + rng.normal(0, 12), 20, w - 20))
        y = float(np.clip(y + rng.normal(0, 12), 20, h - 20))
        img = Image.new("RGB", size, (25, 25, 25))
        d = ImageDraw.Draw(img)
        d.ellipse([x - 18, y - 18, x + 18, y + 18], fill=(80, 200, 120))
        frames.append(np.asarray(img).copy())
    return frames, int(round(1000 / fps))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--bpms", default="60,90,120,128,140,174")
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    for bpm in [int(b) for b in a.bpms.split(",")]:
        # ファイル名の _Nb はループ1周の拍数。GIFディレイは10ms単位に丸まるので、
        # 正解BPMは evaluate.py が N*60/実ループ長 で再計算する。
        f, d = bounce_frames(bpm)
        save_gif(f, os.path.join(a.out_dir, f"bounce_{bpm}bpm_4b.gif"), d)
        f, d = bounce_frames(bpm, shaky=True)
        save_gif(f, os.path.join(a.out_dir, f"bounce_{bpm}bpm_4b_shaky.gif"), d)
        f, d = headbob_frames(bpm)
        save_gif(f, os.path.join(a.out_dir, f"headbob_{bpm}bpm_8b.gif"), d)
        f, d = swing_frames(bpm)
        save_gif(f, os.path.join(a.out_dir, f"swing_{bpm}bpm_4b.gif"), d)
    f, d = random_walk_frames()
    save_gif(f, os.path.join(a.out_dir, "random_walk.gif"), d)
    print("done:", a.out_dir)


if __name__ == "__main__":
    main()
