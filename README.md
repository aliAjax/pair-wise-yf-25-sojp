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
- `POST /api/papers/{id}/review-period`：主席设定论文的评审起止日期；缩短或移动时段后，不再覆盖整个周期的进行中分配会被释放。
- `POST /api/papers/{id}/bids`：评审意向。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突。
- `POST /api/papers/{id}/assignments`：主席邀请评审人，执行利益冲突、评审时段覆盖与负载上限检查。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/busy-intervals` / `GET /api/busy-intervals` / `DELETE /api/busy-intervals/{id}`：评审人登记/查看/删除自己的忙碌（休假）区间；主席可查看全部。新增区间与已接受分配冲突时，该分配立即失效并释放名额。
- `GET /api/chair/backfill`：主席查看待补位论文、人员缺口，以及每个候选评审人被排除的原因。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `GET /api/papers/{id}/history`：审计历史。

主席工作台页面在 <http://127.0.0.1:8101/chair>。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审。未设定评审时段的论文不能发出邀请；评审人登记的忙碌区间与评审时段有任何重叠即视为无法覆盖整个周期，不能邀请；接受后新增冲突休假会让该分配失效并释放负载名额，已完成的评审与已作出的决定不受释放影响。

## 模块划分

- `availability.py`：时段规则——日期校验、区间重叠、整周期覆盖判断（纯函数）。
- `assignments.py`：分配事务——评审时段设定、忙碌区间登记与级联释放、邀请/响应/评审提交、主席补位视图。
- `web/chair.html`：主席页面（评审时段、待补位论文与候选排除原因）。
- `app.py`：其余领域逻辑、Schema 与 HTTP 路由；`errors.py` 为共享异常类型。
