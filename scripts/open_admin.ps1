param(
    [string]$Url = "http://127.0.0.1:8787/",
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "SilentlyContinue"
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)

while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Start-Process $Url
            exit 0
        }
    } catch {
        # Server is not ready yet.
    }
    Start-Sleep -Milliseconds 500
}

exit 1
