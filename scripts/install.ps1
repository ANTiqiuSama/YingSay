<#
  YingSay 一键安装（Windows 10/11）
  用法（PowerShell）：
    irm https://raw.githubusercontent.com/ANTiqiuSama/YingSay/main/scripts/install.ps1 | iex
  从源码安装（需要/会自动安装 Python 3.12）：
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/ANTiqiuSama/YingSay/main/scripts/install.ps1))) -FromSource
#>
param(
  [switch]$FromSource,
  [string]$Dir = "$env:LOCALAPPDATA\Programs\YingSay",
  [string]$Repo = "ANTiqiuSama/YingSay",
  [string]$ApiKey = "",
  [string]$BaseUrl = ""
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Get-Process YingSay, FreeTypeless -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

function New-Shortcut($Path, $Target, $Arguments, $WorkDir, $Icon) {
  $ws = New-Object -ComObject WScript.Shell
  $s = $ws.CreateShortcut($Path)
  $s.TargetPath = $Target
  if ($Arguments) { $s.Arguments = $Arguments }
  $s.WorkingDirectory = $WorkDir
  if ($Icon) { $s.IconLocation = $Icon }
  $s.Save()
}

$startMenu = [Environment]::GetFolderPath("Programs")
$desktop = [Environment]::GetFolderPath("Desktop")

if (-not $FromSource) {
  $exe = Join-Path $Dir "YingSay.exe"
  $url = "https://github.com/$Repo/releases/latest/download/YingSay.exe"
  Write-Host "下载 $url ..."
  Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
  Unblock-File $exe
  $target = $exe; $targetArgs = ""; $icon = $exe
  $cli = "`"$exe`""
} else {
  $py = $null
  foreach ($c in @("py -3.12", "py -3", "python")) {
    try { $v = & cmd /c "$c --version" 2>$null; if ($LASTEXITCODE -eq 0 -and $v -match "Python 3\.(1[0-9])") { $py = $c; break } } catch {}
  }
  if (-not $py) {
    Write-Host "未找到 Python 3.10+，使用 winget 安装 Python 3.12 ..."
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $py = "py -3.12"
  }
  $src = Join-Path $Dir "src"
  $zip = Join-Path $env:TEMP "yingsay-src.zip"
  Invoke-WebRequest -Uri "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
  if (Test-Path $src) { Remove-Item -Recurse -Force $src }
  Expand-Archive -Path $zip -DestinationPath $Dir -Force
  Move-Item -Force (Join-Path $Dir "YingSay-main") $src
  & cmd /c "$py -m venv `"$Dir\venv`""
  & "$Dir\venv\Scripts\python.exe" -m pip install --upgrade pip
  & "$Dir\venv\Scripts\python.exe" -m pip install "$src"
  $target = "$Dir\venv\Scripts\pythonw.exe"; $targetArgs = "-m yingsay"; $icon = ""
  $cli = "`"$Dir\venv\Scripts\python.exe`" -m yingsay"
}

# 清理旧名字 FreeTypeless 的快捷方式
foreach ($old in @((Join-Path $startMenu "FreeTypeless.lnk"), (Join-Path $desktop "FreeTypeless.lnk"))) {
  if (Test-Path $old) { Remove-Item -Force $old }
}
New-Shortcut (Join-Path $startMenu "YingSay.lnk") $target $targetArgs $Dir $icon
New-Shortcut (Join-Path $desktop "YingSay.lnk") $target $targetArgs $Dir $icon

if ($ApiKey) {
  $extra = if ($BaseUrl) { "--set-base-url `"$BaseUrl`"" } else { "" }
  & cmd /c "$cli --set-key `"$ApiKey`" $extra"
}

Write-Host "安装完成：$Dir"
Write-Host "已创建桌面与开始菜单快捷方式，正在启动 ..."
if ($targetArgs) { Start-Process -FilePath $target -ArgumentList $targetArgs -WorkingDirectory $Dir } else { Start-Process -FilePath $target -WorkingDirectory $Dir }
