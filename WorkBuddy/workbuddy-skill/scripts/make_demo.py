#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成脱敏演示数据到 demo/ 目录。
数据全部为脚本生成的假数据，不含任何真实会话、路径或用量信息。
"""
import json
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEMO = os.path.join(ROOT, "demo")
TRACES = os.path.join(DEMO, "traces")
random.seed(20261118)

MODELS = [
    ("fast-model", 0.55), ("balanced-model", 0.20), ("hy4free", 0.12),
    ("hy4flash-free", 0.08), ("wb-lite", 0.05),
]
TOOLS = [("Bash", 0.30), ("Read", 0.18), ("Edit", 0.12), ("Write", 0.10),
         ("WebSearch", 0.09), ("WebFetch", 0.06), ("Grep", 0.05), ("Glob", 0.03),
         ("DeferExecuteTool", 0.03), ("present_files", 0.02), ("Skill", 0.02)]
SKILLS = ["pptx 演示文稿", "docx 文档创作", "表格分析", "PDF 阅读",
          "思维导图", "图片处理", "网页搜索", "代码审查", "数据分析", "会议纪要"]
TITLES = ["季度市场分析报告", "产品需求梳理", "竞品调研与对比", "数据可视化看板",
          "用户画像研究", "技术方案评估", "年度规划草案", "品牌视觉规范",
          "客户反馈整理", "接口联调排查", "营销活动策划", "知识库构建"]
STATUSES = ["completed"] * 7 + ["error"] * 1 + ["working"] * 1 + ["cancelled"] * 1

# 示例包括同名目录、未登记目录、缺少目录、已删除会话及多次追踪。
PROJECT_PATHS = {
    0: "/Users/demo/Projects/project-01",
    1: "/Users/demo/Archive/project-01",
    2: None,
}
REGISTERED = ["/Users/demo/Projects/project-%02d" % i for i in (1, 2, 4, 7)]
TRACE_SESSIONS = {0: 0, 1: 1, 2: 0, 3: 2, 4: 31, 5: 4, 6: 6, 7: 7}


def pick(pairs):
    r = random.random()
    acc = 0
    for name, w in pairs:
        acc += w
        if r <= acc:
            return name
    return pairs[-1][0]


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def build_db():
    db = os.path.join(DEMO, "workbuddy.db")
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE sessions(
        id TEXT PRIMARY KEY, cwd TEXT, user_id TEXT, title TEXT, custom_title TEXT,
        status TEXT, created_at INTEGER, updated_at INTEGER, last_activity_at INTEGER,
        deleted_at INTEGER, is_playground INTEGER, source_mode TEXT,
        is_background_automation INTEGER, mode TEXT, model TEXT, expert_id TEXT)""")
    con.execute("""CREATE TABLE session_usage(
        session_id TEXT, used INTEGER, size INTEGER, updated_at INTEGER, credit_json TEXT)""")
    con.execute("CREATE TABLE workspaces(path TEXT, last_opened_at INTEGER)")
    con.execute("""CREATE TABLE local_skills(
        file_path TEXT, name TEXT, override_key TEXT, source TEXT, description TEXT,
        description_zh TEXT, disabled INTEGER, user_invocable INTEGER, version TEXT,
        size INTEGER, indexed_at INTEGER)""")

    base = datetime.now() - timedelta(days=45)
    n_sessions = 32
    for i in range(n_sessions):
        sid = "demo-%03d" % i
        created = base + timedelta(days=random.randint(0, 44), hours=random.randint(6, 23))
        updated = created + timedelta(minutes=random.randint(5, 180))
        used = random.randint(20000, 260000)
        size = random.choice([168000, 200000, 300000])
        credits = {pick(MODELS): round(random.uniform(0.5, 40), 2) for _ in range(random.randint(1, 4))}
        cwd = "/Users/demo/Projects/project-%02d" % random.randint(1, 8)
        con.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            sid, PROJECT_PATHS.get(i, cwd), "demo-user",
            random.choice(TITLES), None, random.choice(STATUSES),
            int(created.timestamp() * 1000), int(updated.timestamp() * 1000),
            int(updated.timestamp() * 1000), int(updated.timestamp() * 1000) if i == 31 else 0, 0, "local",
            1 if random.random() < 0.1 else 0, "craft", pick(MODELS), None))
        credit_json = json.dumps({pick_model: 0 for pick_model in credits} if i == 1 else credits)
        con.execute("INSERT INTO session_usage VALUES(?,?,?,?,?)", (
            sid, used, 0 if i == 2 else size, int(updated.timestamp() * 1000),
            None if i == 2 else credit_json))

    for i in range(12):
        t = base + timedelta(days=i * 3)
        con.execute("INSERT INTO workspaces VALUES(?,?)", (
            REGISTERED[i] if i < len(REGISTERED) else "/Users/demo/WorkBuddy/session-%04d" % (1000 + i * 7),
            int(t.timestamp() * 1000)))

    for i, name in enumerate(SKILLS):
        con.execute("INSERT INTO local_skills VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            "/demo/skills/%d" % i, name, None, "builtin" if i % 4 else "userSettings",
            "demo skill description", "演示技能描述", 0, 1 if i % 3 else 0,
            "1.0.0", random.randint(2000, 20000), int(base.timestamp() * 1000)))
    con.commit()
    con.close()
    print("  workbuddy.db 已生成：%d 会话 / %d 技能" % (n_sessions, len(SKILLS)))


def build_traces():
    os.makedirs(TRACES, exist_ok=True)
    for f in os.listdir(TRACES):
        if f.endswith(".jsonl"):
            os.remove(os.path.join(TRACES, f))
    n_traces = 26
    base = datetime.now() - timedelta(days=45)
    total_spans = 0
    for t in range(n_traces):
        start = base + timedelta(days=random.randint(0, 44), hours=random.randint(7, 23),
                                 minutes=random.randint(0, 59))
        tid = "trace_demo_%03d" % t
        lines = []
        st = "ok" if random.random() < 0.85 else "error"
        span_start_ts = start
        lines.append({"type": "trace_start", "trace": {
            "traceId": tid, "name": "Agent workflow", "startedAt": iso(start),
            "status": "running", "totalTokens": 0}})

        # 若干 generation span
        n_gen = random.randint(4, 22)
        for g in range(n_gen):
            model = pick(MODELS)
            pt = random.randint(2000, 180000)
            cached = int(pt * random.uniform(0.6, 0.95))
            ct = random.randint(80, 3000)
            reasoning = int(ct * random.uniform(0.1, 0.4))
            tt = pt + ct
            span_id = "span_gen_%03d_%02d" % (t, g)
            t0 = span_start_ts + timedelta(seconds=g * random.randint(3, 12))
            t1 = t0 + timedelta(milliseconds=random.randint(500, 9000))
            usage = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": tt,
                     "prompt_tokens_details": {"cached_tokens": cached},
                     "completion_tokens_details": {"reasoning_tokens": reasoning}}
            payload = [{"model": model, "usage": usage, "choices": []}]
            lines.append({"type": "span_start", "span": {
                "traceId": tid, "spanId": span_id, "name": "generation",
                "type": "generation", "startedAt": iso(t0), "status": "running"}})
            lines.append({"type": "span_end", "span": {
                "traceId": tid, "spanId": span_id, "name": "generation",
                "type": "generation", "startedAt": iso(t0), "endedAt": iso(t1),
                "duration": int((t1 - t0).total_seconds() * 1000), "status": "ok",
                "toolOutput": json.dumps(payload)}})
            total_spans += 2

            # 伴随工具调用
            for _ in range(random.randint(1, 3)):
                tool = pick(TOOLS)
                s_id = "span_fn_%03d_%02d_%d" % (t, g, random.randint(0, 9999))
                ft0 = t0 + timedelta(milliseconds=random.randint(1, 500))
                ft1 = ft0 + timedelta(milliseconds=random.randint(30, 2500))
                lines.append({"type": "span_start", "span": {
                    "traceId": tid, "spanId": s_id, "name": tool, "type": "function",
                    "startedAt": iso(ft0), "status": "running"}})
                lines.append({"type": "span_end", "span": {
                    "traceId": tid, "spanId": s_id, "name": tool, "type": "function",
                    "startedAt": iso(ft0), "endedAt": iso(ft1), "status": "ok"}})
                total_spans += 2

        end = t1 + timedelta(seconds=2)
        lines.append({"type": "trace_end", "trace": {
            "traceId": tid, "startedAt": iso(start), "endedAt": iso(end),
            "status": st, "totalTokens": sum(random.randint(1000, 50000) for _ in range(n_gen))}})

        if t in TRACE_SESSIONS:
            session_id = "demo-%03d" % TRACE_SESSIONS[t]
            if t % 2:
                lines[-1]["trace"]["sessionId"] = session_id
            else:
                lines.insert(-1, {"type": "trace_update", "trace": {"traceId": tid, "sessionId": session_id}})
        if t == 0:
            lines.insert(-1, next(event for event in lines
                                  if event["type"] == "span_end" and event["span"]["type"] == "generation"))

        with open(os.path.join(TRACES, "trace_%s.jsonl" % tid), "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print("  traces 已生成：%d 个 trace，约 %d spans" % (n_traces, total_spans))


if __name__ == "__main__":
    os.makedirs(TRACES, exist_ok=True)
    print("生成脱敏演示数据 →", DEMO)
    build_db()
    build_traces()
    print("完成。运行 WorkBuddy Dashboard 看板时设 WORKBUDDY_DEMO=1 可强制使用演示数据。")
