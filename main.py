from cruiser.agent import  run_auto_scan
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import re
import signal
import requests
import uuid
import random
from cruiser.tools import API_BASE_URL, API_TOKEN


def main():
    """Start in non-interactive scan mode (auto when --target missing) or multi-session."""
    parser = argparse.ArgumentParser(description="Cruiser - CTF Web agent")
    parser.add_argument("--target", type=str, default=None, help="扫描目标地址（如 http://127.0.0.1:8080 ）")
    parser.add_argument("--max-steps", type=int, default=0, help="AI 扫描最大步数（0=无限）")
    parser.add_argument("--quiet", action="store_true", help="静默中间输出，仅保留最终 JSON")
    parser.add_argument("--sessions", type=int, default=1, help="并发工作会话数量（>=1）")
    parser.add_argument("--default-timeout", type=int, default=0, help="run_command 默认超时（秒），0=使用内置默认")
    parser.add_argument("--max-timeout", type=int, default=0, help="run_command 超时上限（秒），0=使用内置上限")
    parser.add_argument("--hint", type=str, default=None, help="题目提示（可选，用于辅助 AI 推理）")
    parser.add_argument("--challenge-code", type=str, default=None, help="题目代码（由调度器下发）")
    args = parser.parse_args()
    # 将超时策略注入环境，供当前/子会话使用
    if args.default_timeout and args.default_timeout > 0:
        os.environ["CRUISER_DEFAULT_TIMEOUT"] = str(args.default_timeout)
    if args.max_timeout and args.max_timeout > 0:
        os.environ["CRUISER_MAX_TIMEOUT"] = str(args.max_timeout)

    DIFF_RANK = {"easy": 0, "medium": 1, "hard": 2}

    def fetch_all_unsolved() -> dict[str, dict] | None:
        """Return a map: code -> {challenge_code, target, difficulty} for all unsolved challenges.
        Returns None on fetch/parse errors or abnormal responses (do NOT update queues in that case).
        Returns {} when fetched successfully but no unsolved challenges are available.
        """
        try:
            resp = requests.get(f"{API_BASE_URL}/api/v1/challenges", timeout=20, headers={"Authorization": f"Bearer {API_TOKEN}"})
            if resp.status_code != 200:
                try:
                    print(f"[Fetcher] 非 200 响应: {resp.status_code} {resp.text}", flush=True)
                except Exception:
                    pass
                return None
            try:
                data = resp.json()
            except Exception:
                return None
            if not isinstance(data, dict):
                return None
            info = data.get("challenges", [])
            if not isinstance(info, list):
                return None
            result: dict[str, dict] = {}
            for item in info:
                if item.get("solved", False):
                    continue
                challenge_code = item.get("challenge_code", "") or ""
                target_info = item.get("target_info", {}) or {}
                ip = target_info.get("ip", "") or ""
                ports = target_info.get("port") or []
                urls = []
                for port in ports:
                    urls.append(f"http://{ip}:{port}" if port else ip)
                diff = (item.get("difficulty") or "").lower()
                payload = {"challenge_code": challenge_code, "target": ", ".join(urls) if urls else ip, "difficulty": diff}
                result[challenge_code] = payload
            return result
        except Exception as e:
            try:
                print(f"[Fetcher] 获取题目失败: {e}", flush=True)
            except Exception:
                pass
            return None

    def fetch_hint_text(code: str) -> str:
        base_delay = 0.5
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                resp = requests.get(f"{API_BASE_URL}/api/v1/hint/{code}", timeout=20, headers={"Authorization": f"Bearer {API_TOKEN}"})
                if resp.status_code == 200:
                    data = resp.json() or {}
                    hint_text = (data.get("hint_content") or "").strip()
                    if hint_text:
                        return hint_text
                else:
                    try:
                        print(f"[HintFetcher] 非 200 响应: {resp.status_code} {resp.text}", flush=True)
                    except Exception:
                        pass
            except Exception as e:
                try:
                    print(f"[HintFetcher] 获取提示异常: {e}", flush=True)
                except Exception:
                    pass
            if attempt < max_attempts - 1:
                # 随机退避：指数回退 + 抖动
                sleep_seconds = min(8.0, base_delay * (2 ** attempt)) * (0.5 + random.random())
                try:
                    print(f"[HintFetcher] 第 {attempt + 1}/{max_attempts} 次失败，随机退避 {sleep_seconds:.2f}s 后重试…", flush=True)
                except Exception:
                    pass
                time.sleep(sleep_seconds)
        return ""

    # 单会话：使用中心题目获取器获取题目并下发给会话；
    # 若提供了 --challenge-code，则直接按下发的题目运行一次并返回
    if args.sessions <= 1:
        if args.challenge_code:
            code = args.challenge_code
            target = args.target or ""
            if code:
                os.environ["CRUISER_CHALLENGE_CODE"] = code
            # 为单会话创建独立工作空间
            try:
                base_ws = "/tmp/cruiser_workspaces"
                os.makedirs(base_ws, exist_ok=True)
                ws_dir = os.path.join(base_ws, f"ws_single_{uuid.uuid4().hex[:8]}")
                os.makedirs(ws_dir, exist_ok=True)
                os.environ["CRUISER_WORKSPACE_DIR"] = ws_dir
            except Exception:
                pass
            result = run_auto_scan(target, max_steps=args.max_steps, quiet=args.quiet, hint=args.hint, challenge_code=code)
            try:
                obj = json.loads(result)
                if isinstance(obj, dict) and "flag" in obj:
                    print(json.dumps(obj, ensure_ascii=False))
                else:
                    print(json.dumps({"flag": result}, ensure_ascii=False))
            except Exception:
                print(json.dumps({"flag": result}, ensure_ascii=False))
            return
        while True:
            chal_map = fetch_all_unsolved()
            chal = None
            # Choose easiest available
            if isinstance(chal_map, dict) and chal_map:
                try:
                    sorted_items = sorted(chal_map.values(), key=lambda it: DIFF_RANK.get((it.get("difficulty") or "easy").lower(), 0))
                    chal = sorted_items[0] if sorted_items else None
                except Exception:
                    chal = None
            if chal_map is None:
                # Fetch error/timeout/abnormal -> do NOT change state, just retry later
                print("[Fetcher] 拉取失败（不更新队列），5 秒后重试…", flush=True)
                time.sleep(5)
                continue
            if not chal:
                try:
                    print("[Fetcher] 暂无可用题目，10 秒后重试…", flush=True)
                except Exception:
                    pass
                time.sleep(10)
                continue
            code = chal.get("challenge_code") or ""
            target = chal.get("target") or (args.target or "")
            if code:
                os.environ["CRUISER_CHALLENGE_CODE"] = code
            # 为单会话循环创建独立工作空间
            try:
                base_ws = "/tmp/cruiser_workspaces"
                os.makedirs(base_ws, exist_ok=True)
                ws_dir = os.path.join(base_ws, f"ws_single_{uuid.uuid4().hex[:8]}")
                os.makedirs(ws_dir, exist_ok=True)
                os.environ["CRUISER_WORKSPACE_DIR"] = ws_dir
            except Exception:
                pass
            result = run_auto_scan(target, max_steps=args.max_steps, quiet=args.quiet, hint=args.hint, challenge_code=code or None)
            try:
                obj = json.loads(result)
                if isinstance(obj, dict) and "flag" in obj:
                    print(json.dumps(obj, ensure_ascii=False))
                else:
                    print(json.dumps({"flag": result}, ensure_ascii=False))
            except Exception:
                print(json.dumps({"flag": result}, ensure_ascii=False))
            # 成功或失败后，继续下一题（由 fetcher 决定是否变更）
            time.sleep(2)
            continue

    # 多会话并发：启动子进程并行扫描，实时将每个会话的输出打印到标准输出；
    # 谁先输出含有 flag 的 JSON，就停止所有会话。
    num = max(1, int(args.sessions))

    def spawn_worker(idx: int, target: str, code: str | None, hint_text: str | None, env_overrides: dict | None = None):
        cmd = [
            sys.executable,
            "-u",  # unbuffered output
            "./main.py",
            "--max-steps",
            str(args.max_steps),
            "--sessions",
            "1",  # 子进程始终以单会话模式运行
        ]
        if target:
            cmd += ["--target", target]
        if code:
            cmd += ["--challenge-code", code]
        if hint_text:
            cmd += ["--hint", hint_text]
        elif args.hint:
            cmd += ["--hint", args.hint]
        if args.quiet:
            cmd.append("--quiet")
        env = os.environ.copy()
        env["CRUISER_WORKER_ID"] = str(idx)
        env["CRUISER_SESSIONS"] = str(num)
        if code:
            env["CRUISER_CHALLENGE_CODE"] = code
        if env_overrides:
            try:
                for k, v in env_overrides.items():
                    if v is None and k in env:
                        env.pop(k, None)
                    elif v is not None:
                        env[str(k)] = str(v)
            except Exception:
                pass
        # 为子会话创建独立工作空间
        try:
            base_ws = "/tmp/cruiser_workspaces"
            os.makedirs(base_ws, exist_ok=True)
            ws_dir = os.path.join(base_ws, f"ws_{(code or 'nocode')}_{idx}_{uuid.uuid4().hex[:8]}")
            os.makedirs(ws_dir, exist_ok=True)
            env["CRUISER_WORKSPACE_DIR"] = ws_dir
        except Exception:
            pass
        # 调试模式：所有输出走标准输出
        if not args.quiet:
            env.pop("CRUISER_DEBUG_STDERR", None)
            env["CRUISER_QUIET"] = "0"
        # 传递超时策略给子进程
        if args.default_timeout and args.default_timeout > 0:
            env["CRUISER_DEFAULT_TIMEOUT"] = str(args.default_timeout)
        if args.max_timeout and args.max_timeout > 0:
            env["CRUISER_MAX_TIMEOUT"] = str(args.max_timeout)
        # 强制子进程无缓冲输出
        env["PYTHONUNBUFFERED"] = "1"
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
        )

    # -------- 新流程：预处理并行 -> 一级并行(带提示) -> 二/三级串行 --------
    count_re = re.compile(r"\[COUNT\]\s*(\d+)")

    def run_parallel_stage(codes: list[str], items: dict[str, dict], step_threshold: int, prefetch_hint: bool, disable_hint: bool, kill_map: dict[str, threading.Event] | None = None) -> tuple[set[str], set[str]]:
        """并行对多个题目运行一轮，达到阈值或出 flag 即停止每个题目的会话组。
        返回 (flagged_codes, remaining_codes_not_solved)"""
        groups = {}
        for code in codes:
            target = (items.get(code) or {}).get("target") or ""
            pre_hint = None
            if prefetch_hint:
                pre_hint = fetch_hint_text(code)
                if pre_hint:
                    try:
                        out_dir = os.path.join(os.getcwd(), "reports")
                        os.makedirs(out_dir, exist_ok=True)
                        with open(os.path.join(out_dir, f"hint_{code}.txt"), "w", encoding="utf-8") as hf:
                            hf.write(pre_hint)
                    except Exception:
                        pass
            # 基础 env（可禁用提示）
            base_env_overrides = {"CRUISER_DISABLE_HINT": "1"} if disable_hint else {}
            procs = []
            # 第一波：3 个 session（带提示）
            blackbox_count = 3 if (prefetch_hint and not disable_hint) else 0
            for i in range(1, blackbox_count + 1):
                env_overrides_bb = dict(base_env_overrides)
                p = spawn_worker(i, target, code, pre_hint, env_overrides=env_overrides_bb)
                procs.append({"proc": p, "wave": 1})
                if i < blackbox_count:
                    time.sleep(2)
            # 第二波：--sessions 个 session（带提示）
            for j in range(1, num + 1):
                env_overrides_audit = dict(base_env_overrides)
                p2 = spawn_worker((blackbox_count + j), target, code, pre_hint, env_overrides=env_overrides_audit)
                procs.append({"proc": p2, "wave": 2})
                if j < num:
                    time.sleep(2)
            groups[code] = {
                "procs": procs,
                "counts": {},
                "winner": None,
                "stop_threshold": False,
                "start_ts": time.time(),
                "kill_evt": (kill_map.setdefault(code, threading.Event()) if kill_map is not None else threading.Event()),
            }

        def reader_thread(code: str, p: subprocess.Popen, idx: int, wave: int):
            grp = groups[code]
            prefix = f"[{code}:S{idx}] "
            if not p.stdout:
                return
            for raw in p.stdout:
                if grp["winner"] is not None or grp["stop_threshold"] or grp["kill_evt"].is_set():
                    break
                line = raw.rstrip("\n")
                # JSON flag?
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and "flag" in obj:
                        print(prefix + line, flush=True)
                        grp["winner"] = line
                        break
                except Exception:
                    pass
                if grp["kill_evt"].is_set():
                    break
                # COUNT
                m = count_re.search(line)
                if m:
                    try:
                        c = int(m.group(1))
                        grp["counts"][idx] = c
                        # 仅当属于第二波才触发步数阈值
                        if wave == 2 and c >= step_threshold and grp["winner"] is None:
                            try:
                                print(f"{prefix}[STAGE] 步数达到阈值 {c}/{step_threshold}，结束该题本轮", flush=True)
                            except Exception:
                                pass
                            grp["stop_threshold"] = True
                            break
                    except Exception:
                        pass
                print(prefix + line, flush=True)

        threads = []
        for code, grp in groups.items():
            for idx, entry in enumerate(grp["procs"], start=1):
                p = entry["proc"]
                wave = entry["wave"]
                t = threading.Thread(target=reader_thread, args=(code, p, idx, wave), daemon=True)
                t.start()
                threads.append(t)

        # 等待所有题目完成本轮（阈值或 flag）
        while True:
            all_done = True
            for code, grp in groups.items():
                if grp["kill_evt"].is_set():
                    grp["stop_threshold"] = True
                if grp["winner"] is None and not grp["stop_threshold"]:
                    # 检查进程是否全部退出
                    any_alive = False
                    for entry in grp["procs"]:
                        p = entry["proc"]
                        if p.poll() is None:
                            any_alive = True
                            break
                    if any_alive:
                        all_done = False
                        break
            if all_done:
                break
            try:
                time.sleep(0.2)
            except Exception:
                pass

        # 停掉所有进程（整组杀进程：先发 SIGTERM 到进程组，超时再 SIGKILL）
        for grp in groups.values():
            for entry in grp["procs"]:
                p = entry["proc"]
                if p.poll() is None:
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                    except Exception:
                        try:
                            p.terminate()
                        except Exception:
                            pass
            # 等待片刻后强制杀
            for entry in grp["procs"]:
                p = entry["proc"]
                try:
                    p.wait(timeout=3)
                except Exception:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except Exception:
                        try:
                            p.kill()
                        except Exception:
                            pass

        flagged = set()
        remaining = set()
        for code, grp in groups.items():
            if grp["winner"] is not None:
                flagged.add(code)
            else:
                remaining.add(code)
        return flagged, remaining

    preprocessed: set[str] = set()
    solved: set[str] = set()
    tier1: set[str] = set()
    tier2: list[str] = []
    tier3: list[str] = []
    stage_lock = threading.Lock()
    kill_flags: dict[str, threading.Event] = {}
    preproc_threads: dict[str, threading.Thread] = {}
    tier1_threads: dict[str, threading.Thread] = {}
    tier2_threads: dict[str, threading.Thread] = {}
    tier3_threads: dict[str, threading.Thread] = {}

    def _rank_code(c: str, items_ref: dict[str, dict]) -> int:
        return DIFF_RANK.get((items_ref.get(c, {}).get("difficulty") or "easy").lower(), 0)

    while True:
        items = fetch_all_unsolved()
        if items is None:
            print("[Fetcher] 拉取失败（等待 5 秒）", flush=True)
            time.sleep(5)
            continue
        # 即便列表为空也要做一次清理/强杀判断
        present = set(items.keys()) if isinstance(items, dict) else set()
        with stage_lock:
            preprocessed &= present
            solved &= present
            tier1 &= present
            tier2 = [c for c in tier2 if c in present]
            tier3 = [c for c in tier3 if c in present]
            # 对于已不在平台但仍在运行的题，触发强杀（包含平台列表为空的场景）
            for code in list(kill_flags.keys()):
                if code not in present:
                    try:
                        print(f"[Fetcher] 题目 {code} 已从平台移除，强制停止其会话…", flush=True)
                    except Exception:
                        pass
                    try:
                        kill_flags[code].set()
                    except Exception:
                        pass
        if not present:
            print("[Fetcher] 暂无可用题目，10 秒后重试…", flush=True)
            time.sleep(10)
            continue

        # 启动预处理（动态、不看提示、每题独立执行器，30步）
        new_codes = [c for c in present if c not in preprocessed]
        for code in new_codes:
            if code in preproc_threads:
                continue
            print(f"[Stage-Preprocess] 启动 {code} 的 30 步预处理…", flush=True)
            def _preproc_one(c: str, items_snap: dict[str, dict]):
                try:
                    flagged, remaining = run_parallel_stage([c], items_snap, step_threshold=30, prefetch_hint=False, disable_hint=True, kill_map=kill_flags)
                    with stage_lock:
                        preprocessed.add(c)
                        if c in flagged:
                            solved.add(c)
                        elif c in remaining:
                            tier1.add(c)
                finally:
                    with stage_lock:
                        preproc_threads.pop(c, None)
                        kill_flags.pop(c, None)
            t = threading.Thread(target=_preproc_one, args=(code, items.copy()), daemon=True)
            preproc_threads[code] = t
            t.start()

        # 启动一级（动态并行、带提示、每题独立执行器，50步）
        with stage_lock:
            tier1_ready = sorted([c for c in tier1 if c not in tier1_threads], key=lambda c: _rank_code(c, items))
        for code in tier1_ready:
            print(f"[Stage-Tier1] 启动 {code}（带提示）至 50 步…", flush=True)
            def _tier1_one(c: str, items_snap: dict[str, dict]):
                try:
                    flagged, remaining = run_parallel_stage([c], items_snap, step_threshold=50, prefetch_hint=True, disable_hint=False, kill_map=kill_flags)
                    with stage_lock:
                        if c in flagged:
                            solved.add(c)
                            tier1.discard(c)
                        else:
                            tier1.discard(c)
                            # 晋级到二级
                            tier2.append(c)
                finally:
                    with stage_lock:
                        tier1_threads.pop(c, None)
                        kill_flags.pop(c, None)
            t = threading.Thread(target=_tier1_one, args=(code, items.copy()), daemon=True)
            with stage_lock:
                tier1_threads[code] = t
            t.start()

        # 启动二级（并行、带提示、每题独立执行器，70步）
        with stage_lock:
            tier2_ready = [c for c in tier2 if c not in tier2_threads]
        for code in tier2_ready:
            print(f"[Stage-Tier2] 启动 {code}（带提示）至 70 步…", flush=True)
            def _tier2_one(c: str, items_snap: dict[str, dict]):
                try:
                    flagged, remaining = run_parallel_stage([c], items_snap, step_threshold=70, prefetch_hint=True, disable_hint=False, kill_map=kill_flags)
                    with stage_lock:
                        # 从二级移除
                        try:
                            tier2.remove(c)
                        except ValueError:
                            pass
                        if c in flagged:
                            solved.add(c)
                        else:
                            # 晋级到三级
                            tier3.append(c)
                finally:
                    with stage_lock:
                        tier2_threads.pop(c, None)
                        kill_flags.pop(c, None)
            t2 = threading.Thread(target=_tier2_one, args=(code, items.copy()), daemon=True)
            with stage_lock:
                tier2_threads[code] = t2
            t2.start()

        # 启动三级（并行、带提示、每题独立执行器，100步）
        with stage_lock:
            tier3_ready = [c for c in tier3 if c not in tier3_threads]
        for code in tier3_ready:
            print(f"[Stage-Tier3] 启动 {code}（带提示）至 100 步…", flush=True)
            def _tier3_one(c: str, items_snap: dict[str, dict]):
                try:
                    flagged, remaining = run_parallel_stage([c], items_snap, step_threshold=100, prefetch_hint=True, disable_hint=False, kill_map=kill_flags)
                    with stage_lock:
                        # 从三级移除
                        try:
                            tier3.remove(c)
                        except ValueError:
                            pass
                        if c in flagged:
                            solved.add(c)
                        else:
                            # 未解出，放回队尾
                            tier3.append(c)
                finally:
                    with stage_lock:
                        tier3_threads.pop(c, None)
                        kill_flags.pop(c, None)
            t3 = threading.Thread(target=_tier3_one, args=(code, items.copy()), daemon=True)
            with stage_lock:
                tier3_threads[code] = t3
            t3.start()

        # 小憩，等待下一轮调度
        time.sleep(1.0)


if __name__ == "__main__":
    main()
