#!/usr/bin/env bash
# 把 .env 里关键密钥（MASTER_KEY / JWT_SECRET / UPDATER_TOKEN）单独导出成一份小文件，
# 便于离线 / 异地备份。
#
# 默认走 gpg 对称加密（提示你输口令）→ 输出 .gpg；
# 显式 --no-encrypt 才会落明文 .txt（不推荐，仅给没装 gpg / age 的环境用）。
#
# 用法：
#   bash deploy/backup-keys.sh                        # 默认 gpg 加密 → keys-backup-*.gpg
#   bash deploy/backup-keys.sh --age                  # 用 age 加密（如装了）→ .age
#   bash deploy/backup-keys.sh --no-encrypt           # 落明文 .txt（自负风险）
#   bash deploy/backup-keys.sh /path/file.gpg         # 指定输出路径
#
# 为什么单独备份：
#   - MASTER_KEY 一旦丢失，所有 TG session / api_hash / api_id / TOTP secret
#     都解不出来 → 等于全部账号要重新登录
#   - DB 备份和 MASTER_KEY 必须分开存：放一起被偷就等于裸奔

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "✗ 找不到 $ENV_FILE" >&2
  exit 1
fi

MODE="gpg"   # gpg | age | plain
OUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-encrypt|--plain)
      MODE="plain"; shift ;;
    --age)
      MODE="age"; shift ;;
    --gpg)
      MODE="gpg"; shift ;;
    -h|--help)
      sed -n '2,18p' "$0"; exit 0 ;;
    *)
      OUT="$1"; shift ;;
  esac
done

# 默认输出路径
TS="$(date +%Y%m%d-%H%M%S)"
case "$MODE" in
  gpg)   OUT="${OUT:-$ROOT_DIR/keys-backup-$TS.gpg}" ;;
  age)   OUT="${OUT:-$ROOT_DIR/keys-backup-$TS.age}" ;;
  plain) OUT="${OUT:-$ROOT_DIR/keys-backup-$TS.txt}" ;;
esac

# 生成明文内容到临时文件（chmod 600）
TMP="$(mktemp -t keys-backup.XXXXXX)"
trap 'rm -f "$TMP"' EXIT
chmod 600 "$TMP"

{
  echo "# TelePilot key backup"
  echo "# generated at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# host:         $(hostname 2>/dev/null || echo '?')"
  echo "#"
  echo "# 还原方式：把下列内容写回 .env，保持 KEY=VALUE 原样即可。"
  echo "# 重要：恢复 DB 备份时，MASTER_KEY 必须与备份当时一致，否则 session 解密失败。"
  echo "#"
  grep -E '^(MASTER_KEY|JWT_SECRET|UPDATER_TOKEN)=' "$ENV_FILE" || {
    echo "✗ .env 里没有可备份的关键密钥" >&2
    exit 2
  }
} > "$TMP"

# 加密 / 落盘
case "$MODE" in
  gpg)
    if ! command -v gpg >/dev/null 2>&1; then
      echo "✗ 未安装 gpg；可装 gnupg 或改用 --age / --no-encrypt" >&2
      exit 3
    fi
    # 对称加密；交互式输 passphrase，AES256
    gpg --symmetric --cipher-algo AES256 --output "$OUT" "$TMP"
    chmod 600 "$OUT"
    echo "✓ 已加密写入：$OUT"
    echo "  解密：gpg --decrypt '$OUT'"
    ;;
  age)
    if ! command -v age >/dev/null 2>&1; then
      echo "✗ 未安装 age；可装 age 或改用 --gpg / --no-encrypt" >&2
      exit 3
    fi
    age --passphrase --output "$OUT" "$TMP"
    chmod 600 "$OUT"
    echo "✓ 已加密写入：$OUT"
    echo "  解密：age --decrypt '$OUT'"
    ;;
  plain)
    cp "$TMP" "$OUT"
    chmod 600 "$OUT"
    echo "⚠ 明文已写入：$OUT"
    echo "  这是 600 权限，但仍强烈建议手动加密后异地保存："
    echo "    gpg -c '$OUT' && rm '$OUT'"
    ;;
esac

echo
echo "下一步建议（手动执行）："
echo "  1) 异地：把输出文件上传到与 DB 备份不同的位置（不同账号 / 不同地域）"
echo "  2) 验证：尝试解密一次，确认口令记得住"
echo
echo "⚠ .gitignore 已默认忽略 keys-backup-*.{txt,gpg,age}，请再确认未误提交。"
