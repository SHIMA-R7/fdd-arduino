param([string]$Json, [int]$LimitSec = 0, [string]$Port = 'COM7')

$seq = Get-Content -LiteralPath $Json -Raw | ConvertFrom-Json
$p = New-Object System.IO.Ports.SerialPort $Port, 115200
$p.NewLine = "`n"
$p.DtrEnable = $false
$p.WriteTimeout = 200
$p.Open()
Start-Sleep -Milliseconds 300
$p.DiscardInBuffer()

# タイミング精度を上げる（Start-Sleepは約15ms刻みなので、直前はスピン待ちにする）
$sw = [Diagnostics.Stopwatch]::StartNew()
$t = 0.0
$count = 0
$maxLate = 0.0

foreach ($n in $seq) {
    $freq = [int]$n[0]
    $dur = [int]$n[1]
    $p.WriteLine("F$freq")

    $late = $sw.Elapsed.TotalMilliseconds - $t
    if ($late -gt $maxLate) { $maxLate = $late }

    $t += $dur
    if ($LimitSec -gt 0 -and $t -gt $LimitSec * 1000) { break }

    # 目標の20ms手前までは眠り、そこからはスピン待ち
    $target = $t - 20
    while ($sw.Elapsed.TotalMilliseconds -lt $target) { Start-Sleep -Milliseconds 5 }
    while ($sw.Elapsed.TotalMilliseconds -lt $t) { [Threading.Thread]::SpinWait(200) }
    $count++
}
$p.WriteLine("F0")
Start-Sleep -Milliseconds 200
$p.Close()
"再生終了: $count 音 / $([int]($sw.Elapsed.TotalSeconds)) 秒 / 最大遅れ $([int]$maxLate) ms"
