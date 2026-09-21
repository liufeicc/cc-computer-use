"""
测试之间**不得**留下进程级副作用（I-15 的回归集，见 docs/REVIEW/review_0.1.0.md）。

为什么单独一个文件、且必须起**子进程**：判据是「跑完某条用例后，进程环境是否干净」，
而这件事只有在**那个 pytest 会话收尾时**才观测得到 —— 在本会话里断言，观测到的是本
会话自己的状态，与被测用例无关（何况 conftest 的守卫夹具已经把本会话擦干净了，
在会话内断言等于在测那把「本来就该生效的扫帚」，永远绿）。

所以做法是：起一个子进程跑 pytest，用 `-p` 挂一个探针插件，在 `pytest_sessionfinish`
（= 最后一个用例的 teardown 之后）把 AT_SPI_BUS_ADDRESS 的真实取值打出来，再由本用例
断言它是 None。

红线自验：把 conftest 里那个 autouse 守卫夹具改回 `yield` / `return` 空实现，本用例
必须变红 —— 实测过（见 REVIEW 文档的红线表）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# 会直接写进程级 os.environ 的用例（display/atspi 那一路的 sync_bus → _apply_bus）。
_LEAKY_TEST = "reader_sync_bus_follows_sandbox"


def test_at_spi_bus_env_is_clean_after_tests_that_write_it(tmp_path):
    """
    跑完那个会写进程级环境变量的用例后，**进程环境必须干净**。

    这条守的是「测试之间互不污染」，不是被测代码的行为：`AtspiReader` 写进程级
    os.environ 是**设计使然**（libatspi 只能从那里读总线地址），必须写；接不住它的是
    测试侧的记账 —— monkeypatch 的 `delenv` 对「本就不存在的键」不入账，于是记下的
    反而是用例自己写进去的假地址，teardown 时原样还原回环境。修法在 conftest 的
    autouse 守卫夹具，本用例是它的红线。
    """
    probe = tmp_path / "cc_cu_env_probe.py"
    probe.write_text(
        "import os\n"
        "\n"
        "\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    print('CC_CU_ENV_PROBE=%r' % os.environ.get('AT_SPI_BUS_ADDRESS'))\n",
        encoding="utf-8",
    )

    env = {**os.environ}
    # 子进程必须是一个**干净的**单测进程：母进程若跑的是 e2e 会话，它自己的
    # AtspiReader 早已把**真实的沙箱总线地址**写进 os.environ，子进程会连它一起继承
    # —— 那样「跑完之后干净」就不是「等于 None」而是「等于继承来的那个值」，
    # 断言会假红（实测踩到过）。真实场景里单测进程本来就不该带这个变量。
    env.pop("AT_SPI_BUS_ADDRESS", None)
    # 探针插件所在的目录要在 sys.path 上（-p 走 importlib）
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env.get("PYTHONPATH", "")
    # 子进程按**单测**跑：守卫夹具只在单测生效，而外层若是 e2e 会话就会把
    # CC_CU_E2E=1 透传下去、让子进程也走 e2e 分支（夹具不生效 → 本用例假红）。
    env["CC_CU_E2E"] = "0"
    # 刻意用 `tests/` 目录 + `-k` 定位，而**不是**写死某个文件名：`_LEAKY_TEST` 所在的
    # 文件会随拆分/重组而搬家（2026-09-18 就搬过一次，写死文件名会让本用例连带失败，
    # 而失败信息指向的是「文件不存在」，与真正守的那件事毫无关系）。
    # 收集整个目录的开销可接受：只收集、不执行其余用例（`-k` 只选中这一条）。
    proc = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/", "-k", _LEAKY_TEST,
         "-p", "cc_cu_env_probe", "-s", "-q", "--tb=short", "--color=no"],
        cwd=str(_ROOT), env=env, capture_output=True, text=True,
    )

    assert proc.returncode == 0, \
        f"子进程 pytest 未通过：\n{proc.stdout}\n{proc.stderr}"
    # 防「什么都没选中」：用例被改名/删除时 -k 会静默选 0 个，探针照样打印 None，
    # 本用例就会变成一条恒真的空断言（这正是本项目反复强调的「判据松的测试」）。
    assert "1 passed" in proc.stdout, \
        f"子进程应恰好跑到 1 条用例（{_LEAKY_TEST} 是否被改名/删除？）：\n{proc.stdout}"

    # 用正则而非行首匹配：pytest 的进度字符（"."）会与探针输出挤在同一行。
    m = re.search(r"CC_CU_ENV_PROBE=(\S+)", proc.stdout)
    assert m, f"探针没打印出来（-s 是否失效？）：\n{proc.stdout}"
    assert m.group(1) == "None", (
        f"用例结束后进程环境里仍残留 AT_SPI_BUS_ADDRESS —— 见 I-15。\n"
        f"实际：CC_CU_ENV_PROBE={m.group(1)}\n预期：CC_CU_ENV_PROBE=None"
    )