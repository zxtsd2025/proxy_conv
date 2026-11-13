#!/usr/bin/env bash
# proxy-tool 卸载脚本：移除程序、shell 集成；默认保留配置。
set -euo pipefail

PREFIX="${HOME:-}/.local"
PURGE=0

usage() {
    cat <<'EOF'
用法: ./uninstall.sh [选项]

选项:
  --prefix PATH   安装前缀，默认 ~/.local
  --purge         同时删除配置文件（默认保留）
  -h, --help      显示帮助
EOF
}

info() { printf '%s\n' "$*"; }
warn() { printf '警告: %s\n' "$*" >&2; }
die() { printf '错误: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            [ "$#" -ge 2 ] || die "--prefix 需要一个路径"
            PREFIX="$2"
            shift 2
            ;;
        --prefix=*)
            PREFIX="${1#*=}"
            shift
            ;;
        --purge)
            PURGE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf '未知选项: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[ -n "${HOME:-}" ] || die "HOME 环境变量未设置"

case "$PREFIX" in
    "~") PREFIX="$HOME" ;;
    "~/"*) PREFIX="$HOME/${PREFIX#\~/}" ;;
esac
case "$PREFIX" in
    /*) ;;
    *) PREFIX="$PWD/$PREFIX" ;;
esac
PREFIX="${PREFIX%/}"
[ -n "$PREFIX" ] && [ "$PREFIX" != "/" ] || die "前缀不能是 /"

BINDIR="$PREFIX/bin"
DATADIR="$PREFIX/share/proxy-tool"
LAUNCHER="$BINDIR/proxy-cli"

case "$DATADIR" in
    "$PREFIX"/*) ;;
    *) die "内部错误：卸载路径异常 ($DATADIR)" ;;
esac

# 先移除 shell 集成，再删程序
if [ -x "$LAUNCHER" ]; then
    "$LAUNCHER" init --uninstall || warn "移除 shell 集成时出现问题，请检查 rc 文件中的 proxy-tool 标记"
else
    OTHER="$(command -v proxy-cli 2>/dev/null || true)"
    if [ -n "$OTHER" ] && [ -x "$OTHER" ]; then
        "$OTHER" init --uninstall || warn "移除 shell 集成时出现问题"
    else
        warn "找不到 proxy-cli，无法自动移除 shell 集成；请手动删除 rc 文件中的 proxy-tool 块。"
    fi
fi

if [ -e "$LAUNCHER" ] || [ -L "$LAUNCHER" ]; then
    rm -f -- "$LAUNCHER"
    info "已删除 $LAUNCHER"
fi

if [ -d "$DATADIR" ]; then
    rm -rf -- "$DATADIR"
    info "已删除 $DATADIR"
fi

if [ "$PURGE" -eq 1 ]; then
    if [ -n "${PROXY_CONFIG:-}" ]; then
        warn "检测到 PROXY_CONFIG=$PROXY_CONFIG，出于安全考虑未删除该文件；请手动处理。"
    else
        CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/proxy-tool"
        CONFIG_DIR="${CONFIG_DIR%/}"
        case "$CONFIG_DIR" in
            */proxy-tool) ;;
            *) die "内部错误：配置目录异常 ($CONFIG_DIR)" ;;
        esac
        if [ -d "$CONFIG_DIR" ]; then
            rm -rf -- "$CONFIG_DIR"
            info "已删除配置目录 $CONFIG_DIR"
        else
            info "配置目录不存在，跳过: $CONFIG_DIR"
        fi
    fi
else
    CONFIG_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/proxy-tool/config.toml"
    info "配置文件已保留: $CONFIG_FILE"
fi

info "proxy-tool 已卸载。重新打开终端后 shell 函数会消失。"
