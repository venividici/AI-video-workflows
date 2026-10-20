# Seedance：从 Blender 预演到手动交接或 API 任务

这套适配器实现了四个命令：`prepare`、`submit`、`poll`、`download`。默认使用离线手动包。只有显式执行带收费确认和任务数量上限的 `submit`，才会创建供应商任务。

已实现的协议是 **Seedance 2.5 的全模态参考生成 / 视频编辑**。模型 ID 由操作员从自己的账号中复制，代码不猜测模型 ID。测试通过本地 HTTP mock 和真实 MP4 文件完成，没有调用付费 Seedance 生成；真实账户开通、素材权限、生成质量仍需在自己的账号验证。

## 1. 先得到可核验的预演

项目中每个待处理镜头应有：

- `shots.json` 中的稳定 `id`、`frames`、`duration_seconds`。
- `renders/<shot_id>/preview.mp4`，帧数和帧率应与项目完全一致。
- 已经审阅的场景、人物站位和运镜；尚未布置的镜头不会自动创建通用场景来冒充预演。

`prepare` 读取真实预演文件，保存其 SHA-256，并使用 `ffprobe` 核对帧数、帧率和视频信息。受管理的渲染流程需要 `ffprobe`；显式提供的外来预演在缺少 `ffprobe` 时仍能制作手动包，但包会标记为不具备 API 提交条件。安装 FFmpeg 后重新准备即可。

默认还会沿用 Blender 的构建/渲染凭据校验，确认当前镜头文字、布局和构建内容对应这个预演。只有帧数相同而内容已变的旧视频也会被拒绝。提交前会再次核对受管理预演的来源，不能在准备后修改 scene 然后误提交旧视频。

如果你明确要使用其他工具产出的外来预演，把它放到对应 `renders/<shot_id>/preview.mp4`，再显式加 `--allow-unmanaged-preview`。此路径记录为 `unmanaged-explicit`，只验证实际视频与镜头时长，不声称校验了布局/构建来源。已有构建或渲染凭据的项目仍按受管理流程检查，标志不会掩盖陈旧凭据。

Blender 与 Seedance 的交接文件是 MP4/MOV 视频和参考图。当前 API 输入合同没有 `.blend`、相机矩阵、深度控制或法线控制接口。白模参考能传达空间与运动，实际生成结果仍需审片。

## 2. 无 API 的完整手动流程

以镜头 `s001` 为例，在仓库根目录执行：

```bash
python -m aivw seedance prepare --project ./my-project --shot s001 --take take001
```

输出目录位于项目的 `outputs/seedance/s001/take001/`。如果项目重定义了 `paths.outputs`，使用相应位置。

| 文件 | 用途 |
| --- | --- |
| `preview.mp4` | 原始预演的完整副本，保留原始字节 |
| `reference_padded.mp4` | 仅在显式补时长时出现；保持尾帧形成的参考视频 |
| `prompt.txt` | 可以复制到生成界面的逐镜提示词 |
| `references.json` | 参考图、视频、音频的顺序、用途及本地/远端映射 |
| `references/` | 操作员明确提供的本地参考素材副本 |
| `timing.json` | 原镜头帧数、目标剪辑时长、生成时长、补帧记录与裁切区间 |
| `request.json` | 对应的真实 API 字段；未配置项使用明确占位符 |
| `manifest.json` | 输入文件哈希、原镜头记录哈希、生成参数及 API 待办 |
| `receipt.json` | 从 prepared 开始的任务回执；不会伪造任务 ID |
| `HANDOFF.md` | 可以交给操作员的步骤说明 |

在所用界面中选择参考生成或视频编辑，上传该包中的视频和参考素材，粘贴提示词。操作员需核对素材编号：**白模预演固定为视频 1**，额外参考视频从视频 2 开始，图片和音频分别从 1 开始编号。

在界面下载生成结果后，可离线登记原片：

```bash
python -m aivw seedance download --project ./my-project --shot s001 --take take001 --file ./downloads/seedance-original.mp4
```

该命令不使用 API Key、不访问网络。它将原始文件复制到 `result/original.mp4` 或 `result/original.mov`，核对文件大小和容器，有 `ffprobe` 时再核对媒体信息，记录哈希。它不会自动剪辑、变速、去除或替换原始文件。

## 3. 补充参考图、提示词和素材映射

新建自己的 `references-map.json`：

```json
{
  "schema_version": "1.0",
  "shots": {
    "s001": {
      "prompt": "参考视频1的运镜和道具运动。图片1是产品的材质与外观参考。保持产品数量与接触关系。完成此单个镜头，不额外增加剧情。",
      "references": [
        {
          "type": "image",
          "name": "product-appearance",
          "local_path": "references/product.png",
          "usage": "图片1：产品外观和材质，对应视频1中央的蓝色立方体"
        }
      ]
    }
  }
}
```

`local_path` 相对于 **references-map.json 所在目录**，也可使用明确提供的绝对路径。仅支持与类型相符的常用图像、MP4/MOV、WAV/MP3 格式。映射文件不包含可执行代码。

```bash
python -m aivw seedance prepare --project ./my-project --shot s001 --take take001 --reference-manifest ./references-map.json
```

只有仍处于 `prepared` 的 take 可以重新准备；旧手动包会移到同镜头的 `_prepared_archive/`。已经提交、状态未知、已失败或已导入原片的 take 均保留历史，不被覆盖。新的有意尝试应使用 `--take take002`。

额外视频/音频参考需要 `duration_seconds`，用来核对总参考时长。额外本地视频若有 `ffprobe` 会优先使用实测时长。对于只能提供远端 URL 的素材，时长是操作员声明，工具不会下载远端参考来替你核验。

## 4. reference 和 edit 的区别

| 项目 | `reference` | `edit` |
| --- | --- | --- |
| 目的 | 参考白模的运动、空间、构图，生成新视频 | 编辑已有白模视频，或将其重渲染 |
| API 字段 | `omni_reference_task_type="reference"` | `omni_reference_task_type="edit"` |
| 时长参数 | 整数 4–30 秒 | 固定 `duration=-1`，基本跟随输入 |
| 画幅参数 | 可选标准画幅或 adaptive | 固定 `ratio="adaptive"` |
| 输入预演长度 | 2–30 秒 | 4–30 秒 |
| 默认输出格式 | MP4 | MOV |
| 控制边界 | 语义/运动参考，不保证逐帧绑定 | 保留原视频时间和画幅的编辑模式，也需检查漂移 |

在 `project.json` 中可以设置：

```json
{
  "provider": {
    "name": "seedance",
    "mode": "manual",
    "profile": "seedance-2.5",
    "model": null,
    "task_type": "reference",
    "resolution": "720p",
    "ratio": "16:9",
    "generate_audio": false,
    "watermark": true
  }
}
```

以上只展示 provider 字段，须并入完整项目配置。`model` 保持 `null` 时手动包仍可用；需要 API 时改为本账号实际启用的 Seedance 2.5 模型或 endpoint ID。`mode` 是默认使用方式，程序不会因为写了 `api` 就自动提交；收费行为始终由显式 `submit` 控制。

这版适配器没有实现首尾帧、延长视频、Draft 转正式视频或可信人像资产授权。它不会把 Seedance 2.0 / 1.x 的参数伪装成 2.5 参数。首帧/首尾帧与全模态参考是互斥 API 场景，映射中的 `first_frame` / `last_frame` 会被拒绝。

### 短镜头和原始剪辑节奏

生成长度与成片镜头长度分别记录。比如原镜头为 24 fps 下的 24 帧（1 秒），原记录保持不变。

- `reference` 默认请求至少 4 秒；若参考预演不足 2 秒，手动包会标记需要补足输入。
- `edit` 预演少于 4 秒时默认拒绝，避免暗中改变时长。
- 使用 `--allow-padding` 明确授权补足尾帧。原始 `preview.mp4` 不变，另存 `reference_padded.mp4`。
- `timing.json` 明确记录补帧数量，裁切计划保留从第 0 帧开始的原镜头帧数。最终取舍需结合生成结果审片，不能因为计划写了 1 秒就假定动作一定在第 1 秒完成。

```bash
python -m aivw seedance prepare --project ./my-project --shot s001 --task-type edit --allow-padding
```

镜头超过 30 秒时要求显式拆分，程序不会自行删剧情或加速。生成输出的当前文档按 24 fps 计算，项目若使用其他帧率，应在后期明确对齐，不要把 API 输入支持 24–60 fps 理解成输出可任意指定 60 fps。

## 5. API 前最后一次本地准备

所有素材上传均由操作员选择方式。此工具不会默认将素材传到公开存储，也不申请或猜测素材授权。

先完成手动包，把实际参考视频上传到你选择的存储或可信素材库；然后在映射中增加：

```json
{
  "shots": {
    "s001": {
      "preview_url": "https://your-authorized-storage.example/s001-reference.mp4",
      "preview_sha256": "从manifest.json的preview_reference.sha256复制实际64位哈希",
      "references": [
        {
          "type": "image",
          "name": "product-appearance",
          "local_path": "references/product.png",
          "url": "https://your-authorized-storage.example/product.png",
          "usage": "图片1对应视频1中央的产品白模，参考外观和材质"
        }
      ]
    }
  }
}
```

支持 HTTPS URL 和平台 `asset://<ASSET_ID>` 引用。这里的哈希是**操作员确认该 URL 指向此参考文件的声明**；工具只校验本地字节和该声明是否一致，`remote_bytes_verified` 始终为 false，不伪称验证了远端内容。额外素材的远端对应关系同样由操作员核对。

重新准备同一未提交 take：

```bash
python -m aivw seedance prepare --project ./my-project --shot s001 --take take001 --model YOUR_ACTUAL_ENABLED_MODEL_ID --reference-manifest ./references-map.json
```

再次检查 `manifest.json` 的 `api_ready` 和 `api_unresolved`。`request.json` 的任何占位符应通过重新准备解决，不直接手改请求绕过哈希校验。

## 6. 显式提交与费用边界

在操作系统环境或凭据管理器中配置 `ARK_API_KEY`。不要把密钥写进项目 JSON、提示词、GitHub 或命令文本。

```bash
python -m aivw seedance submit --project ./my-project --shot s001 --take take001 --confirm-paid --max-tasks 1 --max-generated-seconds 4
```

`--max-tasks` 是本次调用最多新建的收费任务数；`--max-generated-seconds` 是本次调用计划生成时长的可选上限。这两个约束**不是金额上限**。本工具不承诺固定价格、不读取账户余额、不锁定供应商计费费率。

提交前将一次性检查所有选中镜头的包、原镜头记录、原始预演和包内素材哈希，以及数量/时长上限。任何一项未通过，都不会开始这批请求。网络操作必须显式选择 `--shot`，不会隐含提交整部作品。

默认 API base 是 `https://ark.cn-beijing.volces.com/api/v3`。BytePlus 可显式使用 `https://ark.ap-southeast.bytepluses.com/api/v3`；模型 ID、账户权限和计费以相应平台为准。环境密钥只发往这两个官方主机。`--allow-local-http` 仅允许本机回环测试地址，不允许向任意 HTTP 服务发送密钥。

## 7. 回执、防重复提交和中断恢复

每个 take 是一份独立的任务历史：

1. 提交前先持久化 `submitting`。
2. 收到合法任务 ID 后记录为 `submitted`。
3. 请求超时、响应损坏、服务端错误等无法确认是否已受理的情况记为 `unknown`。
4. `unknown` / `submitting` 不会再次 POST。中断后重新执行 submit 也不会造成自动重复收费。
5. 4xx 明确拒绝会保留 `submission_rejected`；它也不会自动重试。
6. 每个 take 有本地互斥锁，避免两个进程同时提交。进程异常退出留下锁时，先核实没有操作在运行，再处理旧锁。即使移除旧锁，回执的 `submitting` 状态仍会阻止盲目重发。

查询已知任务：

```bash
python -m aivw seedance poll --project ./my-project --shot s001 --take take001
```

每次执行只查询一次，状态仍在 queued/running 时稍后再查。查询失败不会删除原任务 ID，也不会转成 POST。

如果提交结果是 unknown，应在供应商控制台按提交时间、素材和提示词找到该任务，确认对应关系后恢复：

```bash
python -m aivw seedance poll --project ./my-project --shot s001 --take take001 --adopt-task-id YOUR_VERIFIED_TASK_ID --confirm-task-match
```

找不到任务不等于供应商没有受理。先核对控制台和账单，再决定是否显式建立新 take。工具不会替你自动重复提交。

已提交 take 的包内输入快照必须完整，但项目之后更新源镜头或重新渲染，不会阻止旧任务的查询和下载。再次提交则会核对当前源预演，避免把旧审批对应到新内容。

## 8. 下载、保留原片和后续剪辑

```bash
python -m aivw seedance download --project ./my-project --shot s001 --take take001
```

下载只在任务 succeeded 后进行。下载请求不携带 `ARK_API_KEY`，会检查 Content-Type、大小上限、Content-Length、MP4/MOV 容器签名，有 `ffprobe` 时检查媒体流。默认最大 512 MiB，可用 `--max-mib` 调整。HTML/JSON 错误页面、截断文件和超限响应不会被保存为正式原片。

成功后保留供应商原始字节并记录哈希。重复 download 会核对已经保存的文件后返回，不覆盖原片。如果下载原片短于原镜头目标时长，会明确记录警告，不自动变速掩盖问题。容器/媒体信息验证不能代替逐帧视觉审查。

最终剪辑仍应检查人物身份、物体数量、空间拓扑、动作接触、时间顺序和前后镜头衔接。文字、关键道具几何、精确接触和音频混音可以在后期使用可控方式完成。

本仓库提供本地对齐和直切初剪命令。观看原片后明确裁切起点，再对所有镜头逐个处理：

```bash
python -m aivw conform --project ./my-project --shot s001 --take take001 --start-seconds 0
python -m aivw assemble-final --project ./my-project
```

`conform` 保留原片，把选定区间转换成项目 fps、分辨率和原分镜精确帧数；原片不足时拒绝变速或补视频帧。`assemble-final` 需要全部镜头的有效片段，按源顺序输出 `outputs/final.mp4`，并保存逐镜时间线。默认保留对应音轨，无音源添加静音轨，所有处理写入回执。详见 [原片对齐与最终直切初剪](FINISHING.md)。

## 9. 当前公开能力与素材限制

核验日期：2026-10-20。

- 官方已提供 Seedance 2.5 白模参考/渲染、视频编辑和最长 30 秒生成能力；白模视频应明确其运动/构图用途，并写清与角色、场景参考的对应关系。
- 当前 2.5 输出可选 480p、720p、1080p。不要把 2.0 的 4K 选项或旧版的 `seed` / `camera_fixed` 参数直接搬入 2.5。
- 当前 2.5 全模态输入上限为 30 张图片、10 段视频、10 段音频；视频总时长和音频总时长分别不超过 30 秒。白模预演占用一个视频名额。
- 当前官方 API 对含真人人脸的直接参考上传有限制，可使用平台支持的原始生成产物、预置数字角色或已授权真人人像资产。即梦界面、火山、BytePlus 的入口和账户权限应分别核对，不能把一个入口的规则当作所有入口都一致。

正式参考：

- [Seedance 2.5 官方产品页](https://seed.bytedance.com/en/seedance2_5)
- [创建视频生成任务 API](https://console.volcengine.com/ark/region:cn-beijing/docs/create-video-generation-task-api)
- [Seedance 2.5 提示词指南](https://console.volcengine.com/ark/region:cn-beijing/docs/82379/2607689?lang=zh)
- [BytePlus API 文档](https://ai.byteplus.com/ark/region:ap-southeast-1/docs/create-video-generation-task-api)
- [上传真人人像素材](https://docs.volcengine.com/docs/ark/upload-real-person-portrait-assets?lang=zh)

`providers/seedance.json` 是版本明确的能力说明，不包含密钥，也不包含假造的模型 ID。官方接口后续变化时，应先重新核验协议，再修改适配器与对应测试。

## 10. 本地验证范围

```bash
python -m unittest discover -s tests -p test_seedance.py -v
```

测试用 FFmpeg 生成通用无人物视频，使用本机 HTTP mock 验证提交、查询、下载、幂等、并发防重、超时未知状态、失败回执、请求哈希与时长限制。只有本机回环网络，没有外部/付费请求。严格禁止本机 socket 的沙箱需要允许 loopback 才能运行 HTTP 测试；这不需要供应商账号或真实密钥。
