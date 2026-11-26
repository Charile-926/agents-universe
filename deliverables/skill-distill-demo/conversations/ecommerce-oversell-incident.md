# 模拟对话记录：电商商城系统 · 双 11 超卖与重复支付回调排查

> 演练说明：以下为模拟的真实技术人员与 Tech Lead 智能体的多轮对话，用于后续「炼化 Skill」。对话中的时间、订单号、指标、SQL 结果均为演练数据；命令与 SQL 仅用于演示排查方法。

## 会话元数据

| 项 | 值 |
| --- | --- |
| 项目 | 电商商城系统（云栖优选） |
| 智能体 | Tech Lead |
| 参与人 | 陈默，后端负责人（8 年经验，负责订单/库存/支付） |
| 时间 | 2026-11-11 00:40 – 01:38（UTC+8） |
| 场景 | 双 11 零点大促，出现库存超卖与支付回调重复 |
| 环境 | prd（生产），只读账号 + 日志平台 |
| 目标 | 定位根因、给出可回滚修复、补齐验证与复盘 |

---

## 第 1 轮 · 陈默

00:40

零点大促出事了，先同步现象：

1. 00:03:05 库存告警：3 个 SKU 库存为负，合计 -37 件（A-1001 -21、A-1002 -11、A-1003 -5）。
2. 00:05:12 支付告警：`payment_callbacks` 出现重复回调，12 条回调涉及 6 个订单，其中 2 个订单已经重复发货。
3. 00:18 我们重启了 order-service，00:26 暂停了支付成功事件的 MQ 消费，00:32 把 3 个 SKU 的库存手工改成 0 并下架。
4. 现在 Redis 里 A-1001 是 0，MySQL 里是 -21，两边对不上。

我需要你帮我做三件事：定位根因、给一个今晚就能上、能回滚的修复方案、再给我一套验证「不再超卖」的办法。数据库只读账号是 `ro_ops`，密码在项目密钥里，你可以直接查。

## 第 2 轮 · Tech Lead

00:42

先不急着改代码，按「时间线 → 数据证据 → 代码路径」三步走。我先查三组数据：负库存分布、预扣流水重复、支付回调重复。

```json
{"tool":"sql_query","input":{"datasource":"order_db_ro","sql":"select sku_id, stock, version, updated_at from inventory where stock < 0 order by stock asc"},"status":"success","output":{"rows":[{"sku_id":"A-1001","stock":-21,"version":0,"updated_at":"2026-11-11T00:03:04+08:00"},{"sku_id":"A-1002","stock":-11,"version":0,"updated_at":"2026-11-11T00:03:05+08:00"},{"sku_id":"A-1003","stock":-5,"version":0,"updated_at":"2026-11-11T00:03:05+08:00"}]}}
```

`version` 全是 0，说明库存更新没有走版本号，先记下。

```json
{"tool":"sql_query","input":{"datasource":"order_db_ro","sql":"select order_id, sku_id, count(*) c, min(created_at) first_at, max(created_at) last_at from stock_reserve where sku_id in ('A-1001','A-1002','A-1003') group by order_id, sku_id having count(*) > 1"},"status":"success","output":{"rows":[{"order_id":"SO202611110001","sku_id":"A-1001","c":2,"first_at":"2026-11-11T00:02:58+08:00","last_at":"2026-11-11T00:03:01+08:00"}]}}
```

```json
{"tool":"sql_query","input":{"datasource":"order_db_ro","sql":"select channel, channel_txn_id, count(*) c, count(distinct order_id) orders from payment_callbacks where created_at >= '2026-11-11 00:00:00' group by channel, channel_txn_id having count(*) > 1"},"status":"success","output":{"rows":[{"channel":"wxpay","channel_txn_id":"4200002...881","c":2,"orders":1},{"channel":"wxpay","channel_txn_id":"4200002...902","c":2,"orders":1}]}}
```

预扣流水有重复，支付回调也有重复。下一步我看库存服务的代码路径，确认是「先 Redis 后 DB」还是「先 DB 后 Redis」。

## 第 3 轮 · 陈默

00:44

代码在 `inventory-service/internal/stock/deduct.go`，核心逻辑就是我之前贴过的那段：

```go
func Deduct(ctx context.Context, skuID string, qty int) error {
    stock, _ := redis.Get(ctx, "stock:"+skuID).Int()
    if stock < qty {
        return ErrStockNotEnough
    }
    redis.DecrBy(ctx, "stock:"+skuID, int64(qty))
    row := db.QueryRow("select stock from inventory where sku_id = ?", skuID)
    if row.Stock < qty {
        redis.IncrBy(ctx, "stock:"+skuID, int64(qty))
        return ErrStockNotEnough
    }
    db.Exec("update inventory set stock = stock - ? where sku_id = ?", qty, skuID)
    return nil
}
```

你是想说 DB 的 select 和 update 之间有问题？但 `update stock = stock - ?` 本身是原子的，为什么还会负？

## 第 4 轮 · Tech Lead

00:46

`update stock = stock - ?` 是原子的，但你的「检查」和「扣减」不是同一个原子操作。两个请求同时读到 `stock=10`，都判断 `10 >= 10`，都执行 `update stock = stock - 10`，结果就是 -10。原子性只保证「减」不丢，不保证「减之前库存够」。

你这里实际有三层问题：

1. **DB 层**：`select` 与 `update` 之间没有 `for update`、没有版本号、没有 `where stock >= qty` 条件，属于典型的 check-then-act 竞态。
2. **Redis 层**：`Get` 与 `DecrBy` 是两次调用，中间可插队；`DecrBy` 本身允许减成负数；DB 失败后的 `IncrBy` 补偿如果失败或重复执行，Redis 与 DB 就永久漂移。
3. **消息层**：`stock_reserve` 没有唯一约束，MQ 重投时同一订单同一 SKU 会再扣一次；支付回调也没有 `(channel, channel_txn_id)` 唯一键，重复回调会重复推进订单。

先确认一下：`stock_reserve` 和 `payment_callbacks` 现在有没有唯一索引？

## 第 5 轮 · 陈默

00:48

我查了，都没有。`stock_reserve` 只有普通索引 `idx_order`，`payment_callbacks` 只有 `idx_payment`。另外 `inventory.version` 字段是有的，但代码从来没写过，一直是 0。

现在有两个现实约束：

- 今晚 10 点还有一波预售尾款，不能停服，最多做在线 DDL + 灰度。
- 8,000 QPS 的秒杀单品是热点，如果直接上 `select ... for update`，行锁会把库存服务打挂。

你给我方案的时候，别只给「加唯一索引」这种正确但没用的话，我要能落地的。

## 第 6 轮 · Tech Lead

00:50

同意，热点行锁不可行。给你一套「DB 兜底 + 分段降冲突 + 幂等表」的组合，分三步上，任何一步都能单独回滚。

### 第一步（今晚必须上，10 分钟）：DB 原子扣减 + 唯一索引

```sql
-- 1) 原子扣减：把「检查」和「扣减」合并成一条语句
UPDATE inventory
SET stock = stock - :qty,
    version = version + 1,
    updated_at = now()
WHERE sku_id = :sku_id
  AND stock >= :qty;
-- affected_rows = 0 表示库存不足，直接返回 STOCK_NOT_ENOUGH
```

```sql
-- 2) 预扣流水幂等：同一订单同一 SKU 只能有一条有效预扣
ALTER TABLE stock_reserve
  ADD UNIQUE KEY uk_order_sku (order_id, sku_id);

-- 3) 支付回调幂等：同一渠道流水号只能落一次
ALTER TABLE payment_callbacks
  ADD UNIQUE KEY uk_channel_txn (channel, channel_txn_id);
```

MySQL 8 的 `ADD UNIQUE KEY` 默认 online DDL，`stock_reserve` 当前 640 万行，预计 2–3 分钟；`payment_callbacks` 120 万行，1 分钟内。先加索引再加代码，索引对旧代码无副作用。

### 第二步（今晚灰度，30 分钟）：Redis Lua 原子预扣

```lua
-- KEYS[1] = stock:{sku}:{segment}
-- ARGV[1] = qty
local stock = tonumber(redis.call('GET', KEYS[1]) or '-1')
if stock < 0 then return -2 end          -- 缓存未预热
if stock < tonumber(ARGV[1]) then return -1 end
redis.call('DECRBY', KEYS[1], ARGV[1])
return stock - tonumber(ARGV[1])
```

Redis 只做「快速失败」，DB 仍是唯一事实源。Lua 返回 -2 时回源 DB 并预热；返回 -1 时换下一个分段；返回 >= 0 才继续。这样即使 Redis 漂移，DB 的 `where stock >= qty` 也能兜住超卖。

### 第三步（本周）：热点分段

单 SKU 拆 10 个分段，`inventory_segment(sku_id, segment_no, stock)`，每个分段独立行：

```sql
UPDATE inventory_segment
SET stock = stock - :qty
WHERE sku_id = :sku_id AND segment_no = :seg AND stock >= :qty;
```

请求按 `hash(order_id) % 10` 选段，失败就顺序试下一段；全部分段都不够才返回售罄。8,000 QPS 均摊到 10 行，单行 800 QPS，行锁等待从「秒级」降到「毫秒级」。汇总库存 = `sum(segment stock)`，对账以汇总为准。

## 第 7 轮 · 陈默

00:54

分段方案我认可，但有两个细节要确认：

1. 分段后 Redis key 也变成 `stock:{sku}:{seg}`，预热和回源逻辑要改，缓存击穿怎么办？
2. 唯一索引加上去之后，旧代码还是「先 insert 再 update」，遇到重复键会抛异常，会不会把正常请求也打死？

## 第 8 轮 · Tech Lead

00:56

两个都是落地时最容易踩的坑，分别处理。

**缓存击穿**：预热用「懒加载 + 单飞」——每个分段一个 `singleflight.Group`，未命中时只允许一个请求回源 DB，其余等待 50ms 后读缓存；预热失败返回「排队中」，不让请求穿透到 DB。大促前 30 分钟用定时任务全量预热，key 过期时间设 2 小时 + 随机 10 分钟，避免同一时刻集体失效。

**唯一索引冲突**：旧代码的 `insert` 要改成幂等写入，不要靠捕获异常做流程控制：

```sql
INSERT INTO stock_reserve (reserve_id, order_id, sku_id, qty, status)
VALUES (?, ?, ?, ?, 'RESERVED')
ON DUPLICATE KEY UPDATE
  status = IF(status = 'RESERVED', status, status);
```

更稳的是先 `select ... where order_id=? and sku_id=?`，存在且 `status in ('RESERVED','CONFIRMED')` 就直接返回成功（幂等），不存在再 insert；insert 仍可能并发冲突，捕获 `DuplicateKey` 后重新 select 一次，确认是同一订单就返回成功，不是同一订单才报错。支付回调同理：`insert ... on duplicate key update callback_id=callback_id`，影响行数为 0 说明已处理，直接 ACK。

## 第 9 轮 · 陈默

01:00

好。那补偿逻辑呢？旧代码 DB 失败会 `incrby` 回补 Redis，但现在 Redis 只是快速失败，回补还有必要吗？另外 MQ 重投导致的重复扣减，怎么保证不重复？

## 第 10 轮 · Tech Lead

01:02

补偿逻辑要改，不能再「失败就 incrby」：

- Redis 扣减成功、DB 扣减失败：不要立刻回补，写一条 `stock_compensate` 任务（order_id, sku_id, qty, reason），由后台任务按 `reserve_id` 幂等回补；回补前先查 `stock_reserve` 状态，只有 `RESERVED` 且 DB 未实扣才回补。
- Redis 扣减失败：本来就没扣，不需要补偿。
- DB 扣减成功、后续订单创建失败：走「取消订单」释放预扣，`stock_reserve.status = RELEASED`，再 `stock = stock + qty`，同样幂等。

MQ 重复消费的根治是「消费幂等 + 唯一键」：

1. 消费者先按 `event_id` 查 `consumed_events`，存在就 ACK。
2. `stock_reserve` 的唯一键保证同一订单同一 SKU 不会重复扣。
3. 订单状态机加乐观锁：`update orders set status='PAID', version=version+1 where order_id=? and status='PENDING_PAYMENT'`，影响行数 0 说明已处理。
4. 支付成功事件带 `event_id = channel + channel_txn_id`，全链路只认这一个幂等键。

## 第 11 轮 · 陈默

01:08

验证这块给我具体点。我怎么证明「不再超卖」？不要只说压测。

## 第 12 轮 · Tech Lead

01:10

给你四层验证，从快到慢，今晚就能跑前两层。

### L1 数据对账（5 分钟）

```sql
-- 负库存必须为 0
select count(*) from inventory where stock < 0;

-- 同一订单同一 SKU 的有效预扣只能 1 条
select order_id, sku_id, count(*) c
from stock_reserve
where status in ('RESERVED','CONFIRMED')
group by order_id, sku_id having count(*) > 1;

-- 同一渠道流水号只能 1 条
select channel, channel_txn_id, count(*) c
from payment_callbacks
group by channel, channel_txn_id having count(*) > 1;

-- Redis 汇总 vs DB 汇总（允许在途预扣差异，需业务确认阈值）
-- 由对账脚本按 sku 比对，输出 |diff| > 0 的明细
```

### L2 并发正确性（20 分钟）

- 准备 1 个 SKU，库存 100，发 10,000 个并发下单请求，每个买 1 件。
- 断言：成功订单恰好 100，失败 9,900，`stock = 0`，无负数。
- 重复支付回调：同一 `channel_txn_id` 投 10 次，断言 `payment_callbacks` 1 条、`payments` 1 条、库存实扣 1 次。

### L3 故障注入（1 小时）

- Redis 挂掉：下单应回源 DB，P99 允许升到 1.5s，但不能超卖。
- DB 主从切换：扣减失败返回明确错误码，不能静默成功。
- MQ 重投 3 次：订单只推进一次。

### L4 大促演练（明天）

- 8,000 QPS 持续 10 分钟，P99 < 500ms，错误率 < 0.1%，无负库存。
- 观察指标：`inventory_update_conflict_total`、`stock_reserve_duplicate_total`、`payment_callback_duplicate_total`。

## 第 13 轮 · 陈默

01:18

灰度怎么切？如果分段方案出问题，怎么回滚？

## 第 14 轮 · Tech Lead

01:20

按「代码兼容、开关控制、数据双写」三原则：

1. 先上 DB 原子扣减 + 唯一索引（对旧逻辑兼容），观察 30 分钟。
2. 打开 `inventory.segment.enabled`，按 SKU 白名单灰度：3 个出问题的 SKU → 10 个秒杀 SKU → 1% 流量 → 10% → 100%，每档至少 30 分钟。
3. 分段期间 `inventory` 与 `inventory_segment` 双写，汇总校验任务每 5 分钟比对 `sum(segment)` 与 `inventory.stock`，差异 > 0 立即告警。
4. 回滚：关闭 `inventory.segment.enabled` 即回到单行扣减（DB 原子扣减仍在，安全性不倒退）；唯一索引不回滚，因为它是纯保护。
5. 数据修复：37 件超卖按「已支付优先发货、未支付取消并补偿 20 元券」处理，6 个重复回调订单走人工冲正，2 个重复发货联系仓库拦截。

## 第 15 轮 · 陈默

01:26

行，方案我认。最后帮我出一份复盘要点和 action items，另外——这次排查过程挺典型的，我想把它炼化成一个 skill，下次大促前直接复用。你先给我复盘，再说 skill 怎么落。

## 第 16 轮 · Tech Lead

01:28

### 复盘要点

- **影响**：3 个 SKU 超卖 37 件，涉及订单 29 笔；重复支付回调 12 条、涉及 6 单，其中 2 单重复发货；直接赔付与优惠券成本约 2,340 元。
- **时间线**：00:03 告警 → 00:18 重启（无效，反而扩大 MQ 积压）→ 00:26 暂停消费 → 00:32 人工改库存 → 00:40 进入排查 → 01:20 方案确认。
- **根因**：check-then-act 竞态（主因）；Redis 与 DB 双写无补偿幂等；唯一键缺失导致 MQ/回调重复。
- **做得好的**：告警在 2 分钟内触发；只读账号和日志齐全；没有在故障中直接改代码。
- **要改进的**：重启服务不是止血手段；缺少库存对账的实时任务；支付回调没有幂等键；大促前没有做「重复回调」演练。

### Action Items

| # | 事项 | 负责人 | 截止 |
| --- | --- | --- | --- |
| 1 | DB 原子扣减 + 两个唯一索引上线 | 陈默 | 11-11 10:00 |
| 2 | Redis Lua + 分段扣减灰度 | 库存小组 | 11-13 |
| 3 | MQ 消费幂等表 `consumed_events` | 消息小组 | 11-14 |
| 4 | 库存实时对账任务 + 告警 | 数据小组 | 11-15 |
| 5 | 大促前故障演练（重复回调/Redis 挂/主从切换） | QA | 11-20 |
| 6 | 复盘文档与 skill 沉淀 | 陈默 | 11-18 |

### Skill 怎么落

把这次对话里的「排查路径 + SQL + 修复模板 + 验证清单」提炼成一个 `guidance` 类型的项目 skill，放在 `skills/` 下，slug 建议 `domain/ecommerce-oversell-incident-response`。触发词覆盖「超卖」「库存为负」「支付回调重复」「大促对账」。正文保留可执行的 SQL 和判定阈值，但不要写死这次的订单号和具体金额；把「先查什么、看到什么算什么、怎么修、怎么验、怎么回滚」固化成步骤。下次大促前，让值班同学直接加载这个 skill 走一遍。

## 第 17 轮 · 陈默

01:38

可以，就按这个来。你把 skill 草稿写出来，我审一遍再入库。

---

## 对话要点提取（供炼化使用）

- **触发场景**：大促/秒杀期间库存为负、支付回调重复、订单状态不一致。
- **关键工具**：`sql_query`（负库存、预扣重复、回调重复）、`filesystem`（读 `deduct.go`）、`knowledge_rw`（写回排查结论）、`shell`（压测/对账脚本）。
- **关键判定**：`inventory.version` 全 0 → 无版本控制；`stock_reserve` 重复 → 无唯一键；`payment_callbacks` 重复 → 无幂等键。
- **修复顺序**：唯一索引 → DB 原子扣减 → Redis Lua → 分段 → 对账/演练。
- **验证四层**：数据对账、并发正确性、故障注入、大促演练。
- **回滚原则**：开关回滚分段，唯一索引不回滚，数据修复单独走人工。
