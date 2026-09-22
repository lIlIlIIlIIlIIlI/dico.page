import argparse
import os
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Dico.page 회원에게 관리자 권한을 부여합니다.")
    parser.add_argument("email", help="관리자로 지정할 회원 이메일")
    args = parser.parse_args()

    database_path = Path(os.getenv("DATABASE_PATH", "data/app.db"))

    if not database_path.exists():
        raise SystemExit(f"데이터베이스를 찾을 수 없습니다: {database_path}")

    with sqlite3.connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE users SET role = 'admin' WHERE email = ? COLLATE NOCASE",
            (args.email.strip(),),
        )

    if cursor.rowcount == 0:
        raise SystemExit("해당 이메일로 가입된 회원을 찾을 수 없습니다.")

    print(f"{args.email} 계정에 관리자 권한을 부여했습니다.")


if __name__ == "__main__":
    main()
