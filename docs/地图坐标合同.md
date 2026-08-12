# 地图坐标与版本合同

`frame_id=map`；x/y 为米；theta 为弧度；theta=0 指向 +X；正方向逆时针。世界原点、rotation、resolution、image pixel origin 和 screen Y flip 由 MapAdapter 最后一层转换，数据库与 MQTT 永远保存世界坐标。

location 必须带 site_code、map_code、map_version、map_checksum、frame_id。Published map version 不可原地修改，变更产生新版本。任务派发前机器人版本/checksum 必须与目标一致；任务保存 map/version/semantic revision/目标姿态/轨迹快照。
