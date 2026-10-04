# 模型兼容与长广告（schema 9）

## 能力依据与边界

查询日期：2026-09-29。这里区分官方模型说明、服务商接口约束、当前通道实片验收。三个层次不互相替代。

| 模型 / 服务商通道 | 每次合法请求 | 源分辨率 | 素材上限（图/视频/音频） | 分镜依据 | 当前自动生产 |
|---|---|---|---|---|---|
| Omni / wxart `omni-flash` | 固定 10 秒 | 720p | 本工作流 3/0/0 | 官方视觉故事板 | 保留完整 1–4 格与单镜流程 |
| Omni / 沧元 `omni-fast-no-water` | 保留 10 秒合同 | 720p | 本工作流 3/0/0 | 相同设计，保留原服务商适配 | 原安全备援流程 |
| Seedance 2.0 / 沧元 `sd11-seedance-2.0` | 整数 4–15 秒 | 480p / 720p / 1080p | 9/3/3 | 分镜脚本图＋人物/场景/道具图 | 待当前通道实片验证 |
| Seedance 2.5 / 沧元 `sd11-seedance-2.5` | 整数 4–30 秒 | 480p / 720p / 1080p | 30/10/10 | 宫格剧情参考、独立有序关键帧 | 待当前通道实片验证 |
| MiniMax H3 / 沧元 `mm2-minimax-h3` | 整数 4–15 秒 | `768P` / `2K`（大小写敏感） | 9/3/3 | 图片可指定对应分镜、视角、位置、顺序 | 待当前通道实片验证 |

默认成片 720p。H3 用 768P 源输出 720p；明确指定 1080p 时用 2K 源输出 1080p。Omni 本通道没有已验证的更高规格，明确报不支持，不能用放大冒充。480p 虽在服务商列表中，本 Skill 不自动降到该规格。成片画幅继续支持 9:16 和 16:9；表中素材总上限不代表所有混合组合已实测。

当前执行器支持图片故事板、独立图片序列、首尾帧，以及每镜分别生成；接续选用末帧。视频/音频参考的上限作为能力记录保留，**本版不开放它们的上传执行与原生 Extend**，避免把文档上限当成已验证实现。未来独立适配器可扩展官方接口，不把中转字段直接发送给官方接口。

来源：

- [Omni 官方故事板提示指南](https://deepmind.google/models/gemini-omni/prompt-guide/)：视觉故事板、左上起始阅读、10 秒故事示例。
- [Seedance 2.0 官方发布说明](https://seed.bytedance.com/en/blog/seedance-2-0-official-launch)：分镜脚本图及角色、场景、道具参考。
- [Seedance 2.5 官方指南](https://docs.volcengine.com/docs/ark/seedance-2-5-prompt-guide?lang=zh)：宫格主要提供剧情参考，严格构图/节点优先独立有序关键帧；不把这个差异解释成取消宫格。
- [H3 官方参考指南](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md)：逐镜说明时点、构图、动作、位置与声音；图片可以是故事板。
- 沧元具体字段与约束：[2.0](https://ai.cangyuansuanli.cn/docs/models/sd11-seedance-2.0)、[2.5](https://ai.cangyuansuanli.cn/docs/models/sd11-seedance-2.5)、[H3](https://ai.cangyuansuanli.cn/docs/models/mm2-minimax-h3)。POST `/v1/videos`；GET `/v1/videos/{task_id}`。
- [MiniMax 语音文档](https://platform.minimaxi.com/docs/api-reference/speech-t2a-http.md)：长文稿应分段；本工作流采用较保守的 2500 字符自然章节。

## 设置模型

在 Skill 根目录执行：

```bash
python3 scripts/commercial_ad.py capabilities
python3 scripts/commercial_ad.py configure --model sd11-seedance-2.5
python3 scripts/commercial_ad.py check-channel --model sd11-seedance-2.5
python3 scripts/commercial_ad.py configure --model omni
```

设置写入被 Git 忽略的 `config/config.local.json`，不存 Key。也可在 `prepare` 使用 `--model`、`--strategy`、`--output-resolution` 覆盖这一次。项目确认后冻结配置，不受后续默认配置变化影响。只读通道检查只用环境变量/钥匙串并 GET 模型目录，不迁移凭据、不生成、不证明画质。默认配置缺省仍是 Omni。

## 四种分镜执行方式

- `full_storyboard`：上传原始完整宫格为 Image1，按稳定镜头编号、宫格阅读顺序与时间执行；保留 Omni 三宫格优势。格数由故事决定，仍 1–4 格，不为素材额度堆宫格。
- `ordered_keyframes`：每镜直接生成一张独立关键帧，依镜头顺序上传，再附母版和适用人物图。参考登记角色 `keyframe`，填 `clip_index` 与 `shot_id`。图像提示词来自 `clips[].frame_prompts[shot_id]`。
- `first_last`：按 `frame_prompts.start_frame/end_frame` 直接生成首尾状态。角色为 `start_frame`、`end_frame`。沧元字段 `first_image_url` / `last_image_url`，其余母版/人物进入 `reference_image_urls`。不套用官方互斥模式到中转。
- `per_shot`：保留导演原镜头动作/图文，把一组镜头展开成若干单镜请求并统一剪辑。镜头编号不变，生成次数和生图数量随实际展开结果在首次确认时展示。

独立帧须 `origin=codex_imagegen`、`directly_generated=true`、`identity_verified=true`、`clean_for_video=true`，及原来的母版/人物来源哈希。不得裁切批准宫格冒充直接生成。首尾帧模式中，若已计划前段末帧接续，则不额外生成本段首帧。所有镜头保留商品品牌与广告图文；禁止宫格边框、静态翻页、无依据删 Logo。

自动策略仅使用该通道已合格记录。新通道先以 `--validation-run` 建立单独验证计划，并提供 `plan.budget` 金额上限；仍需原方案/参考图两次确认，prepare 不调用模型。实片完整 `complete-review` 后，把该项目绝对路径加入下一计划 `qualification_projects`；系统复用相同通道和能力版本下的合格记录，校验批准与成片未被改动。单格故事板只证明单画面执行，可用于 `per_shot` 或单格 `full_storyboard`；两格及以上样片通过后，复用为本工作流 2–4 格的多宫格能力。不要求每个格数、合法时长、分辨率和画幅重新做付费验证。参数是否合法仍在请求前检查，实际成片照常观看试听，能力复用不代表每个组合均已实测。自动改用逐镜路线时，新增图片与视频调用次数在首次方案确认前计入，批准后不擅自改策略。连续接续还须成功项目中包含对应依赖镜头。普通用户制作无需选择技术策略。

## 时长与时间线

`--target-duration` 是用户指定值，优先于计划；没有时 AI 填 `plan.target_duration` 和 `duration_reason`。不存在固定成片分钟上限。存储、预算和服务可用性仍需计划。不能因请求最多 30 秒就把简单镜头强行拉到 30 秒。

`plan.clips` 由 AI 先按整片故事设计；脚本不会复制镜头来凑长片。每段必须落在通道上限内，超过时 AI 沿稳定镜头编号继续切段后重新 prepare。最终每秒 30 帧，无法精确表示的小数取最近一帧，并同时保留 requested_duration。`clips[].keep_duration` 可显式设置；全片帧数 = 保留帧总和 − 各段 `overlap_before` 重叠帧总和。非 Omni 可设 `trim_start`，请求时长会涵盖被剪区间；Omni 保持原动作从零开始，禁止非零 trim_start。

每段保留 `entry_state`、`exit_state`（未填时从首镜画面/末镜动作取值）、场景光线、同一人物/服装和商品母版。前段末帧接续用 `continuity_from: 前段编号`，只能依赖紧邻前段，首尾帧策略，顺序执行。实际末帧取自前段**保留区间末端**，不是模型生成文件多余尾部。遇到提示后 AI 检查实际图片，创建 review JSON 再执行：

```bash
python3 scripts/commercial_ad.py review-continuity --project outputs/PROJECT --review continuity-review.json
python3 scripts/commercial_ad.py resume --project outputs/PROJECT
```

review 包含 `clip_index`、待检查文件的 `sha256`、`reviewer`、三个真实检查布尔值 `identity_preserved/action_state_correct/no_grid_or_text_artifacts`。替换前段后需要同时检查新的末帧与原后段视频，并填 `successor_sha256`。这是 AI 素材质检，不新增用户确认步骤。未通过就不传播；需要新付费素材时沿现有局部修订确认流程处理。

后期支持切镜和叠化，叠化使用 `overlap_before`，按帧准确计入总时长，不整体变速。归一化和转场本地缓存绑定源文件/参数哈希；本地导出失败只重做本地操作，不重付费。新版源片的单帧边界短缺（约 0.033 秒）在既有转码中延续末帧补齐，不能用此方式填充实质缺失的动作。最终利用已有媒体读取检查视频帧数和视频时长，容许整片一帧误差，音频封装尾差单独处理；不为数帧再全片解码一次。schema 7/8 保留原后期与验收容差。广告镜头源缺少计划音效轨时停止复核，不用静音掩盖。

## 长旁白与预算

旁白使用原 `start_time/end_time` 全片窗口；长稿或需章节停顿时使用 `narration.chapters`：每项 `text`、`start_time`（相对旁白窗口开始）、`max_duration`，最多 2500 字符。章节文本拼起来必须与全文一致，不能删稿；章节不能重叠或超窗，同一音色与语速。即使只有一个带停顿的章节也按章节时间执行。每章只提交一次，已有音频按哈希复用，局部拼接失败不重合成。

```json
{
  "budget": {
    "max_video_attempts": 6,
    "max_narration_attempts": 2,
    "max_cost_cny": 120,
    "video_call_upper_cny": 18,
    "narration_call_upper_cny": 3,
    "quote_checked_at": "填写实际核价日期及依据"
  }
}
```

以上金额仅展示字段，不是当前报价。每次提交前账本加锁检查总次数/金额上界，失败或被拒尝试也保守占用金额估算；实际账单以服务商为准。未提供金额上限的原流程只限制次数，不能声称金额已受控。首次未验证策略必须有金额上限。生图在第一次方案中列出 `minimum_imagegen_calls` 和可能修正次数，原生生图服务的费用不混入视频接口账本；若用户给整项目金额上限，AI 必须先预留生图费用，再把剩余视频/旁白额度填 budget。

## 历史项目与局部修订

schema 8 不自动迁移、不扩大原批准额度；schema 7 保持原查询恢复模式。局部修订仍绑定 parent-project/replace-clip，单个父生成段用单个修订请求替换，修订目标保持父段保留时长。父项目若使用 per_shot，替换对应已展开镜头编号。不能创建多请求子项目后只取第一段。后续连续依赖段会被标记重新核对，完成后仍返回完整广告与原旁白。

没有“一换模型每次必然同画质”的保证。保证的是相同设计、素材身份约束、验收标准和不合格处理；文档支持、任务成功、技术可播放、真实效果合格分别记录。

## 日常制作与开发验证分开

`tests/run_offline.py`、模型对照和长片压力测试属于开发维护，不是每条广告的前置步骤。AI 在内部选择方法和核对参数，用户仍只确认方案与实际参考图。合格历史样片由 AI 填入 `qualification_projects` 复用，不让用户收集技术字段。首次接入只测试实际需要的执行方式；已有可用方式就先生产，不要求凑齐四策略矩阵。

成片检查合并为一次观看试听；抽帧是辅助，不额外要求逐帧人工签字。商品身份错误、关键镜头/文案缺失、明显声音或解码问题要处理；细微构图差异或不影响表达的自然动作变化不作为失败。局部导出、音频封装和可恢复失败由原有流程处理；需要新增费用或改变已批准创意时才返回用户确认。
