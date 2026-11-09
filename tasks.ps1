# install.sh가 부른다. 월~목 08:00 매일 판 갱신 · 10:00 전송, 금 07:00 매일 판 마지막 갱신(보내지 않음) · 08:30 종합 생성 · 10:00 종합 전송,
# 토 09:00 주간 인사이트 (Windows 로컬 시각 = KST). 옛 화 · 목 작업은 지운다.
param([string]$Distro, [string]$User)
$run = "/home/$User/trend-automation/run.sh"
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -WakeToRun `
       -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
$jobs = @(
  @{ Name = "generate-daily";  Days = "Monday","Tuesday","Wednesday","Thursday"; At = "08:00"; Args = "generate daily" },
  @{ Name = "deliver";          Days = "Monday","Tuesday","Wednesday","Thursday"; At = "10:00"; Args = "deliver daily" },
  @{ Name = "generate-daily-fri"; Days = "Friday"; At = "07:00"; Args = "generate daily" },
  @{ Name = "generate-weekly"; Days = "Friday";   At = "08:30"; Args = "generate weekly" },
  @{ Name = "deliver-weekly";  Days = "Friday";   At = "10:00"; Args = "deliver weekly" },
  @{ Name = "insight";          Days = "Saturday"; At = "09:00"; Args = "insight" }
)
foreach ($old in "generate-trends", "generate-skills") {
  Unregister-ScheduledTask -TaskPath "\trend-automation\" -TaskName $old -Confirm:$false -ErrorAction SilentlyContinue
}
foreach ($j in $jobs) {
  $cur = Get-ScheduledTask -TaskPath "\trend-automation\" -TaskName $j.Name -ErrorAction SilentlyContinue
  if ($cur -and $cur.State -eq "Running") { Write-Output "skip (running) $($j.Name)"; continue }  # 다시 등록하면 도는 작업이 죽는다
  $act = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distro -u $User -- $run $($j.Args)"
  $trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $j.Days -At $j.At
  Register-ScheduledTask -TaskPath "\trend-automation\" -TaskName $j.Name -Action $act -Trigger $trg -Settings $set -Force | Out-Null
  Write-Output "registered \trend-automation\$($j.Name)"
}
