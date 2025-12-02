"""Cruiser package init.

全局禁用 LangSmith/LangChain 追踪以避免外网请求与配额问题。
在导入任何子模块前执行，确保子进程默认也继承禁用状态。

"""

from __future__ import annotations

import os


# 强制关闭追踪（覆盖外部环境设置）
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

# 同时移除可能存在的 API Key，避免误触发 SDK 初始化
for _k in ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
    os.environ.pop(_k, None)




__all__ = [
    "tools",
]
