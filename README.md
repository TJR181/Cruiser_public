# Cruiser

Cruiser 是一个基于 LLM 的智能 CTF Web 漏洞自动化挖掘与利用 Agent。它采用 ReAct 推理框架，结合多种安全工具，能够自动发现并利用 Web 应用中的漏洞，完成 CTF 挑战并提交 flag。

## 特性

- **ReAct 推理框架** — 通过 Thought → Action → Observation 循环进行多步推理和决策
- **多类型漏洞支持** — SQL 注入、XSS、CSRF、文件包含、文件上传、逻辑漏洞等
- **多会话并发** — 支持多个 Worker 并行扫描同一目标，采用差异化策略减少重复探索
- **多层级调度** — 预处理(30步) → 一级(50步) → 二级(70步) → 三级(100步) 逐级递进
- **内置安全工具** — 集成 dirsearch、sqlmap、fenjing 等工具链
- **自动化资源管理** — 内置用户名字典、密码字典、XSS Payload 字典

## 项目结构

```
Cruiser_public/
├── main.py                  # CLI 入口，题目调度与多会话管理
├── pyproject.toml           # 项目配置与依赖声明
├── uv.lock                  # uv 锁定文件
├── cruiser/
│   ├── __init__.py          # 包初始化，禁用 LangSmith 追踪
│   ├── agent.py             # ReAct Agent 循环，自动扫描核心逻辑
│   ├── llm.py               # LLM 提供者配置（DeepSeek）
│   ├── prompt.py            # 系统提示词与 ReAct 模板
│   └── tools.py             # LangChain 工具集（命令执行、文件读写、扫描等）
└── resource/
    ├── username.txt          # 用户名字典
    ├── password.txt          # 密码字典
    └── xss.txt               # XSS Payload 字典
```

## 环境要求

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/) 包管理器

## 安装

```bash
# 安装 uv（如未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 克隆项目
git clone https://github.com/your-org/Cruiser_public.git
cd Cruiser_public

# 安装依赖
uv sync
```

## 配置

运行前需要配置 LLM API 密钥。编辑 `cruiser/llm.py` 中的 `deepseek_key`，或通过环境变量设置：

```bash
export DEEPSEEK_MODEL="deepseek-chat"               # 模型名称（默认 deepseek-chat）
export DEEPSEEK_BASE_URL="https://api.deepseek.com/v1"  # API 地址
```

## 使用方法

### 单目标扫描

```bash
uv run python main.py --target http://127.0.0.1:8080 --challenge-code <CODE>
```

### 自动模式（从平台拉取未解题目）

```bash
uv run python main.py
```

### 多会话并发扫描

```bash
uv run python main.py --sessions 3 --target http://127.0.0.1:8080 --challenge-code <CODE>
```

### 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--target` | 无 | 扫描目标地址（如 `http://127.0.0.1:8080`） |
| `--challenge-code` | 无 | 题目代码（由调度器下发） |
| `--max-steps` | 0 | AI 扫描最大步数（0 = 无限制） |
| `--sessions` | 1 | 并发工作会话数量 |
| `--hint` | 无 | 题目提示（辅助 AI 推理） |
| `--quiet` | False | 静默中间输出，仅保留最终 JSON |
| `--default-timeout` | 0 | `run_command` 默认超时秒数（0 = 使用内置默认） |
| `--max-timeout` | 0 | `run_command` 超时上限秒数（0 = 使用内置上限） |

## 环境变量

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_MODEL` | DeepSeek 模型名称 |
| `DEEPSEEK_BASE_URL` | DeepSeek API 地址 |
| `CRUISER_CHALLENGE_CODE` | 当前题目代码 |
| `CRUISER_WORKER_ID` | Worker 会话编号 |
| `CRUISER_SESSIONS` | 总会话数 |
| `CRUISER_WORKSPACE_DIR` | 会话工作空间目录 |
| `CRUISER_TEMP_MIN` | LLM 温度下限（默认 0.0） |
| `CRUISER_TEMP_MAX` | LLM 温度上限（默认 0.9） |
| `CRUISER_DEFAULT_TIMEOUT` | 命令默认超时（秒） |
| `CRUISER_MAX_TIMEOUT` | 命令超时上限（秒） |
| `CRUISER_QUIET` | 设为 `1` 抑制中间输出 |
| `CRUISER_DEBUG` | 设为 `1` 开启调试输出 |

## 内置工具

| 工具 | 功能 |
|------|------|
| `run_command` | 执行系统命令（curl、grep 等） |
| `run_python` | 执行 Python 脚本 |
| `read_file` | 读取文件内容 |
| `submit_flag` | 提交 flag 到平台 |
| `find_resource` | 查找内置字典资源 |
| `dirsearch_scan` | 目录扫描（自动缓存与报告） |
| `fuzz_xss` | XSS Payload 模糊测试 |
| `list_security_tools` | 列出可用安全工具 |

## 许可证

本项目仅供安全研究与 CTF 竞赛学习使用。
