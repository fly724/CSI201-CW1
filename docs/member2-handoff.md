# Member 2 交接 · Server networking and sessions

## 合并和启动

本分支在 M2 提交 eb7d4cf 上合入 main 的 a706f61。登录、查询题库和注销已与 M1 的真实 CLI 联调。房间请求接入下文的 RoomService。

在仓库 Codes 目录运行。`test_member2.py` 使用 `tests/fixtures/member2_questions.json` 作为独立测试题库；正常运行服务器时仍需小组的 `questions.json`：

```sh
python3 server.py --host 127.0.0.1 --port 5100 --questions questions.json
python3 -m unittest discover -s tests/integration -p test_member2.py -v
```

成功输出 SERVER_READY。Ctrl+C 停服。主机和端口是示例；不同容器部署时可绑定 0.0.0.0，并通过可达的服务器地址连接。

## M1 · 客户端和协议

服务端与客户端共用 common/protocol.py 的 encode_message、FrameReader 和六字段信封。每个连接独立解码 1–65,536 字节的 UTF-8 JSON 帧。每条请求填唯一 request_id。它用于配对响应，重复请求会重新执行。使用单一接收循环按 request_id 投递回包，按 event_id 处理推送；事件可能先于触发它的请求响应到达。

首条请求示例：

```json
{"version":1,"type":"LOGIN_REQUEST","request_id":"r1","event_id":null,"session_id":null,"payload":{"user_id":"001","name":"Alice"}}
```

成功响应使用 LOGIN_RESPONSE，原样返回 request_id，顶层和 payload 均包含同一个新 session_id；payload 包含 success=true、user_id 和 name，以兼容 M1 客户端。所有 response 显式包含 event_id=null，所有 event 显式包含 request_id=null。保存 token，后续请求在同一连接上回传。ID 区分大小写，允许重名；请求编号最多 64 字符；ID、显示名均接受 1–128 个可打印字符且不能全空白。重复活动 ID 返回 DUPLICATE_USER，原连接保留，新连接可换 ID 重试。连接关闭后重登获得新的 token。

| CLI 操作 | 请求类型 | payload |
|---|---|---|
| 查询题库 | LIST_QUIZZES_REQUEST | {}；响应含 quizzes，每项含 quiz_id、title、description、question_count |
| 查看房间、玩家、排名 | LIST_ROOMS_REQUEST / PLAYER_LIST_REQUEST / LEADERBOARD_REQUEST | {} |
| 创建、加入 | CREATE_ROOM_REQUEST / JOIN_ROOM_REQUEST | 分别传 quiz_id、room_code |
| 准备、开始 | READY_REQUEST / START_QUIZ_REQUEST | 分别传 ready 布尔值、{} |
| 答题 | ANSWER_REQUEST | question_id、answer |
| 离房、退出 | LEAVE_ROOM_REQUEST / LOGOUT_REQUEST | {}；注销响应发送后关闭连接 |

业务 payload 为 M3 对接建议，字段合法性由 M3 检查。正常响应将 _REQUEST 换为 _RESPONSE。错误统一为 ERROR_RESPONSE，payload.code 取 INVALID_REQUEST、INVALID_SESSION、INVALID_IDENTITY、DUPLICATE_USER、UNKNOWN_REQUEST 或业务错误码。非法帧长度、无效 UTF-8/JSON 或帧中途 EOF 会关闭该连接。结构错误但仍是合法 JSON 时返回 INVALID_REQUEST；不可用的 request_id 返回 null。

## M3 · 房间与比赛

实现 quiz_server/rooms.py 中 RoomService 的三个方法。构造器接收通过校验的完整 bank 以及 emit。handle(session, kind, payload) 同步返回可公开的 dict，失败抛 RequestError("业务错误码")。已登录的真实身份取自不可变 Session(user_id, name, session_id)，业务请求内自报身份不能用来授权。M3 保存房间成员的 Session，disconnect(session) 负责离房、房主处理和通知余下成员；close() 释放所有房间资源。

广播时对目标房间的成员快照逐个调用 self.emit(member, "PLAYER_STATUS_EVENT", public_payload)。返回 False 表示会话已失效或队列已满。事件含 version、type、event_id、session_id、payload；过期会话不会投递给同 ID 的新连接。题目事件仅构造允许公开的字段，关闭前省略 answer。当前适配器返回 ROOM_SERVICE_UNAVAILABLE，故联调时可区分业务尚未接入与协议错误。test_member2.py 的 RoomProbe 给出了可运行的最小对接例子。

## M4 · 同步与计时

所有身份登记、请求路由、房间变更及断开回调都在同一个事件循环中同步执行。回调须快速返回；用 loop.time() 记录截止时间，loop.call_at() 登记每房间计时器，并在离房或结束时取消过期任务。在外部线程产生的动作须经 loop.call_soon_threadsafe() 投递回事件循环。网络层按连接维护独立读写任务；64 帧发送队列满或单次发送超过 5 秒会回收该连接。M4 和 M3 联调两间房时，记录各房的开题/关题顺序，并检查另一房的客户端未收到这些事件。

## M5 · 校验与官方环境复测

quiz_server/question_bank.py 提供 load_and_validate_questions(path)，成功返回保留文件顺序的 dict，失败抛 ValueError 或 OSError。服务器完成整个文件校验才创建监听 socket。当前将 1 MB 解释为 1,000,000 字节。M5 可直接接管这个模块，保留函数名与返回结构即可。

将合并后的 Codes 目录放入教师 compose.yaml 所在目录（该配置挂载当前目录为 /code）。在该目录运行以下命令；第一条测试命令覆盖入口和网络层，第二条供互动演示，使用 Ctrl+C 关闭。

```sh
docker compose build python
docker compose run --rm -w /code/Codes python -m unittest discover -s tests/integration -p test_member2.py -v
docker compose run --rm -w /code/Codes python server.py --host 127.0.0.1 --port 5100 --questions questions.json
```

教师 Dockerfile 已设置 python 为 entrypoint。自动化测试在同一容器内启动服务和测试客户端。演示多终端时，用 docker exec 进入正在运行服务器的同一容器启动 M1 客户端。

## 验证记录与报告素材

2026-11-23，Windows Python 3.12.7 实测。M2 集成用例包括真实 server.py 与 client.py 子进程登录、重复 ID、目录显示、正常退出、异常终止后重新登录，以及协议层与会话隔离测试。另在独立 Windows 控制台向带连接的服务端发送 Ctrl+C，两次退出码均为 0，中间用同一端口重启成功。教师 Docker 和真实双房间比赛仍待联合验收。全套回归命令为 python3 -m unittest discover -s tests -p "test_*.py" -v。

**Report draft — server architecture and failure handling.** The server validates the entire question bank before binding its IPv4 TCP socket. Each accepted connection has independent read and write tasks. A single event loop serialises registration and room callbacks, so two simultaneous login attempts cannot both claim the same active ID. Each authenticated request must carry the token associated with its connection. Explicit logout and unexpected disconnect both release the identity and notify the room service. Outgoing events identify a session generation as well as a user, preventing delayed updates from reaching a newly connected player. The transport limits each outgoing queue and applies a write deadline so that a slow receiver cannot suspend other connections.

**个人视频演示顺序。** 先启动服务器展示 SERVER_READY，再用两个客户端尝试相同 ID；保留第一个连接，展示第二个收到 DUPLICATE_USER。退出第一个客户端，再用同一 ID 登录并查询题库。最后解释请求到 RoomService 的调用及会话断开后的清理。
