# D&D / CoC 审计收敛实施 Handover

更新时间：2026-08-15

> 完成状态：本文列出的任务一权限、Host context、actor lifecycle、D&D/CoC
> mutation、NPC conversation、Agent、Skills 与回测收敛项已经实现。下文缺口描述
> 保留为审计来源，不再代表当前代码状态。Content Pack 预制 Combat Grid 仍属于
> 独立任务二，本轮未实施。

当前协议只有一个 actor lifecycle 路径和一个 conversation v3 / proposal v4 Host
transport 路径。Core authorization fingerprint 包含 campaign membership 与完整 actor
grants；D&D/CoC session 在授权变化时发出 `tools/list_changed` 并改变 Host context
epoch。CoC 公开 50 个 native tools，私有 transport 经过 Host token 鉴权且永不列出，
旧公开 worker 没有 alias 或 fallback。

验收证据包括四个 Python 仓库完整 pytest/Ruff、CoC Skills validator、D&D/CoC
实际 stdio MCP + Agent conversation、公共 Lobby → Play → Combat → Play、DM/player、
Grid/Agent、retry、restart、snapshot/branch 与 undo/redo 回归。两个 CoC 私有模块在
只读 Pack 来源和隔离数据库副本上以当前 runtime 重跑，报告位于
`.runs/coc-private-current-20260815-run5/reports/parallel-campaign-backtest.json`。
旧 Pack 归档不满足当前 scene metadata schema 时会被预检拒绝；本轮没有为旧归档
恢复兼容导入。

## 目标

把 D&D 自身审计与 CoC 对标 D&D 审计合并为一条实现线：

1. 先补齐共享的权限、宿主上下文、角色生命周期和幂等边界；
2. 修复 D&D 已确认的问题；
3. 以修复后的 D&D 当前协议补齐 CoC，而不是复制 D&D 的旧实现；
4. 更新 Agent、Skills、UI/网关和真实回测，使公开协议、宿主行为和证据一致；
5. 删除被新协议取代的旧入口，不保留永久兼容分支。

本文整合以下证据：

- [Core 数据库全链路审计](../../sagasmith-core/docs/DATABASE_END_TO_END_AUDIT.md)
- [CoC 对标 D&D 旧矩阵](../../SagaSmith-coc-mcp/docs/DND_PARITY.md)
- 2026-08-15 对 D&D、CoC、Core、Agent 和 Skills 当前代码的补充审计

“完全更新”在本文中指已确认的公共能力、权限、revision、幂等、协议和真实
回归缺口。它不表示无限增加规则内容，也不允许把来源解释或叙事判断下沉到
Core/System/MCP。

## 起点与工作约束

审计起点均为干净工作树：

| 仓库 | 分支 | 起点提交 |
| --- | --- | --- |
| `sagasmith-core` | `main` | `07f9c20` |
| `sagasmith-dnd` | `main` | `346ec29` |
| `SagaSmith-dnd-mcp` | `main` | `f6716d8` |
| `SagaSmith-dnd-skills` | `master` | `1e44c39` |
| `sagasmith-coc` | `main` | `bd95caa` |
| `SagaSmith-coc-mcp` | `main` | `3c96ebe` |
| `SagaSmith-coc-skills` | `master` | `441e7c2` |
| `SagaSmith-agent` | `main` | `20d2865` |

- 根目录不是 Git 仓库；每个子目录独立提交。
- 不修改 Narrative 或其他系统，除非共享 Core 当前协议迫使其同步且先获得明确授权。
- 不 push。每个仓库使用小而连贯的提交，完成时相关工作树必须干净。
- 数据迁移只有一个当前协议和一次性迁移边界；不增加别名、双读、fallback 或永久
  兼容入口。
- 不削弱权限、证据、revision 或事务保证来让旧回测通过。

## 合并审计结论

### P0：必须先修的共享权威边界

#### 1. Actor grant 变化没有进入 Host context 身份

D&D 的 `host_context_binding` 当前只绑定 domain、campaign、principal fingerprint、
role、audience 和 branch。Actor grant 的 `can_control` / `can_view_private` 变化不会改变
`context_epoch`。调用时权限检查会拒绝未来读取，但宿主模型上下文里已经看到的私有
信息不会因此自动失效。

CoC 的缺口更大：动态 exposure 有 phase/role 检查，但没有 D&D 等价的权威
`host_context_binding` 响应协议。

正确边界：

- Core `AccessService` 提供系统中立、规范排序的 authorization fingerprint，覆盖当前
  membership role 和该 principal 在 campaign 下的全部 actor grants；不存储系统语义。
- D&D 与 CoC 的 Host binding 都必须包含该 fingerprint，并由它派生新的
  `context_epoch`。
- grant、权限降级和 revoke 后，对受影响 principal 的活动 MCP session 强制发送
  `tools/list_changed`/context barrier，即使可见工具名称没有变化。
- Agent 只接受新的当前 binding 结构；authorization fingerprint 或 context epoch
  变化必须清除对应 campaign/principal 的可复用模型上下文。

#### 2. CoC NPC conversation 仍在使用公开 local-only worker

D&D 当前协议是：

- 公共模型只看到 `npc_conversation`；
- `npc_conversation_transport` 是经过 principal/session 绑定的 Host 私有工具；
- transport 不出现在 MCP `tools/list`；
- Agent 根据 capability 私下构造 transport，并只把已验证 publication 返回 Director。

CoC 当前仍把 `npc_conversation_worker` 放在 Play 的公共 profile 中，仅靠
`principal_id == system:local` 限制调用。它没有声明
`host_transport=private_authenticated_unlisted`，因此当前 Agent v3 不会为它建立 D&D
等价的私有 transport。这与旧 parity 文档中的“完成”结论冲突。

必须把 CoC 直接切到 D&D 的当前边界：注册隐藏的
`npc_conversation_transport`、绑定 exposure principal/campaign/session、更新 capability，
并删除 MCP 公开 `npc_conversation_worker` 及 Skills 中对应旧契约。不得保留旧 worker
别名或 fallback。

#### 3. 角色创建/实例化不属于完整的权威 mutation

Core `StateMutationService` 只更新已存在角色；`RevisionService._apply` 也要求目标行存在。
因此角色创建/删除不是一等 lifecycle revision。

D&D 的影响：

- `addon_actor_instantiate` 在 Play/Combat 可创建角色，但创建不进入 undo/redo；
- Combat 中创建和后续 `combat_join` 是两个事务，join 失败会留下孤立角色；
- `character_create_from(mode="template")` 丢弃 `idempotency_key`，重试会再次插入；
- campaign-less direct create 没有限制为本地服务 principal，非本地用户可污染全局角色库。

CoC 的影响更广：

- `character_change(create|instantiate)` 没有强制幂等 key；
- create/instantiate 直接写 Character 后再 grant actor，不是同一权威 mutation；
- 创建、grant 和可能的 campaign/combat 状态没有共同 revision/undo 边界；
- update 虽有 character revision 检查，但不通过带 idempotency receipt 的
  `StateMutationService`。

正确修复不是只在 `RevisionService._apply` 中允许缺失行。Core 应增加一个窄的、系统
中立的 actor lifecycle mutation，能在单一事务中：

- 创建一个 campaign actor；
- 可选从 library template 复制并保留 `template_id`；
- 可选同时写 campaign state（例如 Combat participant）；
- 可选同时建立 actor grant；
- 写幂等 replay receipt；
- 写包含完整 actor document 的 lifecycle revision；
- undo 删除本次创建并清理其从属 grant，redo 用同一 actor id 恢复；
- 在撤销前验证没有未纳入 mutation group 的外部引用。

不要让 Core 验证 D&D/CoC sheet，也不要把 Combat 规则放入 Core。System/MCP 先验证
文档，再把显式、完整的 mutation 交给 Core。

### P1：D&D 自身修复

按以下顺序完成：

1. 使用共享 authorization fingerprint 更新 Host binding 和 session 失效测试。
2. 给 Core template instantiate 增加原子幂等入口；D&D `character_instantiate` 接受并
   透传 key；`character_create_from(mode="template")` 不再丢 key。
3. campaign-less library create 只允许 `LOCAL_SYSTEM_PRINCIPAL_ID`；远程 principal
   必须通过 campaign-scoped、role-checked 的创建路径。
4. `addon_actor_instantiate` 改用 actor lifecycle mutation。Combat 调用必须将 actor
   创建与 join 放入同一 mutation group 和事务；Play/Lobby 创建同样可 undo/redo。
5. 更新 `SagaSmith-dnd-skills` 的 NPC conversation capability 要求：当前 conversation
   schema 为 v3、proposal 为 v4、Host transport 为私有且不列出。删除 schema v2 痕迹。
6. 增加真实 Agent + stdio/HTTP MCP v3 conversation 回归。现有 Fake MCP 单元测试不算
   真实宿主证据。
7. 对当前提交重新跑模块回测；2026-08-13 的 Avernus v29 等结果不能证明 8 月 14/15
   的事务、压缩和 conversation 变更。

D&D 较低优先级但必须登记的后续项：

- Workbench 当前是本地单 principal DM 工具，不得描述为远程多用户安全 UI；若产品要
  远程使用，另行设计服务端身份映射、HttpOnly session 和 SSE 鉴权。
- rule pack 的 `test_refs` validator 需要验证目标可解析且去重，不能只验证非空字符串。
- `server.py` 体积过大是变更耦合问题；只在上述行为稳定后按 facade 域拆分，不引入
  通用编排框架。

### P2：以修复后的 D&D 当前协议更新 CoC

CoC 不应先照搬 D&D 旧行为再二次迁移。完成共享 P0 与 D&D P1 后，一次性落到以下
当前协议：

1. 实现相同结构的 Host context binding、authorization fingerprint 和 session
   barrier；保留 CoC domain id，不复制 D&D 规则字段。
2. 删除公开 `npc_conversation_worker`，实现隐藏且鉴权的
   `npc_conversation_transport`，更新 capability、Agent 集成、Skills 和真实 Host 测试。
3. `campaign_change` 增加明确的 revoke 操作。Campaign revoke 必须原子删除下属 actor
   grants；actor permission 降级必须触发目标 principal context barrier。
4. `character_change(create|instantiate|update)` 统一要求 expected revision（适用时）和
   idempotency key，使用共享 lifecycle/state mutation；角色创建与初始 grant 原子提交。
5. 对 CoC 的 runtime NPC/creature 创建、Combat/Chase participant 生命周期做同样的
   orphan、undo/redo、snapshot restore 和 retry 检查。
6. 更新 `SagaSmith-coc-skills`：删除 host-local MCP worker 描述，改为公共
   `npc_conversation` + Host 私有 transport；工具清单必须来自当前 profile。
7. 更新 `DND_PARITY.md`，把“完成”恢复为有证据的结论。不得用内部 service 测试代替
   public facade、真实 native tool refresh 和 Agent 回归。

CoC 只对标概念能力和保证等级。D&D 法术、职业、五尺方格细节不进入 CoC；CoC 的
SAN、Luck、Chase 和调查规则仍由 CoC system/MCP 拥有。

## Content Pack 是否加入预制、可编辑 Combat Grid

### 决策：应该加入，但必须是可选的 D&D 系统 profile 数据

现状已经支持：

- Module Scene Atlas 保存保守的 `spatial` location/dimension/connection 证据；
- `combat_start(positioning_mode="grid")` 从 scene evidence 编译 encounter-local 临时地图；
- DM 可以在启动时传 bounds、blocked/difficult cells，并在战斗中写 world patches；
- 运行时地图冻结在 encounter state，进入 snapshot/revision。

缺少的是：经过 Agent/source review、随已终结 Pack 发布、可在多个 campaign 重用的
确定性网格模板。因此推荐在 D&D scene 的
`metadata.profile_data.combat_grid_templates` 下增加可选协议，而不是在 Core 增加
D&D grid 表或顶层通用字段。

建议的最小模板字段：

```json
{
  "schema_version": 1,
  "id": "stable-scene-local-id",
  "title": "Goblin ambush",
  "location_key": "trail-clearing",
  "grid": {"kind": "square", "cell_ft": 5},
  "bounds": {"width_cells": 24, "height_cells": 18},
  "blocked_cells": [{"x": 4, "y": 7}],
  "difficult_cells": [{"x": 9, "y": 3}],
  "deployment_zones": [
    {"id": "party-entry", "cells": [{"x": 2, "y": 16}]}
  ],
  "map_asset_key": "optional-package-image-asset",
  "source_refs": [
    {"source_key": "module", "chunk_key": "...", "page": 12, "note": "reviewed map"}
  ]
}
```

边界规则：

- Pack draft 中可编辑；finalize 后不可原地修改。复用性修改创建新 Pack draft/version。
- Combat 启动时复制模板为新的 encounter-local map id；运行时 patch 不回写 Pack。
- `map_asset_key` 只是视觉/证据资产，不自动推导墙、视线、掩体或地形机制。
- 只接受引擎已实现的权威字段。当前先支持 bounds、blocked、difficult 和部署区域；
  cover、opaque edge、elevation 等必须等机械契约存在后再加入。
- participant 的具体 actor id 不应写进可移植 Pack。Pack 只保存命名部署区域；Agent 在
  `combat_start` 时把 campaign actor 显式分配到区域/坐标。
- 自动解析只能产生 draft candidate，不能把 PDF 图片像素直接当成权威拓扑。Agent 必须
  查看来源证据、编辑并确认。
- Core 只保存/校验通用 Content Pack 结构和 source refs；D&D system 负责模板 schema
  和五尺方格限制；MCP 负责 draft facade、权限、幂等、实例化和 runtime mutation。

### Combat Grid 实施顺序

1. `sagasmith-dnd`：定义并验证 `combat_grid_templates` schema；给编译器增加从模板生成
   encounter-local map 的纯函数。
2. `SagaSmith-dnd-mcp`：给 `module_draft(edit)` 增加单一 `combat_grid` operation，支持
   upsert/remove draft template，要求 scene、evidence、expected revision 和 idempotency。
3. finalize 时把模板放入 `profile_data`；import 后必须得到相同规范值和 checksum。
4. `combat_start` 接受 `battle_map_template_id`，模板与自由 `battle_map` override 不得同时
   形成两个权威来源。允许的 DM override 必须显式记录 receipt。
5. Skills 指导 Agent 按“查看地图证据 → 编辑候选 → 验证 → finalize”工作，不增加
   MCP 自动理解地图语义。
6. 增加 Pack export/import/restart、跨 campaign 实例化、runtime patch、snapshot、
   branch、undo/redo 和损坏资产/越界 cell 失败测试。

CoC 只有在其 system 定义了等价、稳定的 Grid 模板机械后才复用这个概念；不能因为
“对标 D&D”就把 D&D 五尺方格 schema 复制到 CoC。

## 数据迁移与旧兼容删除

- NPC conversation 持久数据已经是 v3；本轮迁移的是 Host transport/exposure 协议。
  删除 CoC MCP `npc_conversation_worker` 注册、tool policy、capability 和 Skills 引用。
- Agent 只保留 `private_authenticated_unlisted` transport 路径，不添加 CoC fallback。
- Host binding 升级为单一当前结构；所有 D&D/CoC caller 和测试同一提交序列更新。
- actor lifecycle 若需要 schema migration，使用单一 Alembic head 和一次性升级；迁移后
  runtime 不读旧字段。回滚方式是恢复与旧 runtime 匹配的完整 SQLite 备份，而不是
  downgrade 新库。
- 现有 v32 数据库先在临时副本上迁移、`quick_check`、foreign-key、public snapshot
  verify、undo/redo 和 restart；不得直接用保留回测库做破坏性试验。

## 验收门槛

### 单元与公共 facade

- D&D/CoC template instantiate 同 key 同 payload 返回同 actor id；同 key 不同 payload
  冲突。
- runtime actor create + Combat join 是一个事务；任一失败不留下 actor、grant、combat
  participant、idempotency receipt 或半个 mutation group。
- create undo 删除 actor，redo 恢复同 id/模板 lineage/文档/grant；snapshot restore 与
  branch checkout 后一致。
- campaign revoke 和 actor-private 降级会让目标 Host context epoch 改变，并强制活动
  session barrier；下一次私有读取失败。
- CoC `tools/list` 永远不出现 private transport；Director 也看不到 capsule/raw proposal。
- D&D/CoC Skills validator 不包含 v2 或 `npc_conversation_worker` MCP 契约。

### 真实宿主与回测

- 真实 Agent + MCP 覆盖 Lobby → Play → Combat → Play、DM/player、Grid/Agent、
  `tools/list_changed`、grant/revoke、NPC conversation close/abort/settle、幂等重试、
  revision refresh、restart/resume、snapshot/branch restore 和 undo/redo。
- D&D 当前全部 runnable modules 至少一个合法结局；互斥路径使用 focused coverage，
  exclusions 机器可读。
- CoC 两个私有模组回测用当前 Agent/transport 重跑，不得沿用 2026-08-13 的旧工具
  surface 结果作为新协议证据。
- Combat Grid 若实施，至少一个真实 Pack 从地图证据创建模板，跨 campaign 导入后启动
  Grid Combat，并验证模板未被 runtime patch 反向修改。

### 质量与交付

- 各 Python 仓库运行完整适用 pytest 与 `ruff check .`；Skills 运行仓库 validator；UI
  运行 typecheck/build/测试和至少一条真实浏览器流程。
- 更新 server capabilities、tool catalog、Skills、UI/网关文档和 parity 矩阵。
- 搜索并删除所有被替代的 v2、公开 worker、旧 Host binding 字段、compat/fallback
  痕迹。
- 每个仓库小提交、无 trailer、工作树干净、不 push。
