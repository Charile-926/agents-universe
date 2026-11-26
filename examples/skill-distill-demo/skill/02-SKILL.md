---
slug: "domain/ecommerce-oversell-incident-response"
description:
  zh-CN: "大促/秒杀期间库存超卖、支付回调重复、订单状态不一致的排查与修复方法：先取证再止血，用唯一键与原子扣减兜底，分段降热点，四层验证后再放量。"
  en-US: "Diagnose and fix oversell, duplicate payment callbacks, and order-state drift during flash sales: collect evidence before mitigating, enforce idempotency keys and atomic stock updates, shard hot SKUs, and verify in four layers before ramping traffic."
type: "guidance"
triggers:
  - "库存超卖"
  - "库存为负"
  - "支付回调重复"
  - "大促对账"
  - "订单状态不一致"
  - "秒杀库存热点"
tools:
  - sql_query
  - filesystem
  - shell
  - knowledge_rw
  - user_confirm
---

# Skill：电商大促库存超卖与重复支付事件响应

## 适用场景

在以下任一情况下加载本 skill：

- 大促、秒杀、预售尾款期间出现库存为负、超卖、售罄后仍能下单。
- 支付回调重复、同一订单重复发货/重复退款、支付单与订单状态不一致。
- MQ 重投后同一订单被重复扣库存，或预扣流水出现重复。
- 需要在大促前做库存一致性演练、对账或容量评估。

**不适用**：普通商品改价、优惠券规则、物流轨迹查询等与库存/支付幂等无关的问题。

## 核心原则

1. **先取证，再止血。** 重启服务不是止血手段，会扩大 MQ 积压、丢失现场；先查数据、日志、代码路径。
2. **DB 是库存唯一事实源。** Redis 只做快速失败和削峰，任何 Redis 扣减成功都不代表库存真正扣减。
3. **检查与扣减必须原子。** `select` 后 `update` 是 check-then-act 竞态；用 `where stock >= qty` 或行锁/版本号把判断和扣减合并。
4. **所有写接口必须有幂等键。** 支付回调、MQ 消费、预扣流水、订单状态推进，四处都要幂等。
5. **热点必须分段。** 单行库存扛不住 8k QPS；分段 + 汇总校验，而不是给热点行加锁。
6. **先加保护，再改逻辑。** 唯一索引对旧代码向后兼容，先上；分段/双写用开关控制，随时可回滚。

## 输入清单（开始前必须拿到）

| 信息 | 用途 | 拿不到时 |
| --- | --- | --- |
| 故障时间线与已做的操作 | 区分「故障本身」与「止血引入的新问题」 | 先向值班同学要，不要猜 |
| 负库存 SKU 与数量 | 评估影响面 | 用 `inventory where stock < 0` 查 |
| 重复回调/重复预扣的订单号 | 判断幂等缺口 | 用分组查询找 |
| 库存扣减代码路径 | 定位 check-then-act | 读 `deduct`/`reserve` 相关文件 |
| 当前 QPS、峰值、热点 SKU | 决定是否需要分段 | 查网关/监控 |
| 可用变更窗口与回滚方式 | 保证方案可落地 | 直接问负责人 |

## 标准流程

### S0 止血（5 分钟内，禁止重启）

1. 关闭问题 SKU 的下单入口或置为售罄，避免继续扩大。
2. 暂停支付成功事件的 MQ 消费（不是停 order-service），保留现场。
3. 记录已发生的重复回调/重复发货订单，标记人工冲正。
4. **不要**在故障中直接改代码、重启服务、清 Redis。

### S1 固化时间线

按「告警 → 发现 → 止血 → 排查 → 修复 → 恢复」记录每个动作的时间、操作人、影响。特别标注哪些动作是止血、哪些可能引入新问题（例如重启导致 MQ 积压翻倍）。

### S2 三组 SQL 取证

```sql
-- 1) 负库存分布（确认影响面）
SELECT sku_id, stock, version, updated_at
FROM inventory
WHERE stock < 0
ORDER BY stock ASC;

-- 2) 预扣流水重复（同一订单同一 SKU 只能有一条有效预扣）
SELECT order_id, sku_id, COUNT(*) AS c,
       MIN(created_at) AS first_at, MAX(created_at) AS last_at
FROM stock_reserve
WHERE created_at >= :incident_start
GROUP BY order_id, sku_id
HAVING COUNT(*) > 1;

-- 3) 支付回调重复（同一渠道流水号只能落一次）
SELECT channel, channel_txn_id, COUNT(*) AS c,
       COUNT(DISTINCT order_id) AS orders
FROM payment_callbacks
WHERE created_at >= :incident_start
GROUP BY channel, channel_txn_id
HAVING COUNT(*) > 1;
```

同时检查索引与版本字段：

```sql
SHOW INDEX FROM stock_reserve;         -- 是否有 (order_id, sku_id) 唯一键
SHOW INDEX FROM payment_callbacks;     -- 是否有 (channel, channel_txn_id) 唯一键
SELECT COUNT(*) FROM inventory WHERE version = 0;  -- 版本号是否从未使用
```

### S3 根因判定表

| 现象 | 证据 | 结论 |
| --- | --- | --- |
| 库存为负，且 `version` 全为 0 | 扣减代码是 `select` 后 `update` | DB check-then-act 竞态 |
| Redis 与 DB 库存长期不一致 | 失败后 `incrby` 补偿，无幂等/重试 | 双写补偿不可靠 |
| `stock_reserve` 同订单同 SKU 多条 | 无唯一键，MQ 重投重复执行 | 预扣不幂等 |
| `payment_callbacks` 同流水号多条 | 无唯一键，消费者并发处理 | 回调不幂等 |
| 售罄后仍下单成功 | Redis 判断与扣减是两次调用，可插队 | Redis 预扣非原子 |
| 单 SKU 扣减超时、行锁等待高 | 热点行集中，QPS 远超单行能力 | 需要分段 |

### S4 修复（按顺序上线，每步可独立回滚）

#### 4.1 DB 原子扣减（必须最先上）

```sql
UPDATE inventory
SET stock = stock - :qty,
    version = version + 1,
    updated_at = NOW()
WHERE sku_id = :sku_id
  AND stock >= :qty;
-- affected_rows = 0 → 库存不足，返回 STOCK_NOT_ENOUGH
```

#### 4.2 唯一键（与 4.1 同批，向后兼容）

```sql
ALTER TABLE stock_reserve
  ADD UNIQUE KEY uk_order_sku (order_id, sku_id);

ALTER TABLE payment_callbacks
  ADD UNIQUE KEY uk_channel_txn (channel, channel_txn_id);
```

大表加索引优先走 online DDL；先确认无重复数据，否则先清理再建索引。

#### 4.3 幂等写入（替换「先 insert 再 update」）

```sql
-- 预扣：存在且未释放直接返回成功，不重复扣
INSERT INTO stock_reserve (reserve_id, order_id, sku_id, qty, status)
VALUES (:reserve_id, :order_id, :sku_id, :qty, 'RESERVED')
ON DUPLICATE KEY UPDATE reserve_id = reserve_id;
-- affected_rows = 0 → 已存在，按幂等成功处理

-- 支付回调：同一渠道流水号只落一次
INSERT INTO payment_callbacks (callback_id, payment_id, channel, channel_txn_id, raw_body)
VALUES (:callback_id, :payment_id, :channel, :channel_txn_id, :raw_body)
ON DUPLICATE KEY UPDATE callback_id = callback_id;
-- affected_rows = 0 → 已处理，直接 ACK
```

#### 4.4 Redis Lua 原子预扣（快速失败，不当事実源）

```lua
-- KEYS[1] = stock:{sku}:{segment}
-- ARGV[1] = qty
local stock = tonumber(redis.call('GET', KEYS[1]) or '-1')
if stock < 0 then return -2 end          -- 未预热，回源
if stock < tonumber(ARGV[1]) then return -1 end
redis.call('DECRBY', KEYS[1], ARGV[1])
return stock - tonumber(ARGV[1])
```

- 返回 `-2`：回源 DB 预热，再重试一次。
- 返回 `-1`：换下一个分段；全部分段不足才返回售罄。
- 返回 `>= 0`：继续走 DB 原子扣减兜底。

#### 4.5 热点分段（本周内）

```sql
-- 单 SKU 拆 10 段，每段独立行
UPDATE inventory_segment
SET stock = stock - :qty
WHERE sku_id = :sku_id
  AND segment_no = :segment_no
  AND stock >= :qty;
-- affected_rows = 0 → 换下一段
```

- 选段：`hash(order_id) % segment_count`，失败顺序试下一段。
- 汇总库存：`SELECT SUM(stock) FROM inventory_segment WHERE sku_id = ?`。
- 双写期：`inventory` 与 `inventory_segment` 同时更新，每 5 分钟对账一次。

#### 4.6 补偿与释放

- Redis 扣减成功、DB 扣减失败：写 `stock_compensate` 任务，不要立刻 `incrby`。
- 回补前查 `stock_reserve.status`，只有 `RESERVED` 且 DB 未实扣才回补，按 `reserve_id` 幂等。
- 订单取消/超时：`status = RELEASED` 后再 `stock = stock + qty`，同样幂等。
- DB 扣减成功、订单创建失败：走取消释放，不要重复回补。

#### 4.7 MQ / 事件幂等

1. `consumed_events(event_id)` 唯一键，消费前查/插入，已存在直接 ACK。
2. `stock_reserve(order_id, sku_id)` 唯一键。
3. 订单状态机乐观锁：`UPDATE orders SET status='PAID', version=version+1 WHERE order_id=? AND status='PENDING_PAYMENT'`。
4. 支付事件 `event_id = channel + channel_txn_id`，全链路只认这一个键。

### S5 验证四层

#### L1 数据对账（5 分钟）

```sql
-- 负库存必须为 0
SELECT COUNT(*) FROM inventory WHERE stock < 0;

-- 有效预扣只能 1 条
SELECT order_id, sku_id, COUNT(*) c
FROM stock_reserve
WHERE status IN ('RESERVED','CONFIRMED')
GROUP BY order_id, sku_id HAVING COUNT(*) > 1;

-- 回调只能 1 条
SELECT channel, channel_txn_id, COUNT(*) c
FROM payment_callbacks
GROUP BY channel, channel_txn_id HAVING COUNT(*) > 1;
```

Redis 与 DB 汇总比对：按 SKU 输出 `|redis_sum - db_stock| > 0` 的明细，允许在途预扣差异但必须可解释。

#### L2 并发正确性（20 分钟）

- 库存 100，发 10,000 个并发下单，每个买 1 件：成功恰好 100，失败 9,900，`stock = 0`，无负数。
- 同一 `channel_txn_id` 投 10 次：`payment_callbacks` 1 条、`payments` 1 条、库存实扣 1 次。
- 同一订单 MQ 重投 3 次：订单只推进一次。

#### L3 故障注入（1 小时）

| 故障 | 期望 |
| --- | --- |
| Redis 挂掉 | 回源 DB，P99 允许升到 1.5s，但不超卖 |
| DB 主从切换 | 扣减失败返回明确错误码，不静默成功 |
| MQ 重投 3 次 | 订单只推进一次 |
| 分段全部不足 | 返回售罄，不产生负库存 |

#### L4 大促演练（放量前）

- 8,000 QPS 持续 10 分钟：P99 < 500ms，错误率 < 0.1%，无负库存。
- 关键指标：`inventory_update_conflict_total`、`stock_reserve_duplicate_total`、`payment_callback_duplicate_total`、`stock_compensate_backlog`。

### S6 灰度、回滚与数据修复

1. 先上 4.1 + 4.2（向后兼容），观察 30 分钟。
2. 打开 `inventory.segment.enabled`，按「问题 SKU → 秒杀 SKU → 1% → 10% → 100%」灰度，每档至少 30 分钟。
3. 双写期每 5 分钟比对 `SUM(segment)` 与 `inventory.stock`，差异 > 0 立即告警。
4. 回滚：只关分段开关，唯一索引与 DB 原子扣减保留，安全性不倒退。
5. 数据修复：超卖订单按「已支付优先发货、未支付取消 + 补偿券」处理；重复回调订单人工冲正；重复发货联系仓库拦截。

## 复盘模板

```markdown
## 影响
- 超卖 SKU/数量/订单数：
- 重复回调/重复发货/重复退款：
- 直接成本（赔付/券/人工）：

## 时间线
- 告警 → 发现 → 止血 → 排查 → 修复 → 恢复（含操作人与动作）

## 根因
- 主因：
- 放大因素：
- 为什么现有测试/监控没拦住：

## 做得好的 / 要改进的
- 做得好：
- 要改进：

## Action Items
| # | 事项 | 负责人 | 截止 | 验收 |
```

## 常见错误（不要做）

1. 故障中重启服务、清 Redis、直接改生产代码。
2. 只加唯一索引，不改 check-then-act，库存仍会负。
3. 用 `SELECT ... FOR UPDATE` 锁热点行，8k QPS 下会把库存服务打挂。
4. Redis 扣减成功后不回源 DB，把缓存当事实源。
5. 失败后无脑 `INCRBY` 回补，导致 Redis 与 DB 越补越偏。
6. 用「捕获唯一键异常」做正常流程，正常请求被误判为失败。
7. 分段后忘记汇总校验，`inventory` 与 `inventory_segment` 长期漂移。
8. 只做压测不做重复回调/故障注入，幂等缺口上线才发现。

## 质量检查清单

- [ ] 负库存查询结果为 0，且连续 3 次采样都稳定。
- [ ] `stock_reserve` 与 `payment_callbacks` 的唯一键已生效（`SHOW INDEX` 验证）。
- [ ] 同一回调投 10 次只产生 1 条支付记录、1 次库存实扣。
- [ ] 10,000 并发对 100 库存，成功数恰好 100。
- [ ] Redis 与 DB 汇总差异可解释，且在阈值内。
- [ ] 分段开关可一键回滚，回滚后安全性不倒退。
- [ ] 复盘文档包含时间线、根因、影响、Action Items。
- [ ] 结论写回项目知识库（`domain/context` 或 `technical/system-architecture`）。

## 关联知识

- [[domain/context]] — 项目背景、业务目标、角色与范围
- [[technical/technical-stack]] — 服务、数据库、缓存、MQ 版本
- [[technical/api-map]] — 下单、支付回调、订单查询接口
- [[technical/system-architecture]] — 库存/支付/订单的边界与数据归属
