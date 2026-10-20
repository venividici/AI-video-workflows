# 来源与版本基线

核验日期：2026-10-20。代码行为以仓库内可执行实现及实测记录为准；供应商能力可能变化，升级时重新核对。

| 组件 | 本仓库采用的基线 | 原始来源 |
|---|---|---|
| Codex 项目规则 | 根目录 AGENTS.md | [OpenAI 官方说明](https://developers.openai.com/codex/guides/agents-md) |
| Codex skill | `.agents/skills` 仓库级发现 | [OpenAI 官方说明](https://developers.openai.com/codex/skills) |
| Codex MCP | STDIO、项目配置、启动/调用超时 | [OpenAI 官方说明](https://developers.openai.com/codex/mcp) |
| Blender MCP | `mcp-for-blender==2.2.1`，server 与 bundled addon 配套 | [上游社区仓库](https://github.com/ahujasid/mcp-for-blender)、[PyPI](https://pypi.org/project/mcp-for-blender/2.2.1/) |
| Blender | 实测 4.5.3 LTS；CLI/Blender Python | [Blender 下载](https://www.blender.org/download/)、[Python API](https://docs.blender.org/api/current/)、[命令行渲染](https://docs.blender.org/manual/en/latest/advanced/command_line/render.html) |
| uv/uvx | 固定上游包、项目内 addon 安装 | [官方安装文档](https://docs.astral.sh/uv/getting-started/installation/) |
| FFmpeg | PNG编码、精确帧数、裁切与初剪 | [官方文档](https://ffmpeg.org/documentation.html) |
| Seedance | 2.5 reference/edit 协议；模型 ID 由使用方配置 | [创建任务 API](https://console.volcengine.com/ark/region:cn-beijing/docs/create-video-generation-task-api)、[提示词指南](https://console.volcengine.com/ark/region:cn-beijing/docs/seedance-2-5-prompt-guide)、[使用指南](https://console.volcengine.com/ark/region:cn-beijing/docs/seedance-2-5) |

制作方法中的镜头分组、检查顺序、通用项目接口、回执与缓存策略是本仓库的工程设计；不代表上游保证生成结果具备电影质量或严格像素一致性。

Blender MCP 为社区集成，与 Blender Foundation 无隶属关系。运行时下载上游包，仓库未复制其完整源码；上游许可与声明由对应包保留。
