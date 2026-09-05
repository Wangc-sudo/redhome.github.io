#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容薄壳：实现已上移到 common/dingtalk.py，本文件仅保持旧 import 路径可用。
新代码请直接: from common.dingtalk import DingTalkClient, DingTalkError
"""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.dingtalk import DingTalkClient, DingTalkError  # noqa: F401,E402
