#!/usr/bin/env python3
"""Bootstrap 3D sygg credentials without interrupting production runs."""

from __future__ import annotations

import getpass
import argparse
import sys

try:
    from .providers import (
        KEYCHAIN_ACCOUNT,
        CANGYUAN_KEYCHAIN_SERVICE,
        Keychain,
        MINIMAX_KEYCHAIN_SERVICE,
        OMNI_KEYCHAIN_SERVICE,
        ValidationError,
    )
except ImportError:
    from providers import (
        KEYCHAIN_ACCOUNT,
        CANGYUAN_KEYCHAIN_SERVICE,
        Keychain,
        MINIMAX_KEYCHAIN_SERVICE,
        OMNI_KEYCHAIN_SERVICE,
        ValidationError,
    )


def store_secret(service: str, secret: str) -> None:
    Keychain.store(service, secret, KEYCHAIN_ACCOUNT)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="maintenance-only: replace the wxart video credential using hidden input",
    )
    parser.add_argument(
        "--interactive-cangyuan",
        action="store_true",
        help="maintenance-only: replace the Cangyuan fallback credential using hidden input",
    )
    parser.add_argument("--interactive-minimax", action="store_true", help="explicit standalone narration only: save MiniMax key")
    parser.add_argument("--with-narration", action="store_true", help="explicit standalone narration only: also check MiniMax")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if sum((args.interactive, args.interactive_cangyuan, args.interactive_minimax)) > 1:
        raise ValidationError("Choose only one interactive credential option")
    if args.interactive_cangyuan:
        print("维护模式：输入内容不会显示，也不会进入命令参数或项目文件。")
        cangyuan = getpass.getpass("Cangyuan fallback API key: ")
        store_secret(CANGYUAN_KEYCHAIN_SERVICE, cangyuan)
        print("Cangyuan 降级密钥已更新到独立的 macOS 钥匙串条目。")
        return 0
    if args.interactive:
        print("维护模式：输入内容不会显示，也不会进入命令参数或项目文件。")
        omni = getpass.getpass("wxart Omni API key: ")
        store_secret(OMNI_KEYCHAIN_SERVICE, omni)
        print("wxart 视频密钥已安全保存；默认无需 MiniMax 密钥。")
        return 0

    if args.interactive_minimax:
        store_secret(MINIMAX_KEYCHAIN_SERVICE, getpass.getpass("MiniMax standalone narration API key: "))
        print("独立配音密钥已安全保存。")
        return 0
    try:
        from . import capabilities as caps
    except ImportError:
        import capabilities as caps
    config = caps.read_config()
    route = caps.route(config.get("model", "omni"), config.get("provider", "auto"))
    services = [CANGYUAN_KEYCHAIN_SERVICE if route["provider"] == "cangyuan" else OMNI_KEYCHAIN_SERVICE]
    if args.with_narration:
        services.append(MINIMAX_KEYCHAIN_SERVICE)
    status = Keychain.bootstrap(services=services)
    for label in ("omni", "cangyuan", "minimax"):
        row = status.get(label)
        if row is None:
            continue
        print(f"{label}: available={row['available']}, source={row['source']}, service={row['service']}")
    print("自动凭据预检通过；后续生产调用不会要求手动输入 Key。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
