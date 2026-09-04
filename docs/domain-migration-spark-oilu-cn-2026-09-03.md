# `spark.oilu.cn` 域名绑定运维日记

日期：2026-09-03\
项目：DouYinSparkFlow 多用户 Web 控制台\
服务器：`39.106.220.31`\
新访问地址：<https://spark.oilu.cn/login>

## 一、变更目标

将项目公开域名从 `wangze.oilu.cn` 切换为 `spark.oilu.cn`，完成以下工作：

- 配置新域名 DNS 解析；
- 为新域名签发 HTTPS 证书；
- 增加新域名的 Nginx 反向代理；
- 更新项目公开地址环境变量；
- 重建并验证 Docker 服务；
- 暂时保留旧域名，作为故障回退入口；
- 不推送本次服务器配置到 GitHub。

## 二、域名解析配置

### 1. 网站 A 记录

`oilu.cn` 使用腾讯云 DNSPod 管理权威解析。新域名需要添加以下记录：

| 主机记录 | 类型 | 线路 | 记录值 | TTL |
| --- | --- | --- | --- | --- |
| `spark` | `A` | 默认 | `39.106.220.31` | `600` |

该记录最终形成：

```text
spark.oilu.cn -> 39.106.220.31
```

### 2. HTTPS 证书 DNS 验证记录

由于服务器公网 HTTP/HTTPS 请求受到阿里云备案接入检查影响，Certbot 的 HTTP 验证可能被拦截，因此本次使用 DNS-01 验证。

在 DNSPod 中临时添加：

| 主机记录 | 类型 | 线路 | 记录值 | TTL |
| --- | --- | --- | --- | --- |
| `_acme-challenge.spark` | `TXT` | 默认 | Certbot 本次生成的一次性验证值 | `600` |

完整验证域名为：

```text
_acme-challenge.spark.oilu.cn
```

一次性 TXT 值没有记录在本文档中，避免把已经失效的验证值误用于以后续期。证书签发成功后可删除该 TXT 记录，但不能删除 `spark` 的 A 记录。

### 3. 本次遇到的 DNS 错误

第一次操作时，批量解析页面把原有的 `spark` A 记录修改成了 TXT 记录，导致：

- `spark.oilu.cn` 暂时失去 A 记录；
- `_acme-challenge.spark.oilu.cn` 实际并未创建；
- 旧 DNS 缓存失效后，网站可能无法访问。

随后已在 DNSPod 普通“记录管理”页面修复为两条独立记录：

```text
spark                     A      39.106.220.31
_acme-challenge.spark     TXT    <一次性 Certbot 验证值>
```

经验：证书验证记录应使用普通“添加记录”，不要通过“批量修改记录”修改现有的网站 A 记录。

## 三、HTTPS 证书签发

在服务器执行：

```bash
sudo certbot certonly \
  --manual \
  --preferred-challenges dns \
  --key-type rsa \
  --rsa-key-size 2048 \
  --cert-name spark.oilu.cn \
  -d spark.oilu.cn
```

等待 DNSPod 的 TXT 记录可以从多个公共 DNS 查询到之后，再让 Certbot 继续验证。

查询示例：

```powershell
nslookup -type=TXT _acme-challenge.spark.oilu.cn 1.1.1.1
nslookup -type=TXT _acme-challenge.spark.oilu.cn 8.8.8.8
```

本次签发结果：

```text
证书名称：spark.oilu.cn
证书类型：RSA 2048 bit
颁发机构：Let's Encrypt
生效时间：2026-09-03 17:26:06（北京时间）
到期时间：2026-12-02 17:26:05（北京时间）
证书路径：/etc/letsencrypt/live/spark.oilu.cn/fullchain.pem
私钥路径：/etc/letsencrypt/live/spark.oilu.cn/privkey.pem
```

私钥内容、服务器登录密钥和长期 API 密钥均未写入本文档。

## 四、Nginx 配置

旧站点配置位于：

```text
/etc/nginx/sites-available/wangze.oilu.cn
/etc/nginx/sites-enabled/wangze.oilu.cn
```

本次增加新站点：

```text
/etc/nginx/sites-available/spark.oilu.cn
/etc/nginx/sites-enabled/spark.oilu.cn
```

核心配置如下：

```nginx
server {
    server_name spark.oilu.cn;

    location / {
        proxy_pass http://127.0.0.1:8899;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 5s;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
    }

    listen 443 ssl;
    listen [::]:443 ssl;
    ssl_certificate /etc/letsencrypt/live/spark.oilu.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/spark.oilu.cn/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}

server {
    listen 80;
    listen [::]:80;
    server_name spark.oilu.cn;
    return 301 https://$host$request_uri;
}
```

### IPv6 监听冲突

复制旧配置后，第一次执行 `nginx -t` 检测到：

```text
duplicate listen options for [::]:443
```

原因是新旧两个 HTTPS 虚拟主机都重复声明了：

```nginx
listen [::]:443 ssl ipv6only=on;
```

新站点已改为：

```nginx
listen [::]:443 ssl;
```

随后 `sudo nginx -t` 检查通过，再执行：

```bash
sudo systemctl reload nginx
```

由于校验失败时没有重载 Nginx，因此该错误没有影响当时正在运行的线上服务。

## 五、项目环境变量与 Docker 部署

项目目录：

```text
/opt/douyin-spark-console
```

环境变量文件：

```text
/opt/douyin-spark-console/.env.console
```

公开地址由旧域名改为：

```dotenv
SPARK_PUBLIC_BASE_URL=https://spark.oilu.cn
```

该变量会影响邮件通知、密码找回以及其他需要生成公网链接的功能。

变更前确认没有状态为 `running` 且未结束的任务，然后重建服务，使新环境变量进入容器：

```bash
cd /opt/douyin-spark-console
sudo docker compose -f compose.console.yml up -d --force-recreate
sudo docker compose -f compose.console.yml ps
```

涉及的服务：

- `spark-web`
- `spark-worker`
- `spark-auth`
- `spark-notifier`

## 六、验证结果

完成配置后执行了以下验证：

1. `sudo nginx -t`：通过；
2. 服务器本机使用 `spark.oilu.cn` SNI 请求 `/login`：HTTP `200`；
3. 证书主题：`CN = spark.oilu.cn`；
4. 公钥类型：RSA 2048 bit；
5. TLS 协议：TLS 1.3；
6. 独立 CA 信任验证：`authorized = true`；
7. `spark-web`：运行且状态为 `healthy`；
8. `spark-worker`、`spark-auth`、`spark-notifier`：均处于运行状态；
9. 容器内环境变量已确认是 `https://spark.oilu.cn`；
10. 旧域名的 Nginx 配置和证书继续保留，可用于回退。

浏览器若仍显示旧的“不安全”状态，可关闭旧标签页，用无痕窗口重新访问；必要时清除 Windows SSL 状态和 DNS 缓存：

```cmd
ipconfig /flushdns
```

## 七、当前仍需处理的问题

服务器端域名、证书和应用配置已经生效，但外部 HTTP 请求曾出现：

```text
HTTP 403
Server: Beaver
Non-compliance ICP Filing
```

部分外部 HTTPS 请求也出现握手被中断。这是阿里云大陆服务器的接入备案检查，不是 Nginx、Docker 或证书配置错误。

当前域名备案主体为域名持有人，服务器属于另一账号时，应由原备案主体继续保留备案主体身份，并使用当前阿里云服务器资源办理“接入备案”。通常需要：

- 域名备案主体配合身份核验；
- 服务器所有者提供可用于备案的服务器资源或授权；
- 在阿里云备案系统完成该服务器的接入备案；
- 等待阿里云审核和接入状态同步。

备案接入完成前，不同运营商、地区或代理网络的访问结果可能不一致。

## 八、备份与回滚

本次变更前的服务器配置备份位于：

```text
/var/backups/spark-domain-20260903-182606
```

包含：

```text
nginx-wangze.conf
env.console
```

备份目录权限为 `700`，只有 root 可以进入。

如需回退公开地址，可执行：

```bash
sudo rm /etc/nginx/sites-enabled/spark.oilu.cn
sudo cp /var/backups/spark-domain-20260903-182606/env.console \
  /opt/douyin-spark-console/.env.console
sudo nginx -t
sudo systemctl reload nginx
cd /opt/douyin-spark-console
sudo docker compose -f compose.console.yml up -d --force-recreate
```

执行回滚前仍需先确认没有任务正在运行，避免重启执行器时中断任务。

## 九、证书续期提醒

本次证书使用 Certbot 手动 DNS 验证签发，不能在无人操作的情况下自动续期。必须在 `2026-12-02` 到期前重新签发。

后续建议为 DNSPod 配置最小权限的 DNS API 凭据及 Certbot DNS 插件，实现自动添加和删除 `_acme-challenge.spark` TXT 记录。API 密钥应只保存在服务器权限受限的环境文件中，禁止写入 Git、Markdown 文档或前端页面。

## 十、本次变更结论

- 新域名 `spark.oilu.cn` 已完成服务器端绑定；
- 新证书已签发并由 Nginx 使用；
- 项目公开地址已切换；
- Docker 服务运行正常；
- 旧域名仍保留用于回退；
- 本次没有向 GitHub 提交或推送代码；
- 公网稳定访问仍依赖阿里云接入备案完成。
