"""
PyInstaller / 命令行入口脚本。

为什么单独建：server.py 用相对导入（from . import _bootstrap），不能直接当脚本跑；
PyInstaller 需要一个绝对导入的入口文件。本文件只做一件事：调 server.main()。

import computer_use_mcp.server 会先执行 _bootstrap.setup_gi_environment()
（在任何 import gi 之前设好 GI_TYPELIB_PATH），冻结后同样生效。
"""

from computer_use_mcp.server import main

if __name__ == "__main__":
    main()
