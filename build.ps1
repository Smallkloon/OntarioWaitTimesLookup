<#
.SYNOPSIS
	Builds CTWaits.exe (tkinter GUI) and ctwaits-mcp.exe (MCP server over stdio).
.DESCRIPTION
	Creates a virtual environment, installs requirements.txt, runs the pytest
	suite, then runs PyInstaller twice. Both exes land in dist\.
.PARAMETER Python
	Python used to create the venv (default: python on PATH). Python 3.10 or newer.
.PARAMETER VenvDir
	Venv location relative to this script (default: .venv).
.PARAMETER SkipTests
	Skip the pytest run.
.EXAMPLE
	.\build.ps1
.EXAMPLE
	.\build.ps1 -Python py -SkipTests
#>
[CmdletBinding()]
param(
	[string]$Python = "python",
	[string]$VenvDir = ".venv",
	[switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root $VenvDir
$py = Join-Path $venv "Scripts\python.exe"

function Invoke-Step {
	# Runs a native command and fails on a non-zero exit code. Native tools (pip,
	# pytest, PyInstaller) write progress to stderr, which Windows PowerShell 5.1
	# would otherwise turn into a terminating error under "Stop".
	param([string]$Label, [string]$Exe, [string[]]$Arguments)
	Write-Host "==> $Label"
	$prev = $ErrorActionPreference
	$ErrorActionPreference = "Continue"
	& $Exe @Arguments
	$code = $LASTEXITCODE
	$ErrorActionPreference = $prev
	if ($code -ne 0) { throw "$Label failed (exit code $code)" }
}

if (-not (Test-Path $py)) {
	Invoke-Step "Creating venv at $venv" $Python @("-m", "venv", $venv)
}

Invoke-Step "Upgrading pip" $py @("-m", "pip", "install", "--upgrade", "pip", "--quiet")
Invoke-Step "Installing requirements" $py @("-m", "pip", "install", "--quiet", "-r", (Join-Path $root "requirements.txt"))

if (-not $SkipTests) {
	Invoke-Step "Running tests" $py @("-m", "pytest", "-q", (Join-Path $root "tests"))
}

$contacts = Join-Path $root "ctwaits\contacts.json"
if (-not (Test-Path $contacts)) { throw "Missing $contacts (researched phone and fax table)" }

$common = @(
	"-m", "PyInstaller",
	"--noconfirm", "--clean", "--onefile", "--log-level", "WARN",
	"--paths", $root,
	"--add-data", "$contacts;ctwaits",
	"--distpath", (Join-Path $root "dist"),
	"--workpath", (Join-Path $root "build"),
	"--specpath", (Join-Path $root "build")
)

Invoke-Step "Building CTWaits.exe" $py ($common + @("--windowed", "--name", "CTWaits", (Join-Path $root "ctwaits\gui.py")))
Invoke-Step "Building ctwaits-mcp.exe" $py ($common + @("--console", "--name", "ctwaits-mcp", (Join-Path $root "ctwaits\mcp_server.py")))

Write-Host ""
Write-Host "Build complete:"
Get-ChildItem (Join-Path $root "dist") -Filter *.exe | ForEach-Object {
	"  {0,-18} {1,8:N0} KB" -f $_.Name, ($_.Length / 1KB)
}
