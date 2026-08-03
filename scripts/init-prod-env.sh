#!/usr/bin/env bash
# Generate a production-ready .env for Docker Compose without asking users to
# hand-write secrets. Existing .env is preserved unless --force is passed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
source "$SCRIPT_DIR/_lib.sh"

ENV_FILE="$ROOT_DIR/.env"
FORCE=false
WEB_PORT="${WEB_PORT_PUBLISH:-8080}"
COOKIE_SECURE_VALUE="${COOKIE_SECURE:-false}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=true
      shift
      ;;
    --port)
      [[ $# -ge 2 ]] || die "--port 需要一个端口值"
      WEB_PORT="$2"
      shift 2
      ;;
    --cookie-secure)
      COOKIE_SECURE_VALUE=true
      shift
      ;;
    -h|--help)
      cat <<'EOF'
用法：
  ./scripts/init-prod-env.sh [--force] [--port 8080] [--cookie-secure]

生成生产 Docker Compose 所需的 .env：
  - MASTER_KEY
  - JWT_SECRET
  - UPDATER_TOKEN
  - POSTGRES_PASSWORD
  - WEB_PORT_PUBLISH
  - COOKIE_SECURE
  - TRUST_FORWARDED_FOR

默认不覆盖已有 .env；需要重建时传 --force。
EOF
      exit 0
      ;;
    *)
      die "未知参数：$1"
      ;;
  esac
done

if [[ -f "$ENV_FILE" && "$FORCE" != true ]]; then
  ok ".env 已存在，保持不变：$ENV_FILE"
  dim "如需重建，请执行：./scripts/init-prod-env.sh --force"
  exit 0
fi

need_cmd python3 "用于生成随机密钥"

random_values="$(python3 - <<'PY'
import base64
import os
import secrets
import string

alphabet = string.ascii_letters + string.digits
print(base64.urlsafe_b64encode(os.urandom(32)).decode())
print(secrets.token_urlsafe(64))
print(secrets.token_urlsafe(64))
print("".join(secrets.choice(alphabet) for _ in range(32)))
PY
)"

MASTER_KEY_VALUE="$(printf '%s\n' "$random_values" | sed -n '1p')"
JWT_SECRET_VALUE="$(printf '%s\n' "$random_values" | sed -n '2p')"
UPDATER_TOKEN_VALUE="$(printf '%s\n' "$random_values" | sed -n '3p')"
POSTGRES_PASSWORD_VALUE="$(printf '%s\n' "$random_values" | sed -n '4p')"

cp "$ROOT_DIR/.env.example" "$ENV_FILE"

python3 - "$ENV_FILE" "$MASTER_KEY_VALUE" "$JWT_SECRET_VALUE" "$UPDATER_TOKEN_VALUE" "$POSTGRES_PASSWORD_VALUE" "$WEB_PORT" "$COOKIE_SECURE_VALUE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
replacements = {
    "MASTER_KEY": sys.argv[2],
    "JWT_SECRET": sys.argv[3],
    "UPDATER_TOKEN": sys.argv[4],
    "POSTGRES_USER": "telepilot",
    "POSTGRES_PASSWORD": sys.argv[5],
    "POSTGRES_DB": "telepilot",
    "DATABASE_URL": "postgresql+asyncpg://telepilot:{password}@postgres:5432/telepilot".format(password=sys.argv[5]),
    "WEB_PORT_PUBLISH": sys.argv[6],
    "COOKIE_SECURE": sys.argv[7].lower(),
    "TRUST_FORWARDED_FOR": "true",
}

seen: set[str] = set()
out: list[str] = []
for line in path.read_text(encoding="utf-8").splitlines():
    if "=" not in line or line.lstrip().startswith("#"):
        out.append(line)
        continue
    key = line.split("=", 1)[0].strip()
    if key in replacements:
        out.append(f"{key}={replacements[key]}")
        seen.add(key)
    else:
        out.append(line)

for key, value in replacements.items():
    if key not in seen:
        out.append(f"{key}={value}")

path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
PY

auto_tune_env "$ENV_FILE"

ok "已生成生产 .env：$ENV_FILE"
dim "下一步：docker compose up -d --build"
