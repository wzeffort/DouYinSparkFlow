import json
import asyncio
from rich.console import Console
from rich.prompt import Prompt
from login import userLogin
from compress_users_data import compress_users_data
from doTasks import runTasks

# 初始化 rich 控制台
console = Console()

def main():
    console.print("[bold green]欢迎使用 DouYin Spark Flow[/bold green]")
    console.print("[bold yellow]请选择一个操作：[/bold yellow]")
    console.print("[cyan]1.[/cyan] 添加登录")
    console.print("[cyan]2.[/cyan] 压缩 usersData.json")
    console.print("[cyan]3.[/cyan] 测试运行任务")

    # 获取用户选择
    choice = Prompt.ask("[bold green]请输入选项编号 (1/2/3)[/bold green]", choices=["1", "2", "3"])

    if choice == "1":
        console.print("[bold blue]正在添加登录，请稍候...[/bold blue]")
        asyncio.run(userLogin())
        console.print("[bold green]登录完成！[/bold green]")

    elif choice == "2":
        console.print("[bold blue]正在压缩 usersData.json，请稍候...[/bold blue]")
        compress_users_data()
        console.print("[bold green]压缩完成！[/bold green]")

    elif choice == "3":
        console.print("[bold blue]正在运行任务，请稍候...[/bold blue]")
        with open("usersData.json", "r", encoding="utf-8") as f:
            user_data = json.loads(f.read())
        asyncio.run(runTasks(user_data))
        console.print("[bold green]任务运行完成！[/bold green]")

if __name__ == "__main__":
    main()