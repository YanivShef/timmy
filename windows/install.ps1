#Requires -Version 5.1
<#
  מתקין את טימי לווינדוס, למשתמש הנוכחי, בלי הרשאות מנהל. Claude Code מריץ אותו לפי INSTALL.md:
    powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\Timmy\windows\install.ps1" -Name "השם שלך" [-About "העסק"] [-People "דנה כהן, אבי לוי"] [-OutputDir "D:\הקלטות"]

  מה הוא עושה (כל שלב מדפיס שורה שמתחילה ב-"==>"):
    1. בודק: ווינדוס 64 ביט, Windows 10 גרסה 2004 (build 19041) ומעלה, מקום פנוי, חיבור לאינטרנט
    2. uv (מנהל Python קטן) לתיקייה של טימי אם אין, Python 3.12 בסביבה משלו, ההתקנה של טימי והחבילות שלו
    3. מודל העברית (ivrit.ai, בערך 1.6GB): מוריד, וממשיך מאיפה שעצר אם נקטע
    4. config.json: אותם מפתחות כמו במק. תיקיית הקלטות שכבר נבחרה לא נדרסת
    5. קיצורי דרך "טימי" בתפריט התחל ובשולחן העבודה, הפעלה עם הכניסה למחשב, ופותח את טימי (שמתחיל באונבורדינג)
  ההקלטות לא נוגעים בהן. מריצים שוב כדי לעדכן.

  לבדיקות: -AppDir (תיקייה אחרת במקום %LOCALAPPDATA%\Timmy), -Model (למשל Systran/faster-whisper-tiny), -NoModel,
  -NoShortcuts, -NoStartup, -NoLaunch, -DryRun (רק הבדיקות והתוכנית, בלי להוריד ובלי לשנות כלום).
  הקובץ שמור ב-UTF-8 עם BOM: בלי זה PowerShell 5.1 קורא את העברית בקידוד הלא נכון.
#>
[CmdletBinding()]
param(
    [string]$Name = "",
    [string]$About = "",
    [string]$People = "",
    [string]$OutputDir = "",
    [string]$AppDir = "",
    [string]$Model = "",
    [switch]$NoModel,
    [switch]$NoShortcuts,
    [switch]$NoStartup,
    [switch]$NoLaunch,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$DefaultAppDir = Join-Path $env:LOCALAPPDATA "Timmy"
if (-not $AppDir) { $AppDir = $DefaultAppDir }
if ($AppDir -ne $DefaultAppDir) { $env:TIMMY_APP_DIR = $AppDir }
$DefaultModel = "ivrit-ai/whisper-large-v3-turbo-ct2"
if (-not $Model) { $Model = $DefaultModel }
$Venv = Join-Path $AppDir "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$PyW = Join-Path $Venv "Scripts\pythonw.exe"

function Say([string]$m) { Write-Host "==> $m" }
function Ok([string]$m) { Write-Host "  ✓ $m" }
function Warn([string]$m) { Write-Host "  ! $m" }
function Fail([string]$m) { Write-Host "✗ $m"; exit 1 }

function Helper([string[]]$HelperArgs) {
    # one JSON line from timmy.install_helper; never pass empty strings to a native program (PowerShell 5.1 drops them)
    $out = & $Py -m timmy.install_helper @HelperArgs 2>&1
    $line = ($out | Where-Object { "$_".StartsWith("{") } | Select-Object -Last 1)
    if (-not $line) { return @{ ok = $false; error = ($out -join "`n") } }
    return ($line | ConvertFrom-Json)
}

# ---------------------------------------------------------------------------------------------------------------------
Say "בודק את המחשב"
if (-not [Environment]::Is64BitOperatingSystem) { Fail "טימי צריך ווינדוס של 64 ביט." }
$build = [Environment]::OSVersion.Version.Build
if ($build -lt 19041) {
    Fail "צריך Windows 10 גרסה 2004 ומעלה, או Windows 11 (עכשיו: build $build). מעדכנים בהגדרות ואז Windows Update, ומריצים שוב."
}
Ok "ווינדוס build $build"

$modelDir = Join-Path $AppDir ("models\" + ($Model -replace "/", "__"))
$needGB = 4
if ($NoModel -or (Test-Path (Join-Path $modelDir "model.bin"))) { $needGB = 2 }
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$root = [IO.Path]::GetPathRoot((Resolve-Path $AppDir).Path)
$freeGB = [math]::Round((New-Object System.IO.DriveInfo $root).AvailableFreeSpace / 1GB, 1)
if ($freeGB -lt $needGB) { Fail "צריך לפחות $needGB GB פנויים בכונן $root (יש עכשיו $freeGB GB). אפשר לרוקן את סל המיחזור ואת תיקיית ההורדות." }
Ok "מקום פנוי: $freeGB GB"

$online = $true
try { Invoke-WebRequest -Uri "https://huggingface.co" -Method Head -UseBasicParsing -TimeoutSec 15 | Out-Null } catch { $online = $false }
if (-not $online) {
    if ($DryRun) { Warn "אין חיבור לאינטרנט (בהתקנה אמיתית זה היה עוצר)" }
    else { Fail "אין חיבור לאינטרנט (צריך אותו להורדת מודל העברית). בדוק את החיבור והרץ שוב." }
} else { Ok "יש אינטרנט" }

if ($DryRun) {
    Say "בדיקה בלבד (DryRun): לא הורדתי ולא שיניתי כלום"
    Write-Host "  תיקיית טימי: $AppDir"
    Write-Host "  מודל: $Model"
    Write-Host "  קוד: $Here"
    Write-Host "  קיצורי דרך: $(-not $NoShortcuts)  הפעלה עם הכניסה: $(-not $NoStartup)  פתיחה בסוף: $(-not $NoLaunch)"
    exit 0
}

# ---------------------------------------------------------------------------------------------------------------------
Say "מכין את Python ואת טימי (בפעם הראשונה כמה דקות)"
$uv = $null
$cmd = Get-Command uv -ErrorAction SilentlyContinue
if ($cmd) { $uv = $cmd.Source }
if (-not $uv) {
    $uvDir = Join-Path $AppDir "uv"
    $uv = Join-Path $uvDir "uv.exe"
    if (-not (Test-Path $uv)) {
        $zip = Join-Path $env:TEMP "timmy-uv.zip"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
        } catch { Fail "ההורדה של uv נכשלה. בדוק את החיבור לאינטרנט והרץ שוב." }
        New-Item -ItemType Directory -Force -Path $uvDir | Out-Null
        Expand-Archive -Path $zip -DestinationPath $uvDir -Force
        Remove-Item $zip -ErrorAction SilentlyContinue
        $found = Get-ChildItem -Path $uvDir -Recurse -Filter uv.exe | Select-Object -First 1
        if (-not $found) { Fail "לא מצאתי את uv אחרי ההורדה." }
        $uv = $found.FullName
    }
}
Ok "uv: $uv"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $AppDir "python"
if (-not (Test-Path $Py)) {
    & $uv venv --quiet --python 3.12 $Venv
    if ($LASTEXITCODE -ne 0) { Fail "יצירת סביבת Python נכשלה." }
}
& $uv pip install --quiet --python $Py --reinstall-package timmy-windows "$($Here)[audio,asr,ui,system]"
if ($LASTEXITCODE -ne 0) { Fail "ההתקנה של החבילות נכשלה. בדוק את החיבור לאינטרנט והרץ שוב (ממשיך מאיפה שעצר)." }
$ver = & $Py -m timmy --version
Ok "טימי $ver"

# ---------------------------------------------------------------------------------------------------------------------
if (-not $NoModel) {
    Say "מוריד את מודל העברית ($Model). זה החלק הארוך"
    $r = Helper @("model", "--repo", $Model)
    if (-not $r.ok) { Fail "הורדת המודל נכשלה: $($r.error). הרץ שוב, ההורדה ממשיכה מאיפה שעצרה." }
    Ok "המודל מוכן"
}

# ---------------------------------------------------------------------------------------------------------------------
Say "שומר את ההגדרות"
$cargs = @("config")
if ($Name.Trim()) { $cargs += @("--name", $Name) }
if ($About.Trim()) { $cargs += @("--about", $About) }
if ($People.Trim()) { $cargs += @("--people", $People) }
if ($OutputDir.Trim()) { $cargs += @("--output-dir", $OutputDir) }
$r = Helper $cargs
if (-not $r.ok) { Fail "שמירת ההגדרות נכשלה: $($r.error)" }
Ok "שם: $($r.owner_name) | תיקייה: $($r.output_dir)"

# ---------------------------------------------------------------------------------------------------------------------
if (-not $NoShortcuts) {
    Say "יוצר קיצורי דרך"
    $ico = Join-Path $AppDir "timmy.ico"
    $null = Helper @("icon", $ico)
    $shell = New-Object -ComObject WScript.Shell
    foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
        $lnk = $shell.CreateShortcut((Join-Path $dir "טימי.lnk"))
        $lnk.TargetPath = $PyW
        $lnk.Arguments = "-m timmy"
        $lnk.WorkingDirectory = $AppDir
        if (Test-Path $ico) { $lnk.IconLocation = $ico }
        $lnk.Description = "טימי: מקליט ומתמלל שיחות בעברית"
        $lnk.Save()
    }
    Ok "תפריט התחל ושולחן העבודה"
}
if (-not $NoStartup) {
    $r = Helper @("startup", "on")
    if ($r.ok) { Ok "טימי יעלה לבד כשנכנסים למחשב" } else { Warn "לא הצלחתי להגדיר הפעלה עם הכניסה למחשב (אפשר מהתפריט של טימי)" }
}

# ---------------------------------------------------------------------------------------------------------------------
Say "בודק את Claude Code"
$r = Helper @("claude")
if (-not $r.path) { Warn "Claude Code לא נמצא: טימי יתמלל, ובלי Claude לא יהיו נקודות וסיכום. האונבורדינג של טימי יעזור להתקין." }
elseif ($r.logged_in -eq $true) { Ok "Claude Code מחובר" }
else { Warn "Claude Code לא מחובר לחשבון: האונבורדינג של טימי יחבר אותו (בדפדפן)." }
if (-not $r.git_bash) { Warn "חסר Git for Windows (Claude Code בווינדוס צריך אותו). האונבורדינג של טימי ייתן קישור." }

@{ installed = (Get-Date -Format s); version = "$ver"; source = $Here; model = $Model } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $AppDir "install.json")

if (-not $NoLaunch) {
    Say "פותח את טימי"
    Start-Process -FilePath $PyW -ArgumentList "-m", "timmy" -WorkingDirectory $AppDir
}
Write-Host "✓ טימי מותקן."
exit 0
