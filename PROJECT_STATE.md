# PROJECT_STATE

更新时间：2026-08-24

## 当前修复单元

本单元只处理两个可靠性问题，保持现有生命周期、Page Lock、UI 和进程监控架构不变：

1. `server.run()` 在读取 Registry、UI Settings、Proxy Secret Store 或启动
   Chrome 前取得同一 `data-dir` 的跨进程 Windows named mutex；第二个使用同一
   `data-dir` 的 Manager 即使指定不同 API/UI 端口也必须失败。
2. Profile 进程识别解析 Chrome 命令行中的完整 `--user-data-dir` 参数，并对
   规范化后的路径做相等比较，不再使用路径字符串 `Contains`。

## 为什么

多个 Manager 共享一个持久化数据目录会造成 Registry、设置、密钥和 Profile
生命周期的并发写入。模糊路径匹配还可能把 `Profile-100` 误识别为
`Profile-10`，影响进程存活判断、窗口枚举和清理。

## 验证

- 跨进程 named mutex 单元测试覆盖：第二个进程被拒绝、持有者释放后可重新取得。
- 命令行解析单元测试覆盖：空格、引号、大小写、尾部分隔符、分离参数形式，
  以及 `Profile-10` 不匹配 `Profile-100`。
- 未启动 Chrome、服务或网络；Windows named mutex 的实机语义由本地 Windows
  单元测试覆盖。

## 结果

本单元实现后，相关单元测试通过。完整 Chrome 生命周期、异常退出后重新启动
Manager、真实 CIM 输出格式和真实 Profile 锁行为仍待后续 Windows 实机验证。

## Windows 实机验收（2026-08-24）

第一修复单元已完成最小范围实机验收。测试使用全新临时 data-dir 和临时
Profile，没有个人账号、Cookie、真实业务数据、真实网站或网络请求；浏览器只
访问本地 ready page。当前环境没有找到系统安装的 Google Chrome，因此使用项目
支持的 Playwright bundled Chromium 实际可执行文件完成浏览器进程验收。

- 相同 data-dir、不同 API/UI 端口：第二个 Manager 启动失败；PASS。
- 不同 data-dir、不同 API/UI 端口：两个 Manager 同时运行；PASS。
- 强制结束 Manager：同一 data-dir 的后续 Manager 成功启动，mutex 自动释放；PASS。
- 真实 Chromium 启动 `Profile-10` 和 `Profile-100`：两组 PID 均存在且集合不交叉；PASS。
- 停止 `Profile-10`：`Profile-10` PID 清零，`Profile-100` 仍有存活 PID；PASS。
- 最终停止两个 Profile：没有残留的目标 Profile 进程；PASS。

Google Chrome 特定版本尚未单独验收；本次结果证明的是实际 Chromium 与当前
Windows named mutex、命令行解析和 Profile 隔离链路。

## v0.1 冻结维护状态

公开仓库 v0.1 定位为本地人工操作冻结版：本地运行、多 Profile 隔离、人工打开
和操作浏览器、本地管理。当前不继续向长期自动化 Runtime、Agent、无人值守
浏览器、任务系统或远程服务方向开发。

当前状态：`maintenance / frozen`。

除安全、数据隔离和明确严重缺陷外，不继续功能开发或架构开发。已完成的 data-dir
单实例锁和 `--user-data-dir` 精确进程匹配属于本冻结版允许的基础安全与隔离维护。

以下审计项保留为 `Known Issues / Deferred Hardening`，本冻结版不继续处理：

- Profile 生命周期并发状态机；
- Manager 重启后的自动恢复和孤儿 Chrome 识别；
- ProcessObserver、重复 PowerShell/CIM 扫描和监控开销；
- Default Page 与显式 Page ID 的统一锁；
- Page event postcondition 的跨 Page 归属；
- Basic Proxy UI、停止后的 pageCache、导航输入校验；
- 下载同名覆盖、下载哈希内存占用、截图文件名碰撞。

除非出现新的安全、数据隔离或明确严重缺陷，不开启下一修复阶段。

## 遗留风险

本单元没有修改生命周期状态机、Manager 重启恢复、进程探测失败语义、重复
PowerShell 扫描、ProcessObserver、Page Lock、UI 或其他审查项。

named mutex 阻止的是第二个 Manager；如果 Manager 异常退出但 Chrome 仍存活，
后续 Manager 的 Registry 与实际 Chrome 状态不一致问题仍未处理。Google Chrome
而非 bundled Chromium 的版本差异也尚未单独验收。

## 下一步

等待用户确认后，再进入下一个独立修复单元；不得将本单元扩大为全面重构。
