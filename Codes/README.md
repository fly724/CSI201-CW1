py# 运行与操作说明

**状态：初始化骨架，尚不可进行联网答题。** `server.py` 和 `client.py` 目前仅解析参数，支持 `--help`；普通运行会向 stderr 报告尚未实现并以状态 2 退出。没有任何功能测试通过的声明。

## 环境

正式运行使用 Learning Mall 发布的教师 Docker 镜像，内含 Python 3.12 标准库。镜像名称和标签尚未提供，不需要安装任何 Python 包。以下 Docker 命令是待官方镜像发布后使用的模板，并非已执行的验证记录。

在宿主机仓库根目录，填写官方镜像名称和标签后：

```bash
export EXAMINER_IMAGE='REPLACE_WITH_OFFICIAL_IMAGE_AND_TAG'
docker run --rm -it --name csi201-cw1   --mount "type=bind,source=$(pwd),target=/submission"   --workdir /submission/Codes "$EXAMINER_IMAGE" sh
```

该模板假定官方镜像支持 `sh`；具体入口和挂载方法以官方说明为准。后续在同一容器内开启客户端终端，因此示例使用 `127.0.0.1`，不需要发布宿主机端口。

## 启动接口

在 `Codes/` 下，必须支持的精确接口为：

```text
python3 server.py --host <server_IP> --port <port> --questions <json_path>
python3 client.py --server <server_IP> --port <port> --id <user_id> --name <display_name>
```

实现完成后，在容器终端运行服务器：

```bash
python3 server.py --host 127.0.0.1 --port 5000 --questions questions.json
```

在另一宿主终端连接同一容器并启动客户端：

```bash
docker exec -it --workdir /submission/Codes csi201-cw1   python3 client.py --server 127.0.0.1 --port 5000 --id user01 --name Alice
```

重复启动不同 ID 的客户端即可进行多用户测试。`5000` 仅为示例值，用户可通过参数选择其他有效端口。预期服务器在**完成全量题库校验并开始监听后**输出 `SERVER_READY`；当前骨架不会输出此标志。

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
