# API 与模型配置

零基础使用者可先看 [完整教程](ZERO-TO-VIDEO.md) 与 [配置审查提示词](prompts/02-audit.md)。

只需安装时配置一次，日常广告制作不需要反复输入 Key。源码不包含账号、额度或真实密钥。

## 1. 生图能力

当前 Skill 使用 Codex 内置生图工具 `imagegen`。请确保你的 Codex 环境能够实际生成图片，具有相应账号权限和额度。这不是 `OPENAI_API_KEY` 被 Python 脚本直接调用的流程，仓库没有读取 `IMAGE_API_KEY`、`IMAGE_BASE_URL` 或 `IMAGE_MODEL` 的生图适配器。

使用独立生图服务时，必须另外配置该服务的地址、模型、Key，并实现参考图输入、生成结果落盘和来源追踪，再同步调整来源校验与测试。仅提供 Key 不足以让当前版本兼容。

## 2. 视频和旁白密钥

| 用途 | 当前脚本支持的环境变量 | 端点 / 模型 |
|---|---|---|
| 默认视频 | `WXART_OMNI_API_KEY`，也接受 `WXART_API_KEY` / `OMNI_API_KEY` | `https://api.wxart.space` / `omni-flash` |
| 沧元视频及 Omni 备援 | `CANGYUAN_API_KEY`，也接受 `CANGYUAN_OMNI_API_KEY` | `https://ai.cangyuansuanli.cn` / 见下面的模型表 |
| 独立配音（选用） | `MINIMAX_API_KEY` | `https://api.minimaxi.com` / `speech-2.8-hd` |
| MiniMax 区域地址（可选） | `MINIMAX_BASE_URL` | 根据你的 MiniMax 账号区域配置官方地址 |

只需配置实际使用的视频供应商，默认由视频模型生成人声、音效、背景音乐与环境音，只有明确要求单独使用语音模型才配置 MiniMax。Omni 自动备援要使用时才配置沧元密钥。视频端点目前由适配器固定，不能通过随意填写 `VIDEO_BASE_URL` 接入任何供应商。

macOS / Linux 可以在运行 Codex 或脚本的**同一个终端会话**中，隐藏输入密钥：

```bash
# 用 bash 执行这段；输入不会显示，也不会成为命令历史中的真实密钥
read -r -s -p 'wxart video key: ' WXART_OMNI_API_KEY
printf '\n'
export WXART_OMNI_API_KEY
```

使用沧元时，把第一组变量名改为 `CANGYUAN_API_KEY`。不要把真实 Key 放进聊天、截图、方案或 Git。终端环境变量不会自动传入已经打开的 Codex 桌面进程；桌面用户推荐下面的 macOS 钥匙串方式，或者在启动 Codex 前配置它实际继承的环境。

macOS 钥匙串方式，在 Skill 根目录运行：

```bash
# 隐藏输入并保存 wxart 视频 Key；默认无需语音 Key
python3 scripts/setup_keys.py --interactive
# 使用沧元视频时：单独保存沧元 Key
python3 scripts/setup_keys.py --interactive-cangyuan
```

只有明确需要独立配音时，另运行 `python3 scripts/setup_keys.py --interactive-minimax`。钥匙串服务为 `3d-sygg-omni`、`3d-sygg-cangyuan`、`3d-sygg-minimax`，账号均为 `api-key`。无参数的 `setup_keys.py` 只检查当前配置的视频服务；明确启用独立配音时再加 `--with-narration` 检查 MiniMax。

脚本**不会自动加载 `.env`**。环境变量优先，其次是 macOS 钥匙串。可选 `SYGG_OMNI_KEY_SOURCE` / `SYGG_CANGYUAN_KEY_SOURCE` / `SYGG_MINIMAX_KEY_SOURCE` 指向你自己明确指定的本地密钥文件，迁移到钥匙串；该方式要求 macOS。开源版不搜索作者机器的私有目录。

## 3. 选择视频模型

在 Skill 根目录运行，只改变后续新项目：

```bash
python3 scripts/commercial_ad.py configure --model omni
# 显式使用沧元 Omni
python3 scripts/commercial_ad.py configure --model omni --provider cangyuan
# 或选择以下任一通道
python3 scripts/commercial_ad.py configure --model sd11-seedance-2.0
python3 scripts/commercial_ad.py configure --model sd11-seedance-2.5
python3 scripts/commercial_ad.py configure --model mm2-minimax-h3
```

| 选择值 | 供应商 | 本版本适配器的单次请求范围 |
|---|---|---|
| `omni` | 默认 wxart，可指定沧元 | 固定 10 秒、720p |
| `sd11-seedance-2.0` | 沧元 | 整数 4–15 秒 |
| `sd11-seedance-2.5` | 沧元 | 整数 4–30 秒 |
| `mm2-minimax-h3` | 沧元 | 整数 4–15 秒 |

以上是仓库中的适配合同，不是供应商实时可用性承诺。正式付费前用 `check-channel --model 选择值` 核对目录，再按 [实片验证规则](../.agents/skills/3d-sygg/references/live-validation.md) 判断是否需要验证。新通道的验证记录未随私有素材一起公开，不能视为已经自动合格。

`configure` 生成 `config/config.local.json`，只存模型、供应商、输出分辨率与策略，禁止存 Key。默认视频原生音色与台词由 AI 纳入方案；只有独立配音模式才查询 MiniMax 系统音色。

## 4. 费用与参考图公开

生成调用由第三方收费，以账号当前报价为准。方案会展示预计生成段数、旁白调用与有限恢复额度；提交结果不明时停止新增收费请求，运行 `resume` 查询原任务。

视频供应商需要公开 HTTPS 图片。确认参考图与视频阶段后，cloudflared 临时公开批准清单中的生成参考图；原商品照片、未采用草稿与密钥不在清单中。临时地址不是永久托管，旧任务不通过换地址重新提交。API 细节见 [合同与恢复](../.agents/skills/3d-sygg/references/api-contracts.md)。
