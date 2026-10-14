"""给快捷方式设置图标（用 PowerShell 的 WScript.Shell，避免额外的 COM 依赖）。"""

import os
import pathlib
import subprocess
import sys

ICON = pathlib.Path(r"E:\QQ群抽奖\assets\icon.ico")


def ps_set_icon(lnk: pathlib.Path, icon: pathlib.Path) -> bool:
    script = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$sc = $s.CreateShortcut('{lnk}'); "
        f"$sc.IconLocation = '{icon},0'; "
        "$sc.Save()"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    return r.returncode == 0


def ps_read_icon(lnk: pathlib.Path) -> str:
    script = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$sc = $s.CreateShortcut('{lnk}'); "
        "Write-Output $sc.IconLocation"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    return (r.stdout or "").strip()


def main() -> int:
    if not ICON.is_file():
        print(f"图标不存在: {ICON}")
        return 1
    print(f"图标: {ICON}（{ICON.stat().st_size} 字节）")

    startup = pathlib.Path(
        os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
    )
    candidates: list[pathlib.Path] = []
    for folder in (
        startup,
        pathlib.Path(os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")),
        pathlib.Path(os.path.join(os.environ.get("PUBLIC", ""), "Desktop")),
        pathlib.Path(r"E:\QQ群抽奖"),
    ):
        if folder.is_dir():
            candidates += list(folder.glob("*.lnk"))

    changed = 0
    for lnk in dict.fromkeys(candidates):
        if not ("QQ群抽奖" in lnk.name or "NapCat" in lnk.name):
            continue
        if ps_set_icon(lnk, ICON):
            print(f"  已设置: {lnk.name}")
            changed += 1
        else:
            print(f"  失败: {lnk.name}")

    print(f"\n共处理 {changed} 个快捷方式")
    print("\n校验:")
    for lnk in dict.fromkeys(candidates):
        if "QQ群抽奖" in lnk.name or "NapCat" in lnk.name:
            print(f"  {lnk.name} -> {ps_read_icon(lnk)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
