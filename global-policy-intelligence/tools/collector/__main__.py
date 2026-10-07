# -*- coding: utf-8 -*-
"""支持 `python -m collector <子命令>`。

CLI 实现在 cli.py；本文件只做入口转发，使包级调用与脚本调用行为一致。
"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
