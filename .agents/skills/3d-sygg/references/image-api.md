# 图片 API 接入与恢复

供执行 Skill 的 AI 在安装配置或使用外部图片模型时读取。用户只需提供该服务的文档与安全保存的 Key；AI 从文档核对地址、模型 ID、参考图输入、尺寸、返回格式，再完成配置。默认没有图片配置时继续使用 Codex 内置工具。

## 安装时选择

- 内置：`python3 scripts/image_api.py configure --preset builtin`。无需图片 API Key，`check` 只报告不需要 Key；AI 另外核对内置工具可用性。
- OpenAI 风格：纯文字请求发 JSON 到 `/images/generations`；有参考图时把全部本地图片作为 multipart 文件发送到 `/images/edits`。预设字段 `image[]`，以中转站实际文档为准。
- Seedream 风格：文字与图片均发 JSON 到 `/images/generations`，参考图作为 `image` 数组，每张是 data URL（图片原始字节的 base64 表达，不裁切或重编码）；预设只返回一张非流式图片。
- 其他 JSON 或 multipart 服务：复制示例配置，修改字段、路径和返回映射。异步服务配置任务 ID、GET 查询路径和完成/失败状态；每次 `resume` 查询一次，未完成时等待合理间隔再查询。原生 Responses、聊天流式生图、签名鉴权或只接收公网 URL 的接口需要单独适配，不能仅换模型 ID 就判定能用。普通商品订单不为此临时公开原图。

示例是协议模板，里面的模型名称须替换为服务账号真实 ID。Image2.5 是用户中转服务的名称示例，不是本 Skill 声明已验证的官方模型。Seedream 5.0 应核对具体版本（如 5.0 lite）、地区与部署 ID，不硬编码旧型号。

```bash
# AI 从该站文档填地址、尺寸和实际模型 ID，不把密钥放进命令。
python3 scripts/image_api.py configure --preset openai --base-url https://YOUR-SERVICE/v1 --model ACTUAL-MODEL-ID --size 1536x1024 --documentation https://YOUR-SERVICE/docs/images
python3 scripts/image_api.py configure --preset seedream --base-url https://ark.cn-beijing.volces.com/api/v3 --model ACTUAL-SEEDREAM-ID --size 2048x2048 --documentation https://docs.volcengine.com/docs/ark/image-generation-api
# 需要自定义字段或异步任务时：由 AI 根据真实文档编辑本地配置后载入。
python3 scripts/image_api.py configure --profile /absolute/path/image-profile.json
python3 scripts/image_api.py check
```

预设示例见 `config/image-openai.example.json` 和 `config/image-seedream.example.json`；异步模板见 `config/image-async.example.json`。不要直接使用占位模型和假示例地址提交。`base_url` 包含版本前缀，路径只写后缀；不会自动补 `/v1`。图片尺寸由 AI 为本次参考图选择并配置，须符合实际模型文档；宫格整图比例不强制等于视频比例。

本地配置 `config/image.local.json` 不进 Git 或发行包。`extra_body` 只能放非敏感生成参数；模型、提示词、尺寸与参考图字段由脚本填写。`model_field` / `prompt_field` / `size_field` / `image_field` 可配置；JSON 支持如 `request.images` 的点路径，multipart 使用文档规定的字段名。返回配置 `b64_path` / `url_path` 使用如 `data.0.b64_json` 的点路径。

`auth_header` 默认 `Authorization`，`auth_prefix` 默认 `Bearer `；使用 `X-API-Key` 等头时把前缀设为空。没有可配置的明文密钥字段，不将账号 Key 放入 `extra_body`、URL、日志或项目。只接受 HTTPS（本地测试允许 localhost HTTP）；API 和图片下载的跳转都不自动跟随，CDN 下载不携带 API Key。

## 密钥与安装检查

默认读取 `IMAGE_API_KEY`，可在配置里指定其他**环境变量名**。macOS 可显式运行 `python3 scripts/image_api.py set-key`，隐藏输入保存到按服务隔离的钥匙串。Linux 使用环境变量。生产不会问 Key。显式设置 `SYGG_IMAGE_KEY_SOURCE` 可沿用项目已有 macOS 本地文件迁移机制；只读取用户指定文件，不扫描其他私人目录。

`check` 仅验证配置与执行环境是否安全读到 Key，不联网，不检查余额，不代表模型生成成功。先检查实际服务文档是否支持足够数量的参考图、所选尺寸与响应格式。超出参考图数量会停止，不丢弃照片。首次效果验证是单独的付费任务：先展示真实素材、费用与调用上限，取得对应授权。

资料依据（2026-10-09 查询）：[OpenAI 图片编辑接口](https://developers.openai.com/api/reference/resources/images/methods/edit)、[火山引擎 Seedream 图片接口](https://docs.volcengine.com/docs/ark/image-generation-api)。中转站始终以用户提供的该站文档为准，这两个链接不能替代该站协议。

## 广告制作中的图片生成

`prepare` 将当前图片配置、调用上限保存在 `reference_asset_plan`，纳入原有方案确认。服务、模型、尺寸、参数、文档依据和每资产次数在该项目冻结，后来 `configure` 只影响新项目。老项目没有该字段时仍走原内置流程，不修改批准记录。

方案必须展示：图片服务商与模型、图片数量、最多调用数、当前费用（或明确金额未核对）、原商品照片直接传给谁。`max_attempts_per_asset` 为 1 或 2，默认 2：一张候选，加一次明确错误的针对性修正；不自动重试。可写 `unit_cost_upper_bound` 与 `price_checked_at` 展示当前同币种费用上界。无报价不能当作免费。

AI 使用冻结提示词，先实际查看输入图片，再按生成顺序执行。外部图片不会自动成为视觉验收通过：商品母版要先与原图核对，人物图也需检查，随后才给分镜用。原商品照片仅用于图片服务输入，禁止登记为视频资产。

```bash
# 在 approve-plan 后，生成商品母版；每张冻结原图都要作为 --input 传入。
python3 scripts/image_api.py generate --project outputs/PROJECT --asset product_master --input /absolute/path/source-1.png --input /absolute/path/source-2.png
# 如方案有人物，生成设定图（脚本使用 talent_reference_prompt）。
python3 scripts/image_api.py generate --project outputs/PROJECT --asset talent
# AI 先看图，再用实际已生成的母版与该片段适用人物图生成分镜。
python3 scripts/image_api.py generate --project outputs/PROJECT --asset storyboard:1 --input /absolute/path/generated-master.png
# 人物出镜片段重复 --input 指定人物图；纯商品片段不传人物图。
# 独立关键帧使用 keyframe:片段号:shot_id；首尾帧使用 start_frame:片段号 / end_frame:片段号。
```

脚本只输出状态、实际文件路径、哈希（用于确认文件是否改变）、尺寸与 `receipt`。AI 按既有 `register-references` 清单写 `origin=external_image_api`、`generation_receipt=receipt绝对路径`，并填写实际输入关系：母版 `derived_from_source_hashes` 为全部原图哈希；分镜/关键帧 `derived_from_product_master_sha256` 与适用的 `derived_from_talent_sha256` 为真正传入的图片哈希。原有 `identity_verified` 等字段仍必须来自目视核对。不得把 API 图片声明成 Codex 图片，也不能伪造通过标记。

每张图片的原始响应缓存位于项目私有 `requests/image-tasks.json`；该文件可能含签名下载链接，不分享。单独的生成回执在 `requests/images/`。登记时检查回执与调用账本、已批准模型/方案、实际图片字节和声明输入一致；原有商品身份、像素重复、分镜顺序与人物检查继续执行。旧的内置清单不需要新增回执。

## 中断与修正

```bash
# 原任务查询或结果下载失败：恢复只查询/下载，不发新 POST，不再收费。
python3 scripts/image_api.py resume --project outputs/PROJECT --asset product_master --input /absolute/path/source-1.png --input /absolute/path/source-2.png
# 唯一针对性修正：必须已看见明确问题、处于原批准的次数内。
python3 scripts/image_api.py generate --project outputs/PROJECT --asset product_master --input /absolute/path/source-1.png --input /absolute/path/source-2.png --retry-reason "已目视核对：商品标签错误，按原提示词重做一次"
```

已完成结果优先复用；下载失败恢复原响应，异步查询恢复原任务 ID。POST 前先落账；断线、超时、服务 5xx 或回复无法解析时记 `submission_unknown`，停止所有新图片提交。明确的 HTTP 拒绝不会自动重试；改配置需要新方案。无法确认是否创建任务时联系服务商查原请求，不能为了继续运行删除账本。

`--retry-reason` 只允许修正已完成的错误图或明确失败任务，最多次数来自原方案；提示词与输入须保持原批准内容。需要改创意、换模型/尺寸、改变参考素材或扩大费用次数时重新准备并确认方案。完成分镜后继续原有“展示实际参考图 → 确认参考图并生成视频”，没有新增日常确认阶段。
