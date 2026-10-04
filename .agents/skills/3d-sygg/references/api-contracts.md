> 版本说明：本文保留原 Omni/历史项目合同。schema 9 新增模型、独立关键帧、长片时长/转场/章节旁白字段以 [模型与长片能力](multi-model-long-video.md) 为准；Omni 原完整宫格输入仍按本文执行。

# API、凭据与可恢复性

## 自动凭据

正常工作不索取 Key。默认 Omni 依次读取 `WXART_OMNI_API_KEY` / `WXART_API_KEY` / `OMNI_API_KEY`；降级服务商读取 `CANGYUAN_API_KEY` / `CANGYUAN_OMNI_API_KEY`；MiniMax 读取 `MINIMAX_API_KEY`。其次读 macOS 钥匙串，服务名分别是 `3d-sygg-omni`、`3d-sygg-cangyuan`、`3d-sygg-minimax`，账号 `api-key`。开源版不搜索作者私有目录；仅在显式指定本地来源时一次性迁移至 macOS 钥匙串，写入后回读核对。`SYGG_OMNI_KEY_SOURCE` / `SYGG_CANGYUAN_KEY_SOURCE` / `SYGG_MINIMAX_KEY_SOURCE` 只允许放本地来源路径。

不输出凭据，不写入项目，不放入进程参数。`setup_keys.py` 默认为无交互诊断，交互维护仅在用户明确要求时使用。缺少全部安全来源时付费前停止，不能伪造 Key 或使用示例值。

## 默认 wxart

规范模型 ID 是 `omni-fast-no-water`。实时目录检查显示 wxart 当前只公开服务商别名 `omni-flash`，因此默认请求必须继续发送 `omni-flash` 才能保持可用；项目状态同时保存规范模型 ID 和服务商实际别名，不能把这两个字段混为一谈。Cangyuan 直接公开规范模型 ID。两家服务商的底层模型是否完全相同，供应商目录没有给出可验证证明，Skill 不做未经证实的等同性承诺。

Base URL `https://api.wxart.space`；创建 `POST /v1/videos`，查询 `GET /v1/videos/{task_id}`。

```json
{"model":"omni-flash","prompt":"按实际图片顺序与角色编译","mode":"ref","images_url":["https://approved-image.example/image.png"],"aspect_ratio":"16:9","duration":10,"resolution":"720p","watermark":false}
```

只有这些合同字段进入供应商 payload。1–3 张原文件的来源、角色、顺序与哈希另存 `reference_bindings`。`Image1` 是数组第一张的自然语言简称；不发送未经证实的专用控制令牌。详见 [参考图合同](omni-storyboard-guidance.md)。

## Cangyuan 降级服务商

默认仍优先使用 wxart。降级服务商只在默认服务商的非计费健康检查失败，或默认服务商明确返回 HTTP 400/401/403/404/429 这类“没有创建任务”的拒绝时启用；超时、5xx、返回格式不完整等无法判断是否已创建任务的情况不会盲目切换。

Base URL `https://ai.cangyuansuanli.cn`；模型 `omni-fast-no-water`；创建、查询和内容下载仍使用 `/v1/videos`、`/v1/videos/{task_id}`、`/v1/videos/{task_id}/content`。实际返回验证表明该服务商把单独的 `first_image_url` 当作首尾帧模式，要求成对字段；因此本 Skill 的单图或多图图生视频统一使用公开 HTTPS 的 `images` 数组，时长传字符串 `"10"`，同时传 `prompt`、`aspect_ratio`、`resolution`。API Key 读取环境变量 `CANGYUAN_API_KEY` / `CANGYUAN_OMNI_API_KEY`，其次读取 macOS 钥匙串服务 `3d-sygg-cangyuan`、账号 `api-key`。

降级提交与原片段、参考图哈希和任务账本绑定，会记录 `fallback_of` 和服务商名称；成功任务按原片段继续轮询、下载和剪辑，不会另建方案。新服务商的模型可见性和图片字段已通过 `/v1/models` 只读检查；真正生成仍以本次图生视频测试和后续账本为准。

## MiniMax

官方 `https://api.minimaxi.com`，可用 `MINIMAX_BASE_URL` 按账号区域覆盖。`POST /v1/get_voice` 的 `voice_type=all` 是只读音色查询，只使用返回的 `system_voice`；`voice_cloning` 和 `voice_generation` 不作为候选。

付费 `POST /v1/t2a_v2` 固定 `speech-2.8-hd`，整片一次连续旁白、非流式、MP3、32kHz、128kbps、单声道、`subtitle_enable=false`。参见 [官方同步语音接口](https://platform.minimaxi.com/docs/api-reference/speech-t2a-http)。用户提供的 OpenMontage 规范用于核对端点、鉴权、音频和混音做法；其中克隆音色默认路由不适用于本 Skill。

## 付费账本

- 每次 POST 前写精确请求文件，再原子记录 `attempted`。账本采用文件锁，跨线程、跨进程均不能重复占用同一片段或整片旁白。
- 幂等边界是项目内的逻辑任务，不是临时网址。更换 URL 不能重置额度；修改提示词不能冒充原方案重试。仅明确失败且有批准额度时允许替代任务。
- POST 超时、断线、HTTP 408/5xx、异常响应、缺失任务 ID/音频记 `submission_unknown`，不重试且停止后续付费任务。已有 `attempted` 却无确认响应的崩溃窗口同样阻断。
- 创建请求依次得到 ID 后，最多两个已知任务并行等待。后续 GET/下载可有限重试；仍未确认终态时继续恢复原 ID，不创建替代任务。明确终态 `failed/error` 或明确 HTTP 429 拒绝，才可在原批准额度内重试；通用错误仍保持原因未明。
- 每次查询保存状态、进度与脱敏后的 `error.code` / `error.message`。通用失败保持 `cause=undetermined`，不按错误说明猜测版权、人物、三格或提示词长度。
- `produce` / `resume` 共用执行路径；完整已下载文件以账本哈希核验后复用，未完成任务查询原 ID，明确失败段按冻结额度重试，未提交的原计划段继续生成。启用旁白时必须有哈希一致的已合成文件，不能静默变成无旁白成片。
- 项目状态操作有独占锁，避免两个 CLI 同时覆盖批准记录、剪辑和任务状态。

## 发布会话

默认 `preflight` 只做内部检查，`produce` 使用一次真实发布会话。`preflight --verify-tunnel` 仅是可选诊断，不是正常用户步骤。

临时 HTTP 服务只返回批准清单中的哈希文件，目录页和其他路径返回 404。隧道启动日志、HTTP 访问摘要、初始公网内容校验、每 10 秒的存活观察、每 30 秒的公网探测、关闭记录写到 `requests/publication/`，永不放进公开目录。探测失败有记录；付费前检查进程存活。

服务覆盖创建、查询和下载；正常退出清理公开副本与隧道，但保留证据。本机公网 GET 和服务端 HTTP 200 只能证明一次传输观察，不能证明请求来自供应商或已被模型消费；记录明确 `supplier_fetch_confirmed=false`。供应商失败原因未明时不伪造“素材已被模型读取”。

Quick Tunnel 不提供可恢复的固定网址。用户中断、进程崩溃或轮询超时导致会话关闭后，旧 URL 不能由 `resume` 重建；已知未完成任务只能继续查询，换网址不能改变旧请求。旧任务明确失败且有批准额度时，可以重新发布相同文件并提交替代任务；新 URL 与原文件哈希绑定，不改变图片或提示词。若真实生产证明确有素材长时间等待需求，再单独评估稳定托管；本轮不引入新云存储或常驻服务。

恢复账本以唯一 `attempt_id` 记录每次 POST，用 `retry_of` 连接替代任务；`semantic_hash` 绑定除临时 URL 外的参数和有序素材哈希。文件锁内检查首次记录的重试上限与未决提交，防止重复执行、进程重启或新 URL 重置额度。HTTP 400/401/403 等需要修正输入的拒绝、主动取消、提交不明均不自动替代。每个新项目默认每失败片段最多重试一次；额度耗尽保留项目，不新建补救方案或参考图。

局部修订属于原片的替换素材：按批准的原片关联回填，不创建独立交付目标。旁白的缓存同时核对请求内容与文件哈希；完整拼接前必须存在已批准音频，`assembly.json` 留下实际混音来源和全片开口点。
