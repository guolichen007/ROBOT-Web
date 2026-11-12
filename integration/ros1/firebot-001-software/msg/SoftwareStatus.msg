# /firebot_001/software/status —— 软件状态机与诊断状态
string state           # idle / ready / patrol / homing / fault
float64 uptime_seconds
int32 pid
float64 load_1m
float64 mem_available_bytes
string diagnostics     # 简短诊断文本
string ts
