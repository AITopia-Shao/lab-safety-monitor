"""
画面异常检测（V-JEPA 2，在电脑上运行，可选）
hub/jepa_worker.py

原理（"预期违背"检测，Violation of Expectation）：
  V-JEPA 2 是 Meta 发布的视频自监督模型，由"编码器 + 预测器"组成，训练目标是：
  给它一段视频的一部分，在特征空间里预测被遮住的那部分。
  我们每隔一段时间从开发板取一段台面画面（16 帧），做三件事：
    1. 只把前 8 帧交给编码器，得到"上下文"特征；
    2. 让预测器根据上下文，预测后 8 帧的特征；
    3. 把完整的 16 帧交给编码器，取后 8 帧的真实特征，和预测结果比对。
  预测误差 = "惊奇度"。画面变化符合常识（人正常操作、液体平稳加热）时误差小；
  出现不符合预期的变化（液体突然飞溅、器皿倾倒、冒烟）时误差变大。
  注意第 1 步只给前半段：如果把完整视频交给编码器再挑前半段的特征，编码器已经"看过"后半段，
  预测误差就失去了意义。

  不同场景的误差绝对值差别很大，所以程序先用开头若干段正常画面建立基线，
  上报的是"相对基线偏离了几个标准差"（异常度，用中位数 / MAD 计算，抗干扰）。
  异常度超过阈值时开发板记一条提醒事件，交给安全助手。它不参与任何断电决策。

运行环境：电脑（有 NVIDIA 显卡更好，纯 CPU 也能跑，每段约十几秒）。
  pip install -r requirements-host.txt
首次运行会从 Hugging Face 下载模型 facebook/vjepa2-vitl-fpc64-256（约 1.3GB）。
国内网络可先设置镜像：set HF_ENDPOINT=https://hf-mirror.com

用法：
  python hub/jepa_worker.py --board http://192.168.1.50:8080 --token 口令
  python hub/jepa_worker.py --selftest      # 不下载模型，用随机小模型检查整个流程
"""
import argparse
import io
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import deque
from typing import List, Optional

import numpy as np

DEFAULT_MODEL = "facebook/vjepa2-vitl-fpc64-256"
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


# ----------------------------------------------------------------------------
# 模型与打分
# ----------------------------------------------------------------------------
class JepaScorer:
    def __init__(self, model_id: str = DEFAULT_MODEL, device: Optional[str] = None, model=None):
        import torch
        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        if model is None:
            from transformers import AutoModel
            dtype = torch.float16 if self.device == "cuda" else torch.float32
            model = AutoModel.from_pretrained(model_id, dtype=dtype)
        self.model = model.to(self.device).eval()
        cfg = self.model.config
        self.crop = int(cfg.crop_size)
        self.patch = int(cfg.patch_size)
        self.tubelet = int(cfg.tubelet_size)
        self.dtype = next(self.model.parameters()).dtype

    def preprocess(self, frames: List[np.ndarray]) -> "torch.Tensor":
        """BGR 帧列表 → (1, T, C, H, W)：短边缩放到 crop、中心裁剪、ImageNet 归一化"""
        import cv2
        out = []
        for f in frames:
            h, w = f.shape[:2]
            s = self.crop / min(h, w)
            f = cv2.resize(f, (max(self.crop, round(w * s)), max(self.crop, round(h * s))), interpolation=cv2.INTER_AREA)
            h, w = f.shape[:2]
            y, x = (h - self.crop) // 2, (w - self.crop) // 2
            f = cv2.cvtColor(f[y:y + self.crop, x:x + self.crop], cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            out.append(((f - MEAN) / STD).transpose(2, 0, 1))
        x = self.torch.from_numpy(np.stack(out))[None]
        return x.to(self.device, self.dtype)

    def score(self, frames: List[np.ndarray]) -> dict:
        """返回 {surprise: 平均预测误差, heat: 每个空间格子的误差 (g×g), ms: 耗时}"""
        torch = self.torch
        n = len(frames)
        assert n >= 2 * self.tubelet and n % (2 * self.tubelet) == 0, "帧数需为 2×tubelet 的整数倍"
        t0 = time.time()
        video = self.preprocess(frames)
        grid = self.crop // self.patch
        per_t = grid * grid                      # 每个时间片的 token 数
        t_all = n // self.tubelet                # 完整片段的时间片数
        t_ctx = t_all // 2
        n_ctx, n_all = t_ctx * per_t, t_all * per_t
        with torch.no_grad():
            # 真实特征：完整片段过编码器，取后半段
            full = self.model(pixel_values_videos=video, skip_predictor=True).last_hidden_state
            target = full[:, n_ctx:n_all]
            # 上下文：只把前半段画面交给编码器（不让它看到后半段）
            ctx = self.model(pixel_values_videos=video[:, : n // 2], skip_predictor=True).last_hidden_state
            ctx_mask = [torch.arange(n_ctx, device=self.device).unsqueeze(0)]
            tgt_mask = [torch.arange(n_ctx, n_all, device=self.device).unsqueeze(0)]
            pred = self.model.predictor(encoder_hidden_states=ctx, context_mask=ctx_mask,
                                        target_mask=tgt_mask).last_hidden_state
            ln = torch.nn.functional.layer_norm
            d = target.shape[-1]
            err = (ln(pred.float(), (d,)) - ln(target.float(), (d,))).abs().mean(-1)[0]   # (n_target,)
        heat = err.reshape(t_all - t_ctx, grid, grid).mean(0).cpu().numpy()
        return {"surprise": float(err.mean().item()), "heat": heat, "ms": int((time.time() - t0) * 1000)}


class Baseline:
    """用正常画面建立基线：异常度 = (误差 - 中位数) / (1.4826 × MAD)"""

    def __init__(self, warmup: int = 8, size: int = 60):
        self.warmup = warmup
        self.hist = deque(maxlen=size)

    def zscore(self, s: float) -> Optional[float]:
        if len(self.hist) < self.warmup:
            self.hist.append(s)
            return None
        med = statistics.median(self.hist)
        mad = statistics.median(abs(x - med) for x in self.hist) or 1e-6
        z = (s - med) / (1.4826 * mad)
        if z < 3.0:            # 只用正常片段更新基线，避免异常把基线"带偏"
            self.hist.append(s)
        return z


# ----------------------------------------------------------------------------
# 与开发板通信
# ----------------------------------------------------------------------------
class Board:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def frame(self, which: str = "desk") -> Optional[np.ndarray]:
        import cv2
        req = urllib.request.Request(f"{self.base}/api/frame/{which}.jpg", headers={"X-Token": self.token})
        try:
            data = urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            return None
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)

    def push(self, score: float, note: str, model: str):
        body = json.dumps({"score": round(score, 3), "note": note, "model": model}).encode("utf-8")
        req = urllib.request.Request(f"{self.base}/api/jepa/score", data=body, method="POST",
                                     headers={"X-Token": self.token, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()


def collect_clip(board: Board, n: int, interval: float, which: str) -> Optional[List[np.ndarray]]:
    frames = []
    while len(frames) < n:
        f = board.frame(which)
        if f is None:
            return None
        frames.append(f)
        time.sleep(interval)
    return frames


def hot_region(heat: np.ndarray) -> str:
    """误差最大的位置，用九宫格描述（左上 / 正中 / 右下……）"""
    g = heat.shape[0]
    y, x = np.unravel_index(int(heat.argmax()), heat.shape)
    rows, cols = ["上", "中", "下"], ["左", "中", "右"]
    r, c = rows[min(2, y * 3 // g)], cols[min(2, x * 3 // g)]
    return "正中" if (r, c) == ("中", "中") else (r + c if r != "中" else "中" + c)


def run(args):
    board = Board(args.board, args.token)
    print(f"[JEPA] 加载模型 {args.model}（首次运行会下载，约 1.3GB）…")
    scorer = JepaScorer(args.model, args.device)
    print(f"[JEPA] 模型就绪，设备 {scorer.device}；先用 {args.warmup} 段正常画面建立基线")
    base = Baseline(args.warmup)
    while True:
        frames = collect_clip(board, args.frames, args.frame_interval, args.camera)
        if frames is None:
            print("[JEPA] 取不到画面，5 秒后重试（检查地址、口令、开发板是否在运行）")
            time.sleep(5)
            continue
        r = scorer.score(frames)
        z = base.zscore(r["surprise"])
        if z is None:
            print(f"[JEPA] 建立基线 {len(base.hist)}/{args.warmup}  误差 {r['surprise']:.4f}  ({r['ms']} ms)")
        else:
            note = f"原始误差 {r['surprise']:.4f}，变化最大的位置：画面{hot_region(r['heat'])}"
            try:
                board.push(z, note, "V-JEPA 2 " + args.model.split("/")[-1])
            except Exception as e:
                print(f"[JEPA] 上报失败: {e}")
            print(f"[JEPA] 异常度 {z:+.2f}  {note}  ({r['ms']} ms)")
        time.sleep(max(0.0, args.period - args.frames * args.frame_interval))


def selftest():
    """不下载模型：用随机初始化的小模型跑通"取帧 → 打分 → 基线"整条流程"""
    import torch
    from transformers import VJEPA2Config, VJEPA2Model
    torch.manual_seed(0)
    cfg = VJEPA2Config(crop_size=64, patch_size=16, frames_per_clip=16, tubelet_size=2, hidden_size=64,
                       num_hidden_layers=2, num_attention_heads=4, pred_hidden_size=32,
                       pred_num_hidden_layers=2, pred_num_attention_heads=4, num_pooler_layers=1)
    scorer = JepaScorer(model=VJEPA2Model(cfg), device="cpu")
    rng = np.random.default_rng(0)
    still = [np.full((120, 160, 3), 128, np.uint8) for _ in range(16)]
    r = scorer.score(still)
    assert r["heat"].shape == (4, 4) and np.isfinite(r["surprise"]), r
    base = Baseline(warmup=4)
    zs = []
    for _ in range(6):
        noisy = [np.clip(f.astype(int) + rng.integers(-3, 4, f.shape), 0, 255).astype(np.uint8) for f in still]
        zs.append(base.zscore(scorer.score(noisy)["surprise"]))
    jump = still[:8] + [rng.integers(0, 256, (120, 160, 3), dtype=np.uint8) for _ in range(8)]
    z_jump = base.zscore(scorer.score(jump)["surprise"])
    assert all(z is not None and np.isfinite(z) for z in zs[4:]) and np.isfinite(z_jump)
    print("自检通过：取帧预处理、前半段编码、预测后半段、误差热区、基线异常度整条流程都能跑通。")
    print("（这里用的是随机权重的小模型，分数本身没有意义；判断准不准要用正式模型在真实画面上看。）")


def main():
    ap = argparse.ArgumentParser(description="V-JEPA 2 画面异常检测（电脑端）")
    ap.add_argument("--board", default="http://127.0.0.1:8080", help="开发板网页地址")
    ap.add_argument("--token", default=os.environ.get("LABGUARD_TOKEN", ""), help="网页口令（小屏「设置」页）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default=None, help="cuda / cpu，默认自动")
    ap.add_argument("--camera", default="desk", choices=["desk", "pano"])
    ap.add_argument("--frames", type=int, default=16, help="每段帧数（需为 4 的倍数）")
    ap.add_argument("--frame-interval", type=float, default=0.25, help="取帧间隔，秒")
    ap.add_argument("--period", type=float, default=10.0, help="每隔多少秒分析一段")
    ap.add_argument("--warmup", type=int, default=8, help="建立基线所用的正常片段数")
    ap.add_argument("--selftest", action="store_true", help="不下载模型，自检流程")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    if not args.token:
        sys.exit("请用 --token 提供网页口令（小屏「设置」页可查看）")
    run(args)


if __name__ == "__main__":
    main()
