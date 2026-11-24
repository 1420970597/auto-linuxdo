import argparse
import getpass
import sys
from time import time

from linuxdo.cookies import MAX_IMPORT_BYTES, parse_cookie_input
from linuxdo.state import Store
from linuxdo.v2ex import TOKEN_FILENAME, configure_token


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the local Linux.do bridge")
    parser.add_argument("command", choices=(
        "configure", "token", "sync-token", "resume", "forget",
        "v2ex-configure", "v2ex-forget", "v2ex-resume",
    ))
    args = parser.parse_args()
    store = Store()
    if args.command.startswith("v2ex-"):
        store = Store(store.root / "v2ex")
    if args.command == "configure":
        try:
            if sys.stdin.isatty():
                content = getpass.getpass("Linux.do Cookie 请求头: ")
            else:
                content = sys.stdin.buffer.read(MAX_IMPORT_BYTES + 1).decode("utf-8")
            if len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
                parser.error("Cookie 配置文件过大")
            store.configure(parse_cookie_input(content))
        except (ValueError, UnicodeError):
            parser.error("Cookie 配置无效，请提供包含有效 _t 的请求头或 JSON。")
        print("已保存 Cookie，Chrome 将检查会话。")
    elif args.command in ("token", "sync-token"):
        print(store.token(sync=args.command == "sync-token"))
    elif args.command == "forget":
        store.forget()
        print("Cookie 配置已移除，服务将清除 Chrome 论坛会话。")
    elif args.command == "v2ex-configure":
        try:
            if sys.stdin.isatty():
                token = getpass.getpass("V2EX 个人访问令牌: ")
            else:
                token = sys.stdin.buffer.read(1025).decode("ascii")
            if len(token) > 1024:
                raise ValueError
            configure_token(store.root / TOKEN_FILENAME, token.strip())
        except ValueError:
            parser.error("请提供有效的 V2EX 个人访问令牌。")
        print("已保存 V2EX 个人访问令牌。")
    elif args.command == "v2ex-forget":
        (store.root / TOKEN_FILENAME).unlink(missing_ok=True)
        print("V2EX 令牌已移除，将使用公开网页。")
    else:
        hold = store.hold()
        if hold and hold.retry_at and hold.retry_at > time():
            parser.error("论坛限流等待期尚未结束")
        if hold and hold.code == "cookies_expired":
            parser.error("请先导入新的论坛会话 Cookie")
        store.clear_hold()
        print("暂停状态已清除。")


if __name__ == "__main__":
    main()
