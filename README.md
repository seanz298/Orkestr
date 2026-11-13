# Orkestr

Autonomous Multi-Agent Software Engineering

你提需求、做最终验收；Codex、DeepSeek、Claude 自动完成分析、开发、测试、审核和修复。任何代码在你批准之前都不会合并。

```text
你（产品负责人）: 写任务文件 → 最后 approve / reject
        │
Python Orchestrator: 任务队列 · 状态 · 预算 · 修复循环 · 审批
        │
  ┌─────┼──────────┬──────────────┐
Codex            DeepSeek        Claude
codex exec       Deep Agents     Claude Agent SDK
(计划/兜底修复)   (开发/修复)      (只读审核)
  └─────┴──────────┴──────────────┘
        │  统一交付契约 (orchestrator/contract.py)
Git worktree 隔离：每个任务一个分支 ai/<任务号>
        │
独立质量门禁：跑你的测试命令 + 检查受保护路径
        │
AWAITING_APPROVAL → 你批准后才合并
```

## 当前验证状态（请先读）

| 部分 | 状态 |
| --- | --- |
| Orchestrator 核心：队列、状态、预算、worktree、质量门禁、修复循环、只读强制、审批合并、MoA 流程、记忆接口 | **已测试**（14 个自动化测试 + `selftest`，用脚本化的假 Agent） |
| Claude 适配器 | 接口已对照 claude-agent-sdk 0.2.172 核对；**未做真实调用** |
| Codex 适配器（`codex exec`） | 按官方文档编写，**未运行过** |
| DeepSeek 适配器（Deep Agents） | 按官方文档编写，**未运行过** |
| Dockerfile / docker-compose | **未构建过**（开发环境没有 Docker） |
| MoA 的真实 API 调用 | **未运行过** |

所以首次部署请按下面的顺序一步步来，哪一步报错就把报错贴回来修。

## 部署（Windows + Docker Desktop）

前提：装好 Docker Desktop 和 Git。在 PowerShell 或 CMD 里：

```bat
git clone https://github.com/seanz298/Orkestr
cd Orkestr
copy .env.example .env
notepad .env
```

在 `.env` 里填三个 API 密钥，并核对模型名。然后：

```bat
docker compose build
docker compose run --rm orchestrator python -m orchestrator selftest
docker compose run --rm orchestrator python -m orchestrator doctor
docker compose up -d
```

- `selftest` 不需要任何密钥，通过就说明核心流程在你的机器上能跑。
- `doctor` 逐项检查密钥、依赖包和 Codex CLI，全部 OK 再继续。

## 跑第一个任务

```bat
orkestr add-repo https://github.com/你的用户名/你的仓库
copy tasks\example.yaml tasks\first.yaml
notepad tasks\first.yaml
orkestr submit /tasks/first.yaml
orkestr status
```

任务结束后：

```bat
orkestr log <任务号>        看每一步发生了什么
orkestr diff <任务号>       看改了什么
orkestr approve <任务号>    合并到基础分支（只在容器内本地合并，不会 push）
orkestr reject <任务号>     不要这次的结果（分支保留供检查）
orkestr retry <任务号>      从头重跑
```

实时日志：`docker compose logs -f`

**建议的启用顺序**：第一次先在 `config.yaml` 里把 `planner` 和 `escalation` 设为 `null`，只验证 DeepSeek 开发 + Claude 审核；跑通后再打开 Codex。改完 `config.yaml` 执行 `docker compose restart`。

私有仓库：`add-repo` 时用带令牌的地址 `https://<token>@github.com/...`。批准后的 push 需要你手动执行：
`docker compose exec orchestrator git -C /workspace/repos/<仓库名> push`

## 任务状态

`QUEUED → PLANNING → DEVELOPING → GATING → REVIEWING ⇄ REPAIRING → AWAITING_APPROVAL → APPROVED / REJECTED`

失败出口：`FAILED`（修复轮数用尽或 Agent 出错）、`BUDGET_EXCEEDED`、`INTERRUPTED`（容器重启时正在跑的任务）。

## 安全设计

- **验收测试不由写代码的 Agent 决定**：`protected_paths` 里的文件一旦被改，门禁直接判不通过。把你亲自写的验收测试放进去。
- **只读是强制的**：审核和计划角色结束后，工作区若有任何改动会被自动还原并记录 `readonly_violation`，不依赖模型自觉。
- **密钥隔离**：每个 Agent 进程只拿到自己的密钥；测试命令和 Deep Agents 的 shell 拿不到任何密钥。
- **硬上限**：每次调用有轮数上限和超时（到时杀掉整个进程组），每个任务有预算上限。
- **不自动发布**：`approve` 只做本地合并，push 永远是你手动做。

## 记忆接口（ALM 接入点）

`orchestrator/memory.py` 定义了记忆系统与流水线之间的全部契约，默认实现 `NullMemory` 不做任何事。

- 开工前 `get_context(spec)`：返回最多 3 条提示，**只发给规划者**（planner 或 MoA）；没有规划者时不调用。
- 收工后 `record_outcome(outcome)`：任务每次运行结束时调用一次，内容全部来自事件日志。
- 失败任务在预算允许时由审核角色做一次只读的事后归因；预算不够则记 `postmortem_skipped`。
- 记忆系统抛异常不会让任务失败，只记 `memory_failed` 事件；它的花费计入任务预算。

接入方式：构造 `Pipeline(..., memory=你的实现)`。目前还没有配置项，`cli.py` 里用的是默认的 `NullMemory`。

注意：`record_outcome` 发生在流水线结束时，成功任务的状态是 `AWAITING_APPROVAL`。你之后的 `approve` / `reject` 目前不会通知记忆系统。

## 已知限制

1. **预算统计默认不完整**。Claude 的费用由 SDK 返回，是准的；DeepSeek 和 Codex 的费用靠 `config.yaml` 里的 `price_in / price_out` 乘以 token 数，默认是 0。不填单价，预算就只算 Claude 的花费。请按官网现价填写。
2. **Codex 在容器里的沙箱可能启动失败**。若报沙箱相关错误，把 `config.yaml` 里 codex 的 `sandbox_write` 改为 `danger-full-access`（此时容器本身是隔离边界）。
3. **容器不是强沙箱**。Agent 在容器内有网络，能读写整个 `/workspace`。不要在里面放无关的重要数据。
4. **代码会发送给三家模型服务商**。涉及不能外传的代码请勿使用。
5. **依赖版本未锁定**。第一次跑通后执行 `docker compose exec orchestrator pip freeze` 并把版本写回 `requirements.txt`。
6. 单 worker 串行执行任务，暂不支持并发。

## 本地开发

```bash
pip install pydantic pyyaml httpx pytest
python -m pytest -q
python -m orchestrator selftest
```

## 目录

```text
orchestrator/
  contract.py     交付契约：TaskSpec / Delivery / GateReport
  pipeline.py     任务生命周期
  gate.py         质量门禁与密钥清洗
  workspace.py    Git worktree 隔离与合并
  moa.py          按需启动的多模型方案综合
  memory.py       记忆接口（ALM 接入点）
  store.py        SQLite 任务与事件日志
  cli.py          命令行
  selftest.py     离线端到端自测
  agents/         三个 Harness 的适配器（每个在独立子进程里运行）
config.yaml       角色分工、模型、限额
tasks/            任务文件
```
