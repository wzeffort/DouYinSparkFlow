## DouYin Spark Flow

> 抖音自动续火花脚本

**特性**
- [x] 多用户,同时批量支持多个账户
- [x] 多目标,一个账户支持多个续火花目标
- [x] 一言支持,更丰富的消息文本

使用`PlayWright`以及`chrome-headless-shell`自动化操作[抖音创作者中心](https://creator.douyin.com/)，进行定时发送抖音消息来续火花

### 使用

1. 克隆项目到本地，并完成环境配置
```shell
pip install -r requirements.txt
```

下载[chrome-headless-shell-win64](https://storage.googleapis.com/chrome-for-testing-public/142.0.7444.175/win64/chrome-headless-shell-win64.zip)解压到本地的`chrome\chrome-headless-shell-win64\chrome-headless-shell.exe`

下载[chrome-headless-shell-win64](https://storage.googleapis.com/chrome-for-testing-public/142.0.7444.175/win64/chromedriver-win64.zip)解压到本地的`chrome\chrome-win64\chrome.exe`

2. 本地运行脚本并完成测试
```shell
python run.py
```

你需要先按照提示添加用户，如果需要添加多个用户则多次运行脚本
之后进行本地测试，如果发送消息成功即可，选择`压缩 usersData.json`获取到压缩后的usersData

3. Github Action部署

克隆本项目，并在Action中启用`DouYin Spark Flow Schedule Run`这个工作流

之后再settings->Environments下新建一个`user-data`环境，继续在这个`user-data`环境的Environment secrets添加名为`USER_DATA`的项目，内容就是压缩后的usersData

4. （可选）手动运行Action进行测试

