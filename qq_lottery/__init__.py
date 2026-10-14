"""QQ 群活跃群友随机抽奖工具。

按「近期 N 天内发过言的群友」为候选范围随机抽奖，
支持按发言条数加权、黑名单剔除、一次抽取多人不重复。

数据来源：
  1. OneBot v11 机器人 HTTP 接口（NapCat / Lagrange / go-cqhttp 等）
  2. 离线导出的聊天记录文件（json / jsonl / csv / txt）
"""

__version__ = "1.0.0"
