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

## 当前冻结维护修复单元：Manager 重启孤儿 Chrome fail-safe（2026-08-24）

本单元只处理 Manager 重启后 Registry 与真实 Chrome Profile 进程不一致的
安全边界，不实现自动恢复、自动杀进程或 CDP 接管：

- Manager 启动时对每个 Profile 做严格的 `--user-data-dir` 进程探测；发现仍有
  对应 Chrome，或探测本身失败时，记录 `status=error`、
  `last_error=recovery_required:*` 和可诊断的进程信息；
- `start(profile_id)` 在启动任何 Chrome 前再次做严格预检；发现 live Chrome
  或探测失败即拒绝启动；
- Registry 中的 `running`、`starting`、`stopping` 只有在确认没有对应 live
  Chrome 时才按原逻辑归一为 `stopped`；`stopped + live Chrome` 也会进入
  `error/recovery_required`，不会静默允许重复启动；
- 保持普通 stopped/no-process 的启动路径和现有 API 结构不变；保留默认进程
  查询函数的既有行为，不在本单元改造全局 Monitor/process-probe 语义。

验证：新增 5 个孤儿/预检决策单元测试；可靠性专项测试 10/10 通过；全量
unit tests 15/15 通过；未启动服务、Chrome 或网络，未提交 commit。真实
Windows Chrome/Chromium Manager 重启验收仍待执行。

## recovery_required 闭环补丁（2026-08-24）

Windows 实机验收发现：orphan Chrome 清理后，Registry 中的
`error + recovery_required` 会永久阻止 `start()`。本补丁保持 fail-safe 语义，
但把 recovery 状态解释为“需要重新确认的当前状态”：

- Manager `_load()` 或 `start()` 对 recovery Profile 重新执行严格
  `user-data-dir` 进程探测；
- 仍有 live Chrome 时继续保持 `error/recovery_required`，更新实际
  `process_ids`，不自动 kill 或 connect/recover；
- 严格探测成功且为空时，清空旧 PID、root PID、CDP endpoint、窗口句柄和
  target 元数据，恢复为 `stopped`，随后允许普通 start；
- 严格探测失败时保持 fail-safe，并记录明确的
  `recovery_required:process_probe_failed`。

根因修复单元测试覆盖 6 个场景；可靠性专项测试 16/16 通过；全量 unit tests
21/21 通过；`git diff --check` 通过。

## Fix Unit 1 第二次 Windows 实机验收（2026-08-24）

使用全新临时 data-dir、Profile-10、Profile-100 和 Playwright Chromium
151.0.7922.34，仅访问本地 ready page，未使用个人账号、Cookie 或真实网站。

通过项目目标范围内的验收：

- Manager 被强制结束后，Profile-10 Chrome root PID 仍存活；
- 重启 Manager 后 live Profile 被标记为
  `error/recovery_required:live_profile_processes`，实际 `process_ids` 被记录；
- 再次 start 被拒绝，未产生第二套同 `user-data-dir` root Chrome；
- Profile-100 可独立启动，两个 Profile 的 PID 集合不交叉；
- 人工结束 Profile-10 orphan 后，严格探测为空，Profile-100 仍继续运行；
- 不修改 Registry 重启 Manager 后，Profile-10 reconciliation 为完整的
  `stopped` 状态并清空旧运行元数据；
- Profile-10 重新启动成功，产生不同于旧 orphan 的新 root PID；
- 两个 Profile 的进程识别保持精确隔离。

因此，Fix Unit 1 按重新界定的范围满足关闭条件：检测 orphan、禁止重复启动、
fail-safe、人工清理后的严格空探测 reconciliation，以及恢复正常 start。

明确不属于本单元关闭条件的行为：Manager 重启后仍存活的 Chrome 属于 orphan；
新 Manager 不自动 CDP reconnect、不自动接管、不自动 kill。此时只能进入
`recovery_required`，用户人工关闭 orphan 后，严格探测确认为空才恢复 `stopped`。
新 Manager 直接 `stop()` orphan 当前不支持，不作为 Fix Unit 1 blocker。

## Fix Unit 2 checkpoint：per-profile lifecycle serialization（2026-08-24）

在保持 Fix Unit 1 `recovery_required`、orphan 和严格进程匹配语义不变的前提下，
ProfileManager 已为每个 `profile_id` 增加独立的 `asyncio.Lock`：

- 同一 Profile 的 `start()`、`stop()`、`restart()` 串行执行；
- `restart()` 使用内部 unlocked helper，避免持锁后重复获取同一把锁造成死锁；
- 不同 Profile 使用不同锁，生命周期操作保持并行；
- `start_many()`、`stop_all()` 继续通过公开生命周期入口，保持 per-profile 锁语义；
- recovery Profile 在并发调用下仍不能启动。

验证结果：reliability tests 22/22 通过；全量 unit tests 27/27 通过；
`git diff --check` 通过。Windows Chromium 实机并发验收全部通过：同 Profile
并发 start/start、start/stop、restart/start、stop/stop、不同 Profile 并行启动、
recovery_required 并发保护均无死锁、无重复 root Chrome；Monitor 未纳入
lifecycle lock，但本轮未观察到状态覆盖、假 `browser_process_disappeared` 或
无法恢复的 Registry/PID 不一致。

完整 lifecycle 状态机、Monitor tri-state、ProcessObserver 和其他架构加固仍为
Deferred；Fix Unit 2 不扩大为这些后续工作。

## Fix Unit 3 checkpoint：process probe error semantics（2026-08-24）

在保持 Fix Unit 1 的 `recovery_required` / orphan 语义和 Fix Unit 2 的
per-profile lifecycle lock 不变的前提下，进程探测现在明确区分三种结果：

- `found`：探测成功且发现目标 Profile 的 Chrome 进程；
- `absent`：探测成功且确认没有目标 Chrome 进程；
- `error`：PowerShell/CIM、超时、JSON 解析或其他探测本身失败。

`error` 不再等价于 `absent`。Monitor、`status()`、`start()`、`stop()` 和
recovery reconciliation 均使用 fail-safe 语义：只有明确的 `absent` 才能作为
“没有目标 Chrome”的证据；探测失败会保留当前安全边界、记录
`process_probe_failed` 诊断，并阻止可能造成误启动或误停止的决策。stop 期间
探测失败也不会写成 `stopped`；Registry 中的整数 `process_ids` 会安全保留为
可诊断的 `remaining_processes`。

验证结果：reliability tests 29/29 通过；全量 unit tests 34/34 通过；
`git diff --check` 通过。Windows Chromium 实机验收通过：正常 found/absent、
running Monitor probe failure、stop probe failure、orphan recovery probe failure、
live orphan re-confirmation、人工清理后的 successful absent reconciliation、
重新启动和最终 stop 均符合验收标准；未观察到第二个 root Chrome，也未观察到
Fix Unit 1 / Fix Unit 2 回归。Chromium 子进程 PID 集合允许动态变化，验收以
root PID、精确 Profile 路径匹配和生命周期状态不变量为准。

HTTP API 的部分错误状态码与 Profile `status=error` 返回语义仍属于 Deferred，
本单元不做 API 状态码整体重构。

## v0.1 冻结维护状态

公开仓库 v0.1 定位为本地人工操作冻结版：本地运行、多 Profile 隔离、人工打开
和操作浏览器、本地管理。当前不继续向长期自动化 Runtime、Agent、无人值守
浏览器、任务系统或远程服务方向开发。

当前状态：`maintenance / frozen`。

除安全、数据隔离和明确严重缺陷外，不继续功能开发或架构开发。已完成的 data-dir
单实例锁和 `--user-data-dir` 精确进程匹配属于本冻结版允许的基础安全与隔离维护。

以下审计项保留为 `Known Issues / Deferred Hardening`，本冻结版不继续处理：

- 完整 Profile 生命周期状态机；当前已完成 per-profile lifecycle serialization；
- Manager 重启后的自动 CDP reconnect、孤儿 Chrome 的自动接管或自动终止；当前
  仅支持 fail-safe 检测、`recovery_required` 和人工清理后的 reconciliation；
- ProcessObserver、重复 PowerShell/CIM 扫描和监控开销；
- Default Page 与显式 Page ID 的统一锁；
- Page event postcondition 的跨 Page 归属；
- Basic Proxy UI、停止后的 pageCache、导航输入校验；
- 下载同名覆盖、下载哈希内存占用、截图文件名碰撞。
- Manager 重启后对仍存活 orphan 的直接 `stop()`；
- lifecycle HTTP 200 但返回 Profile `status=error` 的 API 语义统一。

除非出现新的安全、数据隔离或明确严重缺陷，不开启下一修复阶段。

## 遗留风险

本单元没有实现完整生命周期状态机、Manager 自动恢复、重复 PowerShell 扫描、
ProcessObserver、Page Lock、UI 或其他审查项。Monitor 没有纳入 lifecycle lock，
但其进程探测现在能区分 `found`、`absent` 和 `error`；本轮实机未观察到它干扰
lifecycle。

当前 fail-safe 不会终止或接管 orphan Chrome；用户仍需人工处理 orphan，随后
由 Manager 通过新的严格空探测解除 recovery。若 Manager 重启时其他 Profile
也仍有 live Chrome，这些 Profile 同样会成为 orphan；新 Manager 不会为其建立
runtime，因此直接 `stop()` 可能返回 HTTP 200 但 Profile 状态为 `error` 且仍有
`profile_processes_remain`。这属于明确限制和 Deferred API 语义问题。

启动时对所有 Profile 的同步严格探测可能增加 Manager 启动延迟；PowerShell/CIM
真实失败会保守地阻止该 Profile 启动。

## 下一步

等待用户确认后，再进入下一个独立修复单元；不得将本单元扩大为全面重构。
