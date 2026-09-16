"""Admin helper: set or reset the control-UI password without editing files.

    python3 -m gx_control_ui.passwd                 # prompt twice (interactive)
    python3 -m gx_control_ui.passwd --stdin         # read one line from stdin
    python3 -m gx_control_ui.passwd --generate      # random password, written
                                                    # to a 0600 file, not printed
    python3 -m gx_control_ui.passwd --status        # is a password configured?
    python3 -m gx_control_ui.passwd --acceptance    # (re)create the loopback-only
                                                    # 'acceptance' test account
    python3 -m gx_control_ui.passwd --remove-acceptance

Every change bumps the store's generation, which logs out every session on
the running server within one request. The password is never printed to a
log, never put on the command line, and never written to the Git checkout.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import string
import sys
import time

from .auth import AuthError, PasswordStore, write_private_file
from .config import UIConfig

_ALPHABET = string.ascii_letters + string.digits + "-_.~"


def generate_password(length: int = 24) -> str:
    while True:
        pw = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gx-ui-passwd", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--username", default="admin")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stdin", action="store_true", help="read the password from stdin")
    mode.add_argument("--generate", action="store_true",
                      help="generate a random password into <secret_dir>/initial-admin-password (0600)")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--acceptance", action="store_true",
                      help="create/rotate the 'acceptance' account (loopback-only sign-in; password in "
                           "<secret_dir>/acceptance-password, 0600)")
    mode.add_argument("--remove-acceptance", action="store_true")
    args = parser.parse_args(argv)

    cfg = UIConfig(hosts=("127.0.0.1",))
    store = PasswordStore(cfg.password_file)

    if args.acceptance:
        acc = PasswordStore(cfg.acceptance_file)
        password = generate_password(32)
        record = acc.set_password(cfg.ACCEPTANCE_USER, password)
        write_private_file(cfg.acceptance_password_file, password + "\n")
        print(f"acceptance account ready (generation {record['generation']}); password in "
              f"{cfg.acceptance_password_file} (0600). It can only sign in from 127.0.0.1.")
        return 0
    if args.remove_acceptance:
        for path in (cfg.acceptance_file, cfg.acceptance_password_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        print("acceptance account removed")
        return 0

    if args.status:
        try:
            data = store.load()
        except (AuthError, ValueError) as exc:
            print(f"password store invalid: {exc}")
            return 1
        if not data:
            print(f"no password configured ({cfg.password_file})")
            return 1
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data.get("updated", 0)))
        print(f"configured: user={data['username']} generation={data.get('generation')} updated={stamp}")
        print(f"store: {cfg.password_file} (mode 0600)")
        if cfg.initial_password_file.exists():
            print(f"initial password file still present: {cfg.initial_password_file}")
        print(f"acceptance account: {'present (loopback-only)' if cfg.acceptance_file.exists() else 'absent'}")
        return 0

    if args.generate:
        password = generate_password()
    elif args.stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            print("refusing to prompt without a terminal; use --stdin or --generate", file=sys.stderr)
            return 2
        password = getpass.getpass("New control-UI password: ")
        if getpass.getpass("Repeat: ") != password:
            print("passwords do not match", file=sys.stderr)
            return 1

    try:
        record = store.set_password(args.username, password)
    except AuthError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    if args.generate:
        write_private_file(cfg.initial_password_file, password + "\n")
        print(f"password set for '{record['username']}' (generation {record['generation']}).")
        print(f"it is in {cfg.initial_password_file} (mode 0600); read it once, then set your own with:")
        print("  legenex/control-ui/scripts/gx-ui-passwd")
    else:
        # A user-chosen password supersedes the generated one.
        try:
            cfg.initial_password_file.unlink()
        except FileNotFoundError:
            pass
        print(f"password set for '{record['username']}' (generation {record['generation']}); "
              "all existing sessions are now logged out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
