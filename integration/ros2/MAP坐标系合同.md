# MAP 坐标系合同

`frame_id=map`；x/y 单位米；theta 单位弧度；theta=0 指向 +X；正方向逆时针。location 必须携带 site_code、map_code、map_version、map_checksum。数据库与 MQTT 保存世界坐标，像素转换仅由 Web MapAdapter 完成。
