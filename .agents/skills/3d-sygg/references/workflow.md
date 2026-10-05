> 版本说明：本文保留原 Omni/历史项目合同。schema 9 新增模型、独立关键帧、长片时长/转场/章节旁白字段以 [模型与长片能力](multi-model-long-video.md) 为准；Omni 原完整宫格输入仍按本文执行。

# 工作流字段

新项目 `schema_version=10`，声音路由见 [声音规则](voice-routing.md)。schema 8/9 的已批准声音方案保留。版本 7 保留已知任务恢复入口，不自动迁移批准内容，不允许旧项目重新付费。更早版本需单独核对旧账本，不应直接改版本号冒充兼容。

状态：`awaiting_plan_approval → awaiting_reference_approval → approved_for_generation → generating → awaiting_manual_review → delivered`。异常为 `failed` / `submission_unknown`。技术合格后仍待看图、观看与试听，不自动交付。

## 分析与方案

`analysis` 必填：`source_images`（1–6 个绝对路径）、`product_type`、`visible_features`、`materials`、`colors`（#RRGGBB 数组）、`uncertainties`。`logo_text` 可选。脚本将原图保存到项目私有 `private/source-images/`，冻结 SHA-256 和解码像素指纹，并把 analysis.source_images 指向持久快照。original_path 保留来源位置；临时剪贴板消失不影响后续生图。快照不在参考图登记目录或发布白名单内，只供生图与事实核对。

`plan` 必填：`big_idea`、`style_rationale`、`story_arc`、`audiovisual_tone`、`global_anchor`、`style`、`aspect_ratio`、`aspect_ratio_reason`、`palette`（primary/secondary/rim）、`ad_copy`（已批准的短标题/品牌字清单，数量按时长与阅读节奏决定）、`talent_strategy`、`clips`；`audio` 和 `narration` 省略时脚本填入视频原生声音默认值。风格枚举见 [视觉原则](visual-standards.md)。面向受众与使用场景的判断写入 Big Idea 与选择理由，不另加重复审批字段。

纯产品人物策略：

```json
{"mode":"pure_product","reason":"真实商品结构可以独立说明价值","framing":"none","interaction_actions":[]}
```

人物模式：`mode=human_interaction`、`adult_only=true`、`framing` 为 hands_only/partial_body/face_and_body，必填 `persona`、`wardrobe`、`grooming`、`identity_anchor`、非空 `interaction_actions`；需要出镜的段写 `talent_action`，不出镜的段留空。允许混合；至少一段确实出镜才使用人物模式。具体内容由商品和广告创意决定。

默认 `audio={"mode":"video","speech":true}`；`narration={"enabled":false,"text":"","reason":"视频模型输出声音"}` 表示不使用独立语音，不表示禁人声。台词与声音方向写在 `clips[].audio` 的 `speech_text/voice/music/ambience`，逐镜生成改用 `shots[].audio`。无台词留空，明确不要人声时 audio.speech=false。

独立语音仅在用户明确请求后使用：`audio.mode=external`、`audio.user_requested_external=true`（或 narration.user_requested=true）；`narration.enabled=true`，必填 text/voice_id/voice_name/reason。speed 可选 0.5–2；start_time/end_time 是全片开口和最晚结束秒数。音色必须从当前账号查询，实际音频须落在窗口内。

每段 `clips[]`：

| 字段 | 含义 |
|---|---|
| index | 从 1 连续编号；片段数 ceil(T/10) |
| narrative_role | 在全片中承担的独立叙事任务 |
| storyboard_reason | 为什么选当前格数及节奏 |
| shots | 1–4 个镜头；每镜 purpose/visual/action/product_detail/camera/transition |
| graphics（在 shot 内，可选） | text/icon/effect 元素列表，每项 kind/content/direction；文字精确匹配 ad_copy，direction 写材质、位置、空间关系与动态。不使用时省略或 [] |
| duration_weight（在 shot 内） | 正有限权重；由保留区间换算时间 |
| execution_reference_roles | storyboard 必须第一，按需加 product_master/talent，最多三张 |
| execution_reference_reason | 素材职责与选择理由，在首次确认前展示 |
| keep_duration | 可选；全部显式填写时每段 >0 且 ≤10，总和 T；否则均衡分配 |
| tail_margin | 可选；保留区间内的剪辑余量，默认 min(0.8, keep/2) 秒 |
| talent_action | 有出镜段写动作，无人物段留空；不会被全片人物策略强制填充 |
| visual_progression / camera | 导演总览，不能与逐镜设计冲突 |
| transition_in / transition_out | 进入状态与退出状态，服务跨段衔接 |
| sfx | 该段实际音效，不自动叠加固定鼓点套餐 |

成片秒数必须显式传入 `--target-duration`，支持正有限数值，包括小数秒；用户没有指定时由 AI 根据叙事优先提出整十秒时长并在首次方案确认，用户明确时长优先；这不是固定行业套餐。例如 7.5 秒一次、23.5 秒三次、37 秒四次、65 秒七次。所有上游请求仍为 10 秒。本地保留从 0 秒开始；例如 15 秒可由导演指定 9+6，省略时 8+7；20 秒 10+10；25 秒默认 9+8+8。不是延长上游生成或增加次数。1/4 格不需要额外许可标记，节奏由镜头权重决定。

`big_idea` 不只写品类卖点，要说明商品特征怎样成为可见的广告事件；`story_arc` 不预设微距/模特/定格三步。详见 [视觉导演原则](visual-standards.md)。

逐镜图形示意（非固定特效模板）：

```json
{"graphics":[{"kind":"text","content":"一眼入场","direction":"大号镀铬立体字在后景受压聚拢，沿画面弧线滑入前景后回弹让出商品；关键读字时正面稳定，不挡 Logo"},{"kind":"icon","content":"由已知轮廓提炼的抽象圆环","direction":"薄线图形沿转场方向扫过背景；装饰图形，不显示虚构认证和性能数字"}]}
```

脚本在 prepare 时校验结构及文字清单，时间轴保留 graphics。导演页、生图和视频从同一描述渲染，批准后改动图文动效会使原批准失效。没有 graphics 的已有方案兼容，但不会自动得到创意增强或被擅自加字。艺术质量仍由 AI 导演判断，不以词库数量判断。

## 图像登记

每个资产先放进当前项目 `references/`，只登记最终选定版本。以下是字段示意，实际格数由方案决定：

```json
{"references":[
  {"role":"product_master","path":"/absolute/project/references/product-master.png","origin":"codex_imagegen","derived_from_source_hashes":["全部冻结的原图哈希"],"identity_verified":true},
  {"role":"storyboard","clip_index":1,"path":"/absolute/project/references/storyboard-01.png","origin":"codex_imagegen","derived_from_product_master_sha256":"母版哈希","panel_count":2,"clean_for_video":true,"panel_order_verified":true,"distinct_panels_verified":true}
]}
```

人物模式再登记一张全局 `talent`，需要出镜的段分镜补 `derived_from_talent_sha256`。人物设定可由 Codex 生成；已批准用户模特图可登记 user_provided，但原商品图不行。不要为风格另生一张付费图，风格进入方案和分镜。

声明必须基于实际生图输入和目视核对；SHA-256 只能冻结声明及文件，不能证明生图过程或视觉效果。文件或解码像素完全相同会拦截；不凭低分辨率感知哈希判断“相似就无效”，不按等宽裁切猜各格内容。

`reference_sets` 根据批准角色顺序指向登记文件：storyboard 在执行清单中的角色名为 `storyboard_reference`。报告、公开白名单、images_url、位置绑定共用它，不手写另一份。每张原文件不变，格子比例不阻断视频。

## 批准与复核

方案哈希冻结分析、原图、视觉设计、图像提示、人物、时间、模型参数、素材角色和付费次数。参考确认再冻结登记文件、来源声明、视频提示词与执行清单。改动任何已批准内容都需重新展示；新方案使用新项目 ID，不覆盖历史付费产物。

`complete-review` 需要 `reviewer`、`notes` 和全部布尔项：

- storyboard_sequence_followed
- product_identity_preserved
- approved_copy_only
- no_collage_grid_or_unwanted_text
- visual_quality_professional
- audio_review_passed
- motion_and_transitions_coherent
- talent_identity_and_anatomy_preserved（纯产品无人物时检查未意外出现人物）

任一项失败就继续待复核。不得仅因抽帧或技术测试通过而虚构试听结论。

## 同项目自动恢复

`prepare` 为新计划补充 `recovery_policy: {"max_video_retries_per_clip": 1}`，支持整数 0–3，在两次审阅报告展示并随方案冻结。`paid_counts.omni` 是正常调用数，最大可能调用数为它乘以 `(1 + max_video_retries_per_clip)`。不因失败改变创意、图片或时长。

`produce` / `resume` 共用执行路径，成功片段校验后复用；未完成任务查询原 ID；明确终止失败且有额度才建立有父任务记录的替代任务。补提时可重新发布相同文件到新 URL，素材哈希、角色、顺序、参数与提示词必须保持一致。没有任何重新生图步骤。无恢复字段的旧批准按原查询权限处理，不擅自扩大费用授权。

## 全片交付与局部修订

常规失败重试仍使用原素材和原项目。改变某个片段的创意/素材时，`prepare --parent-project ORIGINAL --replace-clip INDEX` 创建绑定原片的修订记录，其局部 target-duration 等于原段 keep_duration。`plan.replacement_for` 由脚本写入并随确认冻结，不手填；包含原片路径、原段索引、原批准指纹、原全片规格与旁白。母版和身份兼容的人物图可从 `reference_asset_plan.reusable_assets` 复用。

修订完成生成后处于 `segment_ready`，记录 `artifacts.replacement_video`；程序登记原片 `clip_replacements`，自动恢复原片。原片总时长、其他段和整片旁白不变。替换素材与批准分镜按哈希关联；组装/最终观看使用修订后的镜头时间和分镜审阅页。没有绑定的独立 10 秒项目不视为 20 秒原片的完成凭据。

`assembly.json` 记录有序素材、原片起止时间、实际审阅分镜和旁白源。只给第二段、缺段、错序、源文件改变、启用旁白却缺失、局部项目冒充全片都会被阻止；完整文件通过技术检查后，仍需实际观看与试听。局部 `produce/resume` 的正常最终返回值为原片 manifest，正式 `complete-review` 在原片执行。
