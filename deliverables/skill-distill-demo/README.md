# 五个公共项目 · 前提信息补全与「炼化 Skill」实战

本目录是一次完整的演练产物：先给线上实例里的五个公共项目补齐背景、数据与前提约束，再选其中一个项目模拟一段真实技术人员与 Tech Lead 智能体的多轮对话，最后用平台的「炼化 Skill」能力把对话提炼成一个可复用的项目 Skill。

> 所有公司、指标、订单号、故障数据均为**模拟数据**，仅用于演练；真实业务以项目最新知识库为准。

## 一、五个公共项目

线上实例：<http://8.217.209.59:8000>（账号 `123`）。五个项目均已在实例中补全 `knowledge/domain/context.md` 与新增的 `knowledge/domain/project-brief.md`。

| # | 项目 | 分类 | 一句话背景 | 本地前提信息包 |
| --- | --- | --- | --- | --- |
| 1 | 电商商城系统 | 软件项目 | 云栖优选 B2C 商城，年 GMV 3.2 亿；大促要解决超卖、重复支付回调、订单对账 | [01-ecommerce.md](projects/01-ecommerce.md) |
| 2 | CRM 客户管理系统 | 软件项目 | 云帆科技 120 人销售团队；线索→客户→商机→合同，重点是去重、跟进时效、权限审计 | [02-crm.md](projects/02-crm.md) |
| 3 | 数据分析看板 | 数据分析 | 32 家门店 + 线上商城的统一经营看板；GMV/订单量/客单价/转化率/复购率，T+1 | [03-data-dashboard.md](projects/03-data-dashboard.md) |
| 4 | 智能客服助手 | 智能客服 | 日均 6.5 万会话；只基于知识库与订单接口回答，金额/时效必须有依据，不确定就转人工 | [04-customer-service.md](projects/04-customer-service.md) |
| 5 | 企业官网改版 | 软件项目 | 云帆科技官网从展示页升级为获客入口；LCP 4.2s → <2s，表单转化 1.9% → 3.2% | [05-corporate-site.md](projects/05-corporate-site.md) |

每个信息包都包含：项目背景、业务目标与成功标准、范围与边界、用户与角色、核心流程/状态机、数据与容量基线、核心表与接口、技术栈、非功能指标、已知风险、验收关注点、给智能体的前提假设。

### 线上写入清单

| 项目 | project_id | 写入内容 |
| --- | --- | --- |
| 电商商城系统 | `404a63fd-22ac-4183-9656-8311e69337bc` | `knowledge/domain/context.md`、`knowledge/domain/project-brief.md`、`knowledge/technical/technical-stack.md`、`knowledge/technical/api-map.md`、`skills/domain/ecommerce-oversell-incident-response.md`、`docs/conversation-2026-11-11-oversell.md` |
| CRM 客户管理系统 | `43905d5f-dfc8-41d3-aecd-9916b4d75301` | `knowledge/domain/context.md`、`knowledge/domain/project-brief.md` |
| 数据分析看板 | `963d871f-a67b-4a75-a9db-f94bbe037f84` | `knowledge/domain/context.md`、`knowledge/domain/project-brief.md` |
| 智能客服助手 | `ea5c047d-72a9-4cc4-8a72-676fc39560ca` | `knowledge/domain/context.md`、`knowledge/domain/project-brief.md` |
| 企业官网改版 | `6197e45b-94c6-4140-88ec-ffb355d7cb3d` | `knowledge/domain/context.md`、`knowledge/domain/project-brief.md` |

## 二、选中的项目与对话

选择 **电商商城系统**，因为它同时具备「业务数据可量化」「技术根因典型」「修复方案可复用」三个条件，最适合炼化成技能。

- 场景：2026-11-11 双 11 零点，3 个 SKU 超卖 37 件、12 条支付回调重复、2 单重复发货。
- 参与人：陈默（后端负责人）× Tech Lead 智能体。
- 规模：17 条消息、12 次工具调用（`sql_query` 7 次、`knowledge_rw` 2 次、`shell` 2 次、`filesystem` 1 次）。
- 对话记录：[conversations/ecommerce-oversell-incident.md](conversations/ecommerce-oversell-incident.md)

对话不是「一问一答的客服话术」，而是真实排障节奏：先同步现象 → 要求取证 → 质疑「update 是原子的为什么还会负」→ 给出热点约束 → 挑战唯一键冲突与缓存击穿 → 追问补偿与 MQ 幂等 → 要验证方案 → 要灰度回滚 → 要复盘与 Skill 沉淀。

## 三、炼化 Skill

### 1. 原始草稿（平台算法输出）

平台的「炼化 Skill」是确定性生成，不依赖外部模型。它从会话标题抽触发词、从用户消息抽典型需求、从工具调用抽工具表与先后顺序、从失败调用抽已知问题。用同一套逻辑对本次对话生成的草稿：

- [skill/01-distill-draft.md](skill/01-distill-draft.md)

草稿能给出「用了哪些工具、先后顺序、失败过什么、产出长什么样」，但它只会输出通用句式（例如「先执行 sql_query，再执行 filesystem」），不能直接当 skill 用——这正是平台提示「请按实际情况删改后再投入使用」的原因。

逐段对照（哪一句对话进了哪个章节、草稿删了什么、补了哪些数字）见 [skill/03-distill-trace.md](skill/03-distill-trace.md)。

### 2. 精炼后的 Skill

在草稿基础上，把对话里的领域知识固化成步骤、判定表、SQL 模板、阈值与检查清单：

- [skill/02-SKILL.md](skill/02-SKILL.md)

| 项 | 值 |
| --- | --- |
| slug | `domain/ecommerce-oversell-incident-response` |
| type | `guidance` |
| 触发词 | 库存超卖、库存为负、支付回调重复、大促对账、订单状态不一致、秒杀库存热点 |
| 工具 | `sql_query`、`filesystem`、`shell`、`knowledge_rw`、`user_confirm` |
| 结构 | 适用场景 → 核心原则 → 输入清单 → S0–S6 流程 → 复盘模板 → 常见错误 → 质量检查清单 |

与草稿相比，精炼版新增了：check-then-act 判定表、原子扣减 SQL、两个唯一键、Redis Lua、热点分段、补偿/释放、MQ 幂等四件套、四层验证、灰度回滚顺序、复盘模板。

### 3. 已写入线上项目

- `skills/domain/ecommerce-oversell-incident-response.md`（项目 Skill，Tech Lead 在本项目对话中可被触发词或 `/domain/ecommerce-oversell-incident-response` 加载）
- `docs/conversation-2026-11-11-oversell.md`（对话记录，供复盘与审计）

Skill 已用 `agent_core.skills.loader.load_skill` 校验：slug、type、双语 description、triggers、tools 均可解析。

### 写入后校验

- 五个项目的知识列表均出现 `domain/context`（标题为「项目背景：项目名」）与 `domain/project-brief`（标题为「项目前提信息：项目名」）。
- 完整度分数（`GET /api/projects/{id}/knowledge`）示例：电商 `domain/context` 61.1、`domain/project-brief` 86.4；其余项目 brief 80.6–84.5，均从 0 提升。
- 项目 Skill 与本地文件 sha256 一致：`9c4ad6081df6627f2c4731bf6b0e87e3bcfaba4da46b2d809a20b1ad86aaec8c`（12,575 bytes）。
- Skill 已用 `agent_core.skills.loader.load_skill` 校验：slug、type、双语 description、triggers、tools 均可解析。

## 四、目录结构

```
deliverables/skill-distill-demo/
├── README.md
├── projects/
│   ├── 01-ecommerce.md
│   ├── 02-crm.md
│   ├── 03-data-dashboard.md
│   ├── 04-customer-service.md
│   └── 05-corporate-site.md
├── conversations/
│   └── ecommerce-oversell-incident.md
└── skill/
    ├── 01-distill-draft.md
    ├── 02-SKILL.md
    └── 03-distill-trace.md
```

## 五、使用建议

1. 在线上实例打开「电商商城系统」→「知识」，可看到 `domain/context` 与 `domain/project-brief` 已补全，完整度分数上升。
2. 新建对话选择 Tech Lead，说「大促库存超卖怎么排查」，触发词会命中新 Skill；也可输入 `/domain/ecommerce-oversell-incident-response` 显式加载。
3. 下次大促前，按 Skill 的 S5 四层验证跑一遍：数据对账 → 并发正确性 → 故障注入 → 大促演练。
4. 如果要继续炼化其他项目：CRM 适合沉淀「线索去重与权限审计」，看板适合「指标口径对账」，客服适合「政策依据与转人工」，官网适合「性能与 SEO 验收」。

## 六、说明与边界

- 线上实例的「炼化 Skill」页面只读取数据库中的真实会话；本次对话是模拟记录，因此草稿由平台同一套 `_build_skill` 算法在本地复现生成，最终 Skill 再人工精炼。这样既保留了平台的提炼口径，也避免把模拟数据写进真实会话库。
- 五个项目的前提信息包是「补全」而非「覆盖真实业务」：所有数字都带「模拟」标记，真实使用时请用项目实际数据替换。
- 写入线上实例的内容仅限这五个公共项目的知识文件、一个项目 Skill 和一份对话记录，未改动其他项目或账号数据。
