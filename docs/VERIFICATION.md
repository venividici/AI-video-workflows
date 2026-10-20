# 实际验证记录

验证日期：**2026-10-20**。本页区分真实软件运行、本地模拟接口和仍需使用方环境确认的能力。测试使用自拟通用素材，没有运行任何付费视频任务。

## 1. 环境与完整测试

| 项目 | 实际环境 |
|---|---|
| 系统 | Ubuntu 24.04.3，Linux x86_64 |
| Python | 3.12.14 |
| Blender | 官方便携版 4.5.3 LTS，build `67807e1800cc` |
| Blender 下载校验 | 官方 Linux x64 压缩包 SHA-256：`975c58fcb244273838534bba771e64ad87739216b0f9b39a888531a49a72d845` |
| FFmpeg / ffprobe | 6.1.1 |
| Blender MCP | PyPI `mcp-for-blender==2.2.1` 与 bundled addon |
| MCP addon SHA-256 | `05a6def0122681808afaad0ac1d0bd0f4f10bb54fe15946eb6839d1ba23c08b3` |

完整测试 **68 / 68 通过，无跳过，38.435 秒**。日志：[unittest.txt](verification/unittest.txt)。

```bash
AIVW_RUN_BLENDER_TESTS=1 BLENDER_BIN=/absolute/path/to/blender LIBGL_ALWAYS_SOFTWARE=1 python -m unittest discover -s tests -v
```

以上为 Bash 写法。PowerShell 先分别设置 `$env:AIVW_RUN_BLENDER_TESTS="1"`、`$env:BLENDER_BIN="C:\path\blender.exe"`，再执行 Python 命令。普通 `unittest` 命令仍可运行；没有启用真实 Blender 检查或缺 FFmpeg 时，对应检查会明确标为 skipped，不能宣称完整集成通过。

| 测试模块 | 项数 | 核验内容 |
|---|---:|---|
| Blender | 14 | 真实渲染、复杂工程快照、资源打包、外部依赖、损坏帧补渲、缓存失效、路径和时长 |
| MCP / 配置 | 13 | 分段 UTF-8 socket、真实子进程 STDIO fixture、错误处理、TOML 转义、配置保留与幂等性 |
| 分镜 | 13 | CSV/JSON/XLSX、多语言映射、重复编号、来源保留、时长量化、缺项和过期输入 |
| Seedance | 18 | 本地 HTTP mock：任务成功/失败/未知、避免重复提交、回执恢复、下载校验与费用边界 |
| 最终初剪 | 10 | 真实 FFmpeg：精确帧数、切点顺序、帧率转换、音频处理、原片保护和过期来源拒绝 |

## 2. 真实 Blender MCP 联通

在临时 Xvfb 显示上启动了真实 Blender GUI，使用测试端口 19876。配置与 addon 由本仓库命令生成；运行了真正的固定版本 MCP server，并执行 `initialize → tools/list → tools/call`。

- `get_scene_info` 读取到真实 Scene、Cube、Camera、Light。
- `execute_blender_code` 创建临时 Scene 和立方体，插入第 1 / 8 帧位置关键帧。
- 实际动画求值为 X = **-0.5 / 0.5**，再用 Cycles CPU 渲染 160×120 PNG。
- 恢复原场景并清理自己创建的数据；随后只读核验原对象、活动对象和帧位置，确认临时场景不存在。
- 初次检查发现非活动 Scene 的动画求值问题；已在测试中切换临时活动 Scene 并在 finally 中恢复，通过复验。

证据：[MCP 协议结果](verification/mcp-probe.json)、[制作与清理结果](verification/mcp-smoke.json)、[实际渲染图片](verification/mcp-smoke.png)。公开报告中的本机输出路径已替换为相对文件名。

还通过真实 MCP 调用了 `blender_scripts.scene_builder.build_shot`，创建产品样例的 `p001` 镜头并保存 `.blend` 和构建回执，再交给后台 Blender 渲染出 **48 帧、24 fps、2 秒**的视频。该检查核验了交互制作到批量渲染的实际交接，以及调用后用户 `save_version` 偏好恢复。证据：[交接报告](verification/mcp-roundtrip.json)、[实际视频](verification/mcp-roundtrip.mp4)。保存偏好的成功/异常两条路径随后通过了 14 项 Blender 专项复验（22.954 秒）。

MCP 握手的 `serverInfo.version` 返回 `1.30.0`，是服务框架自报值；安装包版本仍由固定的 PyPI 规格与 addon 回执确认，二者没有混为一谈。

**本次没有把 Blender 工具加载进当前聊天会话的工具列表，也没有连接使用方的个人电脑。** `codex_session_tools_verified` 保持 false。用户本地 Codex 打开仓库后，仍需按 SETUP 中第三层检查实际调用工具；这一步不能由另一个环境的 STDIO 检查替代。

## 3. 真实预演样例

真实 Blender 集成测试使用 Cycles CPU、2 samples、160×90，渲染两类示例共 5 镜、204 帧，再通过 FFmpeg 输出逐镜视频、合片和接触表。它还接入了带 Bevel 修改器的自定义 torus、外部链接库和图片材质，验证工程快照保留与资源变化检查。

仓库另外保存了便于直接查看的 320×180 Workbench 软件 OpenGL 小样：

| 示例 | 镜数 | fps | 总帧数 | 时长 | 证据 |
|---|---:|---:|---:|---:|---|
| 产品运镜 | 2 | 24 | 84 | 3.5 秒 | [视频与接触表](../examples/product-orbit/README.md) |
| 双人走位与反打 | 3 | 24 | 120 | 5 秒 | [视频与接触表](../examples/dialogue-blocking/README.md) |

各小样目录含源 JSON、渲染代码、视频与接触表的 SHA-256。实际查看过首/中/尾帧；双人样例修正过反打机位。人偶是走位代理，没有真实步态、口型或配音，这些样例不代表最终表演质量。

## 4. 泛化与最终初剪

另用一份气象气球题材分镜测试导入：不同列名、中文场景、重复源镜号 A/A/B、0.5 / 1.25 / 0.0625 秒短镜，分别得到 12 / 30 / 2 帧；原始文件哈希保持不变。缺少时长、没有布局和缺少实际预演会在依赖它们的阶段拒绝处理。

还把仓库 skill 交给一个没有继承开发过程的执行者，要求制作新的“蓝球在红方块左侧、灰地面、相机向右移动”镜头。它自行建立项目、检查与修正地面亮度、完成 **72 帧 / 3 秒**预演、审阅记录和离线 Seedance 包。全部帧投影边界在画幅内，相机沿 X 方向移动，最终状态为 1 镜构建/渲染 current、Codex review pass。它通过三页连续检查图实际查看全部 72 个解码帧，没有进行实时播放器观看。

该执行者如实识别了当前会话没有 Blender 工具，采用仓库后台构建路径，同时完成 addon 和 CLI STDIO 复检；没有把后台制作冒称为当前会话 MCP 工具调用。制作期间代码维护触发输入哈希变化，旧构建被拒绝后，用固定工具副本重新构建了全部帧。证据：[独立接手报告](verification/independent-trial.json)、[实际视频](verification/independent-trial.mp4)、[接触表](verification/independent-trial.jpg)。

最终初剪测试使用真实 FFmpeg 自制红/蓝短片，36 + 48 = **84 帧**；解码像素核对镜头顺序，解码 PCM 核对有声与静音段。24→30 fps 后时长保持，原片不足时拒绝。还验证了 `seedance prepare → download --file → conform` 的无 API 路径。

## 5. 自动检查与实测边界

`.github/workflows/verify.yml` 在 push / pull request 时运行 Linux 与 Windows Python 检查；真实 Blender 下载与渲染任务通过 `workflow_dispatch` 手动触发，使用上述固定校验值。CI 的实际运行状态以 GitHub Actions 页面为准，本地通过不代表所有远端作业已经运行。

已真实运行的是 Linux 上的 Blender GUI / 后台渲染、MCP STDIO 和 FFmpeg。本次未在 macOS 或 Windows 桌面上运行 Blender GUI，也没有验证使用方的显卡、插件组合或本地 Codex 重开行为。

Seedance API 的付费提交、真实模型输出、账号权限、素材可访问地址和人物授权未实测。API 测试使用本地回环 mock 与假 Key；离线包和手动结果导入已实测。使用方首次调用真实平台时，应先在既定授权额度内做单镜验证。

复杂工程支持 Blender 能识别和记录的资源。第三方插件在脚本或自定义属性中隐藏的路径、未烘焙模拟及任意外部服务，不在自动资源检查保证内；按 `BLENDER.md` 先整理或烘焙。
