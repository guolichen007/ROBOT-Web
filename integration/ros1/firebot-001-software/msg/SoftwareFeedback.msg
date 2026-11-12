# /firebot_001/software/feedback —— 命令执行反馈（真实状态机结果）
string command_id
string action
string status          # ACCEPTED / RUNNING / COMPLETED / REJECTED / STOPPED
string detail          # 说明（含错误原因）
bool software_test     # 恒为 true：本反馈来自软件测试闭环，无硬件执行
string ts
