# Lumon Auto Test

在 Dashboard 的 **配置 → Auto Test** 保存 Jira 查询、排程和模型。排程默认关闭，默认间隔为五分钟。

在 **自动化 → Auto Test** 可立即执行并查看测试记录。打开一笔记录可查看用例统计、Excel、录屏及 Jira 发布状态，也可停止运行或重测。

## Jira 触发条件

在 **Settings → Jira** 中填写两个自动化功能共用的 Jira 站点和项目键。用 **Test connection** 检查 TWG 能否读取该项目。Auto Test 的触发查询在 **Settings → Auto Test** 中设置。

下列示例选择进行中的 Sprint 里处于 **Ready For QA** 且带有 Jira Flag 的卡片。

```jql
project = TEAM
AND sprint in openSprints()
AND status = "Ready For QA"
AND Flagged = Impediment
ORDER BY Rank ASC
```

手动运行和定时任务使用同一查询。每次按排序领取一张尚未执行的卡片。**重测**会检查原卡资格，符合条件时建立一轮新记录。

## 配置本机工具

1. 在运行 Lumon 的账号下登录 TWG，确认可以查询 Jira、读取卡片与评论、添加附件及创建和编辑自己的评论。
2. 准备 Playwright Chromium。源码环境使用 `uv run playwright install chromium`。通过 Shell 安装 Lumon 时，使用 `~/.local/share/lumon/venv/bin/python -m playwright install chromium`。
3. 测试 iOS 时，准备 Xcode、可用的 iPhone 模拟器、Appium 和 XCUITest 驱动。用 `appium driver install xcuitest` 安装驱动。
4. 向工作进程提供 `OPENAI_API_KEY`。手动运行读取 Dashboard 或 CLI 的环境，定时运行读取 macOS 用户 launchd 环境。所选 OpenAI 项目需允许配置的 Computer Use 模型。 [OpenAI Computer Use 文档](https://developers.openai.com/api/docs/guides/tools-computer-use)

## 构建和运行

Agent 从 Jira 验收条件出发，检查工作区已登记的储存库。首次读取某个储存库时，Lumon 固定其登记分支的最新提交。Agent 根据 README、项目文件和锁文件选择目标与用例。工作区可以通过 `lumon/flows/auto-test-case-generation.md` 或已启用的 `test-case-generation` Workflow 补充用例规则。

- Web 使用已登记的 npm、Yarn 或 pnpm 构建脚本与锁文件，或直接使用现有静态页面。Lumon 在临时副本构建，通过本机 HTTP 服务打开静态产物。
- iOS 依据共享 Xcode Scheme、配置和可用的 iPhone Simulator 构建，使用 Appium 操作新建的测试模拟器。React Native 依赖使用项目锁文件。有 Podfile 时使用锁定的 CocoaPods 依赖。
- 无法确定支持的构建目标时，该平台的用例会标记为阻塞。不同平台各自准备构建。

工作区本机配置 `~/.lumon/workspaces/<workspace-id>/config.toml` 中已有的 `[auto_test.web]` 和 `[auto_test.ios]` 仍可作为明确的构建覆盖。一般设置保存时会保留这些配置。

本机 Web 页面可直接测试。外部 QA 服务需要由工作区维护者在 `[auto_test]` 的 `qa_origins` 中核准 HTTPS origin。需要指定账号时，构建覆盖中的 `setup` 可以引用 `LUMON_QA_` 开头的环境变量。设置步骤在录屏前完成。需要核对后端提交的用例使用本机配置中的 `backends`。版本查询必须属于已核准的 QA origin，并返回完整的部署 Git SHA。

## 复核和补发

测试记录显示通过、可复现失败、待复核和阻塞。失败用例会在新浏览器或 Appium 会话中重跑。在 Evidence 中预览 Excel、截图和录屏，并下载原始文件。

Lumon 在原卡写一条本轮结果评论并附上产物。附件失败时可用 **重试 Jira 发布**补发，原评论会更新。Jira 未确认写入时，重试会先查阅原卡，避免重复评论或附件。停止后的证据仍可从本机下载。

执行记录和产物保存在工作区的 `lumon/runs/tests/<run-id>/`。

```bash
lumon test poll --workspace-id <workspace-id> --manual
lumon test poll --workspace-id <workspace-id> --retest <run-id>
lumon test stop --workspace-id <workspace-id> <run-id>
lumon test publish --workspace-id <workspace-id> <run-id>
```

启用排程前，在工作区的 QA 环境中各完成一轮 Web 和 iOS 手动测试。对照构建 SHA，并从原 Jira 卡打开 Excel 和录屏，确认 QA 可以复核结果。
