from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Dict, List, Optional
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from .tools import ALL_TOOLS,  list_security_tools_tool
from .llm import get_llm
from .prompt import (
    BASE_SYSTEM_PROMPT,
    REACT_INSTRUCTION_TEMPLATE,
    REFLECT_PROMPT,
    SCAN_QUESTION_TEMPLATE,
    WORKER_HINT_TEMPLATE,
)

# ------------------------------
# 启动时获取可用安全工具概览
# ------------------------------
_SECURITY_TOOLS_OVERVIEW: str = ""

def _refresh_security_tools_overview() -> None:
    """调用 list_security_tools_tool 获取当前环境可用的命令行安全工具，并生成简短概览。
    概览会被注入到 system prompt，帮助模型更好地理解环境能力；同时在启动时打印到控制台（绿色）。
    """
    global _SECURITY_TOOLS_OVERVIEW
    try:
        # 调用工具
        txt = list_security_tools_tool.invoke({"only_available": True, "with_version": True})

        # 将结果先落盘到文件，再从文件读取（便于审计与复用）
        try:
            out_dir = os.path.join(os.getcwd(), "reports")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, "list_security_tools.json")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(txt if isinstance(txt, str) else str(txt))
            # 回读
            with open(out_path, "r", encoding="utf-8") as f:
                txt = f.read()
        except Exception:
            # 文件落盘失败不影响主流程
            pass
        summary = "无"
        try:
            arr = json.loads(txt)
            if isinstance(arr, list):
                items = []
                for t in arr:
                    name = (t or {}).get("name") or ""
                    ver = (t or {}).get("version") or ""
                    items.append(name + (f" ({ver})" if ver else ""))
                summary = ", ".join([s for s in items if s]) or "无"
        except Exception:
            # 若返回不是 JSON，保底显示原文本的首行
            line = (txt or "").strip().splitlines()[:1]
            summary = line[0] if line else "无"
        _SECURITY_TOOLS_OVERVIEW = summary
        # 绿色打印到控制台（非静默模式）
        if os.environ.get("CRUISER_QUIET") != "1":
            try:
                print("\x1b[32m" + f"[SECURITY] 可用工具: {summary}" + "\x1b[0m")
            except Exception:
                print(f"[SECURITY] 可用工具: {summary}")
    except Exception:
        # 静默失败
        pass


# ------------------------------
# 轻量异常判定
# ------------------------------
def _is_context_limit_error(err: Exception) -> bool:
    """更严格地检测上下文超限错误，避免误判。

    仅当错误文本包含以下更明确的短语时才认为是上下文超限：
    - "maximum context length"
    - "max context length"
    - "token limit"
    - "exceeds" 且同时包含 "token" 或 "context"
    - "exceeded" 且同时包含 "token" 或 "context"
    - "context window" 与 "exceed"
    """
    try:
        text = f"{getattr(err, 'message', '')} {err}"
        text = (text or "").lower()
        if "maximum context length" in text or "max context length" in text:
            return True
        if "token limit" in text:
            return True
        if ("exceeds" in text or "exceeded" in text) and ("token" in text or "context" in text):
            return True
        if "context window" in text and ("exceed" in text or "exceeded" in text):
            return True
        return False
    except Exception:
        return False

# ------------------------------
# 会话内对话历史（内存态，仅当前进程生命周期）
# ------------------------------
_CONV_HISTORY: List[Dict[str, str]] = []  # 每项: {"q": str, "a": str, "r"?: str, "n"?: str}


def _format_conversation_context(limit: int = 10) -> str:
    if not _CONV_HISTORY:
        return ""
    lines: List[str] = []
    for item in _CONV_HISTORY[-limit:]:
        q = item.get("q", "")
        a = item.get("a", "")
        r = item.get("r", "")
        n = item.get("n", "")
        # 适度截断，避免超长
        q_show = (q[:400] + "…") if len(q) > 400 else q
        a_show = (a[:400] + "…") if len(a) > 400 else a
        line = f"- U: {q_show}\n  A: {a_show}"
        if r:
            r_show = (r[:400] + "…") if len(r) > 400 else r
            line += f"\n  R: {r_show}"
        if n:
            n_show = (n[:400] + "…") if len(n) > 400 else n
            line += f"\n  N: {n_show}"
        lines.append(line)
    return "\n".join(lines)


def _is_info_duplicate(challenge_code: str, info: str) -> bool:
    """检查关键信息是否已存在（去重）。
    
    使用智能匹配：提取关键部分进行比较，避免重复写入相似信息。
    
    Args:
        challenge_code: 题目ID
        info: 关键信息
        
    Returns:
        如果信息已存在返回True，否则返回False
    """
    if not challenge_code or not info:
        return True
    try:
        out_dir = os.path.join(os.getcwd(), "reports")
        info_path = os.path.join(out_dir, f"info_{challenge_code}.txt")
        if not os.path.exists(info_path):
            return False
        
        # 读取现有信息
        with open(info_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        if not content:
            return False
        
        info_normalized = info.lower().strip()
        
        # 方法1: 完全匹配
        if f"关键信息: {info}" in content or f"关键信息: {info_normalized}" in content.lower():
            return True
        
        # 方法2: 提取关键部分进行匹配（适用于路径、API等）
        # 提取路径、URL、口令等关键部分
        # 提取路径
        path_pattern = r'([/\w\-\.]+\.(php|html|txt|js|css|json|xml))'
        new_paths = set(re.findall(path_pattern, info, re.IGNORECASE))
        if new_paths:
            # 检查现有信息中是否已包含这些路径
            for path_tuple in new_paths:
                path = path_tuple[0] if isinstance(path_tuple, tuple) else path_tuple
                if path.lower() in content.lower():
                    return True
        
        # 提取URL
        url_pattern = r'(https?://[^\s]+)'
        new_urls = set(re.findall(url_pattern, info, re.IGNORECASE))
        if new_urls:
            for url in new_urls:
                if url.lower() in content.lower():
                    return True
        
        # 方法3: 对于短信息（如口令、简单路径），检查是否已存在
        if len(info) < 100:
            # 提取所有已存在的关键信息行
            lines = content.split("\n")
            existing_infos = []
            for i, line in enumerate(lines):
                if "关键信息:" in line:
                    existing_info = line.split("关键信息:")[-1].strip().lower()
                    if existing_info:
                        existing_infos.append(existing_info)
            
            # 检查新信息是否与已有信息高度相似
            for existing in existing_infos:
                # 如果新信息是已有信息的子集或超集，认为是重复
                if info_normalized in existing or existing in info_normalized:
                    # 但允许部分重叠（如不同路径），只对完全包含的情况判定为重复
                    if len(info_normalized) > 20 and len(existing) > 20:
                        # 计算相似度（简单方法：共同词的比例）
                        new_words = set(info_normalized.split())
                        existing_words = set(existing.split())
                        if new_words and existing_words:
                            overlap = len(new_words & existing_words)
                            similarity = overlap / min(len(new_words), len(existing_words))
                            if similarity > 0.8:  # 80%以上相似认为是重复
                                return True
                    else:
                        # 短信息，直接检查包含关系
                        if info_normalized == existing or (len(info_normalized) < 30 and info_normalized in existing):
                            return True
        
        return False
    except Exception:
        # 出错时保守处理，不写入
        return True


def _write_shared_info(challenge_code: str, worker_id: str, info: str) -> None:
    """将关键信息写入共享文件（带去重检查）。
    
    Args:
        challenge_code: 题目ID
        worker_id: 会话编号
        info: 关键信息（如默认口令、路径、API等）
    """
    if not challenge_code or not info:
        return
    
    # 去重检查
    if _is_info_duplicate(challenge_code, info):
        return
    
    try:
        out_dir = os.path.join(os.getcwd(), "reports")
        os.makedirs(out_dir, exist_ok=True)
        info_path = os.path.join(out_dir, f"info_{challenge_code}.txt")
        # 使用追加模式，允许多个会话写入
        with open(info_path, "a", encoding="utf-8") as f:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{timestamp}] 会话: {worker_id}\n")
            f.write(f"关键信息: {info}\n")
            f.write("-" * 60 + "\n")
    except Exception:
        pass


def _read_shared_info(challenge_code: str, limit: int = 5) -> str:
    """读取共享的关键信息文件，默认只返回最后5条。
    
    Args:
        challenge_code: 题目ID
        limit: 返回的信息条数，默认5条
        
    Returns:
        共享信息的文本内容，如果文件不存在或读取失败则返回空字符串
    """
    if not challenge_code:
        return ""
    try:
        out_dir = os.path.join(os.getcwd(), "reports")
        info_path = os.path.join(out_dir, f"info_{challenge_code}.txt")
        if os.path.exists(info_path):
            with open(info_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    # 按分隔线分割，每条信息以 "------------------------------------------------------------" 结尾
                    separator = "-" * 60
                    # 使用分隔符分割，保留分隔符前后的内容
                    parts = content.split(separator)
                    # 过滤空条目，并重新组合（每条信息 + 分隔符）
                    entries = []
                    for i, part in enumerate(parts):
                        part = part.strip()
                        if part:
                            entries.append(part)
                    # 取最后 limit 条
                    if entries:
                        last_entries = entries[-limit:]
                        # 重新组合，每条后面加上分隔线
                        result = (separator + "\n").join(last_entries)
                        if result:
                            return f"\n共享关键信息（来自其他会话，最近{len(last_entries)}条）:\n{result}\n{separator}\n"
    except Exception:
        pass
    return ""


def _extract_key_info(observation: str, thought: str, tool: str, llm) -> Optional[Dict[str, str]]:
    """从观察结果中提取客观的关键信息。
    
    关键信息包括：默认口令、新路径、新API等。
    注意：已确定的漏洞点不算关键信息。
    直接使用LLM判断是否是客观的关键信息。
    
    Args:
        observation: 工具执行的观察结果
        thought: 当前思考
        tool: 使用的工具
        llm: LLM实例，用于判断是否是关键信息
        
    Returns:
        如果是关键信息，返回 {"info": "..."}，否则返回 None
    """
    if not observation or len(observation) < 10 or llm is None:
        return None
    
    try:
        verify_prompt = (
            "请判断以下信息中是否包含客观的关键信息 只包括默认口令、新的路径、新的API\n"
            f"思考: {thought}\n"
            f"观察: {observation}\n"
            f"使用的工具: {tool}\n\n"
            "判断标准：\n"
            "1. 必须是客观的、可验证的信息（如具体的路径、API、口令等）\n"
            "2. 必须对解题有实际帮助\n"
            "3. 不要提取服务器指纹、版本号等基础信息（除非是特定的漏洞版本）\n"
            "4. 不要提取已经广泛知道的信息\n"
            "6. 信息必须简洁明确，避免冗长描述\n\n"
            "7. 你需要从思考和观察中综合判断信息是否正确\n"
            "关键信息示例：默认口令、新发现的路径、新发现的API端点、配置文件位置等。\n"
            "如果是客观的关键信息，返回JSON: {\"is_key\": true, \"info\": \"关键信息（简洁提取，不超过50字）\"}\n"
            "如果不是关键信息，返回JSON: {\"is_key\": false}\n"
            "只返回JSON，不要其他内容。"
        )
        verify_msgs = [
            SystemMessage(content="你是一个信息提取助手，负责识别客观的关键信息。只识别确实存在的、可验证的关键信息，如默认口令、新发现的路径/API等。"),
            HumanMessage(content=verify_prompt),
        ]
        verify_resp = llm.invoke(verify_msgs)
        verify_text = verify_resp.content if isinstance(verify_resp.content, str) else json.dumps(verify_resp.content)
        vs, ve = verify_text.find("{"), verify_text.rfind("}")
        if vs != -1 and ve != -1:
            verify_obj = json.loads(verify_text[vs: ve + 1])
            if verify_obj.get("is_key"):
                info = verify_obj.get("info", "").strip()
                if info:
                    return {
                        "info": info
                    }
    except Exception:
        # LLM判断失败，不记录
        pass
    
    return None


def _should_print_llm_input() -> bool:
    """是否打印传给大模型的输入内容。

    默认关闭；当设置 CRUISER_PRINT_LLM_INPUT 为 "1" 或 "true" 时开启；同时若 CRUISER_QUIET=1 一律不打印。
    """
    if os.environ.get("CRUISER_QUIET") == "1":
        return False
    val = (os.environ.get("CRUISER_PRINT_LLM_INPUT", "0") or "0").lower().strip()
    return val in {"1", "true"}


def _render_msgs_for_print(msgs: List[object]) -> str:
    """将发送给 LLM 的消息渲染为可读文本。"""
    parts: List[str] = []
    for m in msgs:
        try:
            role = getattr(m, "type", None) or getattr(m, "role", None)
            if not role:
                # LangChain 消息类：SystemMessage/HumanMessage/AIMessage/ToolMessage
                cls_name = m.__class__.__name__.replace("Message", "").lower()
                role = "system" if "system" in cls_name else ("human" if "human" in cls_name else ("ai" if "ai" in cls_name else ("tool" if "tool" in cls_name else cls_name)))
            content = getattr(m, "content", "")
            if not isinstance(content, str):
                try:
                    content = json.dumps(content, ensure_ascii=False)
                except Exception:
                    content = str(content)
            parts.append(f"[{role}]\n{content}")
        except Exception:
            try:
                parts.append(str(m))
            except Exception:
                parts.append("<unprintable message>")
    return "\n---\n".join(parts)


def _print_llm_input(msgs: List[object], label: str = "") -> None:
    """打印将发送给大模型的消息内容。"""
    if not _should_print_llm_input():
        return
    try:
        rendered = _render_msgs_for_print(msgs)
        header = f"[LLM Input]{' ' + label if label else ''}"
        to_stderr = os.environ.get("CRUISER_DEBUG_STDERR") == "1"
        if to_stderr:
            print(header, file=sys.stderr)
            print(rendered, file=sys.stderr)
        else:
            print(header)
            print(rendered)
    except Exception:
        pass


def build_system_prompt() -> str:
    # 动态拼接工具说明，避免硬编码
    lines = [BASE_SYSTEM_PROMPT]
    # 注入当前环境的命令行安全工具概览
    if _SECURITY_TOOLS_OVERVIEW:
        lines.append("\n\n当前环境可用的命令行安全工具：" + _SECURITY_TOOLS_OVERVIEW + "\n")
    lines.append("\n可用工具 (name: description):\n")
    for t in ALL_TOOLS:
        desc = (t.description or "").strip()
        lines.append(f"- {t.name}: {desc}")
    return "".join(s if isinstance(s, str) else str(s) for s in lines)


def _tool_map() -> Dict[str, any]:
    return {t.name: t for t in ALL_TOOLS}


def _tool_specs_text() -> str:
    """为每个可用工具生成可读的参数规范与最小示例，帮助模型"感知"如何调用。"""
    parts: List[str] = ["\n工具参数规范与示例:\n"]
    # 预置最小示例（更贴近实际使用）
    minimal_examples = {
        "read_file": {"tool": "read_file", "args": {"path": "./README.md"}},
        "run_command": {"tool": "run_command", "args": {"command": "echo hello"}},
    }
    for t in ALL_TOOLS:
        desc = (t.description or "").strip()
        parts.append(f"- {t.name}: {desc}")
        schema = getattr(t, "args_schema", None)
        try:
            if schema is not None and hasattr(schema, "model_json_schema"):
                js = schema.model_json_schema()
                required = js.get("required", [])
                properties = js.get("properties", {})
                # 只展示精简信息：必填字段与字段描述
                props_lines: List[str] = []
                for k, v in properties.items():
                    pdesc = v.get("description") or ""
                    ptype = v.get("type") or v.get("anyOf") or ""
                    props_lines.append(f"    • {k}: {pdesc} (type={ptype})")
                parts.append(f"  必填字段: {required if required else '无'}")
                if props_lines:
                    parts.append("  字段:")
                    parts.extend(props_lines)
        except Exception:
            pass
        # 示例
        try:
            example = minimal_examples.get(t.name)
            if example:
                parts.append("  示例(JSON 一行):")
                parts.append("  " + json.dumps(example, ensure_ascii=False))
        except Exception:
            pass
        parts.append("")
    return "\n".join(parts)



def _react_until_flag(llm, user_input: str, flag_key: str = "flag", max_steps: int = 0, quiet: bool = False, challenge_code: Optional[str] = None) -> str:
    """Continuous ReAct loop that only stops when a JSON flag is produced.

    - max_steps <= 0 means unlimited.
    - Requires the model to set tool='none' only when it can output final JSON
      like {"flag":"flag_content"}. Premature stops are ignored and the loop continues.
    """
    tool_names = "|".join([t.name for t in ALL_TOOLS] + ["none"])  # 动态生成可选工具名
    conv_ctx = _format_conversation_context(10)

    # 读取可调超时策略（当前未在本函数内直接使用，保留在说明中）

    tool_names_list = [t.name for t in ALL_TOOLS]
    tool_names_str = f"可用工具名: {tool_names_list}。"
    tool_specs_text = _tool_specs_text()
    react_instruction = (
        REACT_INSTRUCTION_TEMPLATE.replace("{tool_names_placeholder}", tool_names_str, 1)
        .replace("{tool_specs_placeholder}", tool_specs_text)
        .replace("{tool_names_placeholder}", tool_names)
    )
    instruction = (
        build_system_prompt()
        + ("\n\n最近对话（最多 10 轮）：\n" + conv_ctx if conv_ctx else "")
        + react_instruction
    )

    def _replace_hint_line(q: str, hint: str) -> str:
        try:
            lines = q.splitlines()
            for i in range(len(lines)):
                if lines[i].strip().startswith("题目提示："):
                    lines[i] = f"题目提示：{hint}"
                    return "\n".join(lines)
            # 否则在最前面插入
            return f"题目提示：{hint}\n" + q
        except Exception:
            return q



    # 绑定当前题目的 challenge_code（由外部调度器下发或环境变量提供）
    current_challenge_code = os.environ.get("CRUISER_CHALLENGE_CODE") or (challenge_code or "")


    # 外层：按任务轮次循环（每轮为一次完整的目标扫描，会在找到 flag 后根据策略决定是否切换目标/等待）
    worker_id = os.environ.get("CRUISER_WORKER_ID", "unknown")
    while True:
        scratchpad = ""
        step = 0
        mapping = _tool_map()
        total_steps_for_target = 0
        hint_requested_for_this_target = False
        hint = ""
        gethint = False
        flag_confirmed = False
        cached_shared_info = ""  # 缓存的关键信息
        last_info_read_step = -1  # 记录上次读取关键信息的步数
        while True:
            # 任务切换由外部调度器（main）监控并控制；会话内不再轮询任务变更

            # 若有共享提示文件（由任一会话写入），则全体会话同步看到提示（可通过环境变量 CRUISER_DISABLE_HINT 禁用）
            if hint == "" and current_challenge_code and os.environ.get("CRUISER_DISABLE_HINT") != "1":
                try:
                    out_dir = os.path.join(os.getcwd(), "reports")
                    os.makedirs(out_dir, exist_ok=True)
                    shared_hint_path = os.path.join(out_dir, f"hint_{current_challenge_code}.txt")
                    if os.path.exists(shared_hint_path):
                        with open(shared_hint_path, "r", encoding="utf-8") as hf:
                            shared_hint = (hf.read() or "").strip()
                        if shared_hint:
                            hint = shared_hint
                            hint_requested_for_this_target = True
                except Exception:
                    pass
            if hint != "" and hint_requested_for_this_target and not gethint:
                gethint = True
                user_input = _replace_hint_line(user_input, hint)
            
            # 在阶段开始时（step == 0）或每10轮读取一次共享关键信息
            should_read_info = False
            if current_challenge_code and os.environ.get("CRUISER_DISABLE_HINT") != "1":
                if step == 0:
                    # 阶段开始时读取一次
                    should_read_info = True
                elif step > 0 and step % 10 == 0:
                    # 每10轮读取一次
                    should_read_info = True
            
            if should_read_info:
                cached_shared_info = _read_shared_info(current_challenge_code)
                last_info_read_step = step
                if cached_shared_info:
                    try:
                        print(f"[SharedInfo] 已加载共享关键信息（第{step}轮）", flush=True)
                    except Exception:
                        pass
            
            if total_steps_for_target == 100:
                break
            
            # 构建用户输入，包含共享关键信息（使用缓存的）
            user_content = f"{user_input}\n\n"
            if cached_shared_info:
                user_content += cached_shared_info + "\n"
            user_content += f"Scratchpad so far:\n{scratchpad}\n\n"
            user_content += "请输出 JSON。"
            
            msgs = [
                SystemMessage(content=instruction),
                HumanMessage(content=user_content),
            ]
            _print_llm_input( msgs, label="decision")
            try:
                ai: AIMessage = llm.invoke(msgs)  # type: ignore
            except Exception as e:  # noqa: BLE001
                if _is_context_limit_error(e):
                    # 达到上下文上限：开启新会话（重置 scratchpad 与计数）
                    if not quiet:
                        try:
                            print("[Session] 上下文已达上限，开启新的会话。")
                        except Exception:
                            pass
                    scratchpad = ""
                    step = 0
                    # 可选：重新获取 LLM 实例，规避潜在会话状态
                    try:
                        llm, _, _ = get_llm()
                    except Exception:
                        pass
                    continue
                # 其他错误：打印并继续尝试下一轮
                if not quiet or os.environ.get("CRUISER_DEBUG") == "1":
                    try:
                        print(f"[Error] LLM 调用失败: {e}")
                    except Exception:
                        pass
                continue

            # 仅在成功调用 LLM 后，才累计步数与总计，并打印 COUNT
            step += 1
            total_steps_for_target += 1
            try:
                print(f"[COUNT] {total_steps_for_target}", flush=True)
            except Exception:
                pass

            text = ai.content if isinstance(ai.content, str) else json.dumps(ai.content)

            # 提取 JSON
            try:
                start = text.find("{")
                end = text.rfind("}")
                obj = json.loads(text[start : end + 1])
            except Exception:
                # 无法解析，继续循环强制 JSON
                scratchpad += "Step %d\nThought: 模型输出非 JSON，继续约束其严格输出 JSON。\n\n" % step
                continue

            thought = obj.get("thought", "")
            tool = obj.get("tool", "none")
            args = obj.get("args", {}) or {}
            scratchpad += f"Step {step}\nThought: {thought}\n"

            if tool == "none":
                final = obj.get("final")
                if isinstance(final, str) and final:
                    # 若 final 是 JSON 并包含 flag，则捕获并按策略处理（不立即返回）
                    try:
                        fs, fe = final.find("{"), final.rfind("}")
                        if fs != -1 and fe != -1:
                            fobj = json.loads(final[fs : fe + 1])
                            flag_val = fobj.get(flag_key)
                            if isinstance(flag_val, str) and flag_val.strip():
                                flag_json = final[fs : fe + 1]
                                if flag_confirmed:
                                    try:
                                        print(f"\x1b[95;1m[FOUND FLAG] {flag_json}\x1b[0m", flush=True)
                                    except Exception:
                                        pass
                                    # 成功提交并确认 flag：立即结束会话，返回结果 JSON
                                    return flag_json
                                else:
                                    # 未经 submit_flag 成功确认，继续
                                    scratchpad += "Action: none\nObservation: 检测到 flag 字段但尚未通过 submit_flag 成功确认，继续流程。\n\n"
                                    continue
                    except Exception:
                        pass
                # 未提供有效 flag，继续
                scratchpad += "Action: none\nObservation: 试图提前停止但未提供有效 flag，继续扫描。\n\n"
                # 若设置了步数上限，则达到上限后保持继续（无限制），因此这里不强制终止
                continue

            fn = mapping.get(tool)
            if not fn:
                scratchpad += f"Action: INVALID_TOOL({tool})\nObservation: 未知工具\n\n"
                continue

            # 执行 Action
            try:
                out = fn.invoke(args)
            except Exception as e:  # noqa: BLE001
                out = f"[ERROR] 工具执行失败: {e}"


            scratchpad += f"Action: {tool}({json.dumps(args, ensure_ascii=False)})\nObservation:\n{out}\n"
            # submit_flag 成功确认
            try:
                if tool == "submit_flag":
                    txt = str(out or "")
                    if ("提交flag正确" in txt) or ("已经提交成功过了" in txt):
                        flag_confirmed = True
            except Exception:
                pass

            # 反思与下一步建议
            try:
                reflect_prompt = REFLECT_PROMPT

                reflect_system = build_system_prompt() + ("\n\n最近对话（最多 10 轮）：\n" + conv_ctx if conv_ctx else "")
                msgs_reflect = [
                    SystemMessage(content=reflect_system),
                    HumanMessage(
                        content=(
                            f"Question: {user_input}\n\n"
                            f"Scratchpad so far:\n{scratchpad}\n\n"
                            f"Observation (latest):\n{out}\n\n"
                            + reflect_prompt
                        )
                    ),
                ]
                _print_llm_input(msgs_reflect, label="reflection")
                rmsg: AIMessage = llm.invoke(msgs_reflect)  # type: ignore
                rtxt = rmsg.content if isinstance(rmsg.content, str) else json.dumps(rmsg.content)
                rs, re = rtxt.find("{"), rtxt.rfind("}")
                robj = json.loads(rtxt[rs: re + 1]) if rs != -1 and re != -1 else {}
                reflection = robj.get("reflection") or ""
                suggested = robj.get("suggested_next") or ""
                if reflection:
                    scratchpad += f"Reflection: {reflection}\n"
                    try:
                        print(f"[Reflection] {reflection}")
                    except Exception:
                        pass
                if suggested:
                    scratchpad += f"Suggested next: {suggested}\n"
                    try:
                        print(f"[Next] {suggested}")
                    except Exception:
                        pass
                
                # 在反思阶段每6轮记录一次关键信息
                if current_challenge_code and step > 0 and step % 6 == 0:
                    try:
                        # 基于观察结果和反思内容提取关键信息
                        combined_context = f"{thought}\n{out}\n{reflection}"
                        key_info = _extract_key_info(combined_context, thought, tool, llm)
                        if key_info:
                            info_text = key_info.get("info", "").strip()
                            if info_text:
                                _write_shared_info(
                                    current_challenge_code,
                                    worker_id,
                                    info_text
                                )
                                try:
                                    print(f"[KeyInfo] 在反思阶段发现共享关键信息: {info_text[:50]}...", flush=True)
                                except Exception:
                                    pass
                    except Exception:
                        pass
            except Exception:
                scratchpad += "Reflection: (failed to generate)\n"

            scratchpad += "\n"

            # 若设置了有限步数，达到后也不终止，只是提示继续（满足“直到找到 flag”）
            if max_steps > 0 and step >= max_steps:
                scratchpad += f"[Info] 已达到步数上限 {max_steps}，继续循环直至找到 flag。\n\n"
                # 重置计数但保留 scratchpad 以便继续
                step = 0


def run_auto_scan(target: str, max_steps: int = 0, quiet: bool = False, hint: Optional[str] = None, challenge_code: Optional[str] = None) -> str:
    """Run a non-interactive LLM-driven vulnerability scan against a target.

    The LLM is instructed to perform web vulnerability discovery and exploitation
    using available tools, and to stop with a strict one-line JSON: {"flag":"..."}
    or {"flag": null} when not found.
    """
    # 启动时刷新一次安全工具概览
    try:
        _refresh_security_tools_overview()
    except Exception:
        pass

    llm, provider, supports_tools = get_llm()
    if llm is None:
        try:
            return json.dumps({"flag": None, "error": "LLM not available"}, ensure_ascii=False)
        except Exception:
            return '{"flag": null, "error": "LLM not available"}'

    worker_id = os.environ.get("CRUISER_WORKER_ID")
    worker_hint = WORKER_HINT_TEMPLATE.format(worker_id=worker_id) if worker_id else ""
    if hint:
        hint_line = f"题目提示：{hint}\n"
    else:
        hint_line = "题目提示：暂无\n"
    code_line = f"题目代码：{challenge_code}\n" if challenge_code else ""
    ws_dir = os.environ.get("CRUISER_WORKSPACE_DIR")
    ws_line = f"会话工作空间目录：{ws_dir}\n" if ws_dir else ""
    question = SCAN_QUESTION_TEMPLATE.format(
        target=target,
        worker_hint=worker_hint,
        code_line=code_line,
        ws_line=ws_line,
        hint_line=hint_line,
    )

    # 自动模式静默中间输出
    prev_quiet = os.environ.get("CRUISER_QUIET")
    if quiet:
        os.environ["CRUISER_QUIET"] = "1"
    try:
        # 连续扫描直到模型给出合法 JSON flag
        result_json = _react_until_flag(llm, question, flag_key="flag", max_steps=max_steps, quiet=quiet, challenge_code=challenge_code)
        return result_json
    finally:
        # 还原环境
        if quiet:
            if prev_quiet is None:
                try:
                    del os.environ["CRUISER_QUIET"]
                except Exception:
                    pass
            else:
                os.environ["CRUISER_QUIET"] = prev_quiet


