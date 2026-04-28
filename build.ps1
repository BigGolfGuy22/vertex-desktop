# Vertex Desktop — bundle into a single .exe with PyInstaller.
#
# Requires (one-time):   pip install pywebview pywin32 pyinstaller bleak pillow
# Run:                   .\build.ps1
# Output:                dist\VertexDesktop.exe   (~40-60 MB, no Python needed)

$ErrorActionPreference = "Stop"

# Clean previous build artefacts so stale caches don't leak in
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }
if (Test-Path VertexDesktop.spec) { Remove-Item VertexDesktop.spec -Force }

# Hidden imports — webview backends + async libs PyInstaller can miss.
$hiddenImports = @(
    "webview.platforms.edgechromium",
    "webview.platforms.mshtml",
    "clr_loader",
    "bleak.backends.winrt",
    "bleak.backends.winrt.client",
    "bleak.backends.winrt.scanner"
)
$hiddenArgs = $hiddenImports | ForEach-Object { "--hidden-import=$_" }

pyinstaller `
    --name VertexDesktop `
    --onefile `
    --windowed `
    --icon vertex.ico `
    --add-data "ui.html;." `
    --add-data "assets;assets" `
    @hiddenArgs `
    vertex_desktop.py

if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed with exit code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "----------------------------------------"  -ForegroundColor Green
Write-Host "Built: $(Resolve-Path dist\VertexDesktop.exe)" -ForegroundColor Green
Write-Host "Size:  $('{0:N1}' -f ((Get-Item dist\VertexDesktop.exe).Length / 1MB)) MB" -ForegroundColor Green
Write-Host "----------------------------------------"  -ForegroundColor Green
Write-Host "Double-click to launch, or run:"
Write-Host "  dist\VertexDesktop.exe"
