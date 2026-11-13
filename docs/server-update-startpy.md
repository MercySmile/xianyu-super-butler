# 使用 Start.py 的服务器更新步骤

适用仓库：`https://github.com/MercySmile/xianyu-super-butler.git`。

本说明仅提供后续手动操作步骤；本次没有连接服务器、停止程序或部署。服务器路径依据已有日志为 `C:\xianyu\xianyu-super-butler`；实际路径不同时请替换。

## 1. 停止原程序并检查仓库

在原先运行 `Start.py` 的窗口按 `Ctrl+C`，等待退出。若你使用其他启动工具，则通过原工具停止本程序。不要直接结束机器上所有 Python 进程。

在服务器 PowerShell 中执行：

```powershell
Set-Location -LiteralPath 'C:\xianyu\xianyu-super-butler'
git remote get-url origin
git status --short
git branch --show-current
```

`origin` 应指向上面的 MercySmile 仓库。若有本地代码或配置修改，先保存并处理，确认不会丢失再继续；不要用 `git reset --hard` 或 `git clean` 强行清理。运行时数据库和上传目录被 Git 忽略，不会出现在正常代码提交里。

## 2. 备份数据和配置

确认程序已经停止后执行，备份放在项目的同级目录：

```powershell
$projectDir = (Get-Location).Path
$backupDir = Join-Path (Split-Path -Parent $projectDir) ('xianyu-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backupDir -ErrorAction Stop | Out-Null
git rev-parse HEAD | Set-Content -LiteralPath (Join-Path $backupDir 'code-commit.txt')

foreach ($name in @('data', 'global_config.yml', '.env')) {
    if (Test-Path -LiteralPath $name) {
        Copy-Item -LiteralPath $name -Destination $backupDir -Recurse -ErrorAction Stop
    }
}
if (Test-Path -LiteralPath 'static\uploads') {
    Copy-Item -LiteralPath 'static\uploads' -Destination (Join-Path $backupDir 'uploads') -Recurse -ErrorAction Stop
}
Write-Host "备份目录：$backupDir"
```

如果使用 `DB_PATH` 指向 `data/` 之外的数据库，还需备份实际数据库及其 WAL/SHM 文件；停止程序后一起复制。不要删除或重建原有数据库。

## 3. 拉取自己的 main

以下操作必须逐步成功后再继续：

```powershell
git fetch origin
if ($LASTEXITCODE -ne 0) { throw '获取代码失败，停止更新' }

git switch main
if ($LASTEXITCODE -ne 0) { throw '切换 main 失败，停止更新' }

git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) { throw '更新失败，停止操作；先检查本地修改或分支分歧' }

git log -1 --oneline
git status --short
```

本次更新已包含构建后的 `static/` 前端文件，服务器无需安装 Node.js 或重新运行前端构建。相对原魔改版本，本次没有变更 `requirements.txt`，沿用已能正常启动程序的 Python 环境即可，无需为这次更新重装全部依赖。

不要改用官方仓库或官方 `latest` 镜像，它们不包含这里的分组卡密、组合发货和漏单补偿定制。

## 4. 用原环境启动

使用你之前能够运行成功的启动命令。例如原来用项目内 `.venv`：

```powershell
.\.venv\Scripts\python.exe Start.py
```

如果原来使用已激活的环境或系统 Python：

```powershell
python Start.py
```

两个命令按实际环境选一个，不要同时启动两份。不要运行 `Start.py` 的同时再手动启动第二个监听程序。

## 5. 验证更新和自动发货

另外开一个 PowerShell 窗口检查后端；端口不同则替换 8080：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/health'
```

随后在管理页面刷新缓存，检查：

1. 关于页版本为 3.3.0，账号处于在线/监听状态。健康接口通过只说明服务启动，不代表闲鱼认证和发货已正常。
2. 同步一次卖出订单，确认不是 Session 过期、0 单假成功或跨循环锁异常。
3. 原有分组库存、规格绑定和组合固定文字仍在。检查实际通知渠道是否配置且可用。
4. 使用测试订单验证自动发货、组合文字完整性、库存数量以及订单再次刷新后不重复出库。
5. 确认掉线期间的待发货订单进入补偿检查；已有出库但发送不确定的订单先人工核对，不要清除防重记录重新扣卡。

原登录态如果已经被闲鱼撤销，需要重新扫码一次。修复不能恢复已被平台作废的会话；恢复登录后系统应重新连接并检查漏单。之后观察 48–72 小时的真实续期和发货情况。

## 回退

如启动失败，先停止新程序并保留新日志。根据备份中的 `code-commit.txt` 恢复旧代码，并使用对应的数据库、配置及上传文件备份；数据库升级后不要仅切回代码而忽略数据版本。恢复前保留当前数据，避免覆盖更新后新增的订单或库存记录。
