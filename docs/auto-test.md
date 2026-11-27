# Lumon Auto Test

在 Dashboard 的 **Settings → Auto Test** 管理排程。排程默认关闭，默认间隔为五分钟。每轮测试使用 **Agent** 中的模型和推理强度。

在 **自动化 → Auto Test** 可立即执行并查看测试记录。打开一笔记录可查看用例统计、Excel、录屏及 Jira 发布状态，也可停止运行或重测。

## Jira 触发条件

在 **Settings → Jira** 中填写 Jira 站点和项目键，再用 **Test connection** 检查 TWG 能否读取该项目。在 **Settings → Auto Test** 保存本工作区的触发 JQL。工作区的站点、项目键、查询和服务地址应保存在本机配置中。

手动运行和定时任务使用同一查询。每次按排序领取一张尚未执行的卡片。**重测**会检查原卡资格，符合条件时建立一轮新记录。

## 配置本机工具

1. 在运行 Lumon 的账号下登录 TWG，确认可以查询 Jira、读取卡片与评论、添加附件及创建和编辑自己的评论。
2. 启动 Docker，提前拉取与 Lumon 的 Playwright 主版本和次版本匹配的 `mcr.microsoft.com/playwright:v<主版本>.<次版本>.0-noble` 镜像。测试启动时读取本机镜像。
3. 安装 Codex CLI。Auto Test 使用 Agent 已登录的 Codex 配置。如需登录，在本机运行 `lumon agent codex-login`。

## 确认环境后运行

Agent 根据 Jira 验收条件、评论及可读取的 HTML 和 Excel 参考附件设计用例。Lumon 在首次读取仓库时固定登记分支的提交。Agent 查阅该版本的 README、构建脚本、配置和锁文件，先识别 `test`、`staging`、`development` 或 `uat`，再选构建目标。环境为生产、证据不足或配置冲突时，该平台的用例标为阻塞。

- Web 在临时副本中离线构建，需要预先缓存工具链和依赖。macOS 使用系统沙盒，Linux 需要支持独立网络命名空间。现有静态页面可以作为本机测试或开发目标。
- Chromium 在禁用网络的容器中运行。Lumon 只转发到本轮静态页面和所选环境已核准 origin 的 HTTP 请求。请求或重定向超出范围时会阻塞该用例。需要 WebSocket 的用例也会阻塞。
- iOS 必须具备模拟器网络隔离才能运行。目前 iOS 用例会在构建和启动模拟器前标为阻塞。

维护者在本机配置的 `[auto_test.qa_environments]` 下按环境核准外部服务的 HTTPS origin。填写前须确认服务属于该非生产环境。同一 origin 只属于一个环境。旧版 `qa_origins` 需移到对应环境后才会生效。缺少核准地址时，`staging` 和 `uat` 会阻塞，`test` 和 `development` 可运行纯本机用例。

```toml
[auto_test.qa_environments]
staging = ["https://staging.example.test"]
```

本机配置可通过 `[auto_test.web]` 指定构建，使用 `environment` 明确指定非生产环境。`setup` 可引用 `LUMON_QA_` 开头的环境变量，在录屏前登录测试账号。需要核对后端提交的用例通过 `backends` 查询部署版本。版本查询必须在所选环境核准的 HTTPS origin 内，并返回与固定提交一致的完整 Git SHA。

工作区可用 `lumon/flows/auto-test-case-generation.md` 或已启用的 `test-case-generation` Workflow 补充用例规则。

## 复核和补发

测试记录显示通过、可复现失败、待复核和阻塞。失败用例会在新浏览器会话重跑。QA 可在 Evidence 查看截图及录屏，并下载含测试环境和构建版本的 Excel。

验收条件缺少关键决定时，Agent 提出具体问题，Lumon 暂停剩余用例并保留已有证据。Lumon 通过 TWG 在原卡写入并确认问题评论，然后查询可用的 Jira 转态。存在可用的 Block 转态时，Lumon 执行并回读确认。Jira 需要额外字段时，评论仍会保留，Dashboard 显示转态失败的原因。

有人回复问题且卡片重新符合触发 JQL 后，Agent 重新阅读评论。每个待确认问题都需要新回复中的证据。仍有未解问题时继续等待，答复齐全后再进入环境检查。

Lumon 在原卡写一条本轮结果评论并附上产物。附件失败时可用 **重试 Jira 发布**补发，原评论会更新。Jira 未确认写入时，重试会先查阅原卡，避免重复评论或附件。停止后的证据仍可从本机下载。

执行记录和产物保存在工作区的 `lumon/runs/tests/<run-id>/`。

```bash
lumon test poll --workspace-id <workspace-id> --manual
lumon test poll --workspace-id <workspace-id> --issue-key <issue-key>
lumon test poll --workspace-id <workspace-id> --retest <run-id>
lumon test stop --workspace-id <workspace-id> <run-id>
lumon test publish --workspace-id <workspace-id> <run-id>
```

`--issue-key` 试跑已配置 Jira 项目内的一张卡。试跑结果保存在本机，关键验收问题可按上述流程回写原卡。已在等待答复的卡片仍须重新符合触发 JQL。

启用排程前，在已核准的环境完成一轮手动测试。对照构建 SHA，并从原 Jira 卡打开 Excel 和录屏，确认 QA 可以复核结果。
