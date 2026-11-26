---
slug: "distilled-30108b-20261126-121944"
description:
  zh-CN: "从 1 个会话、17 条消息、12 次工具调用中提炼出的工作方法；用于复现同类任务的处理步骤与检查要点"
type: "guidance"
triggers:
  - "双11大促库存超卖与支付回调重复排查"
tools:
  - sql_query
  - knowledge_rw
  - shell
  - filesystem
---

# Skill：从历史会话提炼的工作方法

> 本文件由“聊天记录炼化”根据当前账号自己的会话与工具调用记录生成，仅包含该账号可见的内容。请按实际情况删改后再投入使用。

## 适用场景

- 双11大促库存超卖与支付回调重复排查

## 典型需求

- 零点大促出事了：3个SKU库存为负合计-37件，12条支付回调重复涉及6个订单，其中2个已重复发货。00:18重启了order-service，00:26暂停了MQ消费，00:32手工把库存改成0并下架。现在Redis里A-1001是0，MySQL里是-21，两边对不上。帮我定位根因、给今晚能上能回滚的修复方案，再给一…
- 代码在 inventory-service/internal/stock/deduct.go，核心就是 Redis 先扣、DB 后扣。update stock = stock - ? 本身是原子的，为什么还会负？
- 都没有唯一索引，inventory.version 一直是 0。今晚 10 点还有预售尾款，不能停服，最多在线 DDL + 灰度；8000 QPS 的秒杀单品是热点，直接上行锁会把库存服务打挂。别只给加唯一索引这种正确但没用的话。
- 分段方案我认可。但分段后 Redis key 变成 stock:{sku}:{seg}，缓存击穿怎么办？唯一索引加上去，旧代码先 insert 后 update，遇到重复键会不会把正常请求打死？
- 补偿逻辑呢？旧代码 DB 失败会 incrby 回补 Redis，现在 Redis 只是快速失败，回补还有必要吗？MQ 重投导致的重复扣减，怎么保证不重复？
- 验证这块给我具体点，我怎么证明不再超卖？不要只说压测。
- 灰度怎么切？分段方案出问题怎么回滚？
- 行，方案我认。最后帮我出一份复盘要点和 action items。另外这次排查挺典型，我想把它炼化成一个 skill，下次大促前直接复用。
- 可以，就按这个来。你把 skill 草稿写出来，我审一遍再入库。

## 标准流程

1. 先执行 `sql_query`，再执行 `filesystem`（历史出现 1 次）。
2. 先执行 `sql_query`，再执行 `knowledge_rw`（历史出现 1 次）。
3. 先执行 `shell`，再执行 `sql_query`（历史出现 1 次）。
4. 收尾时复核产物是否齐全，并把结论写回项目知识库。

## 工具使用规范

| 工具 | 调用次数 | 常见参数 |
| --- | --- | --- |
| `sql_query` | 7 | datasource, sql |
| `knowledge_rw` | 2 | operation, slug, summary |
| `shell` | 2 | command |
| `filesystem` | 1 | operation, path |

## 已知失败与处理

- `knowledge_rw`：历史失败 1 次，典型原因：knowledge file not found: technical/technical-stack
- `sql_query`：历史失败 1 次，典型原因：Table 'order_db.inventory_segment' doesn't exist

## 产出参考

- 先按时间线、数据证据、代码路径三步走。我先查负库存分布、预扣流水重复、支付回调重复，并读库存扣减代码。
- 原子性只保证减不丢，不保证减之前库存够。两个请求同时读到 stock=10，都判断 10>=10，都执行减 10，结果就是 -10。这里有 DB check-then-act、Redis 两次调用可插队、MQ 重投无幂等三层问题。先确认 stock_reserve 和 payment_callbacks 有没有唯一索引…
- 给一套 DB 兜底 + 分段降冲突 + 幂等表的组合：第一步 DB 原子扣减 update ... where stock >= qty，同时加 stock_reserve(order_id, sku_id) 和 payment_callbacks(channel, channel_txn_id) 两个唯一键；第二步…
- 缓存击穿用懒加载 + singleflight：每个分段只允许一个请求回源 DB，其余等待 50ms 读缓存；大促前 30 分钟全量预热，TTL 2 小时加 10 分钟随机抖动。唯一键冲突要改成幂等写入：先按 order_id+sku_id 查，存在且 RESERVED/CONFIRMED 直接返回成功；insert…
- 补偿不能再失败就 incrby。改为写 stock_compensate 任务，按 reserve_id 幂等回补，回补前查 stock_reserve 状态，只有 RESERVED 且 DB 未实扣才补。MQ 幂等四件套：consumed_events 按 event_id 去重、stock_reserve 唯一键、…

## 质量检查清单

- [ ] 输入数据来源已确认，未混入其他账号或外部系统的数据。
- [ ] 每一步工具调用的结果都已核对，失败步骤有明确处理方式。
- [ ] 输出中不含密钥、本机绝对路径、内部接口地址等敏感信息。
- [ ] 结论与数字可追溯到具体记录，没有凭印象补充。
