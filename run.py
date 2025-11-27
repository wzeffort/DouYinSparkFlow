import json, asyncio
from doTasks import runTasks

# 加载配置和用户数据
with open("usersData.json", "r", encoding="utf-8") as f:
    user_data = json.loads(f.read())

asyncio.run(runTasks(user_data))