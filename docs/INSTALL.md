# 安装指南

## 1. 安装运行工具

需要 Git（使用克隆方式时）、Python 3.10 或更新版本、FFmpeg（含 ffprobe）和 cloudflared。Python 脚本使用标准库，无需 `pip install` 第三方包。需要支持原生图片查看与内置生图的 Codex 环境，单独运行 Python 不会自动完成创意策划和生图。

macOS 已安装 Homebrew 时：

```bash
brew install python ffmpeg cloudflared
python3 --version
ffmpeg -version
ffprobe -version
cloudflared --version
```

Linux：按发行版安装 Python 与 FFmpeg，按 [Cloudflare 官方说明](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) 安装 cloudflared。Linux 不支持本项目的 macOS 钥匙串写入，使用环境变量提供密钥。视频服务需要可访问供应商与临时 HTTPS 隧道的网络。

## 2. 下载并安装 Skill

方式 A：克隆整个仓库，直接用 Codex 打开仓库目录。

```bash
git clone https://github.com/ymh3753201/3d-sygg.git
cd 3d-sygg
python3 .agents/skills/3d-sygg/scripts/commercial_ad.py capabilities
```

方式 B：把 Skill 安装到自己的工作项目。先下载 [Release](https://github.com/ymh3753201/3d-sygg/releases/latest) 中的 `3d-sygg-v2.3.0.zip`，解压得到 `3d-sygg/`。将这个文件夹放入工作项目的 `.agents/skills/`，最终结构必须是：

```text
你的项目/
└── .agents/skills/3d-sygg/
    ├── SKILL.md
    ├── agents/
    ├── scripts/
    ├── references/
    ├── config/
    ├── tests/
    └── reports/
```

终端复制示例（先进入解压包所在目录，将路径改为你的实际路径）：

```bash
mkdir -p /path/to/your-project/.agents/skills
cp -R 3d-sygg /path/to/your-project/.agents/skills/
```

如果已有同名 Skill，先备份再更新，保留 `outputs/` 和本地配置。不要把包含历史任务的目录整体删除。重启或重新打开项目中的 Codex，让它重新发现 Skill。

## 3. 配置并验证

先阅读 [API 配置指南](CONFIGURATION.md)。以下命令不生成视频：

```bash
cd .agents/skills/3d-sygg
python3 scripts/commercial_ad.py --help
python3 scripts/commercial_ad.py capabilities
python3 scripts/commercial_ad.py configure --model omni
python3 scripts/commercial_ad.py check-channel --model omni
```

`check-channel` 会联网查询模型目录，需要视频密钥，不创建生成任务，也不证明实际画面质量。不要用 `produce` 来测试安装，它是生产命令，可能计费。

需要检查程序时，在 Skill 目录执行 `python3 tests/run_offline.py`。新视频通道的实片验证是另外的付费事项，需要准备独立方案、素材与费用上限。

## 4. 开始制作

在 Codex 中提供商品照片、本次诉求、目标时长与投放比例。AI 负责准备分析和方案文件，你只需确认方案与真实参考图。执行者完整规则见 [SKILL.md](../.agents/skills/3d-sygg/SKILL.md)。

## 5. 更新版本

使用克隆方式时先备份本地改动，再执行 `git pull --ff-only`。已有项目继续使用其冻结的请求和批准记录，不自动迁移。ZIP 安装时仅更新源码文件，保留自己的密钥、本地配置与输出项目。
