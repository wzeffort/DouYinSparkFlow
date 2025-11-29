import json
from rich.console import Console
from rich.panel import Panel
from utils.config import get_config

config = get_config()

# 初始化 rich 控制台
console = Console()


def compress_users_data():
    # 压缩 usersData.json 内容
    with open("usersData.json", "r", encoding="utf-8") as f:
        user_data = json.loads(f.read())

    return json.dumps(user_data, ensure_ascii=False)


def print_github_action_config():
    """
    打印 GitHub Action 配置表格
    """

    # 输出前置步骤说明
    steps = (
        "1. 确保已克隆仓库并在仓库的 [bold yellow]Action[/bold yellow] 选项卡下启用 "
        "[bold green]DouYin Spark Flow Schedule Run[/bold green]\n"
        "2. 在仓库的设置选项卡下的 [bold yellow]Environment[/bold yellow] 配置项中添加 "
        "[bold green]user-data[/bold green] 环境，并将下方列出 Secrets 依次添加到该环境的 Secrets 中"
    )
    console.print(Panel(steps, title="前置步骤", expand=False, style="bold cyan"))

    secrets = {
        "USER_DATA": compress_users_data()
    }
    if "proxyAddress" in config and config["proxyAddress"]:
        secrets["proxyAddress"] = config["proxyAddress"]

    # 打印每个键名和键值
    console.print("\n[bold yellow]Secrets 配置：选中后右击鼠标复制（没有弹出菜单点击鼠标右键就完成复制了！）[/bold yellow]")

    for key, value in secrets.items():
        console.rule(f"[bold cyan]{key}[/bold cyan]")
        console.print(f"[green]{value}[/green]\n")