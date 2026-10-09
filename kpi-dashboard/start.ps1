# Запуск дашборда KPI. Если Python не установлен, скачивает портативный Python в папку python\ (один раз, без прав администратора).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$pyVersion = "3.12.10"
$localPy = Join-Path $root "python\python.exe"

function Test-Python($exe, $extra) {
    try {
        $a = @()
        if ($extra) { $a += $extra }
        $a += @("-c", "import sqlite3, sys; print(sys.version_info >= (3, 10))")
        $out = & $exe @a 2>$null
        return ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq "True")
    } catch { return $false }
}

$py = $null; $pyArgs = $null
if (Test-Path $localPy) { $py = $localPy }
elseif (Test-Python "py" "-3") { $py = "py"; $pyArgs = "-3" }
elseif (Test-Python "python" $null) { $py = "python" }

if (-not $py) {
    Write-Host ""
    Write-Host "Python не найден. Скачиваю портативный Python $pyVersion (около 11 МБ) в папку python\ ..." -ForegroundColor Yellow
    $url = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip"
    $zip = Join-Path $env:TEMP "python-$pyVersion-embed-amd64.zip"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath (Join-Path $root "python") -Force
        Remove-Item $zip -ErrorAction SilentlyContinue
        # разрешаем портативному Python видеть папку дашборда
        $pth = Get-ChildItem (Join-Path $root "python") -Filter "python*._pth" | Select-Object -First 1
        if ($pth) { Add-Content -Path $pth.FullName -Value ".." -Encoding ASCII }
    } catch {
        Write-Host ""
        Write-Host "Не удалось скачать Python: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Установите Python вручную: https://www.python.org/downloads/ (при установке отметьте 'Add python.exe to PATH'),"
        Write-Host "затем снова запустите run.bat."
        exit 1
    }
    $py = $localPy
    Write-Host "Готово." -ForegroundColor Green
}

Write-Host ""
Write-Host "Запускаю дашборд. Не закрывайте это окно, пока работаете с дашбордом." -ForegroundColor Cyan
if ($pyArgs) { & $py $pyArgs manage.py run } else { & $py manage.py run }
