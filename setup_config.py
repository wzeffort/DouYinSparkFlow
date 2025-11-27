import os
import sys

CHROME = "chrome/chrome-win64/chrome.exe"
DRIVER = "chrome/chromedriver-win64/chromedriver.exe"
CHROME_HEADLESS = "chrome/chrome-headless-shell-win64/chrome-headless-shell.exe"

CHROME_GITHUB_ACTION_PATH = "/home/runner/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell"

PACK_CHROME = True  # 是否已经打包了 Chrome 浏览器
PACK_CHROME_HEADLESS = False  # 打包的浏览器是否为无头浏览器
HEADLESS = True # 是否以无头模式运行浏览器
    
multiTask = True  # 是否启用多任务,同时运行多个浏览器上下文处理多个账户操作
taskCount = 5     # 并发任务数量

useProxy = False  # 是否使用代理
proxyAddress = "http://your-proxy-address:port"

messageTemplate = "[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n[API]"  # 默认消息模板
hitokotoApi = "https://v1.hitokoto.cn/"  # 一言 API 地址


def getBrowerExecutablePath():
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # PyInstaller 打包环境，使用 _MEIPASS 获取临时目录路径
        base_path = sys._MEIPASS
        if PACK_CHROME_HEADLESS:
            return True, os.path.join(base_path, CHROME_HEADLESS)
        else:
            return True, os.path.join(base_path, CHROME)
    elif os.getenv('GITHUB_ACTIONS') == 'true':
        # 直接使用标准安装位置的 Chrome 浏览器，需提前运行playwright install --with-deps
        return False, CHROME_GITHUB_ACTION_PATH
    else:
        # 选择使用无头还是完整的chrome
        if PACK_CHROME_HEADLESS:
            return True, os.path.join(os.path.dirname(__file__), CHROME_HEADLESS)
        else:
            return True, os.path.join(os.path.dirname(__file__), CHROME)
