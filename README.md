# 学术会议同行评审系统

一个仅使用 Python 3.11+ 标准库的独立示例项目。SQLite 保存数据，`http.server` 提供 JSON API 和演示页面。

## 运行

```bash
python app.py --init --seed
python app.py
```

访问 <http://127.0.0.1:8101>。默认数据库为 `review.db`，端口为 `8101`。测试：

```bash
python -m unittest -v
```

## 角色和主要接口

演示用户：`alice`、`bob`（作者），`r1`、`r2`、`r3`（评审人），`chair`（主席）。所有 API 请求应带 `X-User-Id` 请求头。

- `POST /api/papers`：提交论文。
- `GET /api/papers` / `GET /api/papers/{id}`：按角色隔离查看；评审人看到双盲视图。
- `POST /api/papers/{id}/bids`：评审意向。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突。
- `POST /api/papers/{id}/review-window`：主席为论文设定评审起止时间（ISO 8601，开始早于结束）。
- `GET /api/papers/{id}/chair-detail`：主席查看分配状态、可邀请候选及每个被排除候选的原因。
- `GET /api/chair/attention`：待补位论文列表（有分配被休假失效，或覆盖评审不足两人）。
- `POST /api/reviewers/{id}/busy-periods` / `GET ...`：评审人登记/查看忙碌区间（写入以登录身份为准）。
- `POST /api/papers/{id}/assignments`：主席邀请评审人；须先设定评审起止，且评审人全程有空、未满负载、无冲突。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请；接受时再次校验评审周期内无忙碌冲突。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `GET /api/papers/{id}/history`：审计历史。
- 主席页面：<http://127.0.0.1:8101/chair>。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审。

时段化分配不变量：主席必须先为论文设定评审起止（`review_start < review_end`）才能邀请；只有忙碌区间不与整个评审周期重叠（半开区间，端点相接不算冲突）且未达负载上限的评审人才能被邀请；评审人在接受邀请前再次登记冲突休假将无法接受；接受或完成评审后新增的冲突休假会把该分配置为 `invalidated` 并释放负载名额（已决定论文不受影响），失效不删除评分与意见，论文进入待补位列表供主席重新邀请；被失效记录在冲突消除（如窗口调整）后可被重新邀请复活。

## 模块划分

- `scheduling.py`：时段规则（时间解析、窗口校验、区间重叠查询），纯规则、可独立测试。
- `assignments.py`：分配事务（邀请、响应、休假冲突失效、候选排除原因），函数均在调用方事务内执行。
- `web/chair.html`：主席工作台页面，仅消费 JSON 接口，与规则、事务分开维护。
- `app.py`：存储装配、迁移与 HTTP 路由；`errors.py` 为共享业务异常。
