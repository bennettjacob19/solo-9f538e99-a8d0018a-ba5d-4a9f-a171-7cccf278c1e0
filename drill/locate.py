"""故障定位演练：配置存储、轮次 / 试发记录、并发真实 HTTP 执行、同轮去重。

设计要点
--------
* 演练 / 轮次 / 单次应答都在 SQLite（与旧控制台同一个库，新增三张表）。
* “一键并发试发”由**服务端**用线程池对真实监听端口发起 HTTP GET，
  网关应答什么状态码就记录什么，排除候选只看状态码 —— 定位不读内部故障字段。
* 同一场演练内用进程内互斥锁串行化“建轮次 → 发批 → 落库”，并以
  ``UNIQUE(drill_id, round_no)`` 作为多进程下的最终兜底，保证两个页面
  同时点同一轮时，真正发出的 HTTP 请求只有一批。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import config, configstore, db, planner

MAX_SERVICES = planner.MAX_SERVICES
MAX_PROBES = planner.MAX_PROBES
MAX_BUDGET = 999
MAX_ROUND_CAP = 99
HTTP_TIMEOUT = float(os.environ.get("DRILL_HTTP_TIMEOUT", "8"))

# 不走任何代理：服务端自调 127.0.0.1，避免环境里的 HTTP_PROXY 劫持
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_DRILL_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

LOCATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS locate_drills (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    config_json     TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    created_real    REAL NOT NULL,
    updated_real    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS locate_rounds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    drill_id     TEXT NOT NULL,
    round_no     INTEGER NOT NULL,
    batch_json   TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'strategy', -- strategy|manual
    status       TEXT NOT NULL DEFAULT 'sending',  -- sending|done|interrupted
    deduplicated INTEGER NOT NULL DEFAULT 0,
    created_real REAL NOT NULL,
    started_real REAL NOT NULL,
    finished_real REAL,
    UNIQUE(drill_id, round_no)
);
CREATE INDEX IF NOT EXISTS idx_locate_rounds_drill ON locate_rounds(drill_id);

CREATE TABLE IF NOT EXISTS locate_trials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    drill_id    TEXT NOT NULL,
    round_no    INTEGER NOT NULL,
    probe_id    TEXT NOT NULL,
    probe_name  TEXT NOT NULL DEFAULT '',
    route       TEXT NOT NULL DEFAULT '',
    path_json   TEXT NOT NULL DEFAULT '[]',
    url_path    TEXT NOT NULL DEFAULT '',
    http_status INTEGER NOT NULL DEFAULT 0,
    outcome     TEXT NOT NULL DEFAULT 'error', -- hit|clear|unavailable|error
    elapsed_ms  REAL NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    body_snippet TEXT NOT NULL DEFAULT '',
    created_real REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_locate_trials_drill ON locate_trials(drill_id);
"""


class LocateError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request",
                 extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra or {}


def init_locate_db() -> None:
    config.ensure_dirs()
    with db.session() as con:
        con.executescript(LOCATE_SCHEMA)
        # 新进程启动意味着上一进程内的发送不可能还在进行；回收 sending 残留，
        # 避免两个页面永远看到“另一页发送中”。
        con.execute(
            "UPDATE locate_rounds SET status='interrupted', "
            "finished_real=COALESCE(finished_real, ?) WHERE status='sending'",
            (time.time(),))


def _drill_lock(drill_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lk = _DRILL_LOCKS.get(drill_id)
        if lk is None:
            lk = threading.Lock()
            _DRILL_LOCKS[drill_id] = lk
        return lk


# ---------------------------------------------------------------------------
# 路由（“现有路由” + 维护停用）
# ---------------------------------------------------------------------------

def list_routes() -> list[dict[str, Any]]:
    cfg, _ = configstore.load_current()
    disabled = set(cfg.get("disabled", []))
    routes = []
    for r in cfg.get("routes", []):
        path = r.get("path", "")
        routes.append({
            "path": path,
            "note": r.get("note", ""),
            "upstream": r.get("upstream", ""),
            "enabled": path not in disabled,
        })
    return routes


def enabled_route_map() -> dict[str, bool]:
    return {r["path"]: r["enabled"] for r in list_routes()}


def set_route_enabled(path: str, enabled: bool) -> dict[str, Any]:
    """直接维护 current.json 的 disabled 清单（原子替换）。"""
    if not path or not path.startswith("/"):
        raise LocateError("path 必须是以 / 开头的路由", code="bad_route")
    config.ensure_dirs()
    cur = config.CURRENT_PATH
    if not cur.exists():
        configstore.seed_if_needed()
    try:
        cfg = json.loads(cur.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise LocateError(f"current.json 不可读：{exc}", status=500,
                          code="config_unreadable") from exc

    known = {r.get("path") for r in cfg.get("routes", [])}
    if path not in known:
        raise LocateError(f"现有路由中没有 {path}", status=404, code="route_not_found")

    disabled = [p for p in cfg.get("disabled", []) if isinstance(p, str)]
    dset = set(disabled)
    if enabled:
        dset.discard(path)
    else:
        dset.add(path)
    cfg["disabled"] = sorted(dset)
    cfg["version"] = int(cfg.get("version", 0) or 0) + 1
    cfg["last_maintenance_real"] = time.time()

    tmp = cur.with_name(cur.name + ".maint.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, cur)
    return {"path": path, "enabled": enabled, "disabled": sorted(dset),
            "version": cfg["version"]}


# ---------------------------------------------------------------------------
# 创建 / 校验演练
# ---------------------------------------------------------------------------

def _clean_name(value: Any) -> str:
    return str(value or "").strip()[:24]


def validate_config(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise LocateError("请求体必须是 JSON 对象")

    raw_services = payload.get("services") or []
    if not isinstance(raw_services, list) or not (1 <= len(raw_services) <= MAX_SERVICES):
        raise LocateError(f"模拟服务必须是 1..{MAX_SERVICES} 个")
    services: list[str] = []
    for i, s in enumerate(raw_services):
        name = _clean_name(s if isinstance(s, str) else (s or {}).get("name"))
        if not name:
            raise LocateError(f"第 {i + 1} 个服务名称为空")
        if name in services:
            raise LocateError(f"服务名重复：{name}")
        services.append(name)

    fault = _clean_name(payload.get("fault_service"))
    if fault not in services:
        raise LocateError("故障服务必须从已录入的服务中选择一个（本场固定）")

    try:
        total_budget = int(payload.get("total_budget"))
        round_cap = int(payload.get("round_cap"))
    except (TypeError, ValueError) as exc:
        raise LocateError("试发总次数与轮数上限必须是整数") from exc
    if not (1 <= total_budget <= MAX_BUDGET):
        raise LocateError(f"试发总次数需在 1..{MAX_BUDGET} 之间")
    if not (1 <= round_cap <= MAX_ROUND_CAP):
        raise LocateError(f"轮数上限需在 1..{MAX_ROUND_CAP} 之间")

    raw_probes = payload.get("probes") or []
    if not isinstance(raw_probes, list) or len(raw_probes) > MAX_PROBES:
        raise LocateError(f"探针最多 {MAX_PROBES} 条")

    routes = list_routes()
    known_routes = {r["path"] for r in routes}
    probes: list[dict[str, Any]] = []
    seen_probe_names: set[str] = set()
    for i, p in enumerate(raw_probes):
        if not isinstance(p, dict):
            raise LocateError(f"第 {i + 1} 条探针格式不正确")
        route = str(p.get("route") or "").strip()
        if route not in known_routes:
            raise LocateError(
                f"第 {i + 1} 条探针选择的路由 {route!r} 不在现有路由中",
                code="route_not_found")
        raw_ps = p.get("services") or []
        if not isinstance(raw_ps, list):
            raise LocateError(f"探针 {i + 1} 的 services 必须是数组")
        ps: list[str] = []
        for name in raw_ps:
            name = _clean_name(name)
            if not name:
                continue
            if name not in services:
                raise LocateError(f"探针 {i + 1} 经过了未录入的服务：{name}")
            if name not in ps:
                ps.append(name)
        pname = _clean_name(p.get("name")) or f"探针{i + 1}"
        if pname in seen_probe_names:
            pname = f"{pname}-{i + 1}"
        seen_probe_names.add(pname)
        probes.append({
            "id": f"p{i}",
            "name": pname,
            "route": route,
            "services": ps,
        })

    return {
        "name": _clean_name(payload.get("name"))[:60],
        "services": services,
        "probes": probes,
        "fault_service": fault,
        "total_budget": total_budget,
        "round_cap": round_cap,
    }


def create_drill(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = validate_config(payload)
    did = db.new_id()
    now = time.time()
    with db.session() as con:
        con.execute(
            "INSERT INTO locate_drills(id, name, config_json, candidates_json, "
            "created_real, updated_real) VALUES(?, ?, ?, ?, ?, ?)",
            (did, cfg["name"], json.dumps(cfg, ensure_ascii=False),
             json.dumps(cfg["services"], ensure_ascii=False), now, now),
        )
    return get_state(did)


# ---------------------------------------------------------------------------
# 读取 / 序列化
# ---------------------------------------------------------------------------

def _load_drill_row(con: sqlite3.Connection, drill_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM locate_drills WHERE id=?", (drill_id,)).fetchone()
    if row is None:
        raise LocateError("演练不存在", status=404, code="drill_not_found")
    return row


def _rounds_and_trials(con: sqlite3.Connection, drill_id: str):
    rounds = [dict(r) for r in con.execute(
        "SELECT * FROM locate_rounds WHERE drill_id=? ORDER BY round_no",
        (drill_id,)).fetchall()]
    trials = [dict(r) for r in con.execute(
        "SELECT * FROM locate_trials WHERE drill_id=? ORDER BY id",
        (drill_id,)).fetchall()]
    by_round: dict[int, list[dict[str, Any]]] = {}
    for t in trials:
        t["path_services"] = json.loads(t.pop("path_json", "[]"))
        by_round.setdefault(t["round_no"], []).append(t)
    for r in rounds:
        r["batch"] = json.loads(r.pop("batch_json", "[]"))
        r["trials"] = by_round.get(r["round_no"], [])
    return rounds, trials


def get_state(drill_id: str) -> dict[str, Any]:
    routes = list_routes()
    enabled_map = {r["path"]: r["enabled"] for r in routes}

    with db.session() as con:
        row = _load_drill_row(con, drill_id)
        # 多进程 / 崩溃残留的 sending（HTTP 超时上限 8s/条，整批远小于 30s）
        con.execute(
            "UPDATE locate_rounds SET status='interrupted', finished_real=? "
            "WHERE drill_id=? AND status='sending' AND started_real < ?",
            (time.time(), drill_id, time.time() - 30))
        cfg = json.loads(row["config_json"])
        candidates = json.loads(row["candidates_json"])
        rounds, trials = _rounds_and_trials(con, drill_id)

    services: list[str] = cfg["services"]
    probes: list[dict[str, Any]] = cfg["probes"]
    total_budget = int(cfg["total_budget"])
    round_cap = int(cfg["round_cap"])

    trials_used = len(trials)
    rounds_done = sum(1 for r in rounds if r["status"] == "done")
    rounds_in_flight = sum(1 for r in rounds if r["status"] == "sending")
    trials_left = total_budget - trials_used
    rounds_left = round_cap - rounds_done

    usable = [bool(enabled_map.get(p["route"], False)) for p in probes]
    verdict = planner.plan_verdict(
        services, [p["services"] for p in probes], candidates,
        max(trials_left, 0), max(rounds_left, 0), usable,
    )

    # 给推荐批次补上完整探针对象，前端直接渲染
    vd = verdict.to_dict()
    if vd.get("batch_indexes") is not None:
        vd["batch"] = [probes[i] for i in vd["batch_indexes"]]
    else:
        vd["batch"] = []

    excluded_now = [s for s in services if s not in candidates]
    actual_fault = cfg["fault_service"]
    return {
        "id": drill_id,
        "name": row["name"],
        "created_real": row["created_real"],
        "config": cfg,
        "routes": routes,
        "candidates": candidates,
        "excluded": excluded_now,
        "actual_fault_service": actual_fault,
        "fault_found": verdict.kind == planner.IDENTIFIED
                      and candidates == [actual_fault],
        "verdict": vd,
        "budget": {
            "total": total_budget,
            "used": trials_used,
            "remaining": trials_left,
        },
        "rounds_quota": {
            "cap": round_cap,
            "done": rounds_done,
            "in_flight": rounds_in_flight,
            "remaining": rounds_left,
            "next_round_no": rounds_done + 1,
        },
        "rounds": rounds,
        "trials": list(reversed(trials)),  # 最新在前，供“各次应答”表
    }


def list_drills(limit: int = 20) -> list[dict[str, Any]]:
    with db.session() as con:
        rows = con.execute(
            "SELECT id, name, created_real, updated_real FROM locate_drills "
            "ORDER BY created_real DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            cfg_row = con.execute(
                "SELECT config_json, candidates_json FROM locate_drills WHERE id=?",
                (r["id"],)).fetchone()
            cfg = json.loads(cfg_row["config_json"])
            d["service_count"] = len(cfg["services"])
            d["probe_count"] = len(cfg["probes"])
            d["fault_service"] = cfg["fault_service"]
            d["candidates"] = json.loads(cfg_row["candidates_json"])
            out.append(d)
        return out


def delete_drill(drill_id: str) -> dict[str, Any]:
    with _drill_lock(drill_id):
        with db.session() as con:
            _load_drill_row(con, drill_id)
            con.execute("DELETE FROM locate_drills WHERE id=?", (drill_id,))
            con.execute("DELETE FROM locate_rounds WHERE drill_id=?", (drill_id,))
            con.execute("DELETE FROM locate_trials WHERE drill_id=?", (drill_id,))
    return {"ok": True, "id": drill_id}


# ---------------------------------------------------------------------------
# 网关侧查询（故障注入的唯一内部依据）
# ---------------------------------------------------------------------------

def gateway_probe_context(drill_id: str, probe_id: str) -> dict[str, Any] | None:
    try:
        with db.session() as con:
            row = con.execute(
                "SELECT config_json FROM locate_drills WHERE id=?",
                (drill_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    cfg = json.loads(row["config_json"])
    probe = next((p for p in cfg["probes"] if p["id"] == probe_id), None)
    if probe is None:
        return None
    return {
        "probe": probe,
        "fault_service": cfg["fault_service"],
        "hit_fault": cfg["fault_service"] in probe.get("services", []),
    }


# ---------------------------------------------------------------------------
# 真实 HTTP 试发
# ---------------------------------------------------------------------------

def gateway_base() -> str:
    override = os.environ.get("DRILL_GATEWAY_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    return f"http://127.0.0.1:{config.PORT}"


def _outcome_for_status(status: int) -> str:
    if status == planner.STATUS_CLEAR:
        return "clear"
    if status == planner.STATUS_HIT:
        return "hit"
    if status == planner.STATUS_DISABLED:
        return "unavailable"
    return "error"


def _http_get_probe(base: str, drill_id: str, probe: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode({"_drill": drill_id, "_probe": probe["id"]})
    route = probe["route"]
    # 网关挂载在 /gw/<路由路径>
    url_path = f"/gw{route}?{query}"
    started = time.perf_counter()
    status = 0
    body_snippet = ""
    err = ""
    try:
        req = urllib.request.Request(base + url_path, method="GET")
        try:
            resp = _NO_PROXY_OPENER.open(req, timeout=HTTP_TIMEOUT)
            status = resp.status
            raw = resp.read(800)
        except urllib.error.HTTPError as e:  # 4xx/5xx 也是真实应答
            status = e.code
            try:
                raw = e.read(800)
            except Exception:  # noqa: BLE001
                raw = b""
        body_snippet = raw.decode("utf-8", errors="replace")[:400]
    except Exception as exc:  # noqa: BLE001 连接失败 / 超时
        err = f"{exc.__class__.__name__}: {exc}"
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "probe_id": probe["id"],
        "probe_name": probe["name"],
        "route": route,
        "path_services": probe.get("services", []),
        "url_path": url_path,
        "http_status": status,
        "outcome": _outcome_for_status(status) if status else "error",
        "elapsed_ms": elapsed_ms,
        "error": err,
        "body_snippet": body_snippet,
    }


# ---------------------------------------------------------------------------
# 轮次执行（含同轮去重）
# ---------------------------------------------------------------------------

def send_round(drill_id: str, round_no: int | None = None,
               probe_ids: list[str] | None = None) -> dict[str, Any]:
    """发送（或幂等返回）某一轮。

    两个页面带同一个 ``round_no`` 并发调用时，只有一个调用真正建轮并发 HTTP；
    另一个拿到同一轮记录并标记 ``deduplicated=True``。
    """
    lock = _drill_lock(drill_id)
    with lock:
        return _send_round_locked(drill_id, round_no, probe_ids)


def _send_round_locked(drill_id: str, round_no: int | None,
                       probe_ids: list[str] | None) -> dict[str, Any]:
    with db.session() as con:
        row = _load_drill_row(con, drill_id)
        cfg = json.loads(row["config_json"])

    probes: list[dict[str, Any]] = cfg["probes"]
    probe_by_id = {p["id"]: p for p in probes}
    total_budget = int(cfg["total_budget"])
    round_cap = int(cfg["round_cap"])

    with db.session() as con:
        done_count = con.execute(
            "SELECT COUNT(*) c FROM locate_rounds WHERE drill_id=? AND status='done'",
            (drill_id,)).fetchone()["c"]
        trial_count = con.execute(
            "SELECT COUNT(*) c FROM locate_trials WHERE drill_id=?",
            (drill_id,)).fetchone()["c"]
        max_round = con.execute(
            "SELECT MAX(round_no) m FROM locate_rounds WHERE drill_id=?",
            (drill_id,)).fetchone()["m"]

    next_no = done_count + 1
    if round_no is None:
        round_no = next_no

    # 1) 同轮已存在：幂等回放 / 合并，绝不发第二批
    with db.session() as con:
        existing = con.execute(
            "SELECT * FROM locate_rounds WHERE drill_id=? AND round_no=?",
            (drill_id, round_no)).fetchone()
    if existing is not None:
        ed = dict(existing)
        if ed["status"] == "sending":
            # 同进程锁内理论上看不到活着的 sending；多进程 / 崩溃残留时给出明确错误
            age = time.time() - float(ed["started_real"] or 0)
            if age > 120:
                with db.session() as con:
                    con.execute("UPDATE locate_rounds SET status='interrupted' WHERE id=?",
                                (ed["id"],))
                raise LocateError("该轮上次发送中断，请新建下一轮", status=409,
                                  code="round_interrupted")
            raise LocateError("同一轮正在另一页面 / 请求中发送", status=409,
                              code="round_in_flight",
                              extra={"round_no": round_no})
        state = get_state(drill_id)
        state["send_result"] = {
            "round_no": round_no, "deduplicated": True,
            "reason": "same_round_already_sent",
        }
        return state

    if round_no != next_no:
        raise LocateError(
            f"轮次序号过期：当前应发送第 {next_no} 轮，页面提交的是第 {round_no} 轮，"
            "请刷新后重试", status=409, code="stale_round",
            extra={"next_round_no": next_no})

    # 2) 轮数 / 次数硬额度
    if round_no > round_cap:
        raise LocateError(
            f"轮数上限 {round_cap} 已用完，不能再安排第 {round_no} 轮",
            status=409, code="round_cap_reached",
            extra=get_state(drill_id)["verdict"])

    trials_left = total_budget - trial_count

    # 3) 确定本批探针：显式 ids（手动 / 测试）优先，否则取策略推荐
    source = "manual"
    if probe_ids is None:
        state0 = get_state(drill_id)
        verdict = state0["verdict"]
        if verdict["kind"] != planner.FEASIBLE:
            raise LocateError(
                "当前不存在额度内的保证策略，服务端没有可推荐的批次；"
                "如确需试发，请显式选择探针",
                status=409, code="no_strategy", extra={"verdict": verdict})
        chosen = verdict["batch"]
        source = "strategy"
    else:
        if not isinstance(probe_ids, list) or not probe_ids:
            raise LocateError("probe_ids 必须是非空数组", code="empty_batch")
        if len(probe_ids) > MAX_PROBES:
            raise LocateError(f"一批最多 {MAX_PROBES} 条探针")
        chosen = []
        seen: set[str] = set()
        for pid in probe_ids:
            pid = str(pid)
            p = probe_by_id.get(pid)
            if p is None:
                raise LocateError(f"探针不存在：{pid}", status=404,
                                  code="probe_not_found")
            if pid in seen:
                continue
            seen.add(pid)
            chosen.append(p)

    if len(chosen) > trials_left:
        raise LocateError(
            f"本批 {len(chosen)} 条，但试发总额只剩 {trials_left} 次；"
            "额度不足，整批未发出",
            status=409, code="budget_exceeded",
            extra={"batch_size": len(chosen), "trials_left": trials_left})

    now = time.time()
    batch_desc = [{
        "id": p["id"], "name": p["name"], "route": p["route"],
        "services": p["services"],
    } for p in chosen]

    # 4) 落 sending 轮次（UNIQUE 兜底多进程同轮竞争）
    try:
        with db.session() as con:
            con.execute(
                "INSERT INTO locate_rounds(drill_id, round_no, batch_json, source, "
                "status, created_real, started_real) VALUES(?, ?, ?, ?, 'sending', ?, ?)",
                (drill_id, round_no, json.dumps(batch_desc, ensure_ascii=False),
                 source, now, now))
    except sqlite3.IntegrityError as exc:
        raise LocateError("同一轮已被另一个请求创建", status=409,
                          code="round_duplicate") from exc

    # 5) 真正并发发起 HTTP（不持有 DB 事务），收齐全部应答
    base = gateway_base()
    workers = max(1, len(chosen))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda p: _http_get_probe(base, drill_id, p), chosen))

    # 6) 落每次应答 + 按“实际状态码”排除候选（一个 IMMEDIATE 事务原子提交）
    with db.session() as con:
        con.execute("BEGIN IMMEDIATE")
        drow = _load_drill_row(con, drill_id)
        candidates = json.loads(drow["candidates_json"])
        services = cfg["services"]
        svc_idx = {n: i for i, n in enumerate(services)}
        cand_mask = 0
        for name in candidates:
            if name in svc_idx:
                cand_mask |= 1 << svc_idx[name]

        for res in results:
            p = probe_by_id[res["probe_id"]]
            pmask = 0
            for name in p["services"]:
                if name in svc_idx:
                    pmask |= 1 << svc_idx[name]
            cand_mask = planner.eliminate_mask(cand_mask, pmask, res["http_status"])
            con.execute(
                "INSERT INTO locate_trials(drill_id, round_no, probe_id, probe_name, "
                "route, path_json, url_path, http_status, outcome, elapsed_ms, error, "
                "body_snippet, created_real) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (drill_id, round_no, res["probe_id"], res["probe_name"], res["route"],
                 json.dumps(res["path_services"], ensure_ascii=False), res["url_path"],
                 res["http_status"], res["outcome"], res["elapsed_ms"], res["error"],
                 res["body_snippet"], time.time()))

        new_candidates = [services[i] for i in planner.bits(cand_mask)]
        con.execute(
            "UPDATE locate_drills SET candidates_json=?, updated_real=? WHERE id=?",
            (json.dumps(new_candidates, ensure_ascii=False), time.time(), drill_id))
        con.execute(
            "UPDATE locate_rounds SET status='done', finished_real=? "
            "WHERE drill_id=? AND round_no=?",
            (time.time(), drill_id, round_no))
        con.commit()

    state = get_state(drill_id)
    state["send_result"] = {
        "round_no": round_no,
        "deduplicated": False,
        "sent": len(results),
        "source": source,
    }
    return state
