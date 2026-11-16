<#
.SYNOPSIS
  mongodbtop 설치 / 업데이트 / 제거 (Windows)

.DESCRIPTION
  .\install.ps1               설치 (저장소 안에서 실행하면 그 소스로 설치)
  .\install.ps1 -Update       git pull 후 다시 설치
  .\install.ps1 -Uninstall    명령과 가상환경 제거 (~\.mongodbtop.ini는 남김)

  저장소 밖에서 실행하면 -Repo를 -Dir에 clone한 뒤 설치한다.
  설치 결과: <Dir>\.venv 에 가상환경, <BinDir>\mongodbtop.cmd 실행 파일 (BinDir은 사용자 PATH에 추가).
  실행 정책에 막히면: powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[CmdletBinding()]
param(
    [switch]$Update,
    [switch]$Uninstall,
    [string]$Repo = $(if ($env:MONGODBTOP_REPO) { $env:MONGODBTOP_REPO } else { "https://github.com/bylee5/mongodbtop.git" }),
    [string]$Branch = "main",
    [string]$Dir = $(if ($env:MONGODBTOP_HOME) { $env:MONGODBTOP_HOME } else { Join-Path $env:LOCALAPPDATA "mongodbtop" }),
    [string]$BinDir = (Join-Path $HOME ".local\bin"),
    [string]$Python = "",
    [switch]$Keyring
)
$ErrorActionPreference = "Stop"

function Say($m) { Write-Host "==> $m" }
function Die($m) { Write-Host "error: $m" -ForegroundColor Red; exit 1 }

function Invoke-Native {
    # 네이티브 명령 실패를 오류로 바꾼다 (PowerShell 5.1에서도 동작).
    param([string]$Exe, [string[]]$Arguments, [string]$OnFail)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Die $(if ($OnFail) { $OnFail } else { "$Exe $($Arguments -join ' ') failed" }) }
}

# 이 스크립트가 mongodbtop 저장소 안에 있으면 그 소스를 쓴다.
$scriptDir = $PSScriptRoot
$dirFull = [IO.Path]::GetFullPath($Dir).TrimEnd('\')
if ($scriptDir -and ($scriptDir.TrimEnd('\') -ne $dirFull) -and
    (Test-Path (Join-Path $scriptDir "pyproject.toml")) -and (Test-Path (Join-Path $scriptDir "mongodbtop"))) {
    $src = $scriptDir; $managed = $false      # 사용자가 직접 받은 소스: 제거할 때 소스는 지우지 않는다
} else {
    $src = $dirFull; $managed = $true          # 설치 스크립트가 clone한 소스
}
$venv = Join-Path $src ".venv"
$shim = Join-Path $BinDir "mongodbtop.cmd"

function Test-OurShim {
    # 이 설치 스크립트가 만든 mongodbtop.cmd 인가 (소스 위치는 상관없음)
    if (-not (Test-Path $shim)) { return $false }
    return [bool](Select-String -Path $shim -SimpleMatch "\.venv\Scripts\mongodbtop.exe" -Quiet)
}

function Find-Python {
    $check = "import sys; sys.exit(sys.version_info < (3, 9))"
    if ($Python) {
        & $Python -c $check 2>$null
        if ($LASTEXITCODE -ne 0) { Die "$Python not found or older than Python 3.9" }
        return @($Python)
    }
    # py 런처를 먼저 (python.exe는 Microsoft Store 바로가기일 수 있다).
    $candidates = @(@("py", "-3"), @("python3"), @("python"))
    foreach ($c in $candidates) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $rest = @($c | Select-Object -Skip 1)
        & $c[0] @rest -c $check 2>$null
        if ($LASTEXITCODE -eq 0) { return $c }
    }
    Die "Python 3.9 or newer is required (https://www.python.org/downloads/ or: winget install Python.Python.3.12)"
}

function Get-Source {
    if (-not $managed) {
        if ($Update) {
            if (Test-Path (Join-Path $src ".git")) {
                Say "updating source in $src"
                Invoke-Native git @("-C", $src, "pull", "--ff-only")
            } else {
                Write-Warning "$src is not a git checkout; reinstalling the files as they are"
            }
        }
        return
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git is required (winget install Git.Git)" }
    if (Test-Path (Join-Path $src ".git")) {
        Say "updating source in $src"
        Invoke-Native git @("-C", $src, "fetch", "--quiet", "origin", $Branch)
        Invoke-Native git @("-C", $src, "checkout", "--quiet", $Branch)
        Invoke-Native git @("-C", $src, "pull", "--ff-only", "--quiet", "origin", $Branch)
    } elseif ((Test-Path $src) -and (Get-ChildItem $src -Force | Select-Object -First 1)) {
        Die "$src exists and is not a mongodbtop checkout; choose another -Dir"
    } else {
        Say "cloning $Repo ($Branch) into $src"
        Invoke-Native git @("clone", "--quiet", "--branch", $Branch, $Repo, $src) `
            "git clone failed. Check network access to github.com (proxy / firewall) and the -Repo URL."
    }
}

function Install-Mongodbtop {
    $py = Find-Python
    $pyRest = @($py | Select-Object -Skip 1)
    $ver = & $py[0] @pyRest -c "import sys; print(sys.version.split()[0])"
    Say "using $($py -join ' ') ($ver)"

    # 같은 이름의 다른 프로그램은 덮어쓰지 않는다.
    if ((Test-Path $shim) -and -not (Test-OurShim)) {
        Die "$shim already exists and was not created by this installer. Remove it yourself, or use -BinDir DIR"
    }

    Get-Source

    $vpy = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $vpy)) {
        Say "creating virtualenv $venv"
        if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
        Invoke-Native $py[0] ($pyRest + @("-m", "venv", $venv))
    }
    $extras = if ($Keyring) { "[keyring]" } else { "" }
    Say "installing mongodbtop$extras"
    Invoke-Native $vpy @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--upgrade", "pip")
    Invoke-Native $vpy @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-e", "$src$extras")

    $exe = Join-Path $venv "Scripts\mongodbtop.exe"
    New-Item -ItemType Directory -Force $BinDir | Out-Null
    Set-Content -Path $shim -Encoding Ascii -Value "@`"$exe`" %*"
    Say "created $shim -> $exe"
    Invoke-Native $shim @("--version")

    $first = Get-Command mongodbtop -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($first -and ($first.Source -ne $shim)) {
        Write-Warning "another mongodbtop comes first on PATH: $($first.Source). Run $shim directly, or put $BinDir earlier in PATH"
    }

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($userPath -split ";" | Where-Object { $_ })
    if ($parts -notcontains $BinDir) {
        [Environment]::SetEnvironmentVariable("Path", (($parts + $BinDir) -join ";"), "User")
        Say "added $BinDir to your user PATH (open a new terminal to use 'mongodbtop')"
    }

    if (-not (Test-Path (Join-Path $HOME ".mongodbtop.ini")) -and -not $env:MONGODBTOP_CONFIG) {
        Write-Host ""
        Write-Host "No server list yet. To create one:"
        Write-Host "  Copy-Item `"$src\mongodbtop.ini.example`" ~\.mongodbtop.ini; notepad ~\.mongodbtop.ini"
        Write-Host "  mongodbtop --list"
    }
    Write-Host ""
    Say "done. Update later with: $src\install.ps1 -Update"
}

function Uninstall-Mongodbtop {
    if (Test-OurShim) { Remove-Item -Force $shim; Say "removed $shim" }
    elseif (Test-Path $shim) { Write-Warning "$shim was not created by this installer; left in place" }
    if ($managed) {
        if (Test-Path $src) { Remove-Item -Recurse -Force $src; Say "removed $src" }
    } elseif (Test-Path $venv) {
        Remove-Item -Recurse -Force $venv; Say "removed $venv (source left in place)"
    }
    if (Test-Path (Join-Path $HOME ".mongodbtop.ini")) { Say "kept ~\.mongodbtop.ini" }
}

if ($Uninstall) { Uninstall-Mongodbtop } else { Install-Mongodbtop }
