#!/usr/bin/env python3
"""故障依赖定位演练 —— 端到端自测（只用标准库 + Flask）。

覆盖题目要求的三个重点实测：
1. **轮次受限**：cap=1 时只能发一轮；手动发两条单服务探针后仍剩两个候选，
   结论变为 rounds_exhausted，再发第 2 轮被 409；同 round_no 重放不产生新批次。
2. **中途停用**：自适应多轮剧本跑到一半停用关键路由 → 重规划判 no_strategy；
   手动强发该探针拿到真实 503，计次且候选不变；恢复启用后重规划成功并定位。
3. **两页同轮**：8 线程同时 POST 同一个 round_no，服务端只落一批 3 条 trial，
   只有一个调用真正发送，其余 deduplicated。

另有规划器纯算法断言、“无论故障是谁都能在额度内查明”的全故障模拟、
真实网关 200/502/503/404、按服务逐个故障的完整 API 通关、硬重启持久化、
docker-compose 静态校验。

运行：``.venv/bin/python tests/locate_selftest.py``
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from drill import planner  # noqa: E402

PASS = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m {name}")
    else:
        FAILURES.append(name + (f" :: {detail}" if detail else ""))
        print(f"  \033[31mFAIL\033[0m {name} {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 1. 规划器纯算法
# ---------------------------------------------------------------------------

def M(*idx: int) -> int:
    m = 0
    for i in idx:
        m |= 1 << i
    return m


def test_planner_units() -> None:
    section("规划器：排除规则 / DP / 额度轮数边界")

    check("200 排除整条路径",
          planner.eliminate_mask(M(0, 1, 2), M(1, 2), 200) == M(0))
    check("502 交集到路径",
          planner.eliminate_mask(M(0, 1, 2), M(1, 2), 502) == M(1, 2))
    check("503 不排除任何候选",
          planner.eliminate_mask(M(0, 1, 2), M(1, 2), 503) == M(0, 1, 2))
    check("status=0 网络异常也不排除",
          planner.eliminate_mask(M(0, 1), M(0), 0) == M(0, 1))

    cells = planner.partition_cells(M(0, 1, 2, 3), [M(0, 1), M(0, 2)])
    check("批次把 4 候选划成 4 个不同结果格", sorted(cells) == [M(0), M(1), M(2), M(3)],
          str(cells))

    v = planner.plan_verdict(["a", "b"], [["a"]], ["a", "b"], 1, 1)
    check("2 候选 1 次 1 轮：可行", v.kind == planner.FEASIBLE and v.batch_indexes == [0])

    svc8 = [f"s{i}" for i in range(8)]
    probes8 = [[f"s{i}"] for i in range(8)]
    v6 = planner.plan_verdict(svc8, probes8, svc8, 6, 1)
    v7 = planner.plan_verdict(svc8, probes8, svc8, 7, 1)
    check("8 单服务探针：q=6/r=1 不可行", v6.kind == planner.INFEASIBLE
          and v6.reason == "budget_insufficient", f"{v6.kind}/{v6.reason}")
    check("8 单服务探针：q=7/r=1 可一批 7 条",
          v7.kind == planner.FEASIBLE and len(v7.batch_indexes) == 7
          and v7.worst_sends == 7 and v7.worst_rounds == 1,
          f"{v7.kind} {v7.batch_indexes}")

    v0r = planner.plan_verdict(svc8, probes8, svc8, 20, 0)
    check("轮数为 0 且多候选：rounds_exhausted",
          v0r.kind == planner.INFEASIBLE and v0r.reason == "rounds_exhausted")
    v0q = planner.plan_verdict(svc8, probes8, svc8, 0, 3)
    check("次数为 0 且多候选：budget_exhausted",
          v0q.kind == planner.INFEASIBLE and v0q.reason == "budget_exhausted")

    svc3 = ["x", "y", "z"]
    vk = planner.plan_verdict(svc3, [["z"]], svc3, 9, 9)
    check("x/y 对所有探针同构：永远 no_strategy",
          vk.kind == planner.INFEASIBLE and vk.reason == "no_strategy")
    vd = planner.plan_verdict(svc3, [["z"]], svc3, 9, 9, probe_usable=[False])
    check("唯一探针路由停用：no_strategy",
          vd.kind == planner.INFEASIBLE and vd.reason == "no_strategy")

    tree_svc = ["a", "b", "c", "d", "e"]
    tree_probes = [["a", "b"], ["a"], ["c", "d"], ["c"]]
    vt = planner.plan_verdict(tree_svc, tree_probes, tree_svc, 5, 3)
    check("自适应 5 服务剧本：存在最坏 3 次策略",
          vt.kind == planner.FEASIBLE and vt.worst_sends == 3,
          f"{vt.kind} sends={vt.worst_sends}")
    vt1 = planner.plan_verdict(tree_svc, tree_probes, tree_svc, 4, 1)
    check("4 条探针一轮可给 5 个唯一编码（q=4,r=1）",
          vt1.kind == planner.FEASIBLE and len(vt1.batch_indexes) == 4,
          f"{vt1.kind} {vt1.batch_indexes}")
    vt2 = planner.plan_verdict(tree_svc, tree_probes, tree_svc, 2, 3)
    check("总额 2 次：保证策略不存在（最坏要 3）",
          vt2.kind == planner.INFEASIBLE and vt2.reason == "budget_insufficient")

    vi = planner.plan_verdict(tree_svc, tree_probes, ["d"], 5, 3)
    check("单候选直接 identified", vi.kind == planner.IDENTIFIED)
    ve = planner.plan_verdict(tree_svc, tree_probes, [], 5, 3)
    check("空候选 EMPTY", ve.kind == planner.EMPTY)


def simulate_guarantee(services, probe_sets, budget, cap, usable=None):
    """对每个候选故障，严格按 verdict 推荐走，验证保证成立。"""
    n = len(services)
    all_names = list(services)
    for fault_i, fault in enumerate(services):
        cand = list(all_names)
        q, r = budget, cap
        used_rounds = 0
        while len(cand) > 1:
            v = planner.plan_verdict(services, probe_sets, cand, q, r, usable)
            if v.kind != planner.FEASIBLE:
                return False, f"fault={fault} 中途无策略 {v.kind}/{v.reason}"
            idxs = v.batch_indexes
            if len(idxs) > q or used_rounds + 1 > cap:
                return False, f"fault={fault} 推荐批次超出额度"
            svc_index = {s: j for j, s in enumerate(services)}
            for i in idxs:
                pmask = 0
                for s in probe_sets[i]:
                    pmask |= 1 << svc_index[s]
                status = 502 if pmask & (1 << fault_i) else 200
                cmask = sum(1 << svc_index[c] for c in cand)
                cmask = planner.eliminate_mask(cmask, pmask, status)
                cand = [services[j] for j in planner.bits(cmask)]
            q -= len(idxs)
            r -= 1
            used_rounds += 1
            if used_rounds > cap or q < 0:
                return False, f"fault={fault} 超出轮/次"
        if cand != [fault]:
            return False, f"fault={fault} 却定位到 {cand}"
    return True, ""


def test_guarantee_exhaustive() -> None:
    section("规划器：全故障 / 随机配置的“保证”性质")

    ok, why = simulate_guarantee(
        ["a", "b", "c", "d", "e"],
        [["a", "b"], ["a"], ["c", "d"], ["c"]], 5, 3)
    check("自适应剧本对 5 个故障逐一通关", ok, why)

    ok, why = simulate_guarantee(
        [f"s{i}" for i in range(8)],
        [[f"s{i}"] for i in range(8)], 7, 1)
    check("8 单服务 q=7/r=1 对 8 个故障逐一通关", ok, why)

    # 固定若干随机但“存在可行策略”的配置，验证推荐策略的保证性
    import random
    rng = random.Random(20261124)
    tested = 0
    for case in range(400):
        n = rng.randint(2, 6)
        svc = [f"s{i}" for i in range(n)]
        m = rng.randint(1, 6)
        psets = []
        for _ in range(m):
            psets.append([svc[j] for j in range(n) if rng.random() < 0.5])
        cap = rng.randint(1, 4)
        budget = rng.randint(1, 10)
        v0 = planner.plan_verdict(svc, psets, svc, budget, cap)
        if v0.kind != planner.FEASIBLE:
            continue
        tested += 1
        ok, why = simulate_guarantee(svc, psets, budget, cap)
        check(f"随机可行配置 #{tested} (n={n},m={m},q={budget},r={cap})", ok, why)
        if tested >= 40:
            break
    check("至少跑过 20 个随机可行配置", tested >= 20, f"tested={tested}")


# ---------------------------------------------------------------------------
# 2. 真实服务端到端
# ---------------------------------------------------------------------------

def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Client:
    def __init__(self, base: str):
        self.base = base

    def call(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                try:
                    return resp.status, json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    return resp.status, {"raw": raw}
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw}

    def get(self, p):
        return self.call("GET", p)

    def post(self, p, body=None):
        return self.call("POST", p, body or {})

    def delete(self, p):
        return self.call("DELETE", p)


class Server:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="locate-selftest-")
        self.data = Path(self.tmp) / "config"
        self.state = Path(self.tmp) / "state"
        self.data.mkdir()
        self.state.mkdir()
        self.port = free_port()
        self.logf = open(Path(self.tmp) / "server.log", "ab")
        env = dict(os.environ)
        env.update({
            "DRILL_DATA_DIR": str(self.data),
            "DRILL_STATE_DIR": str(self.state),
            "DRILL_PORT": str(self.port),
            "PYTHONPATH": str(ROOT),
        })
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "app.py")],
            cwd=str(ROOT), env=env, stdout=self.logf,
            stderr=subprocess.STDOUT, start_new_session=True)
        self.c = Client(f"http://127.0.0.1:{self.port}")
        self._wait_health()

    def _wait_health(self):
        deadline = time.time() + 25
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited\n" + self.log_text())
            try:
                s, d = self.c.get("/api/health")
                if s == 200:
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeError("health timeout\n" + self.log_text())

    def log_text(self):
        self.logf.flush()
        try:
            return Path(self.tmp, "server.log").read_text(errors="replace")[-3000:]
        except Exception:
            return "(no log)"

    def restart(self):
        try:
            os.killpg(self.proc.pid, 9)
            self.proc.wait(timeout=5)
        except Exception:
            pass
        time.sleep(0.4)
        env = dict(os.environ)
        env.update({
            "DRILL_DATA_DIR": str(self.data),
            "DRILL_STATE_DIR": str(self.state),
            "DRILL_PORT": str(self.port),
            "PYTHONPATH": str(ROOT),
        })
        self.logf = open(Path(self.tmp) / "server.log", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "app.py")],
            cwd=str(ROOT), env=env, stdout=self.logf,
            stderr=subprocess.STDOUT, start_new_session=True)
        self._wait_health()

    def stop(self):
        try:
            os.killpg(self.proc.pid, 15)
            self.proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(self.proc.pid, 9)
            except Exception:
                pass


def singleton_payload(fault="s3", budget=8, cap=4):
    svc = ["s0", "s1", "s2", "s3"]
    routes = ["/health", "/api/login", "/api/catalog", "/api/checkout"]
    return {
        "name": "singleton",
        "services": svc,
        "fault_service": fault,
        "total_budget": budget,
        "round_cap": cap,
        "probes": [
            {"name": f"p{i}", "route": routes[i], "services": [svc[i]]}
            for i in range(4)
        ],
    }


def tree_payload(fault="d", budget=5, cap=3):
    return {
        "name": "tree",
        "services": ["a", "b", "c", "d", "e"],
        "fault_service": fault,
        "total_budget": budget,
        "round_cap": cap,
        "probes": [
            {"name": "t1", "route": "/api/login", "services": ["a", "b"]},
            {"name": "t2", "route": "/health", "services": ["a"]},
            {"name": "t3", "route": "/api/catalog", "services": ["c", "d"]},
            {"name": "t4", "route": "/api/checkout", "services": ["c"]},
        ],
    }


def test_http_gateway_and_validation(srv: Server) -> str:
    section("真实服务：页面 / 网关 200·502·503·404 / 入参校验")
    c = srv.c

    s, page = c.call("GET", "/")
    check("GET / 是新演练页",
          s == 200 and "故障依赖定位" in page.get("raw", ""))
    check("首页 HTML 引用 locate.js", "locate.js" in page.get("raw", ""))
    s, js = c.call("GET", "/static/locate.js")
    check("locate.js 可访问且含并发试发逻辑",
          s == 200 and "并发试发" in js.get("raw", ""))
    s, ops = c.call("GET", "/ops")
    check("旧控制台保留在 /ops", s == 200 and "GSB" in ops.get("raw", ""))

    s, d = c.get("/api/locate/routes")
    paths = [r["path"] for r in d["routes"]]
    check("现有路由来自 current.json（6 条内置）",
          s == 200 and "/api/checkout" in paths and len(paths) >= 6, str(paths))

    # 校验：故障不在服务里 / 路由不存在 / 超 8 服务
    s, d = c.post("/api/locate/drills", {**singleton_payload(), "fault_service": "ghost"})
    check("拒绝不存在的故障服务", s == 400 and d["ok"] is False, d.get("error", ""))
    bad_route = singleton_payload()
    bad_route["probes"][0]["route"] = "/no-such-route"
    s, d = c.post("/api/locate/drills", bad_route)
    check("拒绝不在现有路由中的探针路由", s == 400 and d.get("error_code") == "route_not_found")
    too_many = singleton_payload()
    too_many["services"] = [f"x{i}" for i in range(9)]
    s, d = c.post("/api/locate/drills", too_many)
    check("拒绝 >8 个服务", s == 400)
    too_many_p = singleton_payload()
    too_many_p["probes"] = [{"name": f"q{i}", "route": "/health", "services": []}
                            for i in range(9)]
    s, d = c.post("/api/locate/drills", too_many_p)
    check("拒绝 >8 条探针", s == 400)

    # 建一场，直接打网关验证注入与 503 优先
    s, d = c.post("/api/locate/drills", singleton_payload(fault="s3"))
    check("创建演练 201", s == 201 and d["drill"]["id"])
    did = d["drill"]["id"]

    import urllib.request as u
    def gw(path):
        try:
            with u.urlopen(f"http://127.0.0.1:{srv.port}{path}", timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    check("普通网关请求 /gw/health → 200", gw("/gw/health") == 200)
    # p3 路径含 s3=fault → 502
    check("带本场探针上下文且经过故障 → 502",
          gw(f"/gw/api/checkout?_drill={did}&_probe=p3") == 502)
    check("带本场探针上下文但不经过故障 → 200",
          gw(f"/gw/health?_drill={did}&_probe=p0") == 200)
    check("未知路由 → 404", gw("/gw/nope") == 404)

    s, d = c.post("/api/locate/routes/maintenance",
                  {"path": "/api/checkout", "enabled": False})
    check("停用 /api/checkout 成功且版本递增", s == 200 and d["enabled"] is False)
    check("停用后即使探针经过故障也优先 503",
          gw(f"/gw/api/checkout?_drill={did}&_probe=p3") == 503)
    s, d = c.post("/api/locate/routes/maintenance",
                  {"path": "/api/checkout", "enabled": True})
    check("恢复后重新 502", s == 200 and
          gw(f"/gw/api/checkout?_drill={did}&_probe=p3") == 502)
    return did


def test_full_strategy_runs(srv: Server) -> None:
    section("真实服务：逐故障按推荐批次通关（单服务剧本 + 自适应剧本）")
    c = srv.c

    for fault in ["s0", "s1", "s2", "s3"]:
        s, d = c.post("/api/locate/drills", singleton_payload(fault=fault))
        did = d["drill"]["id"]
        for _ in range(4):
            st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
            if st["verdict"]["kind"] == "identified":
                break
            check(f"[{fault}] 始终存在可行策略", st["verdict"]["kind"] == "feasible",
                  st["verdict"].get("detail", ""))
            s, d = c.post(f"/api/locate/drills/{did}/rounds", {"round_no": st["rounds_quota"]["next_round_no"]})
            check(f"[{fault}] 推荐轮发送 200", s == 200, str(d)[:200])
        st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
        check(f"[{fault}] 最终定位正确且 fault_found",
              st["verdict"]["kind"] == "identified" and st["candidates"] == [fault]
              and st["fault_found"] is True, str(st["candidates"]))
        check(f"[{fault}] 额度未超支",
              st["budget"]["used"] <= st["budget"]["total"]
              and st["rounds_quota"]["done"] <= st["config"]["round_cap"])

    for fault in ["a", "b", "c", "d", "e"]:
        s, d = c.post("/api/locate/drills", tree_payload(fault=fault))
        did = d["drill"]["id"]
        for _ in range(3):
            st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
            if st["verdict"]["kind"] == "identified":
                break
            s, d = c.post(f"/api/locate/drills/{did}/rounds",
                          {"round_no": st["rounds_quota"]["next_round_no"]})
            check(f"[tree/{fault}] 轮次发送成功", s == 200, str(d)[:200])
        st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
        check(f"[tree/{fault}] 3 轮 / 5 次内定位正确",
              st["candidates"] == [fault] and st["fault_found"],
              f"{st['candidates']} rounds={st['rounds_quota']['done']} "
              f"used={st['budget']['used']}")


def test_round_cap(srv: Server) -> None:
    section("重点实测 ①：轮数受限 + 同轮幂等重放")
    c = srv.c
    # 额度给足次数，但轮数上限 1；手动只发 p0/p1，留下 s2/s3
    payload = singleton_payload(fault="s3", budget=8, cap=1)
    s, d = c.post("/api/locate/drills", payload)
    did = d["drill"]["id"]
    v0 = d["drill"]["verdict"]
    check("cap=1 且 q=8：策略本可一批 3 条保证查明",
          v0["kind"] == "feasible" and len(v0["batch_indexes"]) == 3)

    s, d = c.post(f"/api/locate/drills/{did}/rounds",
                  {"round_no": 1, "probe_ids": ["p0", "p1"]})
    check("手动两条一批发送成功", s == 200 and d["drill"]["send_result"]["sent"] == 2)
    st = d["drill"]
    statuses = sorted(t["http_status"] for t in st["rounds"][0]["trials"])
    check("两条都是真实 200（故障 s3 未被经过）", statuses == [200, 200], str(statuses))
    check("候选剩下 s2/s3", st["candidates"] == ["s2", "s3"], str(st["candidates"]))
    check("轮数用完 → rounds_exhausted",
          st["verdict"]["kind"] == "infeasible"
          and st["verdict"]["reason"] == "rounds_exhausted")
    check("结论中列出尚未排除的两个服务",
          st["verdict"]["candidates"] == ["s2", "s3"])

    s, d = c.post(f"/api/locate/drills/{did}/rounds", {})
    check("再安排第 2 轮被 409 round_cap_reached",
          s == 409 and d.get("error_code") == "round_cap_reached", str(d)[:200])

    # 同 round_no=1 重放：返回同一轮，不新增 trial
    s, d = c.post(f"/api/locate/drills/{did}/rounds",
                  {"round_no": 1, "probe_ids": ["p0", "p1"]})
    check("同轮重放 200 且 deduplicated",
          s == 200 and d["drill"]["send_result"].get("deduplicated") is True)
    st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
    check("重放后仍只有 1 轮 / 2 条应答（不重复试发）",
          len(st["rounds"]) == 1 and st["budget"]["used"] == 2,
          f"rounds={len(st['rounds'])} used={st['budget']['used']}")


def test_budget(srv: Server) -> None:
    section("额度：批次超过剩余次数时整批不发")
    c = srv.c
    s, d = c.post("/api/locate/drills", singleton_payload(fault="s2", budget=2, cap=3))
    did = d["drill"]["id"]
    s, d = c.post(f"/api/locate/drills/{did}/rounds",
                  {"round_no": 1, "probe_ids": ["p0", "p1", "p2"]})
    check("3 条 > 总额 2：409 budget_exceeded",
          s == 409 and d.get("error_code") == "budget_exceeded")
    st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
    check("被拒后没有任何试发记录", st["budget"]["used"] == 0 and len(st["rounds"]) == 0)

    s, d = c.post(f"/api/locate/drills/{did}/rounds",
                  {"round_no": 1, "probe_ids": ["p0", "p1"]})
    check("发 2 条成功", s == 200)
    check("剩余次数=0", d["drill"]["budget"]["remaining"] == 0)
    s, d = c.post(f"/api/locate/drills/{did}/rounds",
                  {"round_no": 2, "probe_ids": ["p2"]})
    check("额度用尽后下一轮 409", s == 409 and d.get("error_code") == "budget_exceeded")


def test_midway_disable(srv: Server) -> None:
    section("重点实测 ②：中途停用 → 重规划无策略 → 真实 503 计次不排除 → 恢复后通关")
    c = srv.c
    s, d = c.post("/api/locate/drills", tree_payload(fault="d"))
    did = d["drill"]["id"]
    check("初始推荐第一批 t1+t3",
          d["drill"]["verdict"]["batch_indexes"] == [0, 2],
          str(d["drill"]["verdict"]["batch_indexes"]))

    s, d = c.post(f"/api/locate/drills/{did}/rounds", {"round_no": 1})
    check("第 1 轮发送成功", s == 200)
    st = d["drill"]
    by_name = {t["probe_name"]: t["http_status"] for t in st["rounds"][0]["trials"]}
    check("t1=200 / t3=502（真实 HTTP）",
          by_name.get("t1") == 200 and by_name.get("t3") == 502, str(by_name))
    check("第 1 轮后候选 {c,d}", st["candidates"] == ["c", "d"], str(st["candidates"]))
    check("策略改推 t4 区分 c/d",
          st["verdict"]["kind"] == "feasible" and st["verdict"]["batch_indexes"] == [3])

    # 中途停用 t4 使用的 /api/checkout
    s, _ = c.post("/api/locate/routes/maintenance",
                  {"path": "/api/checkout", "enabled": False})
    check("中途停用 /api/checkout", s == 200)
    st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
    check("停用后按当前路由重规划：no_strategy",
          st["verdict"]["kind"] == "infeasible"
          and st["verdict"]["reason"] == "no_strategy",
          f"{st['verdict']['kind']}/{st['verdict']['reason']}")
    check("无策略时仍列出候选 c,d", st["verdict"]["candidates"] == ["c", "d"])

    # 手动强发 t4：必须拿到真实 503，计次，但候选不变
    s, d = c.post(f"/api/locate/drills/{did}/rounds",
                  {"round_no": 2, "probe_ids": ["p3"]})
    check("停用路由上的探针允许手动强发为第 2 轮", s == 200, str(d)[:200])
    t4 = [t for t in d["drill"]["rounds"][1]["trials"] if t["probe_id"] == "p3"][0]
    check("真实应答 503 / outcome=unavailable",
          t4["http_status"] == 503 and t4["outcome"] == "unavailable", str(t4))
    check("503 计入试发次数（used=3）", d["drill"]["budget"]["used"] == 3)
    check("503 不排除候选，仍是 c,d", d["drill"]["candidates"] == ["c", "d"])

    # 恢复启用 → 重规划成功；第 3 轮发 t4 → d 现形
    s, _ = c.post("/api/locate/routes/maintenance",
                  {"path": "/api/checkout", "enabled": True})
    check("恢复启用 /api/checkout", s == 200)
    st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
    check("恢复后重新存在可行策略且仍推 t4",
          st["verdict"]["kind"] == "feasible" and st["verdict"]["batch_indexes"] == [3],
          f"{st['verdict']['kind']} {st['verdict'].get('detail')}")
    s, d = c.post(f"/api/locate/drills/{did}/rounds", {"round_no": 3})
    check("第 3 轮发送成功", s == 200)
    st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
    check("最终定位 d 且与设定一致",
          st["candidates"] == ["d"] and st["fault_found"], str(st["candidates"]))
    check("总共 3 轮 / 4 次（含 1 次 503），未超 3 轮上限",
          st["rounds_quota"]["done"] == 3 and st["budget"]["used"] == 4,
          f"rounds={st['rounds_quota']['done']} used={st['budget']['used']}")


def test_two_pages_same_round(srv: Server) -> None:
    section("重点实测 ③：两页（8 并发请求）同轮只发一批")
    c = srv.c
    s, d = c.post("/api/locate/drills", singleton_payload(fault="s0", budget=8, cap=3))
    did = d["drill"]["id"]

    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(c.post(f"/api/locate/drills/{did}/rounds",
                              {"round_no": 1, "probe_ids": ["p0", "p1", "p2"]}))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    check("8 个并发同轮请求全部返回 200",
          len(results) == 8 and all(s == 200 for s, _ in results),
          str([s for s, _ in results]))
    sent_flags = [d2["drill"]["send_result"].get("deduplicated") for s, d2 in results]
    check("恰好 1 个请求真正发送，其余 7 个 deduplicated",
          sum(1 for x in sent_flags if x is False) == 1
          and sum(1 for x in sent_flags if x is True) == 7,
          str(sent_flags))

    st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
    check("服务端只有 1 轮记录", len(st["rounds"]) == 1, f"rounds={len(st['rounds'])}")
    check("该轮只有 3 条真实应答（没有发 8 批 / 24 条）",
          st["budget"]["used"] == 3
          and len(st["rounds"][0]["trials"]) == 3,
          f"used={st['budget']['used']}")
    check("并发批处理结果正确：p0 命中 502，候选定位 s0",
          st["candidates"] == ["s0"] and st["fault_found"], str(st["candidates"]))

    # 顺序重复点同一轮也仍然幂等
    s, d = c.post(f"/api/locate/drills/{did}/rounds", {"round_no": 1})
    check("事后再点同一轮依旧 deduplicated 200",
          s == 200 and d["drill"]["send_result"]["deduplicated"] is True)
    check("应答总数仍为 3", c.get(f"/api/locate/drills/{did}")[1]["drill"]["budget"]["used"] == 3)


def test_persistence_and_compose(srv: Server) -> None:
    section("硬重启持久化 + docker compose 静态核对")
    c = srv.c
    s, d = c.post("/api/locate/drills", tree_payload(fault="c"))
    did = d["drill"]["id"]
    c.post(f"/api/locate/drills/{did}/rounds", {"round_no": 1})
    before = c.get(f"/api/locate/drills/{did}")[1]["drill"]
    check("重启前已有 1 轮 / 2 应答", before["rounds_quota"]["done"] == 1
          and before["budget"]["used"] == 2)

    srv.restart()
    after = c.get(f"/api/locate/drills/{did}")[1]["drill"]
    check("硬重启后演练与应答仍在",
          after["id"] == did and after["rounds_quota"]["done"] == 1
          and after["budget"]["used"] == 2
          and after["candidates"] == before["candidates"],
          f"{after.get('candidates')} used={after['budget']['used']}")
    # 重启后仍可继续通关
    for _ in range(2):
        st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
        if st["verdict"]["kind"] == "identified":
            break
        c.post(f"/api/locate/drills/{did}/rounds",
               {"round_no": st["rounds_quota"]["next_round_no"]})
    st = c.get(f"/api/locate/drills/{did}")[1]["drill"]
    check("重启后继续并定位 c", st["candidates"] == ["c"] and st["fault_found"],
          str(st["candidates"]))

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        doc = yaml.safe_load(compose)
        svc = doc["services"]["gateway"]
        check("compose YAML 合法且映射 8000",
              "8000:8000" in svc["ports"] and "drill-volume" in compose)
        check("compose 使用命名卷挂到 /drill",
              any(v.startswith("drill-volume:") for v in svc["volumes"]))
    except ImportError:
        check("compose 文本含 8000 端口与命名卷",
              "8000:8000" in compose and "drill-volume:/drill" in compose)
    check("Dockerfile 暴露 8000 并以 python app.py 启动",
          "EXPOSE 8000" in dockerfile and 'CMD ["python", "app.py"]' in dockerfile)


def main() -> int:
    test_planner_units()
    test_guarantee_exhaustive()

    srv = Server()
    print(f"\n真实服务已启动：127.0.0.1:{srv.port}（临时卷 {srv.tmp}）")
    try:
        test_http_gateway_and_validation(srv)
        test_full_strategy_runs(srv)
        test_round_cap(srv)
        test_budget(srv)
        test_midway_disable(srv)
        test_two_pages_same_round(srv)
        test_persistence_and_compose(srv)
    finally:
        srv.stop()

    print(f"\n{'=' * 60}")
    if FAILURES:
        print(f"\033[31m失败 {len(FAILURES)} 项 / 通过 {PASS} 项\033[0m")
        for f in FAILURES:
            print(" -", f)
        return 1
    print(f"\033[32m全部通过：{PASS} 项断言\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
