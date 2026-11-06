# install.sh가 부른다. 화 11:00 AI 트렌드 · 목 11:00 스킬 생성, 화 · 목 14:00 전송, 금 06:00 종합 생성 · 09:00 전송, 토 09:00 주간 인사이트 (Windows 로컬 시각 = KST).
param([string]$Distro, [string]$User)
$run = "/home/$User/trend-automation/run.sh"
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -WakeToRun `
       -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
$jobs = @(
  @{ Name = "generate-trends"; Days = "Tuesday";  At = "11:00"; Args = "generate trends" },
  @{ Name = "generate-skills"; Days = "Thursday"; At = "11:00"; Args = "generate skills" },
  @{ Name = "deliver";          Days = "Tuesday","Thursday"; At = "14:00"; Args = "deliver" },
  @{ Name = "generate-weekly"; Days = "Friday";   At = "06:00"; Args = "generate weekly" },
  @{ Name = "deliver-weekly";  Days = "Friday";   At = "09:00"; Args = "deliver" },
  @{ Name = "insight";          Days = "Saturday"; At = "09:00"; Args = "insight" }
)
foreach ($j in $jobs) {
  $cur = Get-ScheduledTask -TaskPath "\trend-automation\" -TaskName $j.Name -ErrorAction SilentlyContinue
  if ($cur -and $cur.State -eq "Running") { Write-Output "skip (running) $($j.Name)"; continue }  # 다시 등록하면 도는 작업이 죽는다
  $act = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distro -u $User -- $run $($j.Args)"
  $trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $j.Days -At $j.At
  Register-ScheduledTask -TaskPath "\trend-automation\" -TaskName $j.Name -Action $act -Trigger $trg -Settings $set -Force | Out-Null
  Write-Output "registered \trend-automation\$($j.Name)"
}
