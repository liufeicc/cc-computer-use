"""生成随包分发的第三方组件许可清单（THIRD-PARTY-LICENSES.txt）。

在客体上、用**构建用的那个 Python** 跑：

    python packaging/windows/gen-third-party.py <输出文件>

═══ 为什么是"生成"而不是仓库里放一个静态文件 ═══

Linux 侧那份 `packaging/deb/THIRD-PARTY-LICENSES.txt` 是静态的 —— 因为它列的是
**随包二进制**（xdotool / Xephyr / tesseract …），那几个是人工挑选、版本写着不动的。

Windows 这份不一样：冻结产物里装的是**整个 Python 运行时 + 几十个 Python 包**，
它们随 `pyproject.toml` 的依赖与客体环境漂移。静态清单会在第一次升级依赖时就过期，
而过期的许可清单比没有更糟（它看起来是权威的）。所以这里从**构建环境自己的**
`importlib.metadata` 里读——那是"实际会被打进产物"的那一份事实。

═══ 两条判据来源 ═══

  · 组件与版本：`importlib.metadata.distributions()`，即当前解释器里真实装了什么；
  · 许可：优先用 `License-Expression`（PEP 639，新式元数据的权威字段），
    退回 `License`（老式，且经常是一整段文字），再退回 Trove 分类器，
    最后如实写「见包内 LICENSE」——**不猜**。

⚠️ 排除名单里放的是**构建工具**（PyInstaller 自身、setuptools、pip…）：
它们不随产物分发，列进来是错的。反过来，凡是可能进产物的一个都不排除——
多列一个无害，漏列一个是真问题。
"""

from __future__ import annotations

import importlib.metadata as md
import sys

# 构建工具，不随产物分发（PyInstaller 的 bootloader 单独在文件头声明）
_BUILD_ONLY = {
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "pywin32-ctypes",
    "altgraph",
    "pefile",
    "setuptools",
    "pip",
    "wheel",
    "packaging",
    "pytest",
    "iniconfig",
    "pluggy",
}


def _license_of(meta) -> str:  # noqa: ANN001
    """按「新式 → 老式 → 分类器 → 如实说不知道」的顺序取许可标识。"""
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()

    lic = (meta.get("License") or "").strip()
    # 老式 License 字段经常是一整段许可正文（几十行），那种不要塞进一行清单
    if lic and len(lic) <= 80 and "\n" not in lic:
        return lic

    classifiers = meta.get_all("Classifier") or []
    for c in classifiers:
        if c.startswith("License ::"):
            return c.split("::")[-1].strip()

    return "见包内 LICENSE 文件"


def collect() -> list[tuple[str, str, str]]:
    """[(包名, 版本, 许可), …]，按包名排序、同名去重。"""
    seen: dict[str, tuple[str, str, str]] = {}
    for dist in md.distributions():
        meta = dist.metadata
        name = (meta.get("Name") or "").strip()
        if not name:
            continue
        if name.lower() in _BUILD_ONLY:
            continue
        version = (meta.get("Version") or "").strip()
        try:
            version = dist.version or version
        except Exception:  # noqa: BLE001 —— 元数据坏掉的包不该让整个构建挂掉
            pass
        # 同名多版本时留版本号更大的那个字符串序（够用，且不引入打包依赖）
        prev = seen.get(name.lower())
        if prev is None or version > prev[1]:
            seen[name.lower()] = (name, version, _license_of(meta))
    return sorted(seen.values(), key=lambda r: r[0].lower())


_HEADER = """\
cc-computer-use —— 随包分发的第三方组件与许可
================================================================================

本安装包内含一个完整的 Python 运行时与若干第三方 Python 包。
它们各自的版权与许可如下（组件清单由构建环境自动生成，不是人工维护的名单）。

  · CPython 3.12 运行时                      PSF License Agreement
    https://docs.python.org/3/license.html
  · PyInstaller 引导器（bootloader）         GPL-2.0-or-later，附「打包不受传染」例外条款
    https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt
    ⚠️ 该例外条款明确：把脚本打包成可执行文件**不会**让被包装的程序受 GPL 约束。

各包的完整许可正文在其自身的发行包内；下表给出标识，供快速核对。

--------------------------------------------------------------------------------
包名                                    版本                许可
--------------------------------------------------------------------------------
"""


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: gen-third-party.py <输出文件>", file=sys.stderr)
        return 2

    rows = collect()
    lines = [_HEADER]
    for name, version, lic in rows:
        lines.append(f"{name:<40}{version:<20}{lic}\n")
    lines.append("--------------------------------------------------------------------------------\n")
    lines.append(f"共 {len(rows)} 个第三方 Python 组件。\n")

    # utf-8-sig：带 BOM。Windows 记事本（尤其是老版本的"打开"路径）对无 BOM 的
    # UTF-8 会按 ANSI 解，中文标题会变乱码 —— 这份文件是要给人看的，容错优先。
    with open(sys.argv[1], "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.writelines(lines)

    print(f"OK 已写出 {len(rows)} 个组件 → {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())