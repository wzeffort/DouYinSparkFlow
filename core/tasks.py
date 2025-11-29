import asyncio
import traceback
import logging
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message
from core.browser import get_browser


complates = {}

config = get_config()
userData = get_userData()
logger = setup_logger(level=logging.DEBUG)


async def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    通用的重试逻辑
    :param name: 操作名称（用于日志记录）
    :param operation: 要执行的异步操作
    :param retries: 最大重试次数
    :param delay: 每次重试之间的延迟（秒）
    :param args: 传递给操作的参数
    :param kwargs: 传递给操作的关键字参数
    """
    for attempt in range(retries):
        try:
            return await operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                await asyncio.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise


async def scroll_and_select_user(page, username, targets):
    """尝试滚动并查找用户名"""
    # 定义目标元素和结束标志的选择器
    friends_tab_selector = 'xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]'
    target_selector = 'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]//div[contains(@class, "semi-list-item-body semi-list-item-body-flex-start")]'
    end_signal_selector = 'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]//div[contains(@class, "status-wrapper-Tayo1v")]'
    scrollable_friends_selector = 'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]/div/div/div[3]/div/div/div/ul/div'

    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")

    logger.debug(f"账号 {username} 点击进入好友标签页")
    # 点击好友标签页
    await page.wait_for_selector(friends_tab_selector)
    await page.locator(friends_tab_selector).click()

    logger.debug(f"账号 {username} 进入好友列表页面")

    # 确保第一个好友元素加载完成
    first_friend_selector = 'xpath=//*[@id="sub-app"]/div/div/div[2]/div[2]/div/div/div[1]/div/div/div/ul/div/div/div[1]/li/div'
    await page.wait_for_selector(first_friend_selector)
    await page.locator(first_friend_selector).click()  # 点击第一个好友，确保列表激活

    logger.debug(f"账号 {username} 已激活好友列表，开始滚动查找目标好友")

    await asyncio.sleep(2)  # 等待好友列表加载

    found_usernames = set()  # 存储找到的用户名

    while True:
        # 查找所有目标元素
        target_elements = await page.locator(target_selector).all()

        for element in target_elements:
            try:
                # 查找子元素 span，模糊匹配 class
                span = element.locator(
                    """xpath=.//span[contains(@class, "item-header-name-")]"""
                )
                targetName = await span.inner_text()

                if targetName in found_usernames:
                    continue  # 已处理过，跳过
                found_usernames.add(targetName)

                logger.debug(f"账号 {username} 找到好友 {targetName}")

                # 检查是否是目标用户名
                if targetName in targets:
                    await element.click()
                    logger.info(
                        f"账号 {username} 选中目标好友 {targetName} 准备开始交互"
                    )
                    yield targetName
                    break
            except Exception as e:
                traceback.print_exc()
        else:
            # 检查是否存在结束标志
            if await page.locator(end_signal_selector).count() > 0:
                break

            # 如果没有找到目标用户名，滚动容器
            scrollable_element = await page.locator(
                scrollable_friends_selector
            ).element_handle()
            await page.evaluate(
                "(element) => element.scrollTop += 100", scrollable_element
            )
            logger.debug(f"账号 {username} 滚动好友列表以加载更多好友")
            await asyncio.sleep(1)  # 等待加载内容
            continue


async def do_user_task(browser, username, cookies, targets, semaphore):
    async with semaphore:  # 使用信号量控制并发数量
        context = await browser.new_context()  # 每个任务使用独立的上下文
        context.set_default_navigation_timeout(90000)  # 设置导航超时时间为 90 秒
        context.set_default_timeout(60000)  # 设置所有操作的默认超时时间为 60 秒

        page = await context.new_page()
        # 打开抖音创作者中心
        await retry_operation(
            "打开抖音创作者中心",
            page.goto,
            retries=3,
            delay=5,
            url="https://creator.douyin.com/",
        )
        # 注入 Cookie
        await context.add_cookies(cookies)

        # 导航到消息页面
        await retry_operation(
            "导航到消息页面",
            page.goto,
            retries=3,
            delay=5,
            url="https://creator.douyin.com/creator-micro/data/following/chat",
        )

        logger.info(f"账号 {username} 开始发送消息")
        # 滚动并选择用户
        async for username in scroll_and_select_user(page, username, targets):
            logger.info(f"账号 {username} 已选中好友 {username} 发送消息")
            # 等待 chat-input-dccKiL 元素加载完成
            chat_input_selector = "xpath=//div[contains(@class, 'chat-input-dccKiL')]"
            await page.wait_for_selector(chat_input_selector)
            chat_input = page.locator(chat_input_selector)

            # 在 chat-input-dccKiL 中输入内容
            message = build_message()
            for line in message.split("\n"):
                await chat_input.type(line)  # 输入每一行
                # 如果不是最后一行，模拟 Shift+Enter 插入换行
                if line != message.split("\n")[-1]:
                    await chat_input.press("Shift+Enter")  # 模拟 Shift+Enter 插入换行

            logger.debug(
                f"账号 {username} 准备发送消息给好友 {username}：\n\t{message}"
            )
            logger.info(f"账号 {username} 给好友 {username} 发送消息完成")
            # 模拟按下回车键发送消息
            await chat_input.press("Enter")
            await asyncio.sleep(2)  # 发送完等待一会儿

        await context.close()  # 任务完成后关闭上下文


async def runTasks():
    playwright, browser = await get_browser()
    try:
        # 检查是否启用多任务和任务数量
        # 创建信号量以限制并发任务数量
        logger.info("开始执行任务,当前配置如下：")
        logger.info(f"多任务模式: {config['multiTask']}, 任务数量: {config['taskCount']}")
        logger.info(f"消息模板: {config['messageTemplate']}")
        logger.info(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.info(f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}")
            
        semaphore = asyncio.Semaphore(config["taskCount"] if config["multiTask"] else 1)

        tasks = []
        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            complates[user["unique_id"]] = []  # 初始化该用户的已完成列表
            username = user.get("username", "未知用户")
            # 创建任务
            tasks.append(do_user_task(browser, username, cookies, targets, semaphore))

        # 并发执行任务
        await asyncio.gather(*tasks)
    finally:
        await playwright.stop()

        # 关闭浏览器实例
        await browser.close()
