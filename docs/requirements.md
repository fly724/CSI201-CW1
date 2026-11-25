# 需求追踪起点

来源：任务书第 1–8、11–12 页。下表保留需求与测试编号的映射；当前实现和已运行的测试范围见 [M2 交接](member2-handoff.md)、[M3 交接](member3-handoff.md) 及 [测试说明](../Codes/tests/README.md)。最终报告须逐项补齐证据和未覆盖边界，不能将映射表本身当作通过记录。

| 需求 ID | 验收目标 | 计划模块 | 计划测试 |
| --- | --- | --- | --- |
| REQ-01 | 验证完整题库后监听，非法题库非零退出，正确打印 SERVER_READY | server.py、quiz_server | T01、T02 |
| REQ-02 | 多客户端身份与重复活跃 ID 的确定性处理 | quiz_server、quiz_client | T03 |
| REQ-03 | 题库目录、创建/加入/查看成员/准备/离开/开赛 | quiz_server、quiz_client | T04 |
| REQ-04 | 权威题序和时限、每人每题最多一次有效答案、分数与最终排序 | quiz_server | T05 |
| REQ-05 | 10 客户端、2 个独立房间，共享状态正确同步 | quiz_server、common | T06 |
| REQ-06 | 非法输入、断连、慢客户端和 Ctrl+C 不破坏无关会话 | 全部模块 | T02、T07、T08 |
| REQ-07 | TCP 分片/合并，明确响应/事件/错误 | common | T07 |
| REQ-08 | 服务器独占题库读取，关闭前答案保密 | quiz_server、quiz_client | T09 |
| REQ-09 | Python 3.12 标准库、原生 socket、纯 CLI、指定参数、无外部服务 | 启动入口、全部模块 | 官方镜像运行及源码审查 |
| REQ-10 | 开发文档完整，需求到设计和验证可追踪 | Documentation | 文档审查 |
| REQ-11 | 正确 ZIP 布局、2 quiz × 至少 5 题、全员参与且不超过 10 分钟的视频 | 提交产物 | 打包与演示检查 |

每次完成需求后补充实际代码位置、测试用例、证据、缺陷及状态。不要把计划当作已经通过的验收结果。
