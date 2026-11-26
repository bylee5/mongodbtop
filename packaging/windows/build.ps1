<#
.SYNOPSIS
  mongodbtop Windows 설치 파일(MSI)을 만든다.

.DESCRIPTION
  1. build\windows\venv 에 PyInstaller 와 mongodbtop 설치
  2. PyInstaller 로 Python 런타임을 포함한 mongodbtop.exe (onedir) 생성
     → 대상 PC 에 Python 이 없어도 동작
  3. WiX 5 로 MSI 생성: Program Files\mongodbtop 에 설치, 시스템 PATH 에 추가

  결과: dist\mongodbtop-<version>-x64.msi
  필요: Python 3.9+ (py 런처 또는 -Python), .NET 6 이상 런타임 (WiX 실행용), 인터넷 (PyPI, NuGet)
  WiX 는 build\windows\tools 에 받아서 쓰며 시스템에 설치하지 않는다.
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$WixVersion = "5.0.2"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Say($m) { Write-Host "==> $m" }
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed (exit $LASTEXITCODE)" }
}

$root  = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$here  = $PSScriptRoot
$build = Join-Path $root "build\windows"
$dist  = Join-Path $root "dist"
New-Item -ItemType Directory -Force $build, $dist | Out-Null

$version = (Select-String -Path (Join-Path $root "mongodbtop\__init__.py") -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value
Say "mongodbtop $version"

# 1. 빌드용 가상환경 -------------------------------------------------------
$venv = Join-Path $build "venv"
$vpy  = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    if ($Python) { $py = @($Python) } elseif (Get-Command py -ErrorAction SilentlyContinue) { $py = @("py", "-3") } else { $py = @("python") }
    $rest = @($py | Select-Object -Skip 1)
    Say "creating build venv ($($py -join ' '))"
    Invoke-Native $py[0] ($rest + @("-m", "venv", $venv))
}
Say "installing PyInstaller and mongodbtop"
Invoke-Native $vpy @("-m", "pip", "install", "-q", "--disable-pip-version-check", "--upgrade", "pip", "pyinstaller")
Invoke-Native $vpy @("-m", "pip", "install", "-q", "--disable-pip-version-check", "--force-reinstall", "--no-deps", $root)
Invoke-Native $vpy @("-m", "pip", "install", "-q", "--disable-pip-version-check", $root)

# 2. PyInstaller (onedir: 시작이 빠르고 백신 오탐이 적다) -------------------
$appOut = Join-Path $build "app"
Say "running PyInstaller"
Invoke-Native $vpy @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
    "--console", "--name", "mongodbtop",
    "--distpath", $appOut, "--workpath", (Join-Path $build "work"), "--specpath", $build,
    "--collect-submodules", "pymongo", "--collect-submodules", "bson", "--collect-submodules", "dns",
    "--exclude-module", "tkinter",
    (Join-Path $here "entry.py")
)
$appDir = Join-Path $appOut "mongodbtop"
foreach ($f in "LICENSE", "README.md", "mongodbtop.ini.example") { Copy-Item (Join-Path $root $f) $appDir }
$out = & (Join-Path $appDir "mongodbtop.exe") --version
if ($LASTEXITCODE -ne 0 -or $out -notmatch [regex]::Escape($version)) { throw "frozen mongodbtop.exe failed: $out" }
Say "frozen app ok: $out"

# 설치 화면의 라이선스 (RTF)
$rtf = Join-Path $build "license.rtf"
$body = (Get-Content (Join-Path $root "LICENSE")) | ForEach-Object { ($_ -replace '\\', '\\' -replace '\{', '\{' -replace '\}', '\}') + '\par' }
Set-Content -Path $rtf -Encoding Ascii -Value ("{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}\f0\fs18`r`n" + ($body -join "`r`n") + "`r`n}")

# 3. WiX (NuGet 패키지를 받아 .NET 런타임으로 실행) --------------------------
$tools = Join-Path $build "tools"
function Get-NuGet([string]$id, [string]$ver) {
    $dir = Join-Path $tools "$id.$ver"
    if (-not (Test-Path $dir)) {
        $lid = $id.ToLowerInvariant()
        $pkg = Join-Path $tools "$id.$ver.zip"
        New-Item -ItemType Directory -Force $tools | Out-Null
        Say "downloading $id $ver"
        Invoke-WebRequest "https://api.nuget.org/v3-flatcontainer/$lid/$ver/$lid.$ver.nupkg" -OutFile $pkg
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($pkg, $dir)
        Remove-Item $pkg
    }
    return $dir
}
$wixDll = Get-ChildItem (Get-NuGet "wix" $WixVersion) -Recurse -Filter wix.dll | Select-Object -First 1
$uiExt  = Get-ChildItem (Get-NuGet "WixToolset.UI.wixext" $WixVersion) -Recurse -Filter WixToolset.UI.wixext.dll | Select-Object -First 1
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) { throw ".NET runtime (dotnet) is required to run WiX: winget install Microsoft.DotNet.Runtime.8" }
$env:DOTNET_ROLL_FORWARD = "Major"   # WiX 5 는 net6.0 대상: 더 새 런타임에서도 실행

$msi = Join-Path $dist "mongodbtop-$version-x64.msi"
Say "building $msi"
Invoke-Native dotnet @(
    $wixDll.FullName, "build", "-nologo", "-arch", "x64",
    "-ext", $uiExt.FullName,
    "-d", "Version=$version", "-d", "AppDir=$appDir", "-d", "LicenseRtf=$rtf",
    "-intermediatefolder", (Join-Path $build "wixobj"),
    "-o", $msi,
    (Join-Path $here "mongodbtop.wxs")
)
Remove-Item (Join-Path $dist "mongodbtop-$version-x64.wixpdb") -ErrorAction SilentlyContinue
Say ("done: {0} ({1:N1} MB)" -f $msi, ((Get-Item $msi).Length / 1MB))
