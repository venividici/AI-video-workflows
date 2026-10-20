---
name: video-workflow
description: "在 AI-video-workflows 仓库内执行 Codex + Blender MCP + Seedance 视频工作流。用于搭建或排查 Blender 连接、导入分镜、制作/修改白模预演、登记复杂 Blender 工程、批量渲染、检查镜头、准备生成素材及按既有授权提交视频任务。不用于仅讨论视频创意、普通代码任务或无关图片生成。"
---

# 视频制作工作流

先读取仓库根目录 `AGENTS.md`，遵守用户的创作约束与已授权范围。定位本技能上三级目录为仓库根目录；从该目录执行命令。

## 接手或恢复

1. 读取项目的 `project.json`、分镜来源与最近检查记录。先执行 `python -m aivw doctor --probe`，已有项目再执行 `status --project <目录>`。
2. 按任务只读取所需指南：
   - 环境、Windows/macOS/Linux、MCP 重启与连错实例：[SETUP.md](../../../docs/SETUP.md)。
   - 制作顺序、检查标准、阶段交接：[WORKFLOW.md](../../../docs/WORKFLOW.md)。
   - CSV/JSON/XLSX、原始行保留与列映射：[PROJECTS.md](../../../docs/PROJECTS.md)。
   - 场景结构、MCP 编辑、快照、渲染：[BLENDER.md](../../../docs/BLENDER.md)。
   - 手动素材包、供应商字段、API 回执恢复：[SEEDANCE.md](../../../docs/SEEDANCE.md)。
   - 原片裁切、帧数对齐、音频与最终初剪：[FINISHING.md](../../../docs/FINISHING.md)。
3. 用户要求执行时直接推进，不停在方案。缺项仅阻塞依赖它的阶段，先完成其他已授权工作。

## 执行核心

- 使用 `setup-mcp` 和 `launch-blender` 建立本地连接。验证 addon socket、MCP stdio 和当前 Codex 工具调用三个不同层次，分别记录；不得把其中一项冒充全部连通。
- 从现有分镜逐行导入。自行写作分镜时标明来自用户材料的部分与待确认的创作选择；不修改既有故事。原时长保存在源分镜，生成素材的长度单独安排。
- 在 Blender 中共享资产与场景，以 MCP 观察、执行 `bpy`、渲染和修正。相同空间跨镜头复用，不重复生成不一致的场景。
- 用声明式 `build` 重建简单布局，使用 `adopt-blend` 保存复杂工程。先确认当前场景所属项目，保护现有工作。
- 输出各镜头视频、接触表与串联预演。查看图像和运动后再记 `review`，说明具体证据；自动技术检查不等于视觉通过。
- 执行 `seedance prepare` 后，检查提示词与白模对象映射、输入时长、参考用途及裁切计划。按已有生成授权和额度调用 submit/poll/download；无授权时保持在已完成的离线交接包。
- 中断后读取输入哈希、渲染回执与任务 receipt，继续未完成部分。外部任务状态未知时先查询，避免重复提交。
- 检查生成原片后明确选择裁切起点，用 `conform` 对齐原分镜，再运行 `assemble-final`。保留原片，不用变速或补视频掩盖时长不足；逐镜查看结果与镜头衔接。

## 交付

交付编辑工程、镜头视频、完整预演、图像证据、检查记录和未解决事项。说明哪些工具在当前环境真实运行过；不要把未连接的本机、未调用的付费模型或未检查的镜头写成完成。
