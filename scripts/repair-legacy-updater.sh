#!/bin/sh
# 一次性修复 v0.87.0-beta.5 等旧 updater 容器的 GHCR attestation 验证环境。
#
# 该脚本只在当前 updater 容器内安装 GitHub CLI，并在 /usr/local/bin 放置兼容
# 包装器。包装器不跳过验签：它只确保旧命令从 GHCR OCI 读取证明、禁用登录
# 提示，并在没有真实 GitHub token 时提供一个不可用于认证的固定占位值。

set -eu

REAL_GH="${TELEPILOT_GH_REAL_PATH:-/usr/bin/gh}"
WRAPPER_GH="${TELEPILOT_GH_WRAPPER_PATH:-/usr/local/bin/gh}"
APK_BIN="${TELEPILOT_APK_BIN:-}"
WRAPPER_MARKER="# TelePilot legacy updater OCI attestation compatibility wrapper"

log() {
  printf '▸ %s\n' "$*"
}

ok() {
  printf '✓ %s\n' "$*"
}

die() {
  printf '✗ %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
用法：repair-legacy-updater.sh

仅用于 v0.87.0-beta.5 等旧版 updater 容器的一次性在线更新脱困。
脚本会：
  1. 在当前 Alpine updater 容器内安装 github-cli；
  2. 安装只拦截 `gh attestation verify` 的兼容包装器；
  3. 强制从 GHCR OCI 读取证明并禁用交互式登录。

脚本不会修改数据库、业务容器、Git 工作树或 GitHub CLI 登录配置。
EOF
}

case "${1:-}" in
  "")
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

[ "$(id -u)" = "0" ] || die "必须以 root 在 updater 容器内运行"
[ "$REAL_GH" != "$WRAPPER_GH" ] \
  || die "GitHub CLI 真实路径与包装器路径不能相同"
case "$REAL_GH" in
  /*) ;;
  *) die "GitHub CLI 真实路径必须是绝对路径：$REAL_GH" ;;
esac
case "$WRAPPER_GH" in
  /*) ;;
  *) die "兼容包装器路径必须是绝对路径：$WRAPPER_GH" ;;
esac
case "$REAL_GH$WRAPPER_GH" in
  *[!A-Za-z0-9_./-]*) die "路径包含不安全字符，拒绝生成包装器" ;;
esac

if [ -z "$APK_BIN" ]; then
  APK_BIN="$(command -v apk || true)"
fi
[ -n "$APK_BIN" ] && [ -x "$APK_BIN" ] \
  || die "当前环境不是带 apk 的 Alpine updater 容器"

if [ -e "$WRAPPER_GH" ] \
  && ! grep -Fq "$WRAPPER_MARKER" "$WRAPPER_GH" 2>/dev/null; then
  die "$WRAPPER_GH 已存在且不是 TelePilot 兼容包装器，拒绝覆盖"
fi

if [ ! -x "$REAL_GH" ] \
  || ! "$REAL_GH" attestation verify --help >/dev/null 2>&1; then
  log "安装支持 attestation 的 GitHub CLI"
  "$APK_BIN" add --no-cache github-cli >/dev/null \
    || die "安装 github-cli 失败"
fi

[ -x "$REAL_GH" ] \
  && "$REAL_GH" attestation verify --help >/dev/null 2>&1 \
  || die "安装后的 GitHub CLI 不支持 attestation verify"

wrapper_dir="$(dirname "$WRAPPER_GH")"
[ -d "$wrapper_dir" ] || die "包装器目录不存在：$wrapper_dir"
wrapper_tmp="${WRAPPER_GH}.tmp.$$"
trap 'rm -f "$wrapper_tmp"' EXIT HUP INT TERM
umask 022

cat > "$wrapper_tmp" <<EOF
#!/bin/sh
$WRAPPER_MARKER
set -eu

REAL_GH="\${TELEPILOT_GH_REAL_PATH:-$REAL_GH}"

if [ "\${1:-}" = "attestation" ] && [ "\${2:-}" = "verify" ]; then
  has_oci_bundle=0
  for arg in "\$@"; do
    if [ "\$arg" = "--bundle-from-oci" ]; then
      has_oci_bundle=1
      break
    fi
  done
  if [ "\$has_oci_bundle" = "0" ]; then
    set -- "\$@" --bundle-from-oci
  fi

  if [ -n "\${GH_TOKEN:-}" ]; then
    export GH_TOKEN
  elif [ -n "\${GITHUB_TOKEN:-}" ]; then
    GH_TOKEN="\$GITHUB_TOKEN"
    export GH_TOKEN
  else
    GH_TOKEN="telepilot-oci-attestation-verification"
    export GH_TOKEN
  fi
  GH_PROMPT_DISABLED=1
  export GH_PROMPT_DISABLED
fi

exec "\$REAL_GH" "\$@"
EOF

chmod 0755 "$wrapper_tmp"
mv "$wrapper_tmp" "$WRAPPER_GH"
trap - EXIT HUP INT TERM

resolved_gh="$(command -v gh || true)"
[ "$resolved_gh" = "$WRAPPER_GH" ] \
  || die "PATH 未优先使用兼容包装器：当前 gh 为 ${resolved_gh:-未找到}"
"$WRAPPER_GH" attestation verify --help >/dev/null 2>&1 \
  || die "兼容包装器自检失败"

wrapper_digest="$(sha256sum "$WRAPPER_GH" | awk '{print $1}')"
ok "旧 updater 验签环境已修复"
log "GitHub CLI：$("$REAL_GH" --version | head -n 1)"
log "兼容包装器：$WRAPPER_GH"
log "包装器 SHA256：$wrapper_digest"
log "无需执行 gh auth login；返回 Web 面板重试在线更新即可"
log "目标 updater 镜像接管后会替换当前容器，此一次性包装器不会继续保留"
