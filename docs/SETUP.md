# 接通 Codex 与 Blender

目录：运行位置 · 依赖 · 自动配置 · 连接验证 · Windows/macOS/Linux · 常见故障。

## 1. 选择运行位置

推荐在安装 Blender 的电脑上用本地 Codex 打开这个仓库。Codex、Blender、FFmpeg 能读取同一组项目文件，MCP 通过本机端口通信。保留用户选择的 Sol/Astra 等模型；本仓库不固定模型、不修改审批配置。

云端工作区可以整理分镜、准备脚本或执行自己的后台 Blender，但它的 `localhost` 无法直接连接用户电脑。远程机器需要用户已经建立的安全通道和共享路径；不要把一个远程连接错误诊断为“本机没有 Blender”。

仓库根 `AGENTS.md` 会在本地 Codex 接手仓库时提供规则。`.agents/skills/video-workflow` 是仓库技能，不需要复制到全局目录。MCP 配置生成到 `.codex/config.toml`，不提交个人绝对路径。

## 2. 发现依赖

```bash
python -m aivw doctor --probe
```

Windows 可把 `python` 换成 `py -3`。核心工具要求 Python 3.11+，使用标准库，无需安装一串 Python 包。其他依赖为 Blender、FFmpeg（含 ffprobe）和 uv/uvx；命令返回机器可读 JSON。

Blender 查找顺序：`--blender`、`BLENDER_BIN`、PATH、常见系统安装目录。FFmpeg 可在其相关命令使用 `--ffmpeg`/`--ffprobe` 或设置 `FFMPEG_BIN`/`FFPROBE_BIN`（支持情况见命令帮助）。`doctor` 只报告 API Key 是否存在，不显示内容。

找不到 Blender 时，Codex 先搜索用户已安装的版本，再根据操作系统从 [Blender 官方下载页](https://www.blender.org/download/) 获取便携版或官方安装器。核对官方校验值，选择项目目录之外的位置保存可执行程序。缺 uv 时用 [uv 官方安装指南](https://docs.astral.sh/uv/getting-started/installation/)；缺 FFmpeg 时用 [FFmpeg 下载页](https://ffmpeg.org/download.html) 指向的平台发行包。不得用来源不明的“整合安装包”。

这一步的具体安装方式由本地 Codex 根据现有环境执行；系统弹出的信任、网络或管理权限应按真实权限机制处理。不要为了安装修改审批策略。

## 3. 配置与启动

先查看将要执行的变更：

```bash
python -m aivw setup-mcp --install-addon
```

在用户已授权搭建环境时执行：

```bash
python -m aivw setup-mcp --apply --install-addon
python -m aivw launch-blender
```

配置器做四件事：

1. 使用发现的 uvx 绝对路径，固定 `mcp-for-blender==2.2.1`。
2. 向本仓库 `.codex/config.toml` 添加 Blender 条目，保留其他内容，并在修改已有文件前备份。
3. 调用上游官方项目提供的安装命令，把配套 addon 放到 `.runtime/blender-addons/`，记录 SHA-256。
4. 新启动的 Blender GUI 使用 `--factory-startup`，避免用户启动文件中的另一份同名 addon 重复注册；随后可打开指定 `--blend`，临时加载仓库 addon。不保存或改写用户全局插件偏好。

已有同名条目时不会覆盖。先验证旧连接；需要独立连接时选择 `--server-name blender_video --port 9877`，启动与探测也使用相同端口。现有 Blender 实例的端口若已正常响应，launcher 会复用它，不会强行关闭窗口；修改前必须确认当前工程属于哪个项目。

新实例不会自动启用用户偏好中的其他插件。依赖第三方插件的工程应先在已有适当实例中制作并烘焙，或由 Codex 明确加载所需插件后验证；不能把这类依赖当成已安装可用。

可显式指定程序或打开工程：

```bash
python -m aivw launch-blender --blender "/absolute/path/to/blender" --blend "projects/demo/build/s001.blend"
```

新加 MCP 配置通常要重开 Codex 会话，Codex 也可能要求信任该仓库。代理不能让正在运行的会话凭空加载新工具。先保存准备结果，再说明这一次必要的重开操作；恢复时继续下面的检查。

## 4. 三层检查，各自记录

| 检查 | 命令/动作 | 能证明什么 |
|---|---|---|
| addon 端口 | `python -m aivw mcp-probe` | 端口确实返回 Blender 场景，超出单纯 TCP 可达 |
| MCP 协议 | `python -m aivw mcp-probe --stdio` | 上游 server 能 initialize、列工具并读取 Blender |
| 当前 Codex 会话 | 在工具列表找到并调用 `get_scene_info` | 这次 Codex 会话确实能使用 Blender 工具 |

然后运行最小制作检查：

```bash
python -m aivw mcp-smoke --stdio --output .runtime/smoke-001.png --report .runtime/smoke-001.json
```

该检查创建独立临时 Scene，给立方体设两个位置关键帧、检查动画求值、用 Cycles CPU 渲染一张小 PNG，并清理自己创建的数据。不会保存或覆盖用户场景。输出文件必须使用新路径，避免误把上次图片当作成功。Codex 应实际查看图片。

MCP 端口只是插件的本机 socket，不能把 `http://localhost:9876` 填成 HTTP MCP 地址。Codex 使用的是 `uvx ... mcp-for-blender` 的 **STDIO** 服务，服务再连接 addon。

## 5. 平台差异

### Windows

优先让 Codex 与 Blender 都在 Windows 原生环境中运行。使用 PowerShell 时路径作为独立参数传递；配置器会正确转义 Windows 路径。GUI 启动的 Codex 可能没有继承刚修改的 PATH，故配置器写入 uvx 绝对路径。

WSL 的 localhost、Windows Blender 的端口和文件路径不一定同属一个环境。先选择统一的执行环境；不要把 `C:\...` 直接当 Linux 路径，或假设 Blender 能读取 `/home/...`。已有 WSL/Windows 转发方案时，把实际主机、文件路径和进程归属记录到项目说明。

### macOS

Blender 通常位于 `/Applications/Blender.app/Contents/MacOS/Blender`。`launch-blender` 自动检查常见位置。系统首次运行提示要由实际系统权限流程处理。

### Linux

MCP 需要 Blender 的图形界面事件循环。正常桌面下直接启动；无桌面的机器可使用 Xvfb 运行 GUI。`blender --background` 不提供该事件循环，不能当成 MCP 服务运行。

后台 `build` / `render` 不依赖 MCP GUI。Workbench 使用图形后端，不能称为纯 CPU 渲染；驱动不支持时选择 `--engine CYCLES` 的 CPU 路线。`--software-gl` 可用于有 Mesa 软件图形支持的环境，实际可用性以 smoke 结果为准。

## 6. 故障处理

| 症状 | 优先检查 | 继续方式 |
|---|---|---|
| `uvx` 在终端能用、Codex 找不到 | GUI 的 PATH 不同 | 使用 setup 自动写入绝对路径，重开会话 |
| 端口能连但没有正确 JSON | 端口属于其他程序或 Blender 主线程被占用 | 查看 `.runtime/logs`，确认实例；不要重复启动 |
| socket 通过、stdio 失败 | MCP 包依赖、版本、启动日志 | 使用固定包版本，确认 addon 与 server 匹配 |
| stdio 通过、Codex 没有工具 | 会话未重开、仓库不受信任、MCP 条目未启用 | 检查实际工具列表与 Codex 设置 |
| 切换文件后连接变化 | 另一个 addon/端口或原项目工程被打开 | 重新读取场景文件路径，按项目选实例 |
| 长渲染超时 | 把全部帧塞进一次 MCP 调用 | 保存工程，在独立后台 Blender 渲染并读日志 |
| Linux GUI 启动即退出 | 没有显示服务/驱动不支持 | 桌面或 Xvfb；独立批量预演用 Cycles CPU |
| 连接的是用户正在编辑的其他工程 | 端口复用了已有实例 | 先保留原工作，使用另一个端口/实例 |

固定版本是可复现基线，不代表永远最新。升级前检查 [MCP for Blender 原仓库](https://github.com/ahujasid/mcp-for-blender)，同时更新 server 与 addon，再跑三层检查和 smoke；不要只更新一侧。
