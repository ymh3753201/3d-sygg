#!/usr/bin/env python3
"""Build the complete learner folder and ZIP from the clean Skill release archive."""
import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

from build_release import ROOT, VERSION

NAME = 'AI商品商业广告视频Skill'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(archive, output):
    folder = output / NAME
    if folder.exists():
        raise SystemExit('Choose an empty staging directory; never overwrite an installed learner folder')
    folder.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            relative = Path(item.filename)
            if relative.parts[0] != '3d-sygg' or relative.is_absolute() or '..' in relative.parts:
                raise SystemExit('Unexpected archive entry')
            if item.is_dir(): continue
            target = folder / 'skills' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(package.read(item))
    skill = folder / 'skills/3d-sygg'
    tutorial = (skill / 'docs/ZERO-TO-VIDEO.md').read_text()
    (folder / '安装与使用说明.md').write_text(tutorial)
    for i, filename in enumerate(('01-install.md', '02-audit.md', '03-produce.md'), 1):
        text = (skill / 'docs/prompts' / filename).read_text()
        blocks = re.findall(r'```text\n(.*?)\n```', text, re.S)
        if len(blocks) != 1: raise SystemExit('A learner prompt must contain exactly one text block')
        names = ('安装配置', '检查确认', '执行生成任务')
        (folder / f'提示词{i}-{names[i-1]}.txt').write_text(blocks[0] + '\n')
    (folder / 'README.md').write_text(f'''# AI商品商业广告视频Skill · {VERSION}

先看 [安装与使用说明](安装与使用说明.md)，依次发送三份提示词：安装配置 → 检查确认 → 执行生成任务。

完整 Skill 在 `skills/3d-sygg/`，安装到自己的工作项目 `.agents/skills/3d-sygg/`，不要只复制 SKILL.md。已有版本更新时保留自己的配置、密钥和 outputs 项目。

图片默认用 Codex 内置工具，也可提供自己的图片服务文档和密钥安全保存方式，让 AI 配置 Image2.5 等中转模型或 Seedream 5.0。已支持 OpenAI 文件上传、Seedream JSON、多参考图和可配置异步查询；特殊协议按真实文档补充适配。详见 `skills/3d-sygg/references/image-api.md`。

声音默认由视频模型输出；只有明确要求独立配音才配置 MiniMax。模型使用自己的账号额度。配置检查与离线测试不代表真实画面效果已验证。

[开源仓库](https://github.com/ymh3753201/3d-sygg) · [正式下载](https://github.com/ymh3753201/3d-sygg/releases/latest)

本包不含密钥、客户照片、成片或运行账本。请勿分享使用后的 outputs 或本地图片配置文件。
''')
    (folder / '安全与去敏感说明.md').write_text('''# 安全与去敏感说明

API Key 只在本机隐藏输入或安全配置为环境变量，不上传到群聊、GitHub 或日志。图片配置和视频配置分开，脚本不自动读取 .env。

中转站必须用该站文档、模型 ID 和对应 Key。图片 API 在方案确认后直接接收实际商品参考照片；不为此公开原照片 URL。视频临时隧道只公开第二次确认的生成参考图。

确认方案后生图，确认实际参考图与费用后生成视频。安装检查不授权收费验证。提交不明保留原任务，不能删除账本或换项目重复付费。

分享时从干净源码构建，不直接压缩使用过的项目。本包没有本地配置、缓存、客户素材和生产输出。
''')
    (folder / '版本与目录说明.md').write_text(f'''# 版本与目录说明

- 版本：{VERSION}；日期：2026-10-09。
- 完整 Skill 从同版本 GitHub 发行包逐文件复制，包含代码、配置模板、文档和离线测试。
- 新增外部图片 API 配置、生成回执、付费次数控制和任务恢复；保留内置生图与历史项目。
- 支持 macOS / Linux、Python 3.10+，需要 FFmpeg/ffprobe，视频参考图发布需要 cloudflared。
- 原生 Windows 未支持。离线测试结果见 `skills/3d-sygg/reports/image-api-validation.md`。

目录：README、安装说明、三份提示词、版本与安全说明、manifest.json、SHA256SUMS.txt、skills/3d-sygg。

SHA256SUMS.txt 记录内容文件的校验值。配置检查不触发付费，真实服务的画面质量须按自己的模型验证。
''')
    files = sorted(p for p in folder.rglob('*') if p.is_file())
    manifest = {'version': VERSION, 'date': '2026-10-09', 'skill': '3d-sygg',
                'source_release_sha256': digest(archive), 'content_file_count': len(files) + 1,
                'production_code_changed': True}
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    files = sorted(p for p in folder.rglob('*') if p.is_file())
    (folder / 'SHA256SUMS.txt').write_text(''.join(f'{digest(p)}  {p.relative_to(folder).as_posix()}\n' for p in files))
    output_archive = output / f'{NAME}-{VERSION}.zip'
    with zipfile.ZipFile(output_archive, 'w', zipfile.ZIP_DEFLATED) as package:
        for p in sorted(folder.rglob('*')):
            if p.is_file(): package.write(p, str(p.relative_to(output)))
    (output / (output_archive.name + '.sha256.txt')).write_text(f'{digest(output_archive)}  {output_archive.name}\n')
    print(json.dumps({'folder': str(folder), 'archive': str(output_archive), 'files': len(files),
                      'sha256': digest(output_archive)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=ROOT / 'dist' / f'3d-sygg-{VERSION}.zip')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.archive, args.output)


if __name__ == '__main__': main()
