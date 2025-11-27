import asyncio
from playwright.async_api import async_playwright
import json
import traceback
import requests
from setup_config import getBrowerExecutablePath, HEADLESS, multiTask, taskCount, hitokotoApi, messageTemplate

complates = {}

async def scroll_and_select_user(page, usernames):
    """尝试滚动并查找用户名"""
    # 定义目标元素和结束标志的选择器
    friends_tab_selector = 'xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]'
    target_selector = 'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]//div[contains(@class, "semi-list-item-body semi-list-item-body-flex-start")]'
    end_signal_selector = 'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]//div[contains(@class, "status-wrapper-Tayo1v")]'
    scrollable_friends_selector = 'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]/div/div/div[3]/div/div/div/ul/div'

    # 点击好友标签页
    await page.wait_for_selector(friends_tab_selector)
    await page.locator(friends_tab_selector).click()

    # 确保第一个好友元素加载完成
    first_friend_selector = 'xpath=//*[@id="sub-app"]/div/div/div[2]/div[2]/div/div/div[1]/div/div/div/ul/div/div/div[1]/li/div'
    await page.wait_for_selector(first_friend_selector)
    await page.locator(first_friend_selector).click()  # 点击第一个好友，确保列表激活

    found_usernames = set()  # 存储找到的用户名

    while True:
        # 查找所有目标元素
        target_elements = await page.locator(target_selector).all()

        for element in target_elements:
            try:
                # 查找子元素 span，模糊匹配 class
                span = element.locator(
                    '''xpath=.//span[contains(@class, "item-header-name-")]'''
                )
                username = await span.inner_text()

                if username in found_usernames:
                    continue  # 已处理过，跳过
                found_usernames.add(username)

                print(f"找到用户名: {username}")

                # 检查是否是目标用户名
                if username in usernames:
                    await element.click()
                    print(f"已点击用户名: {username}")
                    yield username
                    break
            except Exception as e:
                traceback.print_exc()
        else:
            # 检查是否存在结束标志
            if await page.locator(end_signal_selector).count() > 0:
                print("已到达列表末尾，停止滚动")
                break

            # 如果没有找到目标用户名，滚动容器
            scrollable_element = await page.locator(
                scrollable_friends_selector
            ).element_handle()
            await page.evaluate(
                "(element) => element.scrollTop += 100", scrollable_element
            )
            await asyncio.sleep(1)  # 等待加载内容
            continue
        
async def request_hitokoto():
    """请求一言 API 获取一句话"""
    api_url = hitokotoApi
    
    try:
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        theFrom = data.get("from")
        if theFrom is None or theFrom.strip() == "":
            theFrom = "未知来源"
        theFromWho = data.get("from_who")
        if theFromWho is None or theFromWho.strip() == "":
            theFromWho = "未知作者"
        return f"{data['hitokoto']} —— {theFrom} ({theFromWho})"
    except Exception as e:
        return "[error] 无法获取一言内容"


async def do_user_task(browser, cookies, targets, semaphore):
    async with semaphore:  # 使用信号量控制并发数量
        context = await browser.new_context()  # 每个任务使用独立的上下文
        page = await context.new_page()
        # 打开抖音创作者中心
        await page.goto("https://creator.douyin.com/")
        # 注入 Cookie
        await context.add_cookies(cookies)
        await page.goto("https://creator.douyin.com/creator-micro/data/following/chat")

        # 滚动并选择用户
        async for username in scroll_and_select_user(page, targets):
            print(f"已选择用户: {username}")

            # 等待 chat-input-dccKiL 元素加载完成
            chat_input_selector = "xpath=//div[contains(@class, 'chat-input-dccKiL')]"
            await page.wait_for_selector(chat_input_selector)
            chat_input = page.locator(chat_input_selector)

            # 在 chat-input-dccKiL 中输入内容
            message = messageTemplate.replace("[API]", await request_hitokoto()).strip()
            for line in message.split("\n"):
                await chat_input.type(line)  # 输入每一行
                # 如果不是最后一行，模拟 Shift+Enter 插入换行
                if line != message.split("\n")[-1]:
                    await chat_input.press("Shift+Enter")  # 模拟 Shift+Enter 插入换行

            # 模拟按下回车键发送消息
            await chat_input.press("Enter")
            await asyncio.sleep(2)  # 发送完等待一会儿

        await context.close()  # 任务完成后关闭上下文


async def runTasks(user_data):
    
    is_chrome_packed, executable_path = getBrowerExecutablePath()
    
    async with async_playwright() as playwright:
        # 启动一个共享的浏览器实例
        if is_chrome_packed:  # 使用打包的 Chrome 浏览器
            browser = await playwright.chromium.launch(
                headless=HEADLESS,
                executable_path=executable_path
            )
        else:  # 使用系统安装的浏览器
            browser = await playwright.chromium.launch(
                headless=HEADLESS
            )

        # 检查是否启用多任务和任务数量
        # 创建信号量以限制并发任务数量
        semaphore = asyncio.Semaphore(taskCount if multiTask else 1)

        tasks = []
        for user in user_data:
            cookies = user["cookies"]
            targets = user["targets"]
            complates[user["unique_id"]] = []  # 初始化该用户的已完成列表
            # 创建任务
            tasks.append(do_user_task(browser, cookies, targets, semaphore))

        # 并发执行任务
        await asyncio.gather(*tasks)

        # 关闭浏览器实例
        await browser.close()


def main():    
    # 加载配置和用户数据
    with open("users_data/users_index.json", "r", encoding="utf-8") as f:
        user_data = json.loads(f.read())
    asyncio.run(runTasks(user_data))


if __name__ == "__main__":
    main()
