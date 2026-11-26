# Member 4 交接 · 并发、计时与健壮性

## 基线与范围

基线为 `main` 的 `6377a008e329c3990ab8b75336bc41ae3aa49859`，包含已合并的 M1、M2、M3。开发分支为 `feature/member4-concurrency-timing`，通过 PR 交由组员审查后再合入 `main`；实时审查与合并状态以 GitHub 为准。

M4 保留单事件循环、原生非阻塞 TCP socket、六字段协议、`Session` 和 `RoomService(bank, emit)` 接口。生产代码只修改 `Codes/quiz_server/network.py`。M3 的 `rooms.py`、权威时限、计分规则、事件字段及 CLI 均保留。

交付包含网络层修复、5 项新增单元测试、7 项新增集成测试、需求映射和真实 Docker 文本记录。最终整组 PDF、全员视频、提交包和个人贡献声明仍由团队整合；本交接文件不替代最终报告。

## 原有机制与同步模型

服务端全部可变业务状态由同一个 event loop 持有。`dispatch()`、`handle()`、`submit_answer()`、`disconnect()` 和 `close_question()` 都同步执行，中间没有 `await`，因此检查身份、记录答案、结算等复合操作不会在执行中被另一个连接回调插入。

每连接有 reader、writer 和有界输出队列。房间广播只进行非阻塞入队；实际 socket 写入在 writer 中等待。没有全局锁，也没有工作线程直接修改用户、房间或成绩。将来若增加线程，其结果需通过 `loop.call_soon_threadsafe()` 交回事件循环。

M3 已使用 `loop.time()` 单调时钟记录 `deadline`，`loop.call_at()` 登记关题回调。答案以服务器处理时刻判断，`now >= deadline` 即拒绝；不接受客户端自报发送时间作为有效性依据。旧回调同时检查 room、run_id 和 question_id，过期回调不结算。全员答完通过 `call_soon()` 延后关题，保留答题确认先于结果和下一题的约定。

注意：单线程不等于没有调度问题。业务回调不能执行阻塞 I/O；一次处理过多缓存请求也可能使其他 ready callbacks 和 timers 等待。调度公平性和状态转换的原子性是两个不同问题。

## 每处生产代码改动

### 1. `READ_BATCH_SIZE = 16` 与 `Server.read()`

原先 reader 可以连续处理一串已缓存请求，而没有显式让出调度。新增计数器，每处理并入队 16 条响应执行 `await asyncio.sleep(0)`。

- `sleep(0)` 是让出执行权，不是人为增加固定的睡眠时间。
- 让出点位于完整请求处理和响应入队之后，不拆开 RoomService 内部的检查和状态更新。
- 其他 writer、连接和计时器因此获得调度机会。
- 16 是本项目的调度批量选择，不是 TCP 或题库协议字段，也不承诺硬实时延迟上限。

用有界、始终立即可读的消息源测试时，修复前 1000 条全部处理完后，其他已就绪回调才运行；修复后该回调能在整批处理完之前运行。真实 TCP 集成另外检查 300 条查询与一个真实 5 秒题目超时可共存。

### 2. `Peer.closing` 与 `Peer.send()`

原先发送队列满后每次 `send()` 都再次调用连接任务的 `cancel()`。后续广播可能在该任务清理期间重复发起取消。

新增 `closing` 状态：队列第一次满时设置状态并取消一次；之后直接返回 `False`。进入连接清理时也标记 `closing`，防止继续入队。64 帧容量维持不变。

这提供明确的运行/关闭中边界。队列溢出只淘汰相关连接；网络层仍负责解除用户、会话和房间关系。测试同时验证房主离开后的接任、另一房间继续操作和 socket 最终关闭。

### 3. `WRITE_TIMEOUT = 5`

把原来写在 `wait_for()` 内的 5 秒提取为同文件常量，生产行为不变。

测试将它临时设为 0.1 秒，并只阻塞被选中连接的 `sock_sendall()`。这是明确的故障注入，用来确定性验证超时取消、身份释放和其他连接正常运行。它不是物理网络拥塞实验，也不声称测到了真实 WAN 性能。未引入测试专用协议或 CLI 参数。

### 4. `Server.connection()` 的最终 socket 释放

原先在 `await gather(reader, writer)` 之后关闭 socket；若父任务在等待子任务清理时再次被取消，关闭语句可能被跳过。

现在把 socket.close() 放入包围该 await 的内层 `finally`。无论子任务正常完成、异常或第二次取消，都执行资源释放。新增回归测试先阻塞子任务的取消清理，再取消父任务两次；修复前 socket 仍打开，修复后 `fileno() == -1`。

### 5. `Server.serve()` 的停服顺序

顺序改为：关闭监听 socket → `rooms.close()` 销毁房间并取消计时器 → 取得连接任务快照 → 取消并等待任务。

原先先断开玩家再关闭房间，离房回调可能满足“剩余玩家全答完”，从而在停服期间结算或开下一题。先终止业务生命周期后，连接退出只需清理身份。任务快照也让等待对象固定，不依赖 done callback 随后对集合的修改。

测试在两个活动房间中停服，验证没有新增发题/结算，计时器已取消，用户、房间、成员关系清空，socket 关闭，端口可重启。另一个 Linux 子进程测试向真实 `server.py` 发送 SIGINT，验证退出码 0、客户端到达 EOF 和端口重新绑定。

## 测试与需求追踪

单元测试路径：`Codes/tests/unit/test_member4.py`。复用 M3 的可控时钟和准备辅助方法，但不继承并重复发现已有测试。

| 用例 | 预期行为 | 需求 |
| --- | --- | --- |
| `test_timeout_and_all_answered_callbacks_settle_only_once` | 到期与全员完成回调交错时只有一份结算，下一题不受旧回调影响 | REQ-04 |
| `test_queued_early_close_after_room_destroy_does_nothing` | 房间销毁后，排队的提前关题回调无效 | REQ-04、REQ-06 |
| `test_request_burst_yields_to_other_callbacks` | 大量立即可处理的请求不能独占整个调度过程 | REQ-05、REQ-06 |
| `test_full_queue_requests_cancellation_only_once` | 队列满后只发起一次连接取消 | REQ-06 |
| `test_socket_closes_even_if_cleanup_is_cancelled_again` | 清理期间再次取消仍关闭 socket | REQ-06 |

集成测试路径：`Codes/tests/integration/test_member4.py`。复用 M3 的真实 TCP 连接辅助方法。每个连接仅由一个接收协程读取，避免测试本身造成并发 `anext()` 错误。

| 用例 | 预期行为 | 需求 |
| --- | --- | --- |
| `test_both_rooms_answer_concurrently_and_keep_results_separate` | 10 连接、两个各 5 人房间，同时提交全部答案并完成 5 题；事件及最终分数隔离 | REQ-04、REQ-05 |
| `test_coalesced_duplicate_answers_are_accepted_once` | 同一 TCP write 中连续两次答案，只接受第一份，计分一次 | REQ-04、REQ-07 |
| `test_write_timeout_removes_only_slow_host_and_releases_identity` | 故障注入写超时后释放身份、房主转移、可重登，旧会话不能向新连接发事件 | REQ-02、REQ-06 |
| `test_queue_overflow_cleanup_during_play_keeps_other_room_live` | 活动房间发送队列溢出只回收该连接，另一房间正常 | REQ-05、REQ-06 |
| `test_query_burst_does_not_prevent_room_timeout` | 300 次查询期间其他玩家可操作，题目实际到期、未答计 0 | REQ-04、REQ-06 |
| `test_shutdown_cancels_timers_without_advancing_questions` | 活跃双房间停服不推进比赛，释放业务及连接资源，并能重启 | REQ-04、REQ-06 |
| `test_sigint_with_two_active_rooms_releases_port` | Linux 实际 SIGINT 结束服务器，无 traceback/资源警告，客户端 EOF、端口可复用 | REQ-06、REQ-09 |

## 实测环境与复现命令

记录日期：2026-11-26。Windows Docker Desktop 4.53.0 / Docker Engine 29.0.1，Linux 容器，Python 3.12.16。使用仓库内标为课程环境的 Dockerfile 与 Compose，未修改它们，也未安装任何 Python 第三方依赖。

- 基础镜像引用：`docker.m.daocloud.io/library/python:3.12-slim`
- 本次解析摘要：`sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b`
- 构建镜像：`csi201-cw1:latest`
- 本次本地镜像 ID：`sha256:bf1b9214abbbf8d342f17464fe12f2cc1a5544ae3a3b4afebad2d4730e2e44c9`

本次未取得 Learning Mall 原始环境压缩包，不能独立证明仓库 Dockerfile 与当前老师发布版本完全一致。M3 已记录过其核对过程；M5 最终需对照老师当前发布版本确认。Docker tag 可变，不能仅凭标签推断镜像身份。

在仓库根目录：

```sh
docker compose build python
docker compose run --rm -T --entrypoint python python --version
docker compose run --rm -T --entrypoint python python -B -W always::ResourceWarning -m unittest tests.unit.test_member4 tests.integration.test_member4 -v
docker compose run --rm -T --entrypoint python python -B -W always::ResourceWarning -m unittest discover -s tests -p 'test_*.py' -v
```

仅运行原 86 项的命令：

```sh
docker compose run --rm -T --entrypoint python python -B -W always::ResourceWarning -m unittest tests.unit.test_protocol tests.unit.test_client tests.unit.test_rooms tests.integration.test_member2 tests.integration.test_member3 -v
```

实际输出保存在 `Documentation/evidence/member4/`：

- `01_baseline.txt`：生产代码未修改时的原 86 项测试，全部通过。
- `02_regressions_before.txt`：修复前新增 5 项单元测试；3 项失败，2 项通过，展示可复现问题。
- `03_member4_tests.txt`：修复后 12 项 M4 测试全部通过。
- `04_full_regression.txt`：修复后全量 98 项通过，耗时 45.581 秒；没有增加 skip 的 Docker 用例，也没有 ResourceWarning。原始输出保留 asyncio 调度耗时提示。

本机 Windows Python 3.13.1 额外诊断曾出现原 M3 CLI 测试等待 `\n` 超时；原代码在 Docker Python 3.12.16 中 86 项全部通过。因此没有为了本机平台差异修改团队客户端或放宽原测试断言。本机结果不作为课程验收证据。

## 风险与剩余工作

- 单事件循环不是硬实时调度器；本轮不声明最大负载或严格毫秒级时限精度。异步调度耗时提示可能受宿主和调试模式影响，应保留真实输出。
- 对慢写使用确定性故障注入；尚未进行跨主机 WAN、长时间运行、内存上限或持续恶意流量实验。
- 发送队列按帧数限制，不是全服务器内存配额；没有对总连接数、房间数实施全局限流。
- 关服通过队列发送的通知属于 best effort，客户端应接受直接 EOF；不保证每个客户端都在停服前收到 ROOM_CLOSED_EVENT。
- M5 继续负责完整题库边界、老师最终环境比对和最终整包验收。
- 团队按学校要求记录 AI 辅助。每位成员必须理解并自己复现，不能将文档直接当作个人掌握证明。

## 报告英文素材（需结合最终系统审查后使用）

The server owns mutable session and room state on one event loop. Synchronous request handlers complete validation and state transitions without awaiting network I/O. Each connection has a bounded output queue and an independent writer with a five-second send deadline. A reader yields after every sixteen handled requests so buffered traffic does not monopolise execution. Connections entering cleanup reject further output, and socket closure is protected by a finally block even if cleanup is cancelled again.

Question deadlines use the event loop's monotonic clock. The server rejects answers processed at or after the deadline, while run and question identifiers invalidate stale callbacks. Shutdown destroys rooms and cancels their timers before disconnecting players, preventing departure callbacks from advancing gameplay. Verification includes controlled-clock checks, simultaneous answers from ten TCP clients in two rooms, injected writer timeouts, queue overflow, repeated cancellation, and SIGINT against an active server subprocess. These tests establish the documented cases rather than a general hard-real-time or denial-of-service guarantee.

## 个人讲解与演示准备

先解释单事件循环为何仍需要公平调度，再展示“双房间各 5 人同时答题”的实际测试。说明单调时钟、`now >= deadline` 与旧回调标识，然后用发送队列或超时用例解释故障隔离，最后演示实际 Ctrl+C 退出与重启。

需能回答：为什么不加 threading.Lock？sleep(0) 是否会拆开一次计分？为什么满队列后要拒绝后续发送？第二次 cancel 为什么会跳过 await 后的代码？先清理房间再关闭连接解决了什么？故障注入与真实网络实验的证据范围有何不同？
