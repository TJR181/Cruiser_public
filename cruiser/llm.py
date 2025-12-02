from __future__ import annotations

import os
from typing import Any, Optional, Tuple


def _compute_temperature() -> float:
    """根据并发会话编号与总数，按序号从低到高分配温度。

    规则：
    - 当 CRUISER_SESSIONS<=1 或未设置时，返回 CRUISER_TEMP_MIN（默认 0.0）。
    - 线性区间 [CRUISER_TEMP_MIN, CRUISER_TEMP_MAX]，按 worker_id 在 [1..sessions] 等分。
    可通过环境变量调整：
    - CRUISER_TEMP_MIN（默认 0.0）
    - CRUISER_TEMP_MAX（默认 0.9）
    """
    try:
        wid = int(os.environ.get("CRUISER_WORKER_ID", "1") or "1")
    except Exception:
        wid = 1
    try:
        total = int(os.environ.get("CRUISER_SESSIONS", "1") or "1")
    except Exception:
        total = 1
    try:
        tmin = float(os.environ.get("CRUISER_TEMP_MIN", "0.0") or "0.0")
    except Exception:
        tmin = 0.0
    try:
        tmax = float(os.environ.get("CRUISER_TEMP_MAX", "0.9") or "0.9")
    except Exception:
        tmax = 0.9
    if total <= 1:
        return max(0.0, min(1.0, tmin))
    wid = max(1, min(wid, total))
    step = (tmax - tmin) / max(1, total - 1)
    temp = tmin + (wid - 1) * step
    # 合理裁剪区间
    return max(0.0, min(1.0, temp))


def get_llm() -> Tuple[Optional[Any], str, bool]:
    """获取可用的聊天模型并返回三元组：
    (llm_instance, provider_name, supports_tool_calls)

    优先级：
    - DeepSeek（无原生函数调用，使用 JSON 决策路径）
    - Ollama（无原生函数调用，使用 JSON 决策路径）
    - None（均不可用）
    """
    # 1) DeepSeek（OpenAI 兼容协议，但不保证支持函数调用）
    deepseek_key = "..."
    if deepseek_key:
        try:
            from langchain_openai import ChatOpenAI  # type: ignore

            llm = ChatOpenAI(
                model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                api_key=deepseek_key,
                temperature=_compute_temperature(),
            )
            # 标记不支持原生 tool calling，走 JSON 决策路径
            return llm, "deepseek", False
        except Exception:  # noqa: BLE001
            pass
