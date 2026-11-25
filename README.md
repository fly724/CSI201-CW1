# CSI201-CW1 · Networked Quiz Tournament

基于 Python 3.12 标准库和原生 TCP socket 的多人命令行答题竞赛课程项目。

**当前分支状态：M1 客户端、M2 网络会话与 M3 房间/测验/计分已接通。** 2026-11-24 在课程 Dockerfile 构建的 Python 3.12.16 容器中，86 项测试通过，包含 10 个 TCP 客户端、两个独立房间、真实 CLI 完赛和重赛旧答案校验。M2 已经由 PR #1 合入 `main`，M3 由 PR #2 审查。M4/M5 联合审查、最终报告和全员演示仍待团队完成。业务规则和计时接入点见 [M3 交接](docs/member3-handoff.md)。

## 依据与范围

- [Coursework I Task Sheet](docs/reference/Coursework%20I%20Task%20Sheet.pdf)：强制要求，包含运行环境、功能、题库格式、文档及提交规则。
- [Application Layer Protocol Reference](docs/reference/CAN201_Networked_Quiz_Tournament_Appendix_Application_Layer_Protocol.pdf)：说明性示例，可采纳、修改或替换，不是必须遵守的协议。
- 附录使用 CAN201 名称，本仓库按任务书使用 CSI201；两者冲突时以任务书为准。
- 任务书列出的截止时间为 **2026-12-10 23:59**，最终截止时间、ZIP 命名和官方 Docker 镜像标签以 Learning Mall 公告为准。

## 仓库结构

```text
CSI201-CW1/
├── README.md                    # 项目导航、要求与开发计划
├── .editorconfig
├── .gitignore
├── Dockerfile                   # Python 3.12 开发环境定义
├── compose.yaml                 # 共享开发容器与源码挂载
├── .dockerignore                # 最小构建上下文
├── Codes/                       # 最终提交的代码目录
│   ├── server.py                # 服务器命令行入口
│   ├── client.py                # 客户端命令行入口
│   ├── questions.json           # 开发题库：2 个 quiz，每个 5 题
│   ├── questions_50.json        # 扩展题库：5 个 quiz，每个 10 题
│   ├── README.md                # 启动接口、CLI、规则和限制
│   ├── common/                 # 分帧、消息校验、公共常量
│   ├── quiz_server/            # 题库、会话、房间、计时与计分
│   ├── quiz_client/            # 输入解析、连接与事件显示
│   └── tests/
│       ├── README.md           # 测试计划与证据规则
│       ├── unit/
│       ├── integration/
│       └── fixtures/
├── Documentation/
│   ├── README.md               # 开发报告提纲及交付要求
│   └── evidence/               # 预留：官方环境中的真实测试证据
└── docs/                       # 仓库内部开发参考，非默认提交内容
    ├── requirements.md         # 需求、设计模块与计划测试的追踪表
    ├── protocol.md             # 协议候选方案与待决策项
    └── reference/              # 两份原始 PDF 副本
```

空目录使用 `.gitkeep` 保留。最终报告 `Documentation/Development_Documentation.pdf` 尚未生成。

## 必须满足的实现约束

1. 只使用 Python 3.12 标准库，网络核心直接使用 `socket`；不能使用 `socketserver`、第三方框架或 `asyncio` 高层服务器 API，无运行时安装步骤。
2. 客户端与服务器均为纯 CLI；包含或依赖 GUI 会导致 Part I 零分。
3. 所有提交的验证、演示及测试证据必须来自教师提供的 Docker 镜像。正式环境以 Learning Mall 公告为准；仓库中的 Docker 配置用于开发，不纳入作业提交。
4. 服务器完整验证题库后才能监听，监听成功后打印 `SERVER_READY`。端口由参数指定，不能硬编码。
5. 仅服务器读取题库，客户端通过 TCP 获取信息；题目关闭前不得泄露正确答案，目录列表不得提前暴露题干和选项。
6. 至少支持 10 个同时连接的客户端和 2 个独立运行的房间，正确处理 TCP 分片、合并、异常输入、断连和 Ctrl+C。
7. 路径必须相对或由参数指定；不能依赖挂载提交目录之外的文件、数据库、后台服务或外部网络服务。

## 启动与开发

完整参数、CLI 命令和 Docker 使用方式见 [Codes/README.md](Codes/README.md)。启动 Docker 后，在仓库根目录执行：

```bash
docker compose up -d --build
docker compose exec python python3 --version
docker compose exec python python3 server.py --help
docker compose exec python python3 client.py --help
```

容器默认工作目录为 `/code/Codes`；多客户端通过 `docker compose exec` 共用一个容器。Dockerfile 构建指令与已下载的教师环境包一致，Compose 增加了常驻开发容器设置；这些开发配置不纳入课程提交。

服务器和客户端支持以下接口（在容器的 `/code/Codes` 目录执行）：

```bash
python3 server.py --host <server_IP> --port <port> --questions <json_path>
python3 client.py --server <server_IP> --port <port> --id <user_id> --name <display_name>
```

不需要 `pip install` 或第三方依赖清单。测试计划见 [Codes/tests/README.md](Codes/tests/README.md)，需求覆盖见 [docs/requirements.md](docs/requirements.md)。

## 题库要求

输入必须为非空 UTF-8 JSON 对象，最大 1 MB，包含 `schema_version` 和 `quizzes`。除 `options` 必须恰好含 A/B/C/D 外，应允许额外对象字段。任何必需字段缺失、类型错误、越界或标识符重复均使整个文件无效，服务器须返回非零状态且不监听。

| 字段 | 强制约束 |
| --- | --- |
| `schema_version` | 整数，恰为 1 |
| `quizzes` | 1–20 个 quiz；提交的开发题库至少 2 个 |
| `quiz_id` | 1–32 个字母、数字、下划线或连字符，唯一 |
| `title` / `description` | 分别为 1–100 / 0–300 个可见字符 |
| `questions` | 每个 quiz 为 5–50 题 |
| `question_id` | 1–32 个字母、数字、下划线或连字符，全文件唯一 |
| `text` | 1–500 个可见字符 |
| `options` | 恰好 A、B、C、D 四键，各值为 1–200 字符字符串 |
| `answer` | A、B、C 或 D |
| `time_limit` | 整数，5–60 秒 |
| `points` | 整数，1–1000 |

默认按文件顺序展示 quiz 和问题。正确且被接受的答案使用该题 `points`；其他答案不得额外重复计分。每题 `time_limit` 是服务器权威时限。题库校验不把 `bool` 当作整数；当前将 1 MB 解释为 1,000,000 字节，见 M2 交接。

## 开发顺序

- [x] 建立仓库结构、入口骨架、示例题库与说明文档。
- [x] 实现协议、重复身份处理、房主离开、开赛条件及排行榜同分规则，记录当前实现策略；具体业务规则待小组确认。
- [x] 接入题库校验、分帧与消息校验；完整校验边界覆盖由 M5 继续验收。
- [x] 实现会话、房间、客户端交互与端到端流程。
- [x] 实现基础权威计时、答案保密、计分、排行榜及异常清理；M4 继续审查同步和计时。
- [x] 在教师 Dockerfile 构建的容器中验证 10 客户端、2 房间及已列出的故障场景。
- [ ] 合并成员分支，完成 M4/M5 联合验收和最终需求覆盖审查。
- [ ] 完成开发报告、真实测试证据及全员参与的视频演示。

## 提交清单

### Git 仓库

源码、题库、文档和开发配置纳入版本管理，供组员协作和重建环境。Git 仓库包含开发资料，课程提交 ZIP 只包含要求的交付文件。

### 课程提交 ZIP

目标压缩包结构为 `Codes/` 与 `Documentation/Development_Documentation.pdf`。代码目录包含两个入口、题库、操作 README、测试和必要源码模块。打包前移除占位文件以及无关开发资料；不要直接把整个仓库打包，也不要包含虚拟环境、缓存、IDE 文件、二进制、数据库服务器、Docker 镜像或 Dockerfile。

开发报告建议正文 12–18 页（不含封面、目录、参考文献与附录），A4 单栏、11 pt 正文、编号标题、页码、统一图注与 IEEE 引用。文档占 60 分，实现占 40 分；本作业占模块成绩 20%。视频不超过 10 分钟，展示 Tasks 1–5，每名成员均需参与。成员贡献比例须合计 100%。

报告结构、引用和提交证据要求见 [开发报告提纲](Documentation/README.md)。
