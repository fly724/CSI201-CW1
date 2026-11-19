# 运行与操作说明

**状态：初始化骨架，尚不可进行联网答题。** `server.py` 和 `client.py` 目前仅解析参数，支持 `--help`；普通运行会向 stderr 报告尚未实现并以状态 2 退出。没有任何功能测试通过的声明。

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

实现完成后，在一个终端启动服务器：

```bash
docker compose exec python python3 server.py --host 127.0.0.1 --port 5000 --questions questions_50.json
```

在另外的终端分别启动客户端：

```bash
docker compose exec python python3 client.py --server 127.0.0.1 --port 5000 --id user01 --name Alice
docker compose exec python python3 client.py --server 127.0.0.1 --port 5000 --id user02 --name Bob
```

`5000` 为示例端口。服务器应在完整验证题库并开始监听后输出 `SERVER_READY`。当前入口仅为骨架，上述运行命令会报告尚未实现并退出。

题库可通过 `--questions` 切换：`questions.json` 含 2 套共 10 题，`questions_50.json` 含 5 套共 50 题。最终提交仍需包含有效的 `questions.json`。

增加测试用例后执行：

```bash
docker compose exec -T python python3 -m unittest discover -s tests -p 'test_*.py' -v
```

测试计划见 [tests/README.md](tests/README.md)。当前没有测试用例，零条测试不代表功能验证通过。

## CLI 命令草案

以下取自参考附录，供实现时采用或调整；**均未实现**。命令名不是任务书规定的强制接口，最终应与实现和开发报告同步。

| 命令 | 计划行为 |
| --- | --- |
| `/help` | 显示本地帮助 |
| `/quizzes` | 查看公开 quiz 元数据 |
| `/rooms` | 查看可加入的房间 |
| `/create <quiz_id>` | 创建房间并成为房主 |
| `/join <room_code>` | 加入房间 |
| `/players` | 查看成员与准备状态 |
| `/ready` | 设置准备状态 |
| `/start` | 房主在满足开赛条件时开始 |
| `/answer <A\|B\|C\|D>` | 回答当前题目 |
| `/score` | 查看排行榜 |
| `/leave` | 离开房间 |
| `/quit` | 注销并关闭连接 |

计划操作流程：登录 → 查询题库 → 创建/加入房间 → 查看成员 → 准备 → 开赛 → 答题与结果 → 最终排行榜 → 离开/退出。

## 假设与已知限制

- 正式执行的 Python 版本为 3.12；所有组件在官方容器内运行。
- 只允许服务器读取 `questions.json`；客户端必须通过网络取得数据。
- 当前无网络、身份验证、房间、定时器、分帧或计分实现，也没有正式测试证据。
- 重复活跃 ID 的策略、显示名唯一性、房主断连、房间容量、开赛人数、准备撤销、重连、并列排序及关闭规则待团队确定并记录。
- 尚未确定日志位置与资源限制；不得记录未关闭题目的答案或敏感会话标识。
- 正常退出的目标行为为客户端 `/quit`、服务器 Ctrl+C 后释放资源，具体实现与验证待完成。
- 当前不能作为完成版课程提交；必须先完成实现、测试和最终报告。

## 源码分工

`common/` 放双方共用的协议代码；`quiz_server/` 放题库校验、会话、房间与竞赛逻辑；`quiz_client/` 放用户输入、网络交互与事件显示。根目录的两个脚本只承担启动入口职责。测试计划见 [tests/README.md](tests/README.md)。
