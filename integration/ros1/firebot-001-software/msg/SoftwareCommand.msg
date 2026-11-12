# /firebot_001/software/command —— 软件测试命令（SOFTWARE_TEST，无硬件执行器）
string command_id      # 与云端 command_id/msg_id 对应的唯一标识
string action          # start / close / patrol / stop_patrol / status
string params          # JSON 字符串（软件级参数）
