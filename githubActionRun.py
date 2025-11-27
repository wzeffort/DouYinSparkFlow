import json, asyncio, os
from doTasks import runTasks

# 加载配置和用户数据
user_data_json = os.getenv("USER_DATA", None)
if not user_data_json:
    print("环境变量 USER_DATA 未设置")
    exit(1)

user_data = json.loads(user_data_json)

asyncio.run(runTasks(user_data))