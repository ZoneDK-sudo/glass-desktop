# Copies MemoMind's Web SDK file into the plugin's vendor folder.
# Run from anywhere inside your plugin-open-platform checkout:
#   powershell -ExecutionPolicy Bypass -File plugin\get-sdk.ps1
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dir = $here
while ($dir -and -not (Test-Path (Join-Path $dir "PhoneSDK"))) { $dir = Split-Path -Parent $dir }
if (-not $dir) { Write-Error "Couldn't find PhoneSDK. Put this repo inside your plugin-open-platform folder."; exit 1 }
$sdk = Get-ChildItem (Join-Path $dir "PhoneSDK") -Recurse -Filter "gm-plugin-web-sdk.esm.js" |
       Where-Object { $_.FullName -notmatch "node_modules" } | Select-Object -First 1
if (-not $sdk) { Write-Error "gm-plugin-web-sdk.esm.js not found under $dir\PhoneSDK"; exit 1 }
$dest = Join-Path $here "glass-desktop\vendor"
Copy-Item $sdk.FullName $dest -Force
Write-Host "Copied $($sdk.FullName) -> $dest"
