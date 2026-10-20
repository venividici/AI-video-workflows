# Blender：从可编辑场景到逐镜预演

这套工具支持两种同样可自动执行的入口：**Codex 通过 Blender MCP 交互制作并保存真实工程**，或根据 `scene.json` 批量创建声明式场景。MCP 负责交互、当前场景访问和视觉反馈；`bpy` 是实际修改场景的 API；后台 Blender 负责可恢复的批量渲染。三者可以一起使用。

这里的自动化交付是 `.blend`、PNG 帧序列、逐镜 MP4、整片 MP4 和检查接触表。它不把能成功运行脚本等同于导演已经通过镜头，也不把简化人偶称为逼真表演。

## 1. 执行条件

核心 CLI 只用 Python 标准库。实际预演需要 Blender、FFmpeg 和 FFprobe，支持通过 `--blender`、`--ffmpeg`、`--ffprobe` 或 `BLENDER_BIN`、`FFMPEG_BIN`、`FFPROBE_BIN` 指定可执行文件。Blender 脚本由 Blender 自带 Python 执行，不需要向系统 Python 安装 `bpy`。

默认引擎为 Workbench，适合快速白模。**Workbench 仍依赖图形后端，不是原生 CPU 光线追踪引擎。** 支持 Mesa 的 Linux 环境可在 `render` 加 `--software-gl` 请求软件 OpenGL；这不保证每种服务器都具备所需驱动。无可用图形上下文时，用 `build --engine CYCLES --samples 8` 明确构建 CPU 版本。工具不会在失败后悄悄改引擎。

## 2. 交互制作：接纳真实 MCP 工程

先让 Codex 在已连接的 Blender 中完成场景、人物代理、机位和动画，检查实际预览，保存 `.blend`。复杂网格、修改器、骨骼和自定义材质不必压缩进本项目的简单几何 JSON。

```bash
python -m aivw adopt-blend --project projects/demo --shot s001 --input /path/to/authored.blend --note "调整构图并保留自定义角色绑定"
python -m aivw render --project projects/demo --shot s001 --blender /path/to/blender
```

`adopt-blend` 会实际启动 Blender，检查选定 Scene 的相机、帧率、帧区间和分辨率，再另存为 `build/s001.blend`。默认使用文件保存时的活动 Scene；需要时显式加 `--scene SceneName`。工具保留原几何、绑定、修改器和着色系统，只规范 PNG 交付格式并加制作元数据。

接纳时会把 Blender 支持的纹理等资源打包进快照，并将剩余引用固定为绝对路径。Blender 报告的外部文件、链接库、目录，以及图像/体积序列所在目录，会记录 SHA256；渲染前再次核对，外部资源改变或丢失会阻止复用。外部链接库并不会全部复制进项目，换机器时仍需迁移这些资源或在制作环境转为本地资产。第三方插件通过自定义属性/脚本读取的路径可能不出现在 Blender 的资源列表中；这类缓存须先烘焙、打包或显式转成 Blender 可识别的资源，不能仅凭 `.blend` 快照声称已覆盖。

图像序列、体积序列和外部缓存应放在独立素材目录；目录级记录会覆盖全部成员，添加文件也会改变指纹。不要把 `build/`、`renders/` 或接纳后的快照写入被跟踪的素材目录，以免新输出本身改变依赖。文件符号链接会核验当前目标内容；缓存目录内部的目录符号链接会被拒绝，应先整理为实际素材目录。

要求镜头范围为 `1..frames`、`fps_base=1`、分辨率比例为 100%，宽高均为正偶数。若故意采用小样分辨率，需显式加 `--allow-draft-resolution`，回执会记录。外部文件中的 Python 自动执行被禁用；依赖自定义启动脚本或 Python 驱动的工程，应先在受信任的 Blender 制作环境烘焙所需动画再接纳。

成功后，工具把对应布局记为：

```json
{"mode": "external_blend", "scene": "原始场次名称"}
```

这里的 `scene` 必须等于 `shots.json` 中的 `scene_id`；它不是 Blender 内部 Scene 的名称。旧 `.blend`、构建回执和原声明式布局会保存在 `build/history/<shot_id>/`。源码变更后，旧快照不会被悄悄复用；需要对照新分镜调整工程，再次 `adopt-blend`。运行 `build` 会保留有效外部快照，不会用简单几何替换它。

如果明确要将某镜从真实快照改回声明式构建，请先把该镜的 `scene.json` 布局改回完整声明式定义，再用 `build --force`。这种切换保留旧版本。

## 3. 声明式构建：重复资产与批量机位

`shots.json` 保存原始分镜、时长和行号；`scene.json` 保存实际可执行的空间布局。导入器不会凭一句画面描述自动生成通用代用资产。没有布局的镜头会被拒绝。

```bash
python -m aivw build --project examples/product-orbit --blender /path/to/blender
python -m aivw render --project examples/product-orbit --blender /path/to/blender
python -m aivw assemble --project examples/product-orbit
python -m aivw contact-sheet --project examples/product-orbit
```

`--shot ID` 可重复使用，只构建或渲染指定镜头。`assemble` 始终按完整分镜顺序拼接，不会悄悄跳过失败或缺失镜头。`build --resolution 320x180` 是显式草稿尺寸覆盖，不改变原帧率、时长和帧数，回执中保留该选择。

### scene.json 的内容

| 字段 | 用途 |
| --- | --- |
| `materials` | 材质 ID 到颜色和粗糙度的映射；`base_color` 是 RGBA，取值 0–1 |
| `objects` | 可复用物体列表，单位为米；类型支持 `box/sphere/cylinder/cone/plane/empty/actor/text` |
| `scenes` | 场次名称到物体 ID 列表和背景色的映射；场次名称可为中文 |
| `cameras` | 相机 ID 到位置、目标点、焦距及透视/正交设置的映射 |
| `shots` | 以规范化镜头 ID 为键，指定场次、相机、对象动画和相机关键帧 |

物体支持 `location`、`rotation_degrees`、`dimensions`、`scale`、`parent`、`material`。父子变换使用局部坐标；不允许父级循环。`plane` 的尺寸使用前两项，第三项仅保持通用三维格式。`actor` 是 1.75 米的几何人偶，供站位与镜头预演，使用 `scale` 调整体型，不提供自动步态或表情。

`text` 使用 Blender 默认字体；需要中文或指定字形时，应在 MCP 制作环境加载合适字体并通过快照接纳，不依赖默认字体覆盖所有字符。

对象动画每条控制一个 `location`、`rotation_degrees` 或 `scale` 通道，插值可为 `BEZIER`、`LINEAR`、`CONSTANT`；帧必须严格递增且位于镜头范围内。相机关键帧可设 `location`、`target`、`lens_mm`。相机位置与注视点不能重合。对象 ID、相机 ID 和镜头 ID 使用安全 ASCII 名称，原镜号继续保留在 `source` 中。

`target` 可以是三维坐标或场内物体 ID。当前实现将目标在各相机关键帧时的位置转换成相机朝向，并在关键帧之间插值，**不宣称是每帧精确的动态目标约束**；需要复杂跟随约束时，在 MCP 中制作后使用快照通道。

完整字段约束见 `schemas/scene.schema.json`。CLI 还检查引用存在性、父级循环、每镜布局对应关系和关键帧范围。

### 在 MCP 中调用同一个构建 API

Codex 可以在 Blender MCP 的 Python 执行工具里调用同一个函数。下面的代码是受信任的项目代码，不是从表格单元格执行任意 Python：

```python
import sys
from pathlib import Path
sys.path.insert(0, "/absolute/path/to/AI-video-workflows")
from aivw.blender import _load, make_bundle
from blender_scripts.scene_builder import build_shot

project, root, shots, scene = _load("/absolute/path/to/project")
shot = next(item for item in shots if item["id"] == "s001")
settings = {"fps": project["fps"], "resolution": project["resolution"],
            "engine": "WORKBENCH", "samples": 8, "draft_override": False}
bundle = make_bundle(project, root, shots, scene, shot, settings)
result = build_shot(bundle, root / project["paths"]["build"] / "s001.blend")
```

函数新建 `aivw_s001` Scene，保留无关 Scene；重复替换已有同名、由本工具创建的 Scene 需明确 `replace_scene=True`。保存标准 `<shot_id>.blend` 文件时也生成 CLI 能使用的构建回执。若随后继续通过 MCP 修改工程，应再次使用 `adopt-blend` 固定最终版本。

## 4. 帧数、恢复和过期缓存

片长由 `shots.json` 的 `frames` 与 `fps` 定义。`frames` 必须与 `duration_seconds × fps` 按十进制 ROUND_HALF_UP 的结果一致。无法确定时长的镜头不会被擅自赋值。

渲染先生成 `frames/frame_000001.png` 等图片，再编码逐镜 MP4。每次运行都核对当前源码、构建器、Blender 文件哈希、渲染器和设置。匹配时检查 PNG 文件头、尺寸、完整块及 CRC；已记录帧还要核对哈希。缺帧或损坏帧单独补渲；输入变更后清除该镜受管理的旧帧和视频缓存，再完整渲染，避免把两个版本混在一起。

默认每 24 帧启动一次 Blender 并写回完成记录，`--chunk-size` 可修改。编码后使用 FFprobe 实际解码计帧，必须精确匹配帧数、帧率和尺寸。整片拼接前会逐镜检查哈希和视频信息，缺镜、旧版本或混合尺寸都会阻止输出。

修改 `.blend` 文件不会自动获得可信回执。用 `adopt-blend` 接纳修改，或从已更新的声明式布局重新构建。引擎或分辨率变化也需要重新构建/接纳并渲染。

### 输出

| 路径 | 内容 |
| --- | --- |
| `build/<id>.blend` | 当前可编辑、已登记工程 |
| `build/<id>.build.json` | 来源、输入哈希、工程哈希、设置与版本 |
| `build/history/<id>/` | 被替换的已保存版本 |
| `renders/<id>/frames/` | 按顺序的 PNG 图片序列 |
| `renders/<id>/preview.mp4` | 精确帧数的无声逐镜预演 |
| `renders/<id>/render.json` | 每帧哈希、视频哈希、实际计帧结果 |
| `outputs/previs.mp4` | 完整分镜顺序的无声预演 |
| `outputs/previs.json` | 拼接来源与最终校验 |
| `outputs/contact-sheet.jpg` | 每镜首、中、尾帧及镜头 ID、帧号 |

接触表用于快速检查，不等于完整的运动审片；还要观看预演，检查穿插、视线、衔接和动作是否表达了原分镜。对白数据保留供后续声音制作，本阶段输出无声视频。

## 5. 验证

```bash
python -m unittest discover -s tests -p 'test_blender.py' -v
```

默认测试包含数据完整性、缺布局拒绝、精确时长、场景引用、父级循环、路径逃逸、损坏图片、过期构建、真实 FFmpeg 计帧。实际 Blender 测试需显式开启：

```bash
AIVW_RUN_BLENDER_TESTS=1 BLENDER_BIN=/path/to/blender python -m unittest discover -s tests -p 'test_blender.py' -v
```

端到端测试会在临时目录处理两个不同示例，生成和验证视频与接触表，再检查续渲、损坏帧修复、包含自定义网格/修改器的快照接纳、纹理打包后脱离原文件仍可渲染、链接库变更检测，以及源分镜变化后的拒绝复用。未设置环境变量时明确报告跳过，不会当作已经验证 Blender。

### 本仓库的实际运行记录

在 Linux x86_64 环境使用官方 Blender **4.5.3 LTS** 便携版（build `67807e1800cc`）及 FFmpeg/FFprobe **6.1.1**，已执行以下验证：

| 检查 | 实际结果 |
| --- | --- |
| Blender 专项测试 | 14 项通过，包括真实端到端测试及外部资源符号链接转向检测 |
| 自动化测试渲染 | Cycles CPU、2 samples、160×90；两项目 5 镜共 204 帧 |
| 仓库可直接查看的小样 | Workbench 软件 OpenGL、320×180；产品 84 帧 / 3.5 秒，双人 120 帧 / 5 秒 |
| 续渲与损坏帧 | 未变帧保持原文件；截断的 PNG 被单独重渲 |
| 复杂快照 | 自定义 torus、Bevel 修改器和链接库被保留并实际渲染 |
| 外部依赖 | 纹理打包后移除原图仍可渲染；外部链接库变化会阻止旧版本复用 |
| 原分镜变化 | 已接纳工程被标记需对照修改并重新接纳 |

可直接查看 [产品运镜示例](../examples/product-orbit/README.md) 与 [双人走位示例](../examples/dialogue-blocking/README.md)。两者均包含真实 MP4、首中尾帧接触表和来源哈希记录。这里只陈述 Blender、`bpy` 与后台渲染的测试结果；MCP 传输与本机会话接入的验证见项目总验证记录。

## 6. 技术依据与已知范围

- [Blender Python API](https://docs.blender.org/api/current/info_quickstart.html)：通过 Python 操作数据和动画。
- [Blender 命令行渲染](https://docs.blender.org/manual/en/latest/advanced/command_line/render.html)：后台调用与参数顺序。
- [Blender 动画输出](https://docs.blender.org/manual/en/latest/render/output/animation.html)：帧序列与后续编码。
- [原作者 MCP for Blender](https://github.com/ahujasid/mcp-for-blender)：交互连接、Python 执行及视觉反馈；属于社区项目。

工具提供可复现的制作与检查入口，不证明每个镜头已经达到创作要求。声明式示例仅覆盖简单空间和变换动画；高级角色表演、约束、模拟和复杂资产通过真实 Blender 快照接入。视频模型对空间、动作和形状的遵循程度，仍应单独制作小样评估。
