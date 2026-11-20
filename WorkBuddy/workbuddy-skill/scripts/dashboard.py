#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy Dashboard · WorkBuddy 本地使用数据看板
==========================================
把 WorkBuddy 在你本机留下的使用数据，变成一块能直接打开看的实时看板：
Token 消耗（输入/输出/思考/缓存命中）、请求趋势、模型占比、工具调用排行、
会话记录、活跃热力图、技能库、工作区、系统状态。

零安装、零依赖、零上传：仅使用 Python3 标准库，数据始终留在你本机。
workbuddy.db 以 sqlite3 只读 URI(file:...?mode=ro) 打开，绝不写库、不改库；
traces/*.jsonl 仅只读流式解析。

快速开始（项目根目录）:
    Mac     ./start-mac.command
    Windows start-windows.bat
    Linux   ./start-linux.sh
    任意平台 python3 dashboard.py

首次启动自动发现 ~/.workbuddy（国内版）与 ~/.workbuddy-ai（国际版），
看板右上角可切换版本；找不到时回落到 demo/ 脱敏演示数据。

环境变量:
    PANEL_BIND       监听地址，默认 127.0.0.1（仅本机访问，安全默认值）
    PANEL_PORT       固定端口；不设置则从 8790 起自动顺延
    PANEL_TOKEN      访问口令，默认 admin；显式置空则免口令
    WORKBUDDY_EDITION     默认版本：cn（国内版）或 intl（国际版），默认 cn
    WORKBUDDY_DB     手动指定国内版 workbuddy.db 路径或其所在目录
    WORKBUDDY_TRACES 手动指定国内版 traces 目录
    WORKBUDDY_CN_DB / WORKBUDDY_CN_TRACES       同 WORKBUDDY_DB / WORKBUDDY_TRACES
    WORKBUDDY_INTL_DB / WORKBUDDY_INTL_TRACES   手动指定国际版数据路径
    WORKBUDDY_NO_AUTH=1   等同 PANEL_TOKEN 置空，直接免口令
    WORKBUDDY_DEMO=1      强制使用 demo/ 演示数据
    WORKBUDDY_NO_BROWSER=1 启动时不自动打开浏览器
"""

import glob
import json
import math
import os
import signal
import sqlite3
import sys
import threading
import time
import traceback
from collections import defaultdict
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath, PureWindowsPath
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import pathname2url

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(HERE, "index.html")
PORT_FILE = os.path.join(HERE, ".port")
DEMO_DIR = os.path.join(HERE, "demo")

VERSION = "1.0.0"
APP_NAME = "WorkBuddy Dashboard"
DEFAULT_TOKEN = "admin"

CACHE_TTL = 5.0            # 热数据缓存秒数
TRACES_TTL = 30.0          # traces 冷数据缓存秒数（全表流式扫描，较慢）
COLD_TTL = 60.0            # 冷数据后台刷新周期
PORT_START, PORT_END = 8790, 8810
START_TS = time.time()


def _env(name, default=""):
    v = os.environ.get(name)
    return default if v is None else str(v).strip()


BIND = _env("PANEL_BIND", "127.0.0.1") or "127.0.0.1"
_PORT_ENV = _env("PANEL_PORT", "")
FIXED_PORT = None
if _PORT_ENV:
    if _PORT_ENV.isdigit() and 0 < int(_PORT_ENV) < 65536:
        FIXED_PORT = int(_PORT_ENV)
    else:
        sys.stderr.write("PANEL_PORT 取值非法：%r（应为 1-65535 的整数）\n" % _PORT_ENV)
        sys.stderr.flush()
        raise SystemExit(2)


def _resolve_token():
    if _env("WORKBUDDY_NO_AUTH", "").lower() in ("1", "true", "yes", "on"):
        return ""
    raw = os.environ.get("PANEL_TOKEN")
    if raw is None:
        return DEFAULT_TOKEN
    return str(raw).strip()


PANEL_TOKEN = _resolve_token()

EDITIONS = {
    "cn": {"label": "国内版", "dir": ".workbuddy"},
    "intl": {"label": "国际版", "dir": ".workbuddy-ai"},
}
_ED_ENV = _env("WORKBUDDY_EDITION", "").lower()
DEFAULT_EDITION = "intl" if _ED_ENV in ("intl", "international", "ai", "global") else "cn"


def normalize_edition(e):
    e = str(e or "").strip().lower()
    if e in ("intl", "international", "ai", "global"):
        return "intl"
    if e in ("cn", "domestic", "china"):
        return "cn"
    return DEFAULT_EDITION


_last_source = {}


def log(msg):
    try:
        sys.stdout.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
        sys.stdout.flush()
    except Exception:
        pass


# ---------------------------------------------------------------- 数据源发现
def user_home():
    return os.path.expanduser("~")


def discover_sources(edition=None):
    """返回 (db_path, traces_dir, source_label)。按版本与优先级自动发现。"""
    edition = normalize_edition(edition)
    demo_mode = _env("WORKBUDDY_DEMO", "").lower() in ("1", "true", "yes", "on")
    if demo_mode:
        return (os.path.join(DEMO_DIR, "workbuddy.db"),
                os.path.join(DEMO_DIR, "traces"), "demo")

    prefix = "WORKBUDDY_CN" if edition == "cn" else "WORKBUDDY_INTL"
    db_env = _env(prefix + "_DB", "")
    tr_env = _env(prefix + "_TRACES", "")
    if edition == "cn":
        db_env = db_env or _env("WORKBUDDY_DB", "")
        tr_env = tr_env or _env("WORKBUDDY_TRACES", "")

    home = user_home()
    base_dir = os.path.join(home, EDITIONS[edition]["dir"])
    default_db = os.path.join(base_dir, "workbuddy.db")
    default_tr = os.path.join(base_dir, "traces")

    db_path = ""
    traces_dir = ""
    if db_env:
        if os.path.isdir(db_env):
            cand = os.path.join(db_env, "workbuddy.db")
            db_path = cand if os.path.isfile(cand) else db_env
        elif os.path.isfile(db_env):
            db_path = db_env
    if not db_path and os.path.isfile(default_db):
        db_path = default_db
    if tr_env and os.path.isdir(tr_env):
        traces_dir = tr_env
    if not traces_dir and os.path.isdir(default_tr):
        traces_dir = default_tr

    if db_path and os.path.isfile(db_path):
        return db_path, traces_dir, "local"

    # 回落演示数据
    demo_db = os.path.join(DEMO_DIR, "workbuddy.db")
    demo_tr = os.path.join(DEMO_DIR, "traces")
    if os.path.isfile(demo_db):
        return demo_db, demo_tr, "demo"
    return "", "", "none"


def editions_info():
    """各版本的本机数据可用性，供前端切换控件展示。"""
    out = []
    for eid in ("cn", "intl"):
        _, _, source = discover_sources(eid)
        out.append({"id": eid, "label": EDITIONS[eid]["label"],
                    "available": source == "local", "source": source})
    return out


# ---------------------------------------------------------------- 只读工具
class _ROConn:
    def __init__(self, con):
        self._con = con

    def execute(self, sql, args=()):
        return self._con.execute(sql, args)

    def cursor(self):
        return self._con.cursor()

    def close(self):
        try:
            self._con.close()
        except Exception:
            pass


def ro_connect(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        uri = "file:%s?mode=ro" % pathname2url(os.path.abspath(path))
        con = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
        con.execute("PRAGMA query_only=ON")
        con.row_factory = sqlite3.Row
        return _ROConn(con)
    except Exception:
        return None


def rows(con, sql, args=()):
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except Exception:
        return []


def one(con, sql, args=(), default=None):
    try:
        r = con.execute(sql, args).fetchone()
        return dict(r) if r is not None else default
    except Exception:
        return default


def num(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def to_local_dt(s):
    """把 ISO 字符串或秒/毫秒时间戳统一转成当地 datetime。"""
    if s is None or s == "":
        return None
    # 数值时间戳：毫秒(1e12+)自动换算成秒
    if isinstance(s, (int, float)) or (isinstance(s, str) and s.isdigit()):
        try:
            v = float(s)
            if abs(v) >= 1e11:
                v /= 1000.0
            return datetime.fromtimestamp(v)
        except Exception:
            return None
    try:
        x = str(s)
        if x.endswith("Z"):
            x = x[:-1] + "+00:00"
        dt = datetime.fromisoformat(x)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except Exception:
        return None


def fmt_dt(s):
    dt = to_local_dt(s)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def in_date_range(day, start, end):
    return bool(day) and (not start or start <= day) and (not end or day <= end)


# ---------------------------------------------------------------- traces 采集
_TRACES = {}
_TRACES_LOCK = threading.Lock()


def _tr_state(edition):
    with _TRACES_LOCK:
        st = _TRACES.get(edition)
        if st is None:
            st = {"ts": 0.0, "data": None, "running": False}
            _TRACES[edition] = st
        return st


def scan_traces(traces_dir):
    """只保留统计所需的事件字段，并读取追踪与会话的精确关联。"""
    files = glob.glob(os.path.join(traces_dir, "**", "*.jsonl"), recursive=True) if traces_dir else []
    trace_status = {"ok": 0, "error": 0, "running": 0}
    status_map = {}
    trace_sessions = {}
    seen_gen = set()
    generations = []
    tools = []

    for f in files:
        try:
            fh = open(f, "r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue

                etype = event.get("type")
                trace = event.get("trace")
                if isinstance(trace, dict):
                    trace_id = trace.get("traceId")
                    session_id = trace.get("sessionId")
                    if trace_id and isinstance(session_id, str) and session_id:
                        trace_sessions[trace_id] = session_id
                    if etype in ("trace_start", "trace_end", "trace") and trace_id and trace.get("status"):
                        status_map[trace_id] = trace["status"]

                span = event.get("span")
                if etype != "span_end" or not isinstance(span, dict):
                    continue
                stype = span.get("type")
                if stype not in ("generation", "function"):
                    continue
                trace_id = span.get("traceId")
                dt = to_local_dt(span.get("startedAt"))
                day = dt.date().isoformat() if dt else ""
                if stype == "function":
                    tools.append((trace_id, day, str(span.get("name") or span.get("toolName") or "unknown")))
                    continue

                span_id = span.get("spanId")
                if span_id in seen_gen:
                    continue
                raw = span.get("toolOutput")
                try:
                    items = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    continue
                if not isinstance(items, list):
                    continue
                for item in items:
                    usage = item.get("usage") if isinstance(item, dict) else None
                    if not isinstance(usage, dict) or not usage:
                        continue
                    if span_id:
                        seen_gen.add(span_id)
                    pt = num(usage.get("prompt_tokens"))
                    ct = num(usage.get("completion_tokens"))
                    tt = num(usage.get("total_tokens")) or (pt + ct)
                    ptd = usage.get("prompt_tokens_details")
                    ctd = usage.get("completion_tokens_details")
                    cached = num(ptd.get("cached_tokens")) if isinstance(ptd, dict) else 0
                    reasoning = num(ctd.get("reasoning_tokens")) if isinstance(ctd, dict) else 0
                    model = str(item.get("model") or "unknown")
                    hour = (dt.weekday(), dt.hour) if dt else None
                    generations.append((trace_id, day, model, tt, pt, ct, cached, reasoning, hour))

    for status in status_map.values():
        trace_status[status if status in trace_status else "running"] += 1
    return {"generations": generations, "tools": tools, "sessions": trace_sessions,
            "trace_count": len(files), "trace_status": trace_status}


def summarize_usage(generations, tool_events, *, trends=False):
    tokens = dict(total=0, input=0, output=0, cached=0, reasoning=0, requests=0, peak=0, credits=0.0)
    models = {}
    tools = {}
    daily = {}
    monthly = {}
    heatmap = [[0] * 24 for _ in range(7)]
    for _, day, model, total, input_tokens, output_tokens, cached, reasoning, hour in generations:
        tokens["total"] += total
        tokens["input"] += input_tokens
        tokens["output"] += output_tokens
        tokens["cached"] += cached
        tokens["reasoning"] += reasoning
        tokens["requests"] += 1
        tokens["peak"] = max(tokens["peak"], total)

        m = models.setdefault(model, dict(tokens=0, requests=0, input=0, output=0, cached=0))
        m["tokens"] += total
        m["requests"] += 1
        m["input"] += input_tokens
        m["output"] += output_tokens
        m["cached"] += cached

        if hour is not None:
            weekday, slot = hour
            heatmap[weekday][slot] += 1
        if trends and day:
            d = daily.setdefault(day, dict(tokens=0, requests=0, output=0, cached=0))
            d["tokens"] += total
            d["requests"] += 1
            d["output"] += output_tokens
            d["cached"] += cached
            m = monthly.setdefault(day[:7], dict(tokens=0, requests=0))
            m["tokens"] += total
            m["requests"] += 1

    for _, _, name in tool_events:
        tools[name] = tools.get(name, 0) + 1
    tokens["avg"] = round(tokens["total"] / tokens["requests"]) if tokens["requests"] else 0
    tokens["cache_hit_rate"] = round(tokens["cached"] * 100.0 / tokens["input"], 2) if tokens["input"] else 0.0
    return {
        "tokens": tokens,
        "models": sorted((dict(name=k, **v) for k, v in models.items()),
                         key=lambda x: x["tokens"], reverse=True),
        "tools": sorted((dict(name=k, count=v) for k, v in tools.items()),
                        key=lambda x: x["count"], reverse=True),
        "tool_calls": sum(tools.values()), "heatmap": heatmap,
        "daily": daily, "monthly": monthly,
    }


def traces_snapshot(traces_dir, edition):
    st = _tr_state(edition)
    age = time.time() - st["ts"]
    if age > TRACES_TTL and not st["running"] and st["data"] is None:
        return _refresh_traces_sync(traces_dir, edition)
    if age > COLD_TTL and not st["running"]:
        _kick_traces_refresh(traces_dir, edition)
    with _TRACES_LOCK:
        data = st["data"]
    return (data if data is not None else _refresh_traces_sync(traces_dir, edition)), age


def _refresh_traces_sync(traces_dir, edition):
    data = scan_traces(traces_dir)
    st = _tr_state(edition)
    with _TRACES_LOCK:
        st["data"] = data
        st["ts"] = time.time()
    return data, 0.0


def _kick_traces_refresh(traces_dir, edition):
    st = _tr_state(edition)
    st["running"] = True

    def _run():
        try:
            _refresh_traces_sync(traces_dir, edition)
        except Exception:
            pass
        finally:
            st["running"] = False

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------- db 采集
def collect_sessions(con):
    sql = """
        SELECT s.id, s.title, s.custom_title, s.model, s.mode, s.status,
               s.created_at, s.updated_at, s.last_activity_at, s.is_background_automation,
               s.cwd, u.used, u.size, u.credit_json
        FROM sessions s
        LEFT JOIN session_usage u ON u.rowid = (
            SELECT rowid FROM session_usage
            WHERE session_id = s.id
            ORDER BY updated_at DESC, rowid DESC LIMIT 1
        )
        WHERE s.deleted_at IS NULL OR s.deleted_at = 0
        ORDER BY s.last_activity_at DESC
    """
    out = []
    for r in rows(con, sql):
        title = r.get("custom_title") or r.get("title") or "(无标题会话)"
        credit = None
        try:
            obj = json.loads(r["credit_json"]) if r["credit_json"] else None
            if isinstance(obj, dict):
                total = math.fsum(float(v) for v in obj.values())
                if math.isfinite(total):
                    credit = total
        except (ValueError, TypeError, OverflowError):
            pass

        cwd = r["cwd"]
        project_path = None
        if isinstance(cwd, str) and "\x00" not in cwd:
            if PurePosixPath(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute():
                project_path = cwd
        used, size = num(r["used"], None), num(r["size"], None)
        out.append({
            "id": r["id"],
            "title": str(title)[:120],
            "model": r.get("model") or "",
            "mode": r.get("mode") or "",
            "status": r.get("status") or "",
            "created": fmt_dt(r.get("created_at")),
            "updated": fmt_dt(r.get("updated_at")),
            "last_activity": fmt_dt(r.get("last_activity_at")),
            "used": used,
            "size": size,
            "context_percent": round(100.0 * used / size, 2)
                if used is not None and used >= 0 and size is not None and size > 0 else None,
            "credit": credit,
            "cwd": cwd or "",
            "project_path": project_path,
            "background": bool(r.get("is_background_automation")),
        })
    return out


def collect_skills(con):
    sql = """SELECT name, source, description, description_zh, disabled,
                    user_invocable, version, size, indexed_at
             FROM local_skills ORDER BY name"""
    raw = rows(con, sql)
    out = []
    by_source = {}
    by_type = {}
    invocable = 0
    enabled = 0
    for r in raw:
        name = r.get("name") or "unknown"
        src = r.get("source") or "unknown"
        dis = bool(r.get("disabled"))
        inv = bool(r.get("user_invocable"))
        desc = r.get("description_zh") or r.get("description") or ""
        out.append({
            "name": name, "source": src,
            "description": str(desc)[:160],
            "version": r.get("version") or "",
            "disabled": dis, "user_invocable": inv,
            "updated": fmt_dt(r.get("indexed_at")),
        })
        by_source[src] = by_source.get(src, 0) + 1
        if inv:
            invocable += 1
        if not dis:
            enabled += 1
        # 粗略类型：从描述或名字里识别
        low = (name + " " + str(desc)).lower()
        typ = "其他"
        for key, label in (("ppt", "PPT/演示"), ("doc", "文档"), ("excel", "表格"),
                           ("sheet", "表格"), ("pdf", "PDF"), ("image", "图像"),
                           ("design", "设计"), ("search", "检索"), ("web", "网页")):
            if key in low:
                typ = label
                break
        by_type[typ] = by_type.get(typ, 0) + 1
    stats = {"total": len(raw), "enabled": enabled, "invocable": invocable,
             "by_source": by_source, "by_type": by_type}
    return out, stats


def collect_workspaces(con):
    raw = rows(con, "SELECT path, last_opened_at FROM workspaces ORDER BY last_opened_at DESC")
    return [{"path": r.get("path") or "",
             "last_opened": fmt_dt(r.get("last_opened_at"))} for r in raw]


def project_label(path, registered_paths, missing_name):
    if path is None:
        return {"path": None, "name": missing_name, "workspace_registered": False}
    if path.startswith("/") and not path.startswith("//"):
        name = PurePosixPath(path).name
    else:
        parsed = PureWindowsPath(path)
        name = parsed.name or parsed.drive.rsplit("\\", 1)[-1]
    return {"path": path, "name": name or path, "workspace_registered": path in registered_paths}


def project_overview(sessions, registered_paths):
    groups = {}
    for session in sessions:
        groups.setdefault(session["project_path"], []).append(session)
    projects = []
    for path, members in groups.items():
        statuses = {}
        models = {}
        for session in members:
            status = session["status"] or "unknown"
            model = session["model"] or "unknown"
            statuses[status] = statuses.get(status, 0) + 1
            models[model] = models.get(model, 0) + 1
        credits = [s["credit"] for s in members if s["credit"] is not None]
        contexts = [s for s in members if s["context_percent"] is not None]
        used = sum(s["used"] for s in contexts)
        size = sum(s["size"] for s in contexts)
        projects.append({
            **project_label(path, registered_paths, "无有效工作目录"),
            "sessions": len(members),
            "statuses": statuses,
            "background_sessions": sum(s["background"] for s in members),
            "last_activity": max(s["last_activity"] for s in members),
            "session_models": models,
            "context_used": used if contexts else None,
            "context_size": size if contexts else None,
            "context_percent": round(100.0 * used / size, 2) if contexts else None,
            "context_sessions": len(contexts),
            "credits": round(math.fsum(credits), 2) if credits else None,
            "credit_sessions": len(credits),
        })
    return sorted(projects, key=lambda p: (-p["sessions"], p["path"] or ""))


def project_usage(generations, tools, trace_sessions, session_paths, registered_paths, global_usage):
    trace_paths = {tid: session_paths.get(sid) for tid, sid in trace_sessions.items()}
    groups = defaultdict(lambda: ([], []), {None: ([], [])})
    for event in generations:
        groups[trace_paths.get(event[0])][0].append(event)
    for event in tools:
        groups[trace_paths.get(event[0])][1].append(event)

    projects = []
    for path, (project_generations, project_tools) in groups.items():
        usage = summarize_usage(project_generations, project_tools)
        projects.append({
            **project_label(path, registered_paths, "未归属项目"),
            "tokens": usage["tokens"],
            "models": usage["models"],
            "tools": usage["tools"],
            "tool_calls": usage["tool_calls"],
            "heatmap": usage["heatmap"],
        })
    projects.sort(key=lambda p: (p["path"] is None, -p["tokens"]["total"], p["path"] or ""))
    unassigned = projects[-1]
    coverage = {}
    for key, total, unattributed in (
        ("requests", global_usage["tokens"]["requests"], unassigned["tokens"]["requests"]),
        ("tokens", global_usage["tokens"]["total"], unassigned["tokens"]["total"]),
        ("tool_calls", global_usage["tool_calls"], unassigned["tool_calls"]),
    ):
        attributed = total - unattributed
        coverage[key] = {"attributed": attributed, "total": total,
                         "percent": round(100.0 * attributed / total, 2) if total else None}
    return projects, coverage


def fill_daily(daily_map):
    """补齐活跃跨度内逐日，返回按日期升序的列表。"""
    if not daily_map:
        return []
    keys = [k for k in daily_map if k]
    if not keys:
        return []
    keys.sort()
    try:
        start = datetime.strptime(keys[0], "%Y-%m-%d")
        end = datetime.strptime(keys[-1], "%Y-%m-%d")
    except Exception:
        return [dict(v, day=k) for k, v in sorted(daily_map.items())]
    out = []
    for offset in range((end - start).days + 1):
        k = (start.date() + timedelta(days=offset)).isoformat()
        v = daily_map.get(k) or {"tokens": 0, "requests": 0, "output": 0, "cached": 0}
        out.append({"day": k, "tokens": v["tokens"], "requests": v["requests"],
                    "output": v["output"], "cached": v["cached"]})
    return out


def day_span(first_day, last_day):
    try:
        return (datetime.strptime(last_day, "%Y-%m-%d") -
                datetime.strptime(first_day, "%Y-%m-%d")).days + 1
    except Exception:
        return 0


# ---------------------------------------------------------------- 汇总
def compute_stats(edition=None, start="", end=""):
    edition = normalize_edition(edition)
    db_path, traces_dir, source = discover_sources(edition)
    _last_source[edition] = source
    today = datetime.now().strftime("%Y-%m-%d")

    tr, traces_age = traces_snapshot(traces_dir, edition)
    con = ro_connect(db_path)
    try:
        all_sessions = collect_sessions(con) if con else []
        skills, skill_stats = collect_skills(con)
        workspaces = collect_workspaces(con) if con else []
    finally:
        if con:
            con.close()

    sessions = all_sessions
    generations = tr["generations"]
    tools = tr["tools"]
    if start or end:
        sessions = [s for s in all_sessions if in_date_range(s["created"][:10], start, end)]
        generations = [event for event in generations if in_date_range(event[1], start, end)]
        tools = [event for event in tools if in_date_range(event[1], start, end)]
    registered_paths = {w["path"] for w in workspaces}
    session_paths = {s["id"]: s["project_path"] for s in all_sessions}
    usage = summarize_usage(generations, tools, trends=True)
    projects = project_overview(sessions, registered_paths)
    usages, coverage = project_usage(generations, tools, tr["sessions"], session_paths,
                                     registered_paths, usage)

    tk = usage["tokens"]
    daily_list = fill_daily(usage["daily"])
    days_with_data = sorted(day for day, d in usage["daily"].items() if d["tokens"] > 0)
    tk["active_days"] = len(days_with_data)
    tk["first_day"] = days_with_data[0] if days_with_data else ""
    tk["last_day"] = days_with_data[-1] if days_with_data else ""
    tk["span_days"] = day_span(tk["first_day"], tk["last_day"])

    status = tr["trace_status"]
    payload = {
        "range": {"from": start, "to": end},
        "tokens": tk,
        "daily": daily_list,
        "monthly": [dict(month=k, **v) for k, v in sorted(usage["monthly"].items())],
        "models": usage["models"],
        "tools": usage["tools"],
        "heatmap": usage["heatmap"],
        "conversations": sessions,
        "projects": projects,
        "project_usage": usages,
        "project_coverage": coverage,
        "skills": skills,
        "skill_stats": skill_stats,
        "workspaces": workspaces[:50],
        "counts": {
            "sessions": len(sessions), "traces": tr["trace_count"],
            "traces_ok": status["ok"], "traces_error": status["error"],
            "tool_calls": usage["tool_calls"], "models": len(usage["models"]),
            "tools": len(usage["tools"]), "skills": skill_stats["total"],
            "workspaces": min(50, len(workspaces)), "active_days": tk["active_days"],
            "questions": status["ok"],
        },
        "today": {
            "date": today,
            "tokens": sum(event[3] for event in tr["generations"] if event[1] == today),
            "requests": sum(event[1] == today for event in tr["generations"]),
            "tool_calls": sum(event[1] == today for event in tr["tools"]),
            "sessions": sum(s["created"].startswith(today) for s in all_sessions),
        },
    }

    db_mtime = ""
    if db_path:
        try:
            db_mtime = datetime.fromtimestamp(os.path.getmtime(db_path)).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            pass

    payload["meta"] = {
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db_path": db_path, "db_mtime": db_mtime,
        "traces_dir": traces_dir, "trace_files": tr["trace_count"],
        "traces_age_ms": round(traces_age * 1000.0, 1),
        "uptime_sec": round(time.time() - START_TS, 1),
        "source": source, "is_demo": source == "demo",
        "edition": edition, "edition_label": EDITIONS[edition]["label"],
        "editions": editions_info(),
        "app": APP_NAME, "version": VERSION,
    }

    return payload


# ---------------------------------------------------------------- 缓存
_LOCK = threading.Lock()
_COMPUTE_LOCK = threading.Lock()
_CACHE = {}


def _cache_state(edition):
    with _LOCK:
        st = _CACHE.get(edition)
        if st is None:
            st = {"ts": 0.0, "payload": None}
            _CACHE[edition] = st
        return st


def _cache_hit(age, edition):
    st = _cache_state(edition)
    with _LOCK:
        cached = st["payload"]
    if cached is None:
        return None
    out = dict(cached)
    meta = dict(cached.get("meta") or {})
    meta["cache_age_ms"] = round(age * 1000.0, 1)
    meta["server_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta["uptime_sec"] = round(time.time() - START_TS, 1)
    out["meta"] = meta
    return out


def get_stats(edition=None, start="", end=""):
    edition = normalize_edition(edition)
    if start or end:
        try:
            return compute_stats(edition, start, end)
        except Exception:
            log(traceback.format_exc(limit=2))
            return {"error": "无法读取所选日期的统计数据"}
    st = _cache_state(edition)
    age = time.time() - st["ts"]
    hit = _cache_hit(age, edition)
    if hit is not None and age <= CACHE_TTL:
        return hit
    with _COMPUTE_LOCK:
        age = time.time() - st["ts"]
        hit = _cache_hit(age, edition)
        if hit is not None and age <= CACHE_TTL:
            return hit
        try:
            payload = compute_stats(edition)
            with _LOCK:
                st["payload"] = payload
                st["ts"] = time.time()
            return _cache_hit(0.0, edition) or payload
        except Exception:
            hit = _cache_hit(age, edition)
            if hit is not None:
                return hit
            return {"error": traceback.format_exc(limit=2), "meta": {"app": APP_NAME,
                    "version": VERSION, "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}}


def warm_cache():
    try:
        get_stats()
    except Exception:
        pass


# ---------------------------------------------------------------- HTTP
LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(app)s · 登录</title><style>
:root{--bg:#090D18;--cyan:#38E8D4;--purple:#846BFF;--fg:#E8EDF7;--mut:#7D8AA8}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--bg);color:var(--fg);
font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;display:flex;align-items:center;justify-content:center}
.box{width:min(92vw,380px);background:#111831;border:1px solid #22305a;border-radius:16px;padding:34px;
box-shadow:0 20px 60px rgba(0,0,0,.5)}
.brand{font-size:12px;letter-spacing:3px;color:var(--cyan);font-weight:700}
h1{font-size:22px;margin:12px 0 6px}p{color:var(--mut);font-size:13px;margin:0 0 24px}
input{width:100%%;padding:13px 14px;border-radius:10px;border:1px solid #2a3964;background:#0b1122;
color:var(--fg);font-size:15px;outline:none}input:focus{border-color:var(--cyan)}
button{width:100%%;margin-top:14px;padding:13px;border:0;border-radius:10px;cursor:pointer;
background:linear-gradient(90deg,var(--cyan),var(--purple));color:#04121a;font-weight:800;font-size:15px}
.err{color:#ff7b7b;font-size:13px;margin-top:12px;min-height:18px}
.foot{margin-top:22px;font-size:11px;color:#556080;text-align:center}
</style></head><body><div class="box">
<div class="brand">◆ EVAN WORKBUDDY</div><h1>本地数据看板</h1>
<p>输入访问口令以查看你的本机使用数据</p>
<form method="get" action="/"><input type="hidden" name="login" value="1">
<input type="password" name="token" placeholder="访问口令" autofocus autocomplete="current-password">
<button type="submit">进入看板 →</button></form>
<div class="err">%(err)s</div>
<div class="foot">数据仅在本机流转 · 不上传 · 不联网</div>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkBuddy Dashboard/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _authorized(self, qs):
        if not PANEL_TOKEN:
            return True
        t = (qs.get("token") or [""])[0]
        if t == PANEL_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == ("Bearer " + PANEL_TOKEN)

    def _login_page(self, bad=False):
        html = LOGIN_HTML % {"app": APP_NAME, "err": "口令错误，请重试" if bad else ""}
        self._send(200, html, "text/html; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path == "/healthz":
            payload = {
                "ok": True, "app": APP_NAME, "version": VERSION,
                "source": _last_source.get(DEFAULT_EDITION, "auto"),
                "is_demo": _last_source.get(DEFAULT_EDITION) == "demo",
                "uptime_sec": round(time.time() - START_TS, 1),
                "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._send(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return

        if path == "/api/stats":
            if not self._authorized(qs):
                self._send(401, json.dumps({"error": "unauthorized"}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            edition = normalize_edition((qs.get("edition") or [""])[0])
            start = qs.get("from", [""])[0]
            end = qs.get("to", [""])[0]
            try:
                if any(value and date.fromisoformat(value).isoformat() != value for value in (start, end)):
                    raise ValueError()
                if start and end and start > end:
                    raise ValueError()
            except ValueError:
                self._send(400, json.dumps({"error": "日期范围无效"}, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            payload = get_stats(edition, start, end)
            self._send(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return

        if path in ("/", "/index.html"):
            if not self._authorized(qs):
                self._login_page(bad=(qs.get("login") or [""])[0] == "1")
                return
            try:
                with open(INDEX_FILE, "r", encoding="utf-8") as f:
                    html = f.read()
            except Exception as _ie:
                log("读取 index.html 失败：%s | path=%r" % (repr(_ie), INDEX_FILE))
                html = "<h3 style='color:red'>index.html 缺失</h3>"
            self._send(200, html, "text/html; charset=utf-8")
            return

        self._send(404, "Not Found", "text/plain; charset=utf-8")


def _make_server(host, port):
    return ThreadingHTTPServer((host, port), Handler)


def serve():
    warm_cache()
    port = None
    if FIXED_PORT is not None:
        try:
            srv = _make_server(BIND, FIXED_PORT)
            port = FIXED_PORT
        except OSError:
            log("端口 %d 已被占用（PANEL_PORT 固定模式），退出。" % FIXED_PORT)
            return
    else:
        for p in range(PORT_START, PORT_END + 1):
            try:
                srv = _make_server(BIND, p)
                port = p
                break
            except OSError:
                continue
        if port is None:
            log("端口 %d-%d 全部被占用，无法启动。" % (PORT_START, PORT_END))
            return

    url = "http://%s:%d/" % ("127.0.0.1" if BIND in ("0.0.0.0", "") else BIND, port)
    try:
        with open(PORT_FILE, "w") as f:
            f.write(url)
    except Exception:
        pass

    log("%s v%s 已启动：%s" % (APP_NAME, VERSION, url))
    for eid in ("cn", "intl"):
        db_path, traces_dir, source = discover_sources(eid)
        log("%s：%s | %s | traces %s" % (EDITIONS[eid]["label"], source,
                                          db_path or "无", traces_dir or "无"))
    log("默认版本：%s（看板内可切换）" % EDITIONS[DEFAULT_EDITION]["label"])
    log("访问口令：%s" % ("已启用（默认 admin）" if PANEL_TOKEN else "已关闭"))

    no_browser = _env("WORKBUDDY_NO_BROWSER", "").lower() in ("1", "true", "yes", "on")
    if not no_browser:
        try:
            threading.Timer(0.8, lambda: webbrowser.open(url + ("?token=" + PANEL_TOKEN if PANEL_TOKEN else ""))).start()
        except Exception:
            pass

    def _on_term(signum, frame):
        try:
            threading.Thread(target=srv.shutdown, daemon=True).start()
        except Exception:
            pass

    signal.signal(signal.SIGINT, _on_term)
    signal.signal(signal.SIGTERM, _on_term)
    try:
        srv.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
        try:
            os.remove(PORT_FILE)
        except Exception:
            pass
        log("已停止。")


if __name__ == "__main__":
    serve()
