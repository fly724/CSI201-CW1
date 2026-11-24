# 运行与操作说明

**状态：M1 客户端、M2 网络会话和 M3 房间/比赛业务已接通。** 可进行建房、加入、准备、答题、计分与排行榜流程。M3 分支基于尚待合并的 M2 提交；详细规则及 M4 计时接口见 [M3 交接](../docs/member3-handoff.md)。

## 开发环境

使用 Docker Compose 运行 Python 3.12 标准库环境，无需安装 Python 第三方包。

| 配置 | 值 |
| --- | --- |
| 基础镜像 | `docker.m.daocloud.io/library/python:3.12-slim` |
| 构建镜像 | `csi201-cw1:latest` |
| Compose 服务名 | `python` |
| 项目挂载路径 | `/code` |
| 默认工作目录 | `/code/Codes` |

容器保持运行，服务器和多个客户端通过独立的 `exec` 会话启动，在同一容器内通过 `127.0.0.1` 通信，无需发布端口。源码通过挂载共享，修改后无需重新构建镜像。

启动 Docker 后，在仓库根目录执行：

```bash
docker compose config --quiet
docker compose up -d --build
docker compose exec python python3 --version
```

首次构建需要下载基础镜像。正式评估环境以 Learning Mall 公告为准，提交用测试与演示须在教师指定环境中完成。

进入容器或停止环境：

```bash
docker compose exec python sh
docker compose down
```

`down` 停止并移除容器及 Compose 网络，不删除挂载的项目文件。

## 启动接口

程序必须支持以下接口，在容器的 `/code/Codes` 目录执行：

```text
python3 server.py --host <server_IP> --port <port> --questions <json_path>
python3 client.py --server <server_IP> --port <port> --id <user_id> --name <display_name>
```

以下 Compose 命令均在仓库根目录执行。当前可查看入口帮助：

```bash
docker compose exec python python3 server.py --help
docker compose exec python python3 client.py --help
```

在一个终端启动服务器：

```bash
docker compose exec python python3 server.py --host 127.0.0.1 --port 5000 --questions questions_50.json
```

在另外的终端分别启动客户端：

```bash
docker compose exec python python3 client.py --server 127.0.0.1 --port 5000 --id user01 --name Alice
docker compose exec python python3 client.py --server 127.0.0.1 --port 5000 --id user02 --name Bob
```

`5000` 为示例端口。服务器在完整验证题库并开始监听后输出 `SERVER_READY`。

题库可通过 `--questions` 切换：`questions.json` 含 2 套共 10 题，`questions_50.json` 含 5 套共 50 题。最终提交仍需包含有效的 `questions.json`。

执行全套测试：

```bash
docker compose exec -T python python3 -m unittest discover -s tests -p 'test_*.py' -v
```

也可不启动常驻容器，直接在仓库根目录执行 `docker compose run --rm -T --entrypoint python python -B -m unittest discover -s tests -p 'test_*.py' -v`。2026-11-24 在课程 Dockerfile 构建的 Python 3.12.16 容器中，83 项测试通过，包含 10 客户端、双房间及两个真实 CLI 的完整比赛。覆盖与限制见 [tests/README.md](tests/README.md) 和 M3 交接。

## CLI 命令

以下命令已接入 M1 客户端和 M2/M3 服务端。命令名为团队接口；开发报告须与实现保持一致。

| 命令 | 行为 |
| --- | --- |
| `/help` | 显示本地帮助 |
| `/quizzes` | 查看公开 quiz 元数据 |
| `/rooms` | 查看可加入的房间 |
| `/create <quiz_id>` | 创建房间并成为房主 |
| `/join <room_code>` | 加入房间 |
| `/players` | 查看成员与准备状态 |
| `/ready` | 设置准备状态 |
| `/notready` | 在大厅或比赛结束后撤销准备 |
| `/start` | 房主在满足开赛条件时开始 |
| `/answer <A\|B\|C\|D>` | 回答当前题目 |
| `/score` | 查看排行榜 |
| `/leave` | 离开房间 |
| `/status` | 显示本地身份、房间与当前题状态 |
| `/quit` | 注销并关闭连接 |

操作流程：登录 → 查询题库 → 创建/加入房间 → 查看成员 → 准备 → 开赛 → 答题与结果 → 最终排行榜 → 离开/退出。

默认 `questions.json` 的 quiz ID 为 `network-basics` 和 `tcp-python`；上面的 `questions_50.json` 则使用 `geography`、`history` 等 ID。先 `/quizzes` 查询实际目录，再 `/create <quiz_id>`；另一客户端 `/join <room_code>`，至少两人全部 `/ready` 后由房主 `/start`。每题用 `/answer A` 等作答，也可直接输入 A/B/C/D。

## 假设与已知限制

- 正式执行的 Python 版本为 3.12；所有组件在官方容器内运行。
- 只允许服务器读取 `questions.json`；客户端必须通过网络取得数据。
- 活跃用户 ID 必须唯一，显示名可重复；重连取得新会话，不恢复旧房间和成绩。
- 房间容量 12，至少 2 人且全部准备后由房主开赛。大厅房主离开关闭房间；比赛中或结束后由最早加入的剩余玩家接任。离房者从当前排行榜移除。
- 只可加入大厅；结束后留场成员可重新准备并重赛，也可离开另建房间。正确答案只在关题后公布，成绩按分数降序，同分共享名次。
- 开赛后按题库顺序自动推进；全部活跃玩家答完可提前关题。截止时间由服务器处理答案时的单调时钟决定。
- 网络层每连接发送队列为 64 帧，单次发送超时 5 秒；不记录开放题目的答案或敏感会话标识。
- 客户端 `/quit` 注销，服务器 Ctrl+C 停服并清理连接及房间计时器。最终提交仍需团队复测、报告、贡献说明与全员演示。

## 源码分工

`common/` 放双方共用的协议代码；`quiz_server/` 放题库校验、会话、房间与竞赛逻辑；`quiz_client/` 放用户输入、网络交互与事件显示。根目录的两个脚本只承担启动入口职责。测试计划见 [tests/README.md](tests/README.md)。

## Member 1：协议与客户端（已实现）

`common/protocol.py` 提供分帧、信封校验与消息构造；`quiz_client/client.py` 为客户端实现，`client.py` 已接入，接口保持本节列出的 `--server --port --id --name`。单元测试在 `tests/unit/`（22 个协议测试 + 19 个客户端测试），在 `Codes/` 下执行：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

参考对端 `tests/stub_server.py` 只用于单独验证客户端，不属于被评估的服务器。协议设计见 `Documentation/Member1_Protocol_and_Client.pdf`，运行记录见 `Documentation/evidence/member1/`。当前配对服务器位于 `quiz_server/`；网络、房间及完整比赛的真实联调测试位于 `tests/integration/`，接口分别见 [M2 交接](../docs/member2-handoff.md) 与 [M3 交接](../docs/member3-handoff.md)。
