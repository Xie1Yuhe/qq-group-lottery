"""生成最终图标：矢量几何风格（骰子 + 星芒）。

设计说明
--------
**为什么不用照片**：素材照片在 16px（任务栏真实渲染尺寸）下细节必然丢光——
16×16 只有 256 个像素，人脸的五官、发丝、网点全部糊成一个白点。
改为矢量几何图形后，16px 下剩下的是"蓝色圆角方块 + 白色骰子方块"两个大色块，
反而最清楚。

**语义**：
  · 骰子     —— 随机抽取，本工具的核心动作（界面首页用的就是这个 emoji）
  · 金色星芒 —— 中奖 / 手气
  · 蓝色渐变 —— 与界面品牌色一致（--brand #4f6ef7）
  · 圆角方块 —— 符合 Windows 11 图标规范

**技术路线**：SVG 描述几何形状 → 无头浏览器渲染 512px 母图 →
LANCZOS 缩小到各尺寸并打包成多尺寸 .ico。
比让浏览器直接渲染 16px 更可控，也比 Pillow 画图抗锯齿更好。

输出
----
  assets/icon.ico          深色蓝底版（默认，任务栏用）
  assets/icon-light.ico    白底版（备选，浅色任务栏更清楚时用）
  assets/icon-32.png       页面 favicon
  assets/icon-180.png      页面图标（高分屏 / 收藏夹）
  assets/design/*.svg      源文件，改设计只动这些
"""

import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image

ROOT = pathlib.Path(r"E:\QQ群抽奖")
sys.path.insert(0, str(ROOT))
from app import find_browser  # noqa: E402

ASSETS = ROOT / "assets"
DESIGN = ASSETS / "design"
DESIGN.mkdir(parents=True, exist_ok=True)

SIZES = [16, 24, 32, 48, 64, 128, 256]
MASTER = 512

# ── 界面调色板 ─────────────────────────────────────────────────────────
BRAND, BRAND_2, DEEP = "#4f6ef7", "#6f8bff", "#3b55d9"
INK = "#1a2233"
GOLD = "#f2b53c"
PIP_R = 26
DIE = (150, 160, 292, 292)
PIPS = [(193, 203), (296, 306), (399, 409)]


def die_body(pip_fill: str, shadow_fill: str, shadow_op: str) -> str:
    x, y, w, h = DIE
    pips = "".join(f'<circle cx="{cx}" cy="{cy}" r="{PIP_R}" fill="{pip_fill}"/>' for cx, cy in PIPS)
    return (
        f'<g transform="rotate(-7 256 256)">'
        f'<rect x="{x + 5}" y="{y + 13}" width="{w}" height="{h}" rx="52" '
        f'fill="{shadow_fill}" opacity="{shadow_op}"/>'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="52" fill="#ffffff"/>'
        f"{pips}</g>"
    )


def star(cx: float, cy: float, r: float, fill: str) -> str:
    k = r * 0.30
    d = (
        f"M {cx} {cy - r} "
        f"C {cx + k} {cy - k} {cx + k} {cy - k} {cx + r} {cy} "
        f"C {cx + k} {cy + k} {cx + k} {cy + k} {cx} {cy + r} "
        f"C {cx - k} {cy + k} {cx - k} {cy + k} {cx - r} {cy} "
        f"C {cx - k} {cy - k} {cx - k} {cy - k} {cx} {cy - r} Z"
    )
    return f'<path d="{d}" fill="{fill}"/>'


def build_svg(background: str, extra_defs: str, body: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">\n'
        f"<defs>{extra_defs}</defs>\n{background}\n{body}\n</svg>\n"
    )


GRAD = (
    '<linearGradient id="bg" x1="0" y1="0" x2="0.35" y2="1">'
    f'<stop offset="0" stop-color="{BRAND_2}"/>'
    f'<stop offset="0.55" stop-color="{BRAND}"/>'
    f'<stop offset="1" stop-color="{DEEP}"/></linearGradient>'
    '<radialGradient id="glow" cx="0.5" cy="0.46" r="0.52">'
    '<stop offset="0" stop-color="#ffffff" stop-opacity="0.28"/>'
    '<stop offset="1" stop-color="#ffffff" stop-opacity="0"/></radialGradient>'
)

DARK_SVG = build_svg(
    '<rect x="0" y="0" width="512" height="512" rx="114" fill="url(#bg)"/>'
    '<rect x="0" y="0" width="512" height="512" rx="114" fill="url(#glow)"/>',
    GRAD,
    die_body(INK, "#1b2a6b", "0.30") + star(394, 128, 62, GOLD),
)

LIGHT_SVG = build_svg(
    '<rect x="0" y="0" width="512" height="512" rx="114" fill="#ffffff"/>'
    '<rect x="0.5" y="0.5" width="511" height="511" rx="114" fill="none" '
    'stroke="#d5dbe7" stroke-width="5"/>',
    GRAD,
    die_body(BRAND, "#c9d3f2", "0.85") + star(394, 128, 58, GOLD),
)

(DESIGN / "final-dark.svg").write_text(DARK_SVG, encoding="utf-8")
(DESIGN / "final-light.svg").write_text(LIGHT_SVG, encoding="utf-8")


def render(svg: pathlib.Path, out: pathlib.Path, browser: str) -> bool:
    profile = pathlib.Path(tempfile.mkdtemp(prefix="icon-render-"))
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--window-size={MASTER},{MASTER}",
        "--default-background-color=00000000",
        "--virtual-time-budget=4000",
        f"--screenshot={out}",
        svg.resolve().as_uri(),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        pass
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        for _ in range(20):
            try:
                shutil.rmtree(profile)
                break
            except OSError:
                time.sleep(0.3)
    if not (out.is_file() and out.stat().st_size > 0):
        return False
    im = Image.open(out).convert("RGBA")
    side = min(im.size)
    l, t = (im.width - side) // 2, (im.height - side) // 2
    im.crop((l, t, l + side, t + side)).save(out)
    return True


def main() -> int:
    browser = find_browser()
    if not browser:
        print("找不到 Edge/Chrome，无法渲染 SVG")
        return 1

    jobs = [
        (DESIGN / "final-dark.svg", ASSETS / "icon.ico", ASSETS / "icon-256.png"),
        (DESIGN / "final-light.svg", ASSETS / "icon-light.ico", ASSETS / "icon-light-256.png"),
    ]
    for svg, ico, preview in jobs:
        tmp = DESIGN / f"_{svg.stem}.png"
        if tmp.exists():
            tmp.unlink()
        if not render(svg, tmp, browser):
            print(f"  渲染失败: {svg.name}")
            return 1
        master = Image.open(tmp).convert("RGBA")
        master.save(ico, format="ICO", sizes=[(s, s) for s in SIZES])
        master.resize((256, 256), Image.LANCZOS).save(preview)
        # favicon 用同一套（浏览器标签在浅色 chrome 下也能看清）
        master.resize((32, 32), Image.LANCZOS).save(ASSETS / "icon-32.png")
        master.resize((180, 180), Image.LANCZOS).save(ASSETS / "icon-180.png")
        print(f"  {ico.name}: {ico.stat().st_size / 1024:.1f} KB（{len(SIZES)} 个尺寸）")

    # 去掉旧的照片版图标，避免混淆
    for stale in ("icon-face.ico", "icon-face-256.png", "_icon-preview.png",
                  "_icon-applied.png", "icon.ico.bak"):
        path = ASSETS / stale
        if path.exists():
            path.unlink()
            print(f"  已移除旧图标: {stale}")

    print(f"\n设计源文件: {DESIGN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
