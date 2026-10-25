# 火花守护 · 服务器与项目交接文档

> 更新日期：2026-10-25 ｜ 维护期：2026-10-24 ~ 10-25 大重构后状态

---

## 一、服务器清单与连接方式

### 1. 阿里云（主服务器 · 承载全部生产业务）

```bash
ssh -i "C:\Users\Lenovo\id_ed25519_new" admin@39.106.220.31
```

- 用户：`admin`（uid 1004，**免密 sudo**）
- 私钥：`C:\Users\Lenovo\id_ed25519_new`
- 主机指纹（ED25519）：`SHA256:qqQpXVvbhaKIoVh6x/uYuOo1Ld9J1Hc2YG1KYJb9RQw`
- 磁盘 40G（约 90% 已用，4.5G 可用）｜内存 1.8G
- MySQL：本机 3306，root 密码 `wangze`（SparkAssess 使用）

### 2. AWS 悉尼（已退役的 spark 旧栈 · 仍有其他业务）

```bash
ssh -i "C:\Users\Lenovo\.ssh\spark-migration.pem" ubuntu@ec2-3-107-48-147.ap-southeast-2.compute.amazonaws.com
```

- 用户：`ubuntu`（免密 sudo）
- 主机指纹（ED25519，TOFU 首次记录）：`SHA256:sGKkkntHVslZDwr/lM2d7rT/8wbZuokMPSA4L+j5aP8`
- 状态：spark 全栈已停（容器/数据卷保留可复活）；**bps 全套 + workbuddy2api 仍在运行**
- 注意：EC2 实例仍在计费，彻底退订需先迁移 bps/workbuddy

---

## 二、网络架构（2026-10-24 数据库回迁后）

```
用户 → https://spark.oilu.cn (nginx 443, certbot)
     → 切换网关 127.0.0.1:8903（cutover_proxy，容器内）
     → spark-web 127.0.0.1:8900（容器，host 网络）
     → PostgreSQL 127.0.0.1:15432（本机容器，库名 spark_live_20261014_033043）

worker / auth / notifier：同机容器，直连本机 PG
AWS：整套 spark 栈已停（温热备份），隧道 127.0.0.1:15433 仍可达 AWS 旧库（应急回滚通道）
```

- 域名：spark.oilu.cn（HTTPS，certbot 自动续期，下次续期 11 月底）
- 数据库每日每小时备份：`/opt/spark-postgres-backups/`（保留 2 天，自动校验）

---

## 三、运行中的服务（阿里云）

| 服务 | 容器 | 镜像 | 说明 |
|---|---|---|---|
| 火花守护 web | spark-shared-apps-spark-web-1 | spark-shared:login-fix24-20261025 | FastAPI 主站，含游客预览/落地页 |
| 火花守护 worker | spark-shared-apps-spark-worker-1 | spark-shared:worker-secuid-20261025 | 发送执行 + sec_uid 运行时补全 |
| 火花守护 auth | spark-shared-apps-spark-auth-1 | spark-shared:auth-sms2-20261025 | 扫码/短信绑定 + 暖浏览器保活 |
| 火花守护 notifier | spark-shared-apps-spark-notifier-1 | spark-shared:notifier-local-20261024 | 邮件/QQ 通知 |
| PostgreSQL | spark-shared-db-postgres-1 | postgres:18.6 | 主库（80 连接，48MB buffers）|
| 切换网关 | spark-cutover-gateway | spark-console-web:referrals-b25538a | 8903 → 8900 路由 |

### 日常运维命令

```bash
# 查看服务
sudo docker ps
# 更换单个服务镜像（例：web）
cd /opt/spark-shared-db/deploy/shared-db
sudo SPARK_RELEASE_IMAGE=spark-shared:<新tag> docker compose -f compose.apps.yml up -d --no-deps spark-web
# 回滚 web 到上一版
sudo SPARK_RELEASE_IMAGE=spark-shared:login-fix23-20261025 docker compose -f compose.apps.yml up -d --no-deps spark-web
# 日志
sudo docker logs --tail 50 spark-shared-apps-spark-web-1
```

- 发布镜像位于 `/opt/spark-shared-db/releases/<名称>/`（含 Dockerfile 与源码快照）
- 回滚链（web）：fix24 → fix23 → … → flat-base-20261025（压平基础镜像，**新构建一律 FROM 它**）
- auth 镜像链：auth-nickv2 → auth-sms2（当前）；worker：worker-secuid（当前）

---

## 四、项目清单

### 1. 火花守护（主业务）

- 地址：**https://spark.oilu.cn**（管理后台 `/admin`）
- 代码：GitHub `wzeffort/DouYinSparkFlow`（私有开发）+ `2061360308/DouYinSparkFlow`（公开，419 星）
- 分支：`codex/shared-database-20261014`（全部最新改动已推送）
- 数据库：PostgreSQL 本机（见上）；管理员在 users 表 role='admin'

### 2. SparkAssess · 学习能力测评系统

- 地址：**http://39.106.220.31/flask/**（AI 问答：`/flask/ai_chat`）
- 代码：GitHub `wzeffort/SparkAssess`（**私有**，含数据库凭据，勿转公开）
- 位置：`/www/wwwroot/flaskProject`（Flask + MySQL flask 库）
- 运行：gunicorn 4 worker @ 127.0.0.1:5000（有看门狗自动拉起；服务器重启后自动恢复）
- **AI 问答配置**：`/www/wwwroot/flaskProject/config.py` 底部 `AI_CHAT_URL/KEY/MODEL` 三行
  （当前：NVIDIA `openai/gpt-oss-20b`，2026-10-25 换的新密钥；换厂商只改这三行）
- 重启命令：
  ```bash
  sudo pkill -f 'gunicorn.*app:app'; sleep 2
  sudo bash -c 'cd /www/wwwroot/flaskProject && setsid nohup /www/wwwroot/cfp/miniconda/envs/flask-env/bin/gunicorn -w 4 -b 127.0.0.1:5000 app:app >/tmp/cfp-gunicorn.log 2>&1 &'
  ```

### 3. cfp · Django 部分（已下线）

- 代码：GitHub `wzeffort/cfp`（**私有**，settings 含密钥，勿转公开）
- 状态：8000 端口入口已死，未运行；目录 `/www/wwwroot/cfp`（5.3G）+ 打包备份 `cfp_Rhx4n.tar.gz`（1.8G）
- 下线清理可释放 ~7.1G（删前确认）

### 4. bps + workbuddy2api（在 AWS 上运行）

- 与火花守护无关，未触碰；AWS 退役需先处理它们

---

## 五、定时任务（阿里云，全部已启用）

| 定时器 | 周期 | 作用 |
|---|---|---|
| spark-postgres-backup.timer | 每小时 | PG 备份到 /opt/spark-postgres-backups（自动校验，留 2 天）|
| spark-encrypted-backup.timer | 每天 03:20 | 加密备份 |
| spark-health-collector.timer | 30 秒 | 双机健康采集 |
| spark-account-validator.timer | 周日 04:10 | 账号 cookie 每周复检（失效自动标记+停任务+邮件）|
| certbot.timer | 自动 | TLS 证书续期 |

---

## 六、已知问题与注意事项

1. **磁盘 90%**：可清理项——AWS 旧数据卷、douyin-spark-console 残留卷（约 1G）；大文件已清
2. **`gunicorn.service` 显示 failed 是正常的**：学习测评由手动实例 + 看门狗承载，systemd 服务已刻意停用避免端口冲突
3. **`/etc/nginx/conf.d/django.conf`**：IP 直连 80 端口的旧配置（含 /flask/ 与 /static/ 引用），改动需谨慎（学习测评依赖其中的 /flask/ 代理与 X-Script-Name 头）
4. **SparkAssess/cfp 两个仓库含生产凭据**：保持私有；NVIDIA 旧密钥已进 Git 历史，建议作废轮换
5. **nginx 引擎正常但 `http://39.106.220.31/` 根路径 404**：Django 8000 已死所致，无影响
6. 每周复检发现账号失效时会**自动暂停其任务并邮件通知用户**——收到邮件属正常流程
7. Windows 本地的 `sa-work`、`cfp-git` 等临时目录被占用删不掉的，重启后手动删

---

## 七、2026-10-24 ~ 25 变更记录（本次维护期）

| 日期 | 变更 | 提交/版本 |
|---|---|---|
| 10-24 | 磁盘清理（96%→86%）| — |
| 10-24 | 登录安全加固（时序防探测/友好错误/子路径跳转）| login-fix1~2 |
| 10-24 | 公开落地页 + SEO + 游客预览 + 注册引导 | login-fix3~5 |
| 10-24 | 扫码提速（hover 预热/轮询对齐/弹窗重设计）| login-fix6 + auth-scanfix |
| 10-24 | 数据库回迁本机 + 完成双机切换（页面 0.1s）| login-fix7 + cutover 路由 |
| 10-24 | AWS spark 栈退役；错误密钥守卫修复（真昵称）| auth-profilefix |
| 10-25 | 游客预览注册引导弹层；付款页重做；二维码大图弹窗 | login-fix8~10 |
| 10-25 | 选人失败 25 分钟当日重试 + 连续 3 晚自动暂停通知 | worker-secuid |
| 10-25 | 每周账号 cookie 复检定时任务 | auth-warmfix + validator |
| 10-25 | 支付确认按钮高亮；短信验证码绑定（备用登录）| login-fix9 + auth-sms2 |
| 10-25 | 邀请码创建者标注；文案可读化；全站指标面板 | login-fix17~18 |
| 10-25 | 学习测评：子路径修复 + UI 现代化 + AI 后端重写 | SparkAssess 仓库 |
| 10-25 | 指标口径改最终送达率 + 星标芯片 + 服务天数移除 | login-fix19~24 |
