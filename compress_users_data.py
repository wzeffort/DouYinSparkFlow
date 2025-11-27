import json

def compress_users_data():
    # 压缩 usersData.json 内容
    with open("usersData.json", "r", encoding="utf-8") as f:
        user_data = json.loads(f.read())
    
    print(json.dumps(user_data, ensure_ascii=False))