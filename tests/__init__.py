"""测试包。

在这里准备隔离环境 —— 必须在任何 `panmigrate.*` 被导入之前设置，
因为 config.py 在导入时就把 PANMIGRATE_HOME 记成了 DATA_DIR。
不这么做的话，跑测试会直接读写用户真实的 data/config.json 和 data/tasks.db。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import warnings

os.environ.setdefault("PANMIGRATE_HOME", tempfile.mkdtemp(prefix="panmigrate-test-"))

# 用例会造很多临时任务库，进程结束前不一定被回收。
# 这些 sqlite 连接指向系统临时目录，不影响测试结论，只把噪音关掉。
warnings.simplefilter("ignore", ResourceWarning)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
