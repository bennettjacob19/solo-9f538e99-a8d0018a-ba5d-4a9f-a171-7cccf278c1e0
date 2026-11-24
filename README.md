# 故障依赖定位演练台（有限网关试发 + 保证策略规划 + 多页同轮去重）

值班员要用有限的网关试发找出链路上的故障依赖。本项目在原有 Flask + SQLite
网关系（见 [README-legacy.md](README-legacy.md)，旧控制台保留在 `/ops`）上
新增了一个完整的“录入服务 / 探针 → 选定本场固定故障 → 按保证策略一轮轮并发
试发真实网关 → 依据 HTTP 状态码排除候选”的演练台。

所有关键行为都由**真实 HTTP 应答 + SQLite 状态 + 服务端并发**支撑，不是前端
动画，规划器也不读取故障答案。

---

## 1. 快速启动

### 本地 Python

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
# 打开 http://127.0.0.1:8000
```

环境变量：`DRILL_PORT`（默认 8000）、`DRILL_DATA_DIR`（current.json 目录）、
`DRILL_STATE_DIR`（SQLite 目录）、`DRILL_GATEWAY_BASE_URL`（服务端自发自收时
覆盖网关基址，默认 `http://127.0.0.1:$DRILL_PORT`）。

### docker compose

```bash
docker compose up --build
# http://localhost:8000 ，数据持久化在命名卷 gsb-drill-volume（/drill）
```

compose 已配置端口 `8000:8000`、命名卷、`init: true` 与 Dockerfile HEALTHCHECK。

---

## 2. 题目要求 → 实现位置

| 要求 | 实现 |
| --- | --- |
| 最多 8 个模拟服务 | 录入页动态服务行，前后端都强制 `1..8`、名称唯一 |
| 最多 8 条探针 | 探针卡片最多 8 条；可命名 |
| 每条探针选一条**现有路由** | 下拉来自网关 `current.json` 的真实路由（内置 6 条），建场时服务端校验路由存在 |
| 填入请求经过的服务 | 每条探针用 chip 多选本场已录入服务，保留经过顺序用于展示 |
| 选定一个故障服务，本场固定 | 建场时必选；写入不可变 `config_json.fault_service`，整场不变 |
| 故障只注入本场探针 | 网关仅对带 `?_drill=<id>&_probe=<pid>` 且归属校验通过的请求，按**该场**故障注入；普通请求与其他演练不受影响 |
| 定位只用网关实际 HTTP 应答 | 服务端用线程池对真实监听端口发起 HTTP GET；排除候选只看记录下来的状态码（`drill/planner.eliminate_mask`），规划器 / 排除逻辑完全不读 `fault_service` |
| 经过故障 → 502，否则 200 | `app.py` 网关注入点：命中本场故障依赖返回 **502**，启用路由其余返回 **200** |
| 维护停用 → 仍 503、计次、不排除 | 网关先判停用，**503 优先于 502**；trial 照常落库扣额度；`eliminate_mask` 对 503 原样保留候选 |
| 填试发总次数 + 轮数上限 | 建场表单；服务端硬校验批次大小 ≤ 剩余次数、round_no ≤ 轮数上限 |
| 每轮一键**并发**试发，收齐再安排下一轮 | `POST .../rounds` 内 `ThreadPoolExecutor` 并发全部探针，`pool.map` 收齐后一个事务落库，响应返回完整轮次；前端 await 后才刷新出下一批 |
| 两页同场，同一轮只发一批 | 同进程 per-drill 互斥锁 + SQLite `UNIQUE(drill_id, round_no)`；同 `round_no` 的第二个请求拿到同一轮并标 `deduplicated:true`，不产生第二条 HTTP / trial |
| 每轮按当前启用路由规划 | 每次 `GET drill` 都重读 current.json 的 `disabled`，生成 `usable[]` 喂给规划器；停用路由上的探针不会被推荐 |
| 存在保证策略就给“下一批” | DP 在“剩余次数 q、剩余轮数 r、当前候选 C”下判断最坏分支花费是否 ≤ q，并返回见证策略的第一批 |
| 只剩一个候选 → 指出故障服务 | verdict=`identified`，页面大 chip 显示，并与考官设定核对 ✓ |
| 无可行策略 → 列出未排除服务 | verdict=`infeasible`（`no_strategy / budget_insufficient / rounds_exhausted / budget_exhausted`），结论卡列出全部候选 chip |
| 页面显示各次应答与剩余额度 | 应答表：时间 / 轮 / 探针 / 路由 / 经过服务 / 真实状态码徽章 / 排除效果 / 耗时；顶部额度卡实时显示剩余次数（已发/总额）、剩余轮数、候选数，1s 轮询 |

URL 带 `?drill=<id>` 可直接打开同一场——把地址复制到第二个浏览器页面即可
观察同轮去重与轮询同步（页面有“🔗 复制两页同开链接”按钮）。

---

## 3. 候选排除规则

设探针经过服务集合为 \(P\)，当前候选为 \(C\)：

- **200**：请求没经过故障 ⇒ \(C \leftarrow C \setminus P\)（路径上所有服务都排除）；
- **502**：请求经过故障，且全场只有一个固定故障 ⇒ \(C \leftarrow C \cap P\)；
- **503 / 网络错误 / 其他**：无信息量 ⇒ \(C\) 不变，但 503 是真实试发，照扣次数。

集合操作可交换，因此一批内并发应答的落库顺序不影响最终候选。

---

## 4. 保证策略规划器（`drill/planner.py`，纯 Python 无 IO）

一批探针在某个候选故障下会产生一个 0/1 结果向量（每位表示该探针是否 502），
所以一批会把当前候选划分成若干“结果格”。同一格内的候选在这批之后仍不可区分，
下一轮再分别自适应处理。

记忆化递推（只依赖候选掩码 \(C\) 与剩余轮数 \(r\)，服务/探针 ≤8）：

```
best(C, r):
  |C| <= 1        -> 花费 0，已查明
  r <= 0          -> 无解
  否则枚举可用探针的所有非空子集 B（≤ 2^8=256 批）:
      cells = partition(C, B)          # 按本批结果向量分格
      若每个 cell 都有 best(cell, r-1):
          sends = |B| + max_cell(best(cell).sends)
          depth = 1 + max_cell(best(cell).rounds)
      选 sends 最小、再 depth 最小、再批量更大、最后下标序确定的 B
```

为什么是 `max`：额度只沿真实发生的那一条自适应分支扣，不是把所有分支的花费
相加。最后若 `best(C,r).sends <= 剩余次数 q`，判定 `feasible` 并把见证策略的
第一批作为“下一批”。这保证“无论故障落在哪个候选服务上”都能在 q/r 内查明。

- 单轮上限为 1 时，只有“一轮结果向量给所有候选唯一编码”的批次才可行；
- 对所有探针同构（列向量相同）的两个服务，任何预算 / 轮数都无法区分，
  会正确给出 `no_strategy`；
- 规划器有 151 项自测，其中包括 40 个随机可行配置 × **逐故障模拟通关**，
  验证“推荐什么就发什么”时保证性质成立。

---

## 5. 两页同轮只发一批的并发设计

```
页面 A 点击 ─┐
             ├─ POST /rounds {round_no:N, probe_ids:[...]}
页面 B 点击 ─┘
                 │
        per-drill threading.Lock（同进程串行）
                 │
        SELECT 已有 (drill,N) ? ── 是 ──► 返回同一轮 deduplicated:true
                 │ 否
        INSERT locate_rounds(status='sending')   ← UNIQUE(drill_id,round_no) 兜底
                 │
        ThreadPoolExecutor 并发真实 HTTP  GET /gw<route>?_drill&_probe
                 │ 收齐
        BEGIN IMMEDIATE：写 locate_trials + 更新 candidates + round='done' + COMMIT
```

- 两个页面带同一个 `round_no` 同时点：只有一个发送，另一个在锁释放后读到
  done 轮并收到 `deduplicated:true`，前端提示“已合并，未重复试发”；
- 页面每秒 `GET drill`，所以即使 B 没点，也会看到 `sending → done` 与全部应答；
- 多进程部署时第二插入者会撞 UNIQUE 得到 `409 round_duplicate/in_flight`，
  前端自动刷新同步（compose 是单进程 `python app.py`，主路径是锁 + 回放）；
- 进程重启时把残留 `sending` 轮回收为 `interrupted`，GET 时还会清理 30s 以上
  的发送残留，避免页面永久转圈。

---

## 6. 代码结构与 HTTP API

```
app.py                     Flask 入口：/ 新演练页、/ops 旧控制台、网关注入、locate API
drill/planner.py           纯算法：掩码排除、结果分格、保证策略 DP / verdict
drill/locate.py            SQLite 存储、校验、路由维护、真实 HTTP 并发执行、同轮锁
templates/locate.html      新演练页骨架
static/locate.js           原生 JS：建场表单、1s 轮询、批次勾选、开关、应答表、两页同步
static/locate.css          暗色控制台样式
tests/locate_selftest.py   新功能端到端自测（151 项断言，自起真实服务）
tests/selftest.py          旧控制台自测（100 项断言，仍全绿）
docker-compose.yml         8000 + 命名卷 + init
```

主要 API：

| 方法 / 路径 | 说明 |
| --- | --- |
| `POST /api/locate/drills` | 建场（校验 8 服务 / 8 探针 / 现有路由 / 故障归属 / 额度） |
| `GET  /api/locate/drills/<id>` | 全量状态：配置、候选、额度、轮次+应答、实时 verdict |
| `POST /api/locate/drills/<id>/rounds` | 发一轮；body 可带 `round_no`、可选 `probe_ids`（不带则用策略推荐） |
| `DELETE /api/locate/drills/<id>` | 删除演练 |
| `GET  /api/locate/routes` | 现有路由 + 当前启用状态 |
| `POST /api/locate/routes/maintenance` | `{path, enabled}` 直接原子更新 current.json 的 disabled |
| `GET /gw/<route>?_drill=<id>&_probe=<pid>` | 真实网关：503 优先，其次演练 502 注入，否则 200 |

---

## 7. 实测结果（交付前真实执行）

环境：Debian + Python 3.11，自建 `.venv` 安装 Flask 3.1.3；Node v22 + jsdom 28。

### 7.1 新功能 Python 端到端：`151` 项断言全过

```bash
.venv/bin/python tests/locate_selftest.py
# 全部通过：151 项断言
```

脚本自找空闲端口、临时卷、拉起**真实 app.py 子进程**，覆盖：

- 排除规则（200 / 502 / 503 / 网络异常）、结果分格、DP 边界与确定性；
- 8 单服务探针在 q=6/r=1 不可行、q=7/r=1 一批 7 条可行；同构服务永远无解；
- 自适应 5 服务剧本最坏 3 次 / 3 轮；**40 个随机可行配置逐故障模拟全部通关**；
- 真实网关：普通 200、命中故障 502、停用后 503 优先、未知路由 404；
- 4 个单服务故障 × 与 5 个自适应故障，逐个走 HTTP API 按推荐批次通关并与设定核对；
- **轮次受限**：cap=1 手动发 2 条后剩 2 候选 → `rounds_exhausted` 并列候选；
  第 2 轮 `409 round_cap_reached`；同 round_no 重放仍只有 2 条 trial；
- 额度：3 条 > 剩余 2 时整批 `409 budget_exceeded` 且零记录；
- **中途停用**：自适应剧本第 1 轮后停用 `/api/checkout` → 重规划 `no_strategy`
  并列 c/d；手动强发该探针得到**真实 503**、used+1、候选不变；恢复后重规划成功，
  第 3 轮定位 d（共 3 轮 / 4 次，含 1 次 503）；
- **两页同轮**：8 线程 Barrier 齐发同一 `round_no`，全部 200，恰好 1 个
  `deduplicated:false`、7 个 `true`，库里只有 1 轮 / 3 条应答（不是 8 批 24 条）；
- SIGKILL 整服务后用同一卷硬重启，演练 / 候选 / 应答都在并可继续通关；
- compose YAML（PyYAML）与 Dockerfile 静态核对。

### 7.2 真实前端 jsdom 交互：`22/22` 通过

在 `/tmp` 独立目录 `npm i jsdom`，用 jsdom 加载运行中服务的真实页面并执行真实
`static/locate.js`：

- 录入页默认值 / 路由下拉 / fault chip 渲染；
- 真实点击“创建演练”→ DP 推荐均衡探针 → 点击“一键并发试发”→ 真实 502 行渲染 →
  第 2 轮 → 绿色 identified 卡“与本场设定 pay 一致”，额度 8→6、轮数 3→1；
- 真实点击维护开关停用 / 恢复路由，页面出现“维护停用 · 503”；
- **两个 jsdom 页面同时打开 `?drill=<id>` 并在同一 tick 齐点发送**：两页都同步
  出 3 行应答与 s0 定位，服务端仍只有 used=3 / 1 轮，至少一页弹“未重复试发”；
- 两个页面均无未捕获 JS 错误；`node --check static/locate.js` 通过。

### 7.3 旧控制台无回归

```bash
.venv/bin/python tests/selftest.py
# 全部通过：100 项断言
```

`/ops` 旧页面、租约 / fencing / publisher 子进程 / 硬重启 / ENOSPC 等全部保持。

---

## 8. 推荐手动剧本（对应“试跑”三场景）

1. **轮次受限**：示例（4 服务 / 4 单服务探针），轮数上限填 `1`、次数填 `8`。
   规划器会推荐 3 条一批（一发定位）；也可取消“始终跟随推荐”，只勾 2 条发送 →
   剩 2 个候选且顶部“剩余轮数 0”，结论卡变“轮数上限已用完”并列出 2 个服务，
   再点发送按钮禁用 / API 返回 409。
2. **中途停用**：点“多轮自适应示例（5 服务）”，创建后发送推荐的 t1+t3；
   候选变 c/d、推荐 t4。到“路由维护”把 `/api/checkout` 关掉 → 结论立刻变
   “无可行策略”。在批次列表手动勾 t4 并发送 → 应答表出现琥珀色 503，次数 +1、
   候选仍是 c/d。重新打开开关 → 推荐恢复，再发一轮定位 d。
3. **两页同轮**：创建任意演练后点“🔗 复制两页同开链接”，第二个浏览器页面打开；
   两页一起点“一键并发试发”，只有一批真实请求，后返回的页面提示已合并；
   不开第二枪也能在 1s 内轮询到另一页发出的结果。

---

## 9. 缺项 / 已知边界（如实说明）

1. **本沙箱没有 docker / docker compose 可执行文件**，镜像构建与
   `docker compose up` 未在本机实跑；只做了 Dockerfile 核对与 compose YAML
   解析（端口 / 命名卷 / init / 服务名正确）。首次在有 Docker 的机器运行建议确认。
2. **没有真实浏览器 / 截图 / 像素级样式检查**；前端通过 jsdom（无布局渲染）
   做了真实脚本交互与两页齐点验证，视觉 / 窄屏未人工验收。
3. 服务端通过 `127.0.0.1` 对自己发真实 HTTP（compose 单进程 dev server 下
   必然可达）。生产化若改用多同步 worker（如 gunicorn sync），自发自收存在
   worker 占满导致自锁的理论风险，且同轮竞争的第二请求会走 409 + 前端轮询
   而非锁内回放；本项目按演练工具定位继续使用 `python app.py`
   （threaded 单进程，与旧控制台一致）。
4. 规划假设“当前启用路由在本批 / 本策略执行期间保持现状”。中途停用被建模为
   外部事件：下一次 GET 立即重规划；若停用恰好落在一批已发出的请求途中，
   对应应答就是真实 503（计次、不排除），随后重规划。
5. `_drill/_probe` query 是演练专用的弱上下文标识，**没有鉴权 / 签名**；
   本机 / 内网演练用途，请勿直接暴露公网。
6. 新演练与旧控制台共用同一份 `current.json` 与同一个 SQLite 库：旧发布流程
   写入的 disabled 清单会真实影响新演练的“现有路由启用状态”（这正是“维护停用”
   来源），但两边并发改配置是 last-writer-wins，没有做跨模块配置锁。
7. 没有做登录、多演练列表首页、探针定时自动发送、请求 body / 方法编辑
   （探针固定 GET，足够拿到 200/502/503）。
