$ErrorActionPreference = 'Stop'

$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
if ($arch -ne 'AMD64') { throw 'This package requires x64 Windows.' }

$runtime = Join-Path $env:LOCALAPPDATA 'SideScreenPanel\runtime'
$lockFile = Join-Path $PSScriptRoot 'requirements.txt'
$version = (Get-FileHash -LiteralPath $lockFile -Algorithm SHA256).Hash.Substring(0, 16)
$venv = Join-Path $runtime $version
$python = Join-Path $venv 'Scripts\python.exe'
$pythonw = Join-Path $venv 'Scripts\pythonw.exe'
$ready = Join-Path $venv '.ready'

$mutex = New-Object System.Threading.Mutex($false, 'Local\SideScreenPanel-bootstrap')
$locked = $false
try {
    try { $locked = $mutex.WaitOne([TimeSpan]::FromMinutes(10)) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another Side Screen Panel setup is still running.' }
    if (-not ((Test-Path -LiteralPath $ready) -and (Test-Path -LiteralPath $pythonw))) {
        Write-Host 'Preparing Side Screen Panel. The first launch downloads Python and its dependencies.'
        New-Item -ItemType Directory -Force -Path $runtime | Out-Null
        $uv = Join-Path $runtime 'uv-0.13.9.exe'
        if (-not (Test-Path -LiteralPath $uv)) {
            $scratch = Join-Path $runtime ([Guid]::NewGuid().ToString())
            New-Item -ItemType Directory -Path $scratch | Out-Null
            try {
                $zip = Join-Path $scratch 'uv.zip'
                Invoke-WebRequest -UseBasicParsing -TimeoutSec 180 -Uri 'https://github.com/astral-sh/uv/releases/download/0.13.9/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip
                if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne 'EDD61F7F5728A56CC62455386661860BDFC7BAB65872F39B0C25BDCC1D8E1BEB') {
                    throw 'uv download failed SHA-256 verification.'
                }
                Expand-Archive -LiteralPath $zip -DestinationPath $scratch
                $download = Get-ChildItem -LiteralPath $scratch -Recurse -File -Filter 'uv.exe' | Select-Object -First 1
                if (-not $download) { throw 'uv.exe is missing from the download.' }
                Move-Item -LiteralPath $download.FullName -Destination $uv
            }
            finally {
                Remove-Item -LiteralPath $zip -ErrorAction SilentlyContinue
                # Remove only the known archive files, then the empty scratch directory.
                Get-ChildItem -LiteralPath $scratch -File | Remove-Item -ErrorAction SilentlyContinue
                if (-not (Get-ChildItem -LiteralPath $scratch -Force)) { Remove-Item -LiteralPath $scratch }
            }
        }
        & $uv --no-config venv --python 3.11 --managed-python --allow-existing $venv
        if ($LASTEXITCODE -ne 0) { throw 'Python setup failed.' }
        & $uv --no-config pip sync --python $python --require-hashes --only-binary ':all:' $lockFile
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
        & $python -c "import sys; sys.path.insert(0, sys.argv[1]); import dashboard" $PSScriptRoot
        if ($LASTEXITCODE -ne 0) { throw 'Panel dependencies failed to load.' }
        New-Item -ItemType File -Force -Path $ready | Out-Null
    }
}
finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

$watchdog = Join-Path $PSScriptRoot 'panel_watchdog.py'
Start-Process -WindowStyle Hidden -FilePath $pythonw -WorkingDirectory $PSScriptRoot -ArgumentList ('"{0}"' -f $watchdog)
