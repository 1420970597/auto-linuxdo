import argparse
import getpass
import sys
from time import time

from linuxdo.state import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the local Linux.do bridge")
    parser.add_argument("command", choices=("configure", "token", "resume", "forget"))
    args = parser.parse_args()
    store = Store()
    if args.command == "configure":
        if not sys.stdin.isatty():
            parser.error("configure requires an interactive terminal")
        username = input("Linux.do 用户名或邮箱: ").strip()
        password = getpass.getpass("Linux.do 密码: ")
        if not username or not password:
            parser.error("用户名和密码不能为空")
        store.configure(username, password)
        print("账号已保存，服务将自动检查登录状态。")
    elif args.command == "token":
        print(store.token())
    elif args.command == "forget":
        store.credentials_path.unlink(missing_ok=True)
        print("账号已移除，服务将清除 Chrome 论坛会话。")
    else:
        hold = store.hold()
        if hold and hold.retry_at and hold.retry_at > time():
            parser.error("论坛限流等待期尚未结束")
        store.clear_hold()
        print("暂停状态已清除。")


if __name__ == "__main__":
    main()
