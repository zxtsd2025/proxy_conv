#!/usr/bin/env bash
# proxy-tool 安装脚本：复制程序、生成启动器、写入 shell 集成。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[ -n "${HOME:-}" ] || { printf '错误: HOME 环境变量未设置\n' >&2; exit 1; }
PREFIX="$HOME/.local"
SHELL_NAME="auto"
INSTALL_SHELL=1

usage() {
    cat <<'EOF'
用法: ./install.sh [选项]

选项:
  --prefix PATH   安装到指定前缀，默认 ~/.local
                  可执行文件: PATH/bin/proxy-cli
                  程序文件:   PATH/share/proxy-tool
  --shell NAME    目标 shell: auto|bash|zsh|fish，默认 auto
  --no-shell      不修改 shell rc 文件（只安装程序）
  -h, --help      显示帮助

示例:
  ./install.sh
  ./install.sh --shell zsh
  ./install.sh --shell fish
  ./install.sh --prefix "$HOME/.local" --no-shell

安装完成后会生成默认配置。编辑配置中的代理地址后，重新加载对应
shell 的 rc 文件（bash: ~/.bashrc，zsh: ~/.zshrc，fish: config.fish），
然后执行:
  proxy
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
        --shell)
            [ "$#" -ge 2 ] || die "--shell 需要 auto、bash、zsh 或 fish"
            SHELL_NAME="$2"
            shift 2
            ;;
        --shell=*)
            SHELL_NAME="${1#*=}"
            shift
            ;;
        --no-shell)
            INSTALL_SHELL=0
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

case "$SHELL_NAME" in
    auto|bash|zsh|fish) ;;
    *) die "--shell 只能是 auto、bash、zsh 或 fish" ;;
esac

if [ "$SHELL_NAME" = "auto" ]; then
    case "$(basename "${SHELL:-}")" in
        zsh) SUGGEST_SHELL="zsh" ;;
        fish) SUGGEST_SHELL="fish" ;;
        *) SUGGEST_SHELL="bash" ;;
    esac
else
    SUGGEST_SHELL="$SHELL_NAME"
fi

# 展开 ~ 并转成绝对路径，避免从其他目录执行时装到奇怪的位置
case "$PREFIX" in
    "~") PREFIX="$HOME" ;;
    "~/"*) PREFIX="$HOME/${PREFIX#\~/}" ;;
esac
case "$PREFIX" in
    /*) ;;
    *) PREFIX="$PWD/$PREFIX" ;;
esac
PREFIX="${PREFIX%/}"
[ -n "$PREFIX" ] && [ "$PREFIX" != "/" ] || die "不能把前缀设置为 /"

BINDIR="$PREFIX/bin"
DATADIR="$PREFIX/share/proxy-tool"
LIBDIR="$DATADIR/lib"
LAUNCHER="$BINDIR/proxy-cli"

case "$LIBDIR" in
    "$PREFIX"/*) ;;
    *) die "内部错误：安装路径异常 ($LIBDIR)" ;;
esac

[ -d "$SCRIPT_DIR/src/proxy_tool" ] || die "找不到源码目录: $SCRIPT_DIR/src/proxy_tool"
[ -f "$SCRIPT_DIR/src/proxy_tool/cli.py" ] || die "源码不完整：缺少 cli.py"

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || die "找不到 $PYTHON；请安装 Python 3.11+"
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    die "需要 Python 3.11 或更高版本（当前: $("$PYTHON" --version 2>&1)）"
fi
if ! "$PYTHON" -c 'import tomllib' >/dev/null 2>&1; then
    die "当前 Python 缺少 tomllib，无法读取 TOML 配置（需要 3.11+）"
fi

if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
    warn "检测到通过 sudo 运行：会安装到 root 的家目录，shell 集成也会写入 root 的 rc 文件。"
    warn "如果你只是想为当前用户安装，请不要使用 sudo。"
fi

info "==> 安装 proxy-tool 到 $PREFIX"
mkdir -p "$BINDIR" "$LIBDIR"

# 只清理自己管理的目录，且路径必须位于 PREFIX 下
if [ -e "$LIBDIR/proxy_tool" ] || [ -L "$LIBDIR/proxy_tool" ]; then
    rm -rf -- "$LIBDIR/proxy_tool"
fi
mkdir -p "$LIBDIR/proxy_tool"
cp -- "$SCRIPT_DIR"/src/proxy_tool/*.py "$LIBDIR/proxy_tool/"

info "==> 生成启动器 $LAUNCHER"
"$PYTHON" - "$LIBDIR" "$LAUNCHER" <<'PY'
import json
import pathlib
import sys

libdir, launcher = sys.argv[1], sys.argv[2]
content = f"""#!/usr/bin/env python3
import sys

if sys.version_info < (3, 11):
    sys.stderr.write("proxy-tool 需要 Python 3.11 或更高版本\\n")
    raise SystemExit(1)

sys.path.insert(0, {json.dumps(libdir)})
from proxy_tool.cli import main

sys.exit(main())
"""
path = pathlib.Path(launcher)
path.write_text(content, encoding="utf-8")
path.chmod(0o755)
PY

if ! VERSION_OUT="$("$LAUNCHER" --version 2>&1)"; then
    printf '%s\n' "$VERSION_OUT" >&2
    die "启动器自检失败，请检查 $LAUNCHER"
fi
info "==> 程序自检通过 ($VERSION_OUT)"

# 先确保配置文件存在，再安装 shell 集成
CONFIG_PATH="$("$LAUNCHER" path --ensure)"
info "==> 配置文件: $CONFIG_PATH"
if ! "$LAUNCHER" status >/dev/null 2>&1; then
    warn "配置文件已存在，但当前无法解析；请运行 "$LAUNCHER" edit 修复。"
fi

SHELL_OK=0
if [ "$INSTALL_SHELL" -eq 1 ]; then
    info "==> 安装 shell 集成"
    INIT_LOG="$(mktemp "${TMPDIR:-/tmp}/proxy-tool-init.XXXXXX")"
    trap 'rm -f "$INIT_LOG"' EXIT
    if "$LAUNCHER" init --install --shell "$SHELL_NAME" --cli-path "$LAUNCHER" >"$INIT_LOG" 2>&1; then
        SHELL_OK=1
    else
        cat "$INIT_LOG" >&2
        warn "shell 集成安装失败；程序本身已经可用。"
        warn "你可以稍后手动运行: $LAUNCHER init --install --shell $SUGGEST_SHELL"
    fi
    rm -f "$INIT_LOG"
    trap - EXIT
else
    info "==> 按 --no-shell 跳过 shell rc 修改"
fi

# 推断实际写入的 rc 文件，仅用于提示
case "$SHELL_NAME" in
    bash) RC_FILE="$HOME/.bashrc" ;;
    zsh) RC_FILE="${ZDOTDIR:-$HOME}/.zshrc" ;;
    fish) RC_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish" ;;
    auto)
        case "$(basename "${SHELL:-}")" in
            zsh) RC_FILE="${ZDOTDIR:-$HOME}/.zshrc" ;;
            fish) RC_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish" ;;
            *) RC_FILE="$HOME/.bashrc" ;;
        esac
        ;;
esac

info ""
info "proxy-tool 安装完成"
info "  程序      $LAUNCHER"
info "  配置      $CONFIG_PATH"
if [ "$SHELL_OK" -eq 1 ]; then
    info "  集成      $RC_FILE"
else
    info "  集成      未安装（可运行 $LAUNCHER init --install --shell $SUGGEST_SHELL）"
fi
info ""
info "下一步:"
info "  1. 编辑配置，填入你的代理地址:"
info "     ${EDITOR:-vi} $CONFIG_PATH"
if [ "$SHELL_OK" -eq 1 ]; then
    info "  2. 重新加载 shell:"
    printf '     source %q\n' "$RC_FILE"
    info "  3. 启用代理:"
    info "     proxy"
else
    info "  2. 把 $BINDIR 加入 PATH，或直接使用 $LAUNCHER"
fi
info ""
info "提示: 运行 \"proxy test --http\" 可以测试代理是否可用。"
info "卸载: $SCRIPT_DIR/uninstall.sh"
