"""故障定位策略规划（纯函数，不依赖 Flask / DB）。

模型
----
* 一场演练有 ``n <= 8`` 个候选服务，用位掩码表示候选集合。
* 每条探针经过若干服务，也用位掩码 ``p_j`` 表示。
* 探针在“路由启用”时只会得到两种有信息量的真实 HTTP 应答：

  - ``200``：请求没有经过故障服务 ⇒ 故障不在 ``p_j`` 中 ⇒ 从候选中剔除整段 ``p_j``；
  - ``502``：请求经过故障服务     ⇒ 故障在 ``p_j`` 中   ⇒ 候选与 ``p_j`` 取交集。

* 路由维护停用时网关回 ``503``：**没有信息量**，候选不变，但试发次数照扣。

一批探针（同一轮并发发出）会按“各候选故障下这批探针的结果向量”把当前候选
划分成若干等价类（cell）。同一格内的候选在这批应答下不可区分，下一轮再对
每个格分别规划。

最优性递推
----------
``best(C, r)`` 返回在至多 ``r`` 轮内保证查出 ``C`` 中任意故障时，最坏分支
所需的最少试发次数，并给出对应的“下一批”探针::

    best(C, r) = min over 非空批次 B:
        |B| + max(best(cell, r-1).sends)   # 对 B 划分出的每个 cell

边界：``|C| <= 1`` 时已经查明，代价 0；``r <= 0`` 时仍有多个候选则无解。

调用方再用剩余次数 ``q`` 校验 ``best.sends <= q``：存在策略当且仅当
“最坏分支花费”不超过剩余总额度。注意额度只沿真实发生的那一条分支扣，
所以用 ``max`` 而不是求和。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

MAX_SERVICES = 8
MAX_PROBES = 8

# 规划结论种类
IDENTIFIED = "identified"      # 只剩一个候选
FEASIBLE = "feasible"          # 存在保证策略，batch 即该策略的下一批
INFEASIBLE = "infeasible"      # 剩余次数 / 轮数 / 当前启用路由下无保证策略
EMPTY = "empty"                # 候选为空（真实应答与设定矛盾，正常不会发生）

# 无信息 HTTP 状态（含 503 / 网络错误）
STATUS_CLEAR = 200
STATUS_HIT = 502
STATUS_DISABLED = 503


# ---------------------------------------------------------------------------
# 位掩码与排除逻辑
# ---------------------------------------------------------------------------

def bits(mask: int) -> list[int]:
    """返回掩码中置位的下标列表。"""
    out: list[int] = []
    i = 0
    while mask:
        if mask & 1:
            out.append(i)
        mask >>= 1
        i += 1
    return out


def eliminate_mask(cand: int, probe_mask: int, http_status: int) -> int:
    """根据一次真实 HTTP 应答更新候选掩码。

    只认状态码：200 排除路径上全部服务；502 交集到路径；其余（503 / 0 等）
    不排除任何候选。
    """
    if http_status == STATUS_CLEAR:
        return cand & ~probe_mask
    if http_status == STATUS_HIT:
        return cand & probe_mask
    return cand


def batch_signature(fault_idx: int, batch_masks: Iterable[int]) -> int:
    """某个候选故障下，一批探针的结果向量（第 j 位=1 表示该探针会 502）。"""
    sig = 0
    for j, m in enumerate(batch_masks):
        if m & (1 << fault_idx):
            sig |= 1 << j
    return sig


def partition_cells(cand: int, batch_masks: list[int]) -> list[int]:
    """按批次结果向量把候选集合划分成若干 cell 掩码。"""
    cells: dict[int, int] = {}
    for i in bits(cand):
        sig = batch_signature(i, batch_masks)
        cells[sig] = cells.get(sig, 0) | (1 << i)
    return list(cells.values())


# ---------------------------------------------------------------------------
# 策略 DP
# ---------------------------------------------------------------------------

@dataclass
class Best:
    """某个状态下的最优保证策略。"""
    sends: int                 # 最坏分支试发次数
    rounds: int                # 最坏分支轮数
    batch: tuple[int, ...]     # 第一批探针下标；空元组表示已查明

    @property
    def batch_mask(self) -> int:
        m = 0
        for i in self.batch:
            m |= 1 << i
        return m


class StrategyPlanner:
    """针对固定的探针路径掩码与“当前是否可规划”集合做记忆化 DP。

    ``probe_masks`` 始终包含全部探针；``usable`` 为当前路由启用、可进入
    规划的探针下标集合。停用路由上的探针不会被推荐（真要强发只会拿到 503）。
    """

    def __init__(self, probe_masks: list[int], usable: Iterable[int] | None = None):
        self.masks = list(probe_masks)
        self.m = len(self.masks)
        if usable is None:
            self.usable = list(range(self.m))
        else:
            self.usable = [j for j in usable if 0 <= j < self.m]
        self._memo: dict[tuple[int, int], Best | None] = {}

    # -- 公开入口 ---------------------------------------------------------

    def best(self, cand: int, rounds_left: int) -> Best | None:
        if cand == 0:
            return None
        if cand & (cand - 1) == 0:
            return Best(0, 0, ())
        if rounds_left <= 0 or not self.usable:
            return None
        # 超过探针数的轮数没有额外意义：每条有信息的探针在同一候选格内只会
        # 被用一次（再发结果不变），所以最多 m 轮。
        rounds_left = min(rounds_left, self.m) if self.m else 0
        key = (cand, rounds_left)
        if key in self._memo:
            return self._memo[key]

        winner: Best | None = None
        u = self.usable
        ucount = len(u)
        # 枚举当前可用探针的所有非空子集作为“这一批”
        for sub in range(1, 1 << ucount):
            batch = tuple(u[j] for j in range(ucount) if sub & (1 << j))
            batch_masks = [self.masks[j] for j in batch]
            cells = partition_cells(cand, batch_masks)
            if len(cells) < 2:
                continue  # 这批在当前候选上完全不区分，发了白发
            subs: list[Best] = []
            ok = True
            for cell in cells:
                nxt = self.best(cell, rounds_left - 1)
                if nxt is None:
                    ok = False
                    break
                subs.append(nxt)
            if not ok:
                continue
            sends = len(batch) + max(s.sends for s in subs)
            depth = 1 + max(s.rounds for s in subs)
            cand_best = Best(sends, depth, batch)
            if winner is None or self._score(cand_best) < self._score(winner):
                winner = cand_best

        self._memo[key] = winner
        return winner

    @staticmethod
    def _score(b: Best) -> tuple:
        # 首要：最坏分支最省试发次数；其次：最快查明（轮数少，批量并发）；
        # 再次：本轮尽量多发（利用并发、缩短轮数）；最后确定性下标序。
        return (b.sends, b.rounds, -len(b.batch), b.batch_mask)


@dataclass
class Verdict:
    kind: str
    candidates: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    batch_indexes: list[int] | None = None
    worst_sends: int | None = None
    worst_rounds: int | None = None
    reason: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "detail": self.detail,
            "candidates": self.candidates,
            "excluded": self.excluded,
            "batch_indexes": self.batch_indexes,
            "worst_sends": self.worst_sends,
            "worst_rounds": self.worst_rounds,
        }


def plan_verdict(
    services: list[str],
    probe_service_sets: list[list[str]] | list[set[str]],
    candidates: list[str],
    trials_left: int,
    rounds_left: int,
    probe_usable: list[bool] | None = None,
) -> Verdict:
    """给 API / UI 用的一站式结论。

    参数均为“当前时刻”快照：已经按全部真实应答更新过的 ``candidates``、
    剩余次数 / 轮数，以及每条探针在当前路由启停状态下是否可规划。
    """
    n = len(services)
    idx = {name: i for i, name in enumerate(services)}
    cand_mask = 0
    unknown = [c for c in candidates if c not in idx]
    if unknown:
        return Verdict(EMPTY, reason="unknown_candidate",
                       detail=f"候选含未登记服务：{unknown}")
    for c in candidates:
        cand_mask |= 1 << idx[c]

    if cand_mask == 0:
        return Verdict(EMPTY, reason="empty_candidates",
                       detail="所有服务都被应答排除，真实应答与设定不一致")

    excluded = [s for s in services if not (cand_mask & (1 << idx[s]))]

    if cand_mask & (cand_mask - 1) == 0:
        only = services[bits(cand_mask)[0]]
        return Verdict(IDENTIFIED, candidates=[only], excluded=excluded,
                       reason="one_candidate")

    if trials_left <= 0:
        return Verdict(INFEASIBLE, candidates=list(candidates), excluded=excluded,
                       reason="budget_exhausted",
                       detail="试发总次数已用完，仍有多个候选服务")
    if rounds_left <= 0:
        return Verdict(INFEASIBLE, candidates=list(candidates), excluded=excluded,
                       reason="rounds_exhausted",
                       detail="轮数上限已用完，仍有多个候选服务")

    masks: list[int] = []
    for ps in probe_service_sets:
        m = 0
        for name in ps:
            if name in idx:
                m |= 1 << idx[name]
        masks.append(m)

    if probe_usable is None:
        usable = list(range(len(masks)))
    else:
        usable = [j for j, ok in enumerate(probe_usable) if ok]

    planner = StrategyPlanner(masks, usable)
    best = planner.best(cand_mask, rounds_left)
    if best is None:
        return Verdict(
            INFEASIBLE, candidates=list(candidates), excluded=excluded,
            reason="no_strategy",
            detail=("在当前启用路由与剩余轮数内，不存在能保证区分全部候选的"
                    "探针策略（可能有关键路由停用，或若干服务对所有探针同构）"),
        )
    if best.sends > trials_left:
        return Verdict(
            INFEASIBLE, candidates=list(candidates), excluded=excluded,
            reason="budget_insufficient",
            detail=(f"保证查明的策略最坏需要 {best.sends} 次试发，"
                    f"当前只剩 {trials_left} 次"),
            worst_sends=best.sends, worst_rounds=best.rounds,
        )

    return Verdict(
        FEASIBLE, candidates=list(candidates), excluded=excluded,
        batch_indexes=list(best.batch),
        worst_sends=best.sends, worst_rounds=best.rounds,
        reason="strategy_found",
        detail=(f"存在保证策略：本批 {len(best.batch)} 条并发，"
                f"最坏共 {best.sends} 次试发、{best.rounds} 轮内查明"),
    )
