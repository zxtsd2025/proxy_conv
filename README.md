# proxy-tool

在 Linux 终端里用一条命令切换全局代理：

```bash
proxy          # 从配置文件读取代理，写入当前终端
proxy stop     # 停用代理，并恢复原来的环境变量
```

配置保存在用户目录，纯 Python 标准库实现，不需要安装任何第三方依赖。

## 特性

- **bash / zsh / fish**：三种 shell 都有对应的 `proxy` 函数集成。
- **一键启用 / 停用**：`proxy`、`proxy stop`，也支持 `proxy toggle`。
- **恢复原环境**：启用前会记住 `http_proxy` 等变量的旧值，停用时恢复，而不是简单清空。
- **多套配置**：适合公司代理、家里代理、本地 Clash 等场景，`proxy use <名称>` 随时切换。
- **HTTP / HTTPS / SOCKS**：支持 `http://`、`https://`、`socks4://`、`socks4a://`、`socks5://`、`socks5h://`。
- **no_proxy 管理**：支持域名、IP、CIDR、通配符。
- **额外环境变量**：可为 npm、yarn 等程序追加变量。
- **连通性测试**：`proxy test` 检查代理端口，`proxy test --http` 通过代理实际访问网页。
- **子进程模式**：`proxy exec -- <命令>` 不污染当前 shell，适合脚本。
- **自检**：`proxy doctor` 检查 Python、配置、权限、shell 集成和 PATH。
- **密码脱敏**：`proxy status` / `proxy list` 默认隐藏 URL 中的密码，`--reveal` 可查看原文。
- **安全的配置写回**：配置文件权限为 `600`，写入使用临时文件 + 原子替换。

## 安装

要求：Linux、Python 3.11+、bash、zsh 或 fish。`curl` 是可选的（`proxy test --http` 会优先使用）。

```bash
cd /path/to/proxy_conv
./install.sh
```

安装脚本会：

1. 把程序复制到 `~/.local/share/proxy-tool/`；
2. 在 `~/.local/bin/proxy-cli` 创建启动器；
3. 创建默认配置 `~/.config/proxy-tool/config.toml`；
4. 在对应 shell 的 rc 文件中写入 `proxy()` 函数（bash/zsh/fish）。

然后重新加载 shell：

```bash
source ~/.bashrc                         # bash
source ~/.zshrc                          # zsh
source ~/.config/fish/config.fish        # fish
```

编辑配置里的代理地址：

```bash
proxy edit
proxy                   # 启用
proxy status            # 查看状态
```

### 安装选项

```bash
./install.sh --shell zsh      # 明确指定 zsh
./install.sh --shell fish     # 明确指定 fish
./install.sh --no-shell       # 只安装程序，不修改 rc 文件
./install.sh --prefix "$HOME/.local"
./install.sh --help
```

如果使用自定义前缀，可执行文件位于 `<前缀>/bin/proxy-cli`。

## 配置

默认路径：`~/.config/proxy-tool/config.toml`（遵循 `XDG_CONFIG_HOME`）。
也可以用环境变量覆盖：

```bash
export PROXY_CONFIG="$HOME/.config/proxy-tool/work.toml"
proxy
```

一个完整示例：

```toml
# 当前使用哪一套配置
current = "default"

[profiles.default]
# HTTP / HTTPS 代理。只写 http 时，https 会自动复用 http。
http = "http://127.0.0.1:7890"
https = "http://127.0.0.1:7890"

# 可选：SOCKS5 代理（注意：SOCKS 地址要写在 all 里，不能写在 http/https）
# all = "socks5://127.0.0.1:7891"

# 不走代理的地址
no_proxy = ["localhost", "127.0.0.1", "::1", "*.local", "10.0.0.0/8"]

# 可选：为特定程序追加环境变量
[profiles.default.env]
NPM_CONFIG_PROXY = "http://127.0.0.1:7890"
NPM_CONFIG_HTTPS_PROXY = "http://127.0.0.1:7890"

# 第二套配置：公司代理
[profiles.work]
http = "http://proxy.example.com:8080"
https = "http://proxy.example.com:8080"
no_proxy = ["localhost", "127.0.0.1", ".corp.example.com"]

[profiles.work.env]
NPM_CONFIG_PROXY = "http://proxy.example.com:8080"

# 第三套配置：SOCKS5
[profiles.socks]
all = "socks5h://127.0.0.1:7891"
no_proxy = ["localhost", "127.0.0.1", "::1"]
```

说明：

- `http` / `https` 必须是 `http://` 或 `https://`；SOCKS 写到 `all`。
- 只写 `http` 时，HTTPS 默认复用同一个地址。
- `no_proxy` 可以写成 TOML 数组，也可以写成逗号分隔字符串。
- `env` 表里不能覆盖 `http_proxy`、`https_proxy`、`no_proxy` 等保留变量，也不能使用 `PROXY_TOOL_` 前缀。
- 配置中可包含用户名密码，例如 `http://user:pass@127.0.0.1:7890`；展示时会脱敏。
- `proxy use` 只改 `current` 行，会保留手写注释；`proxy add` / `proxy remove` 会重写整个文件，可能丢失注释。日常修改建议用 `proxy edit`。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `proxy` | 启用当前配置，等同于 `proxy on` |
| `proxy on [名称]` | 启用当前配置，或临时启用指定配置 |
| `proxy stop` | 停用并恢复启用前的环境变量 |
| `proxy toggle` | 已启用则停用，否则启用 |
| `proxy use <名称>` | 切换当前配置并立即启用（会写入 `current`） |
| `proxy reload` | 重新读取配置文件并刷新当前 shell |
| `proxy status` | 查看配置、当前 shell 的环境变量是否一致 |
| `proxy status --json` | 机器可读状态；默认脱敏，加 `--reveal` 显示原文 |
| `proxy list` | 列出所有配置，`*` 表示当前配置 |
| `proxy show [名称]` | 查看某套配置详情 |
| `proxy add <名称> <地址>` | 新增配置 |
| `proxy remove <名称>` | 删除配置；删除当前配置需 `--force` |
| `proxy edit` | 用 `$EDITOR` 编辑配置文件 |
| `proxy test [名称]` | 测试代理端口连通性 |
| `proxy test --http` | 额外通过代理访问默认测试地址 |
| `proxy exec -- <命令>` | 只给子进程设置代理，不改变当前 shell |
| `proxy env` | 输出当前 shell 的环境设置语句（bash/zsh: `export`；fish: `set -gx`） |
| `proxy init --install` | 安装/更新 shell 集成 |
| `proxy doctor` | 检查安装和配置 |
| `proxy path` | 显示配置文件路径 |
| `proxy version` | 显示版本 |

### 新增配置

```bash
# HTTP 代理；如果配置里只有安装时生成的默认示例，会自动切换为它
proxy add local http://127.0.0.1:7890

# 同时指定 HTTPS、SOCKS5 和 no_proxy
proxy add office http://proxy.example.com:8080 \
  --https http://proxy.example.com:8080 \
  --all socks5://proxy.example.com:1080 \
  --no-proxy "localhost,127.0.0.1,.corp.example.com"

# 追加 npm 变量，并立即设为当前配置
proxy add npm-local http://127.0.0.1:7890 \
  --env NPM_CONFIG_PROXY=http://127.0.0.1:7890 \
  --use

# 覆盖同名配置
proxy add local http://127.0.0.1:7890 --force
```

### 测试代理

```bash
proxy test                    # 只测代理端口能否连上（快）
proxy test --timeout 3        # 自定义超时
proxy test --http             # 再通过代理访问 https://www.gstatic.com/generate_204
proxy test --url https://example.com
```

如果代理需要认证，`proxy test --http` 使用的 curl 会读取 URL 中的用户名密码。
没有 `curl` 时会退回 Python `urllib`（仅支持 HTTP/HTTPS 代理，不支持 SOCKS）。

### 只在单条命令里使用代理

```bash
proxy exec -- curl -I https://example.com
proxy exec --profile office -- env | grep -i proxy
```

### 手动加载（不安装 shell 函数时）

```bash
# bash / zsh
eval "$(proxy-cli env)"                 # 只设置，不保存旧值
eval "$(proxy-cli --emit-shell stop)"   # 清理

# fish
proxy-cli --shell-type fish env | source
proxy-cli --emit-shell --shell-type fish stop | source
```

推荐还是使用 `proxy` shell 函数，因为它能保存并恢复原来的环境变量。

## 工作原理

子进程无法修改父 shell 的环境变量，所以不能只靠一个 Python 可执行文件完成“设置当前终端”。安装脚本会在 rc 文件中写入：

```bash
proxy() {
    ...
    _proxy_tool_out="$(command /path/to/proxy-cli --emit-shell "$@")" || ...
    eval "$_proxy_tool_out"
}
```

Python 负责读取配置、校验、生成 POSIX 的 `export`/`unset` 或 fish 的 `set -gx`/`set -e` 语句；shell 函数负责在当前 shell 里执行它们。fish 的函数使用 `proxy-cli --emit-shell --shell-type fish ... | source`，避免把 fish 代码交给 `eval`。
启用时会把旧值保存到 `_PROXY_TOOL_SAVED_*` 变量，停用时按原样恢复；如果原来没有该变量，就 `unset`。
当前启用的配置名保存在 `PROXY_TOOL_PROFILE`，受管变量列表保存在 `PROXY_TOOL_VARS`。

每个终端的环境变量是独立的：在 A 终端 `proxy` 不会影响 B 终端；停用时也需要在对应终端执行 `proxy stop`。

## 文件位置

| 路径 | 说明 |
| --- | --- |
| `~/.local/bin/proxy-cli` | 命令行程序 |
| `~/.local/share/proxy-tool/lib/proxy_tool/` | Python 程序文件 |
| `~/.config/proxy-tool/config.toml` | 配置文件（600） |
| `~/.bashrc` / `~/.zshrc` | bash/zsh 的 `proxy()` 集成块 |
| `~/.config/fish/config.fish` | fish 的 `proxy` 函数集成块 |

## 卸载

```bash
cd /path/to/proxy_conv
./uninstall.sh              # 删除程序和 shell 集成，保留配置
./uninstall.sh --purge      # 连同配置一起删除
```

如果安装时使用了自定义前缀：

```bash
./uninstall.sh --prefix "$HOME/opt/proxy-tool" --purge
```

## 常见问题

### `proxy: command not found`

重新加载 shell，或检查 rc 文件里是否有 `# >>> proxy-tool >>>`：

```bash
source ~/.bashrc
type proxy
```

如果没有，重新安装集成：

```bash
~/.local/bin/proxy-cli init --install --shell bash
```

### 提示“配置文件不存在”

```bash
~/.local/bin/proxy-cli init --install
proxy edit
```

### 修改配置后没有生效

```bash
proxy reload
```

或者重新执行 `proxy`。

### `proxy on` 提示需要 shell 集成

直接运行 `proxy-cli on` 不会修改父 shell，这是预期行为。使用安装好的 `proxy` 函数，或：

```bash
proxy-cli init --install
```

### 配置格式错误

```bash
proxy edit
```

编辑器退出后会重新校验配置，错误信息会指出字段或行号。

### zsh 用户

```bash
./install.sh --shell zsh
source ~/.zshrc
```

如果设置了 `ZDOTDIR`，集成会写入 `$ZDOTDIR/.zshrc`。

### bash 登录 shell 找不到 proxy

大多数桌面终端会读取 `~/.bashrc`。如果使用 SSH 登录，bash 可能只读取
`~/.bash_profile` 或 `~/.profile`，而这些文件不一定会加载 `~/.bashrc`。
可以在 `~/.bash_profile` 中加入：

```bash
[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"
```

### fish 用户

```fish
./install.sh --shell fish
source ~/.config/fish/config.fish
proxy
```

如果设置了 `XDG_CONFIG_HOME`，集成会写入
`$XDG_CONFIG_HOME/fish/config.fish`。fish 函数通过管道把 Python 生成的
`set -gx` 代码交给 `source` 执行，同样支持 `proxy stop` 恢复原环境变量。

如果 fish 中提示 `proxy: command not found`：

```fish
source ~/.config/fish/config.fish
type proxy
```

手动使用（不安装函数）时：

```fish
proxy-cli --shell-type fish env | source
```

### 不要用 sudo 安装

默认安装到当前用户的 `~/.local`，不需要 root。用 `sudo ./install.sh` 会把程序装到 root 家目录，并把 shell 集成写进 root 的 rc 文件。

## 安全说明

- 配置文件创建时权限为 `600`，其中可能包含代理用户名密码。
- `proxy status`、`proxy list`、`proxy show` 默认脱敏；需要查看原文时加 `--reveal`。
- 代理凭据会作为环境变量传给子进程，这是使用代理的必然结果；不要在不可信程序中打印环境变量。
- 不要把包含真实密码的配置文件提交到 Git。
- 配置文件中的 `env` 表会直接影响当前 shell 环境；请只使用可信的配置文件，不要加载来源不明的配置。

## 开发与测试

项目结构：

```text
.
├── install.sh
├── uninstall.sh
├── pyproject.toml
├── src/proxy_tool/
│   ├── cli.py        # 命令行入口
│   ├── config.py     # TOML 配置读写与校验
│   ├── shell.py      # shell 代码生成
│   ├── net.py        # 连通性测试
│   └── ui.py
└── tests/
```

运行测试（不需要 pytest）：

```bash
python3 -m unittest discover -s tests -t . -v
```

从源码直接运行：

```bash
PYTHONPATH=src python3 -m proxy_tool --help
PYTHONPATH=src python3 -m proxy_tool path --ensure
```

也可以使用 pip 安装（可选）：

```bash
python3 -m pip install --user .
proxy-cli init --install
```

这种方式仍然需要安装 shell 集成，才能使用 `proxy` 直接修改当前终端。

## License

MIT
