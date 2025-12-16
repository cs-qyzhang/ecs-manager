### ecs — 用阿里云 ECS 管理 Codex session（Python）

**本项目 100% 由 GPT-5.2 生成。**

`ecs` 是一个本地 CLI：每个 Codex session 对应一台新的阿里云 ECS 实例。它会把 **session -> ECS 实例信息/SSH 连接信息** 保存到本地一个 JSON 文件里（可同步到其他机器继续用）。

## 功能

- **create**：通过 API 创建 ECS（指定镜像/实例规格/VPC 参数/KeyPair），并记录 session
- **connect**：一键 `ssh -A` 连接（开启 agent forwarding），支持把 `--` 后面的参数透传给 ssh
- **rename**：改 session 名（本地记录）
- **delete**：删除对应 ECS 实例并清理本地记录
- **tab 补全**：支持对已有 session 名称做补全（Typer/Click completion）

## 安装（Windows PowerShell 示例）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

安装后主命令是 `ecs`。如果你更新了代码/脚本入口，记得重新执行 `pip install -e .` 或 `uv pip install -e .` 以刷新命令。

## 准备环境变量（AccessKey）

`ecs` 会自动加载当前目录（向上递归查找）的 `.env` 文件；也可以用 `ECS_ENV_FILE` 指定 `.env` 的路径。（内置解析器，无额外依赖）

你可以复制仓库里的 `env.example` 为 `.env` 后填写：

```text
ALIBABA_CLOUD_ACCESS_KEY_ID=xxxx
ALIBABA_CLOUD_ACCESS_KEY_SECRET=yyyy
ECS_STATE_FILE=D:\sync\ecs_state.json
ECS_SSH_KEY=C:\path\to\your.pem
```

如果你想看到底层 HTTPS/SSL 的 warning（默认会屏蔽 Aliyun SDK 的 `SNIMissingWarning` 噪声），可以设置：

```powershell
$env:ECS_SHOW_SSL_WARNINGS="1"
```

或直接在 PowerShell 里设置：

```powershell
$env:ALIBABA_CLOUD_ACCESS_KEY_ID="xxxx"
$env:ALIBABA_CLOUD_ACCESS_KEY_SECRET="yyyy"
```

## 初始化默认配置（写入 JSON 状态文件）

下面这些是创建 ECS 最常用的必填项：

```powershell
ecs config set `
  region_id=cn-hangzhou `
  image_id=YOUR_IMAGE_ID `
  instance_type=ecs.g6.large `
  security_group_id=sg-xxxx `
  v_switch_id=vsw-xxxx `
  key_pair_name=YOUR_KEYPAIR_NAME `
  ssh_private_key_path=C:\path\to\your.pem `
  spot_strategy=SpotAsPriceGo
```

- **region_id 提醒**：这里填的是 **RegionId**（例如 `ap-northeast-1`、`cn-hangzhou`），不要填 ZoneId（例如 `ap-northeast-1c`、`cn-hangzhou-i`）。

- **说明**：
  - `key_pair_name`：阿里云侧的 **密钥对名称**（需要提前在对应 Region 创建/导入）
  - `ssh_private_key_path`：你本机的 **私钥文件路径**（通常是 `.pem`），用于 `ssh -i`

状态文件默认位置：

```powershell
ecs path
```

你也可以用环境变量指定状态文件路径（便于同步）：

```powershell
$env:ECS_STATE_FILE="D:\sync\ecs_state.json"
```

## 创建 session

```powershell
ecs create my-session
```

## 公网 IP / 私网 IP

- `ecs` 默认会在实例 Running 后，**尽量保证拿到公网 IPv4**：
  - 先从 `DescribeInstances` 读取
  - 如果没有公网 IP，并且 `internet_max_bandwidth_out > 0`，会调用 `AllocatePublicIpAddress` 分配公网 IP
- 如果你只想用私网（例如有 VPN/堡垒机），可以关闭：

```powershell
ecs config set auto_allocate_public_ip=false
# 或单次创建禁用：
ecs create my-session --no-allocate-public-ip
```

- 如果实例已经创建好了但只有私网 IP：

```powershell
ecs connect my-session --private
ecs public-ip my-session
```

## 抢占式（Spot）参数说明

- **spot_strategy**：
  - `SpotAsPriceGo`：抢占式实例，自动出价（推荐）
  - `SpotWithPriceLimit`：抢占式实例，设置价格上限（需配合 `spot_price_limit`）
  - `NoSpot`：普通按量付费
- **spot_price_limit**：仅在 `SpotWithPriceLimit` 时生效，例如 `0.034`
- **spot_duration**：可选，稳定时长（小时）1-6

也可以在创建时临时覆盖：

```powershell
ecs create my-session --spot-strategy SpotWithPriceLimit --spot-price-limit 0.05 --spot-duration 1
```

## 如果遇到 SystemDisk.Category 不支持

你看到的报错类似：
`InvalidSystemDiskCategory.ValueNotSupported`

可以显式指定系统盘类型（不同实例规格/地域支持不同）：

```powershell
ecs config set system_disk_category=cloud_auto
# 或者：
ecs config set system_disk_category=cloud_essd
```

创建时也可临时覆盖：

```powershell
ecs create my-session --system-disk-category cloud_essd --system-disk-size 40
```

## 连接 session（ssh -A）

```powershell
ecs connect my-session
```

- 自动写入 SSH 配置（`~/.ssh/config`）：
  - `ecs create` 成功后会写入一个 `Host ecs-<session>`，之后可直接 `ssh ecs-my-session`
  - `ecs delete` 会从 `~/.ssh/config` 中移除对应条目
  - 也可手动管理：

```powershell
ecs ssh add my-session
ecs ssh del my-session
```

- 在本地与 session 之间拷贝文件（scp）：

```powershell
# 上传本地 -> 远端
ecs scp my-session .\local.txt :/root/local.txt

# 下载远端 -> 本地
ecs scp my-session :/root/remote.txt .\remote.txt

# 目录递归（把 -r 透传给 scp）
ecs scp my-session .\dir :/root/dir -- -r
```

- 透传额外 ssh 参数（把 `--` 后的参数原样交给 ssh）：

```powershell
ecs connect my-session -- -L 8080:localhost:8080
```

- 如需在另一台机器上使用不同的私钥路径（不改 JSON），可用环境变量覆盖：

```powershell
$env:ECS_SSH_KEY="C:\other\key.pem"
ecs connect my-session
```

## 改名 / 删除

```powershell
ecs rename old-name new-name
ecs delete new-name
```

## 停机节省费用（Stop）

按量付费实例通常可以用 `StopCharging` 模式停机来节省计算费用：

```powershell
ecs stop my-session
```

如果该规格/地域不支持 `StopCharging`，可以改用：

```powershell
ecs stop my-session --mode keep-charging
```

## 开机（Start）

```powershell
ecs start my-session
```

## 同步 state（防止手动删实例导致本地记录过期）

如果你在控制台/其他机器上手动删除了 ECS 实例，本地 `state.json` 可能还保留旧 session。
可以用 `sync` 拉取云端实例列表并更新本地记录：

```powershell
# 刷新已记录 session 的状态/IP；不存在的实例标记为 NotFound
ecs sync

# 直接把不存在的 session 从本地删除
ecs sync --prune

# （可选）把云端实例导入到本地 state：
# 默认只导入被 ecs 打过 tag 的实例（ecs=true）
ecs sync --import

# （谨慎）导入该 region 下所有实例
ecs sync --import --import-all
```

如果你有多个地域的实例，可以用 `--all-regions` 扫描所有地域（更慢一些）：

```powershell
ecs sync --all-regions --import --import-all
```

## 打包成独立可执行文件（Windows / macOS）

> **注意**：PyInstaller/Nuitka 都不能跨平台交叉编译，Windows 的 exe 需要在 Windows 上构建；macOS 的可执行文件需要在 macOS 上构建（Apple Silicon/Intel 也建议分别在对应机器上构建）。

### Windows（生成 `dist\\ecs.exe` 或 `dist\\ecs\\ecs.exe`）

```powershell
.\scripts\build_windows.ps1 -Mode onedir   # 启动更快（推荐）
# 或：
.\scripts\build_windows.ps1 -Mode onefile  # 单文件（启动更慢）
```

### macOS（生成 `dist/ecs` 或 `dist/ecs/ecs`）

```bash
chmod +x scripts/build_macos.sh
./scripts/build_macos.sh 3.12 onedir   # 启动更快（推荐）
# 或：
./scripts/build_macos.sh 3.12 onefile  # 单文件（启动更慢）
```

构建产物在 `dist/` 目录下；把它放到 PATH 里即可直接运行。

### 更快的“编译版”（Nuitka）

PyInstaller 只是打包，不是真正编译；如果你想要更快的启动/更难被反编译，可以用 Nuitka 生成 native 可执行文件（需要 C/C++ 编译工具链）。

- Windows（需要 VS Build Tools 或可用 Nuitka 自动下载工具链）：

```powershell
.\scripts\build_windows_nuitka.ps1 -SyncDeps   # 第一次 / 依赖变更时
.\scripts\build_windows_nuitka.ps1            # 日常增量构建（更快，复用 dist_nuitka 缓存）
# 如需强制全量重编译：
.\scripts\build_windows_nuitka.ps1 -Clean
```

- macOS（需要先安装 Xcode Command Line Tools：`xcode-select --install`）：

```bash
chmod +x scripts/build_macos_nuitka.sh
./scripts/build_macos_nuitka.sh 3.12 1   # 第一次 / 依赖变更时（SYNC_DEPS=1）
./scripts/build_macos_nuitka.sh 3.12 0   # 日常增量构建（复用 dist_nuitka 缓存）
# 强制全量重编译（CLEAN=1）：
./scripts/build_macos_nuitka.sh 3.12 0 1
```

产物在 `dist_nuitka/`。

## 安装 shell 补全（macOS / Linux：zsh / bash / fish）

> 补全是按“你实际输入的命令名”注册的：请确保你平时是用 `ecs`（而不是 `./ecs` 或 `~/.local/bin/ecs`）来运行。

先把可执行文件放到 PATH（例如 `~/.local/bin`）并确保可执行：

```bash
chmod +x ~/.local/bin/ecs
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc  # bash 用 ~/.bashrc
exec $SHELL -l
```

然后执行一次自动安装：

```bash
ecs --install-completion
exec $SHELL -l
```

如果自动安装不生效，可手动加一行到你的 shell rc：

- **zsh**：`~/.zshrc`

```bash
eval "$(_ECS_COMPLETE=zsh_source ecs)"
```

- **bash**：`~/.bashrc`

```bash
eval "$(_ECS_COMPLETE=bash_source ecs)"
```

- **fish**：

```fish
ecs --show-completion > ~/.config/fish/completions/ecs.fish
```

## 安装 PowerShell 补全（Tab completion）

### 方式 A：当前会话立即生效（推荐先用这个验证）

在已 `uv_activate` / 激活 venv 的 PowerShell 里执行：

```powershell
Invoke-Expression (ecs --show-completion | Out-String)
```

然后在同一个窗口里测试：

- `ecs <TAB>`：补全子命令
- `ecs connect <TAB>`：补全 session 名

> 注意：建议输入 `ecs`（不带 `.exe`）。如果你输入 `ecs.exe`，PowerShell 会把它当成另一个命令名，可能无法触发补全。

### 方式 B：永久安装到 PowerShell Profile

如果 `ecs --install-completion` 因编码/策略报错，可以用下面方式手动写入 `$PROFILE`：

```powershell
ecs --show-completion | Out-File -Append -Encoding utf8 $PROFILE
. $PROFILE
```

Typer 自带补全安装（如果你环境里能正常跑）：

```powershell
ecs --install-completion
```

装完后一般需要新开一个 PowerShell 窗口；之后 session 名参数就可以 Tab 补全了。

如果你遇到“只输入 `ecs` 没有任何输出”的情况，通常是 PowerShell 补全用的环境变量卡住了（`_ECS_COMPLETE`）。
在当前 PowerShell 执行一次清理即可：

```powershell
Remove-Item Env:_ECS_COMPLETE -ErrorAction SilentlyContinue
Remove-Item Env:_TYPER_COMPLETE_ARGS -ErrorAction SilentlyContinue
Remove-Item Env:_TYPER_COMPLETE_WORD_TO_COMPLETE -ErrorAction SilentlyContinue
```

## Troubleshooting（常见问题）

### Windows：`ecs` / `ecs scp` 偶发“什么都不输出”

最常见原因是 **PowerShell 补全相关环境变量卡住**（`_ECS_COMPLETE` / `_TYPER_*`），导致 Click/Typer 进入补全模式并直接退出。

- 如果你只是输入 `ecs` 就没输出：按上面清理 3 个 `Env:` 变量即可恢复。
- 对于真实命令（例如 `ecs scp ...` / `ecs --help`），程序内部也会尽量忽略卡住的补全变量，但如果你的 shell 环境非常混乱，清理一次仍然是最稳的办法。

### Windows：明明更新/拷贝了 `ecs.exe`，但行为像旧版本

很可能是 **PATH 里有多个 `ecs.exe`**，你运行到了另一个旧的。

检查你到底在运行哪个：

```powershell
Get-Command ecs -All | Format-Table Name,Source
```

如果你用的是 venv 版本，可以显式运行：

```powershell
.\.venv\Scripts\ecs.exe --help
```

### `ecs scp` 没拷贝成功 / 看起来“没反应”

`ecs scp` 会直接调用系统 `scp`。如果失败会输出 **exit code** 和执行的命令，建议加 verbose 观察原因：

```powershell
ecs scp my-session .\file.txt :/root/file.txt -- -v
```


