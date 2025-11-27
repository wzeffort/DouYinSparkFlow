from playwright.async_api import async_playwright
import asyncio
import json
import os
from setup_config import getBrowerExecutablePath

xpaths = {
    "unique_id": """xpath=//*[contains(@id, "garfish_app_for_douyin_creator_pc_home")]/div/div[2]/div/div[2]/div[1]/div[2]/div[1]/div[3]""",
    "name": """xpath=//*[contains(@id, "garfish_app_for_douyin_creator_pc_home")]/div/div[2]/div/div[2]/div[1]/div[2]/div[1]/div[1]/div[1]""",
}


async def userLogin():
    async with async_playwright() as playwright:
        
        is_chrome_packed, executable_path = getBrowerExecutablePath()
        
        if is_chrome_packed:
            browser = await playwright.chromium.launch(
                headless=False, executable_path=executable_path
            )  # 启动浏览器
        else:
            browser = await playwright.chromium.launch(
                headless=False
            )  # 启动浏览器
        context = await browser.new_context()
        page = await context.new_page()

        # 打开目标页面
        await page.goto("https://creator.douyin.com/")

        print("请手动登录抖音创作者中心")

        # 等待页面跳转或特定元素出现
        # 等待 XPath 元素加载完成
        await page.wait_for_selector(
            'xpath=//*[contains(@id, "garfish_app_for_douyin_creator_pc_home")]/div/div[2]/div/div[2]/div[1]'
            ,timeout=300000  # 设置输入超时时间为 5 分钟
        )

        # 等待 unique_id 元素加载完成
        unique_id_element = await page.wait_for_selector(xpaths["unique_id"])
        unique_id = (await unique_id_element.inner_text())[
            4:
        ]  # 去掉前四个字符 "抖音号："
        print("Unique ID:", unique_id)

        # 等待 name 元素加载完成
        name_element = await page.wait_for_selector(xpaths["name"])
        username = await name_element.inner_text()
        print("Name:", username)

        # 获取所有 Cookie
        cookies = await context.cookies()
        print("Cookies:", f"找到 {len(cookies)} 条 Cookie")

        if os.path.exists("usersData.json"):
            with open("usersData.json", "r", encoding="utf-8") as f:
                userdata = json.load(f)
        else:
            userdata = []

        targets = input(
            "点击互动管理->私信管理->朋友私信，查看并输入目标好友对应昵称（空格分割）"
        )

        for user in userdata:
            if user["unique_id"] == unique_id:
                print(f"用户 {unique_id} 已存在，更新信息。")
                user["cookies"] = cookies
                user["username"] = username
                user["targets"] = [target.strip() for target in targets.split(" ")]
                break
        else:
            print(f"添加新用户 {unique_id} 。")
            userdata.append(
                {
                    "unique_id": unique_id,
                    "username": username,
                    "cookies": cookies,
                    "targets": [target.strip() for target in targets.split(" ")],
                }
            )

        with open("usersData.json", "w", encoding="utf-8") as f:
            json.dump(userdata, f, ensure_ascii=False, indent=4)
            
        print("用户数据已保存到 usersData.json")
        
        await browser.close()


if __name__ == "__main__":
    asyncio.run(userLogin())