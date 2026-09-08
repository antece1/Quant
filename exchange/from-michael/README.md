# exchange/from-michael

Michael 侧的唯一写入点。建立于 2026-09-08。

## 写入边界
- 只有 Michael（及其 Claude）写这里。`../from-jose/` **只读**：不改、不删、不重排。
- 收到的对方材料一律落在 `../../external/<yyyy-mm-dd>-<名字>/`，
  **不进 sys.path，不建 `__init__.py`**，不合进任何 `src/`。
- `../THREADS.md` 两边都可追加，**只追加不改写**。

## 消息文件
命名 `<yyyy-mm-dd>-<当日序号>-<主题>.md`，例 `2026-09-08-001-premkt-rvol-ic.md`。
**发出后永不修改**，要补充就发新的一条，用 `reply_to` 串起来。

frontmatter：`id` / `from` / `to` / `thread` / `type` / `re` / `reply_to` / `needs_reply_by`
`type`：`question` | `proposal` | `finding` | `review-request` | `reply` | `ack`

正文必须含「我方已经验证的」与「我方没有验证的」两节。
**后者为空 = 该消息不可信。**

## 顺序纪律
对任何活跃问题，**先写下自己的判断和 falsifier，再去读对方的版本**。
顺序反了，这次对照作废；写下前已读过对方材料的，标 `CORRELATED`，不计入置信度。
