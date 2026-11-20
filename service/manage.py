"""Admin commands.

    python -m service.manage init           # create tables and (in dev) a signing key
    python -m service.manage genkey         # print a new Ed25519 private key PEM (for SERVICE_SIGNING_KEY)
    python -m service.manage set-plan EMAIL lab|free
"""
from __future__ import annotations

import sys

from .config import ServiceSettings
from .db import Database, get_user_by_email
from .signing import generate_pem, load_signer


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "init"
    s = ServiceSettings.from_env()
    if cmd == "genkey":
        print(generate_pem(), end="")
        return 0
    db = Database(s.database_url)
    if cmd == "init":
        s.ensure_data_dir()
        db.create_all()
        signer = load_signer(s)
        print(f"database ready; signing key id {signer.key_id}")
        return 0
    if cmd == "set-plan" and len(argv) == 3:
        with db.session() as session:
            u = get_user_by_email(session, argv[1])
            if not u:
                print("no such user", file=sys.stderr)
                return 1
            u.plan, u.plan_status = argv[2], "active"
        print(f"{argv[1]} -> {argv[2]}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
