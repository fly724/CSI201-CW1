# Member 3 交接 · 房间、比赛流程与计分

M3 业务在 `Codes/quiz_server/rooms.py`，接入 M2 的服务端和 M1 的命令行客户端。实现基于 M2 提交 `9a84b7b`。先合并 M2 的 PR #1，再合并 M3，随后由 M4、M5 联合验收。

## 当前业务规则

以下是当前实现采用的规则。容量、开赛条件、离房和同分策略仍需小组确认；如需调整，应同步更新代码、README 和最终报告。

- 每个会话同时只在一个房间；服务器生成 4 位房间码，加入时忽略大小写。房间最多 12 人。
- 只有 `LOBBY` 房间出现在可加入列表中。列表包含题库名称、房主、人数和玩家显示名，不含题干、选项、答案或会话 token。
- 至少 2 人、所有人 `/ready`、由房主 `/start` 才能开赛。大厅可 `/notready` 撤销准备，开赛后不能加入或修改准备状态。
- 题目按题库数组顺序自动发布，使用每题 `time_limit` 和 `points`。所有仍在房间的玩家答完后提前关题，否则截止时自动关题。
- 每个玩家每题只记录第一个合法且按时的 A/B/C/D，接受小写。非法选项不消耗答题机会。重复、迟到、其他题目的答案返回错误，不重复计分。
- 关题时统一计分并公布正确答案：答对加该题 `points`，答错或未答加 0。开放期间查询分数只看到之前已关闭题目的累计分，避免利用即时加分探测答案。
- 最后一道题关闭后公布排行榜和胜者。按分数降序，分数相同共享名次（例如 1、1、3），同分显示顺序按 `user_id`；最高分并列时列出全部胜者。
- 大厅房主离开会关闭房间；比赛中或结束后房主离开，由仍在场、最早加入的玩家接任。普通玩家离开不关闭房间。离开者及其当前答案、成绩从房间中移除。
- 比赛中只剩一人也会继续，不等待已离线玩家。无人时销毁房间并取消计时器。
- `FINISHED` 房间保留给原有玩家查看成绩；至少两名留场玩家重新准备后可重赛，重置分数和答题记录。新玩家需要另建房间。
- 不提供持久化、断线恢复或历史排行榜；重连是新会话，需要重新创建或加入大厅。

## M1 与 M2 对接

保留 M2 约定的 `RoomService(bank, emit)`、`handle(session, kind, payload)`、`disconnect(session)` 和 `close()`。业务失败抛 `RequestError("错误码")`，由 M2 生成 `ERROR_RESPONSE`。成员身份始终取 M2 验证过的 `Session`；忽略答题 payload 自报的 `user_id` 或 `room_code`，不能跨房间代答。

请求字段沿用 [M2 交接](member2-handoff.md)；无需修改 M1 或 M2 源码。

| 请求 | 成功响应的 payload |
|---|---|
| `CREATE_ROOM_REQUEST`、`JOIN_ROOM_REQUEST` | `room_code`、`room`；room 包含题库信息、房主、状态、容量和成员列表 |
| `LIST_ROOMS_REQUEST` | `rooms`，仅可加入的大厅摘要 |
| `PLAYER_LIST_REQUEST` | 当前 room 快照；成员含 `user_id/name/ready/is_host/score` |
| `READY_REQUEST` | `ready` 布尔值 |
| `START_QUIZ_REQUEST` | `room_code/total_questions` |
| `ANSWER_REQUEST` | `accepted/question_id/number`，不提前返回是否正确 |
| `LEADERBOARD_REQUEST` | `room_code/state/leaderboard` |
| `LEAVE_ROOM_REQUEST` | `room_code` |

| 事件 | 主要 payload 字段 |
|---|---|
| `PLAYER_STATUS_EVENT` | 当前 room 快照 |
| `QUIZ_STARTED_EVENT` | `room_code/quiz_id/title/total_questions` |
| `QUESTION_EVENT` | `room_code/question_id/number/total_questions/text/options/time_limit/points`；没有 `answer` |
| `QUESTION_RESULT_EVENT` | `room_code/question_id/number/total_questions/correct_answer/points/results/leaderboard/reason` |
| `QUIZ_FINISHED_EVENT` | `room_code/quiz_id/leaderboard/winner_user_ids/winner_score/tied` |
| `HOST_CHANGED_EVENT` | `room_code/host_user_id/host_name` |
| `ROOM_CLOSED_EVENT` | `room_code/reason` |

`results` 每项包含 `user_id/name/your_answer/correct/points_awarded/score`。排行榜每项包含 `rank/user_id/name/score/correct_count`。业务错误码包括 `ALREADY_IN_ROOM`、`NOT_IN_ROOM`、`QUIZ_NOT_FOUND`、`ROOM_NOT_FOUND`、`ROOM_FULL`、`GAME_IN_PROGRESS`、`NOT_ROOM_HOST`、`NOT_ENOUGH_PLAYERS`、`PLAYERS_NOT_READY`、`INVALID_ROOM_STATE`、`INVALID_REQUEST`、`INVALID_ANSWER`、`DUPLICATE_ANSWER`、`QUESTION_CLOSED`。

### 回包与推送顺序

M2 在 `handle()` 返回后才排入请求响应。M1 收到 `ANSWER_RESPONSE` 会清除本地当前题目，所以最后一个答案不能在 `handle()` 内同步发布下一题。当前通过 `loop.call_soon()` 延后“全员答完”的关题动作，使同一连接上依次收到答题确认、该题结果、下一题。其他操作的成员事件仍可能先于对应响应到达，客户端不能假定所有响应都先于事件。

## M4 计时接入点

房间和计分状态由 M2 的单一事件循环持有，业务回调同步且不等待 socket 或用户输入。不要另起线程直接修改 `rooms`、`memberships`、`players` 或 `answers`。

| 接口或字段 | 当前行为与接入约束 |
|---|---|
| `open_question(room_code, index)` | 发布指定数组下标的题目；由开赛和上一题关闭流程调用 |
| `submit_answer(session, payload)` | 先校验身份、当前题、截止边界与重复情况，再记录一次答案 |
| `close_question(room_code, run_id, question_id, reason="timeout")` | 取消计时器、结算、广播结果并自动推进；失效回调返回 `False` |
| `Room.run_id` | 每次开赛分配服务内递增编号，重赛也更新；用于计时回调，不是 M1 当前请求字段 |
| `Room.deadline` | `loop.time() + time_limit`，使用单调时钟；服务器处理答案时 `now >= deadline` 即拒绝，不以客户端发送时间判断 |
| `Room.timer` | 当前 `call_at` 或提前关题的 `call_soon` handle；关题、销毁、停服均取消 |

回调同时核对房间、场次和题目，防止旧计时器误关下一题或重赛的同一道题。若计时回调略早于截止时间触发，会按原截止时间重新排期。M4 修改调度时需保留答题确认与换题顺序；外部线程产生的动作先用 `loop.call_soon_threadsafe()` 回到服务器事件循环。

## Docker 验证与复现

2026-11-24 在 `csi201-cw1:latest` 内执行，Python **3.12.16**。仓库 Dockerfile 的构建指令与重新下载的 `CSI201_CW1_Docker_Env (1).zip` 中教师 Dockerfile 一致，仅文本换行或末尾空白有差异。基础镜像解析到 `python:3.12-slim@sha256:37fc60d303cb9294d3718b5b48998a0210bb1c73611c1399ef5eb7964c0536e5`，本次镜像 ID 为 `sha256:e73a67e9e9945650bcbc33fcd3cda69b3368f8cd6514481c01d4d8dc25db69f6`。

仓库 Compose 为多终端开发增加了 `/code/Codes` 工作目录和 `sleep infinity` entrypoint。因此一次性测试必须显式覆盖 entrypoint。在仓库根目录执行：

```sh
docker compose build python
docker compose run --rm -T --entrypoint python python -B -m unittest discover -s tests -p 'test_*.py' -v
```

计时边界修复后，**83 项全部通过**：M1 的 41 项、M2 的 9 项、M3 的 28 项业务单元测试与 5 项真实网络/CLI 集成测试，全套耗时 38.510 秒。新增两项回归测试覆盖提前触发后仍按时关题，以及提前触发后全员答完不会误关下一题。运行中仍有调度耗时提示，结束时输出一个未关闭文件的 `ResourceWarning`，未导致测试失败。

| 新增用例 | 真实检查范围 |
|---|---|
| `tests/unit/test_rooms.py`，28 项 | 大厅与权限、容量、题序、答案保密、一次答题、截止边界、提前计时回调、计分、并列、重赛、旧回调、掉线和清理；截止边界使用可控时钟 |
| `test_ten_clients_complete_two_isolated_quizzes` | 10 个真实 TCP 连接，两个房间各 5 人，均完成 5 道题；逐连接核对事件归属和最终成绩 |
| `test_real_timeout_does_not_block_another_room` | 真实 5 秒超时、未答计 0、旧题答案被拒绝，另一房间可独立答完整场 |
| `test_invalid_answers_host_disconnect_and_bad_peer_are_isolated` | 非法/重复答案、未关题分数保密、房主断开后继续、非法帧和跨房间题号隔离 |
| `test_lobby_host_logout_closes_room_but_not_guest_session` | 房主注销关闭大厅、其他会话可另建房、原 ID 重登取得新 token |
| `test_existing_m1_cli_can_answer_all_five_questions` | 真实 `server.py` 加两个 M1 `client.py` 子进程；建房、加入、准备、连续答题、最终胜者和注销 |

测试客户端只通过 TCP 获取题目；测试驱动可以知道固定 fixture 的预期答案来作断言。以上是开发验证记录，成员仍需理解代码、自己复现并采集最终报告和视频所需证据。M4/M5 独立审查、完整题库校验边界覆盖、最终报告和全员演示尚需团队完成。

## 双客户端手动流程

按 [Codes/README](../Codes/README.md) 启动服务器及两个客户端，使用默认 `questions.json`。Alice 执行 `/create network-basics`，Bob 使用返回的房间码 `/join <room_code>`。两人均 `/ready` 后 Alice `/start`，分别答题，检查题目结果、累计分数和最终排行榜。通过 `/players`、`/score`、`/leave`、`/quit` 检查查看与退出行为。第二房间可选 `tcp-python`，不能收到第一房间的题目或结果。
