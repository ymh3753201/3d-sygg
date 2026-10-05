# 3D SYGG · 商业广告制作 Skill

把商品照片变成商业广告：AI 分析商品、设计创意和分镜，在你确认方案和参考图后生成视频，再用 FFmpeg 剪辑、混音和检查成片。

这是供 **Codex 使用的 Skill**，不是打开就能运行的独立网页软件，也不是完整的 3D 建模工具。源码免费开放；生图、视频与旁白服务可能收费，需要你自己的账号和使用额度。

## 零基础学员入口

先看 [完整教程与三份提示词](docs/ZERO-TO-VIDEO.md)：安装配置（一次提供 API 资料）→ 无需填写的检查确认 → 两次确认后执行生成任务。

## 快速开始

1. 按 [安装指南](docs/INSTALL.md) 安装 Python、FFmpeg、cloudflared，并把 Skill 放入项目的 `.agents/skills/3d-sygg`。
2. 按 [API 配置指南](docs/CONFIGURATION.md) 准备生图能力、视频 API，按需配置旁白 API。
3. 在 Codex 中打开这个项目，上传商品照片并发送：

   > 使用 $3d-sygg，为这个商品制作 15 秒竖屏商业广告。先给我方案，等我确认后生成参考图，再等我确认后生成视频。不要编造商品功效。

4. 确认方案 → 查看并确认实际参考图 → 等待视频生成与剪辑。

[下载版本包](https://github.com/ymh3753201/3d-sygg/releases/latest) · [安装说明](docs/INSTALL.md) · [配置说明](docs/CONFIGURATION.md) · [常见问题](docs/FAQ.md)

## 必须准备什么

| 能力 | 当前实现 | 使用者需要准备 |
|---|---|---|
| 生图 | Codex 内置 `imagegen`，生成商品母版、人物与分镜 | 支持内置生图的 Codex 环境、对应权限与额度；本仓库没有通用生图 API 适配器 |
| 视频 | 默认 wxart Omni，可切换沧元适配通道 | 对应供应商的 API Key 与模型使用额度 |
| 人声与声音 | 默认由视频模型直接输出 | 人声、音效、背景音乐与环境音无需独立语音 Key |
| 独立配音（选用） | MiniMax `speech-2.8-hd` | 只有明确要求单独使用语音模型才配置 `MINIMAX_API_KEY` |
| 本地剪辑 | FFmpeg / ffprobe | 安装本地工具，无需剪辑 API |
| 视频参考图传输 | cloudflared 临时 HTTPS 隧道 | 安装工具且网络能够连接；只公开确认清单中的生成参考图 |

**如果你希望使用独立的生图 API**：需要自行配置生图服务的地址、模型和 Key，并开发适配层。当前代码要求 `codex_imagegen` 来源与生成关系，不能只换一个环境变量或伪造来源声明就宣称兼容。该扩展暂不在本版本支持范围内。

## 模型与限制

默认 Omni 路径保留 10 秒单次生成、720p，较长广告通过多段剪辑完成。Seedance 2.0 / 2.5 与 MiniMax H3 已有指定供应商适配器，但部分执行方式仍需独立实片验证；“接口已接入”不代表所有画面效果都已验证。详见 [模型能力与验证范围](.agents/skills/3d-sygg/references/multi-model-long-video.md)。

当前运行依赖 Unix 的文件锁，面向 macOS / Linux。macOS 可使用钥匙串；Linux 使用环境变量。Windows 原生运行未支持，可自行评估 WSL。本次发行在 macOS / Python 3.14 上完成离线验证；其他环境由 CI 或使用者进一步验证。

## 源码与输出

- `.agents/skills/3d-sygg/`：完整 Skill、脚本、文档与测试。
- `docs/`：安装、配置和排错说明。
- `tools/build_release.py`：按明确文件清单制作发行包，附校验值。
- 生成项目默认保存于 Skill 的 `outputs/`；保留目录才能恢复原任务。

付费前会展示调用次数与恢复额度。提交状态不明确时停止新增请求，恢复优先查询原任务。程序检查通过后仍需要实际看图、观看视频和试听；无法试听或商品身份不合格时会标记待复核。

## 开发与许可证

```bash
python3 .agents/skills/3d-sygg/tests/run_offline.py
python3 tools/build_release.py --output dist
```

测试禁止真实外部 HTTP、密钥读取命令与隧道调用，不触发生图或视频付费。贡献前请阅读 [贡献说明](CONTRIBUTING.md)。源码采用 [MIT License](LICENSE)；第三方模型、工具、商品素材与生成内容的权利和服务条款由各自提供方决定。
