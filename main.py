import json
import asyncio
import argparse
from rich.console import Console
from rich.prompt import Prompt
from core.login import userLogin
from utils.github_action_config import print_github_action_config
from core.tasks import runTasks

# 初始化 rich 控制台
console = Console()


def main():
    console.print("[bold green]欢迎使用 DouYin Spark Flow[/bold green]")
    console.print("[bold yellow]请选择一个操作：[/bold yellow]")
    console.print("[cyan]1.[/cyan] 添加用户登录")
    console.print("[cyan]2.[/cyan] 获取Github Action配置")
    console.print("[cyan]3.[/cyan] 本地运行任务")

    # 获取用户选择
    choice = Prompt.ask(
        "[bold green]请输入选项编号 (1/2/3)[/bold green]", choices=["1", "2", "3"]
    )

    if choice == "1":
        console.print("[bold blue]正在添加登录，请稍候...[/bold blue]")
        while True:
            asyncio.run(userLogin())
            if (
                Prompt.ask(
                    "[bold yellow]是否继续添加用户登录？(y/n)[/bold yellow]",
                    choices=["y", "n"],
                )
                == "n"
            ):
                break
    elif choice == "2":
        print_github_action_config()

    elif choice == "3":
        asyncio.run(runTasks())


if __name__ == "__main__":
    # 设置命令行参数解析
    parser = argparse.ArgumentParser(description="DouYin Spark Flow 工具")
    parser.add_argument(
        "--doTask",  # 参数名
        action="store_true",  # 如果提供该参数，则值为 True；否则为 False
        help="是否直接运行任务（默认为 False）",
    )
    args = parser.parse_args()

    if args.doTask:
        asyncio.run(runTasks())
    else:
        main()
