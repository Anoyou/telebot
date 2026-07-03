# 生产部署安全运维 SOP

适用范围：把 TelePilot 控制台从本地开发环境推到生产（公网可达）部署时的安全清单与应急流程。

> **如果你打算公网部署**：本文档是必读清单。默认配置只适合本机开发，直接照搬到公网部署是危险的。

---

## 1. 一次性配置（部署前必做）

把下面这套清单跑一遍，再启服。

### 1.1 .env 强化

| 变量 | 生产值 | 说明 |
| --- | --- | --- |
| `MASTER_KEY` | 32 字符强随机（`Fernet.generate_key()`） | 加密 session / api_hash / totp_secret，**丢了 = 所有 TG 账号要重登** |
| `JWT_SECRET` | 至少 64 字符强随机（`secrets.token_urlsafe(64)`） | 一旦泄露，攻击者可签发任意用户 token |
| `COOKIE_SECURE` | `true` | 必须，前端走 HTTPS；不开则浏览器不带 Secure 标 |
| `TRUST_FORWARDED_FOR` | `true` 仅当部署在 nginx/traefik 后；否则 `false` | 错配会让攻击者通过伪造头绕过登录限速 |
| `POSTGRES_PASSWORD` | 32 字符强随机；**不要用 `telebot`/`changeme`** | `prod-up` 已硬校验，弱口令直接拒启 |
| `LOGIN_RATE_LIMIT_PER_MIN` | 默认 30 即可；高并发可调大；0 = 关闭（不推荐） | 双维度（IP + username） |
| `LOGIN_OTP_FAILED_ATTEMPT_THRESHOLD` | 默认 0；生产建议在 Web 设置页开启 | 密码失败达到阈值后，下一次正确密码需要通知 Bot OTP |
| `LOGIN_OTP_FAIL_WINDOW_SECONDS` | 默认 900 | 登录失败计数窗口 |
| `LOGIN_OTP_TTL_SECONDS` | 默认 300 | 通知 Bot 登录验证码有效期 |
| `LOGIN_OTP_MAX_ATTEMPTS` | 默认 3 | 同一个通知验证码最多尝试次数 |

### 1.2 文件权限

```bash
chmod 600 .env                 # 任何用户可读 .env = 全量泄露
chmod 700 sessions/            # session string 落盘目录（如启用）
chmod 700 data/avatars/        # 头像缓存（不算敏感，但顺手收紧）
```

### 1.3 密钥异地备份

部署完成后**立刻**跑一次：

```bash
bash deploy/backup-keys.sh           # 默认 gpg 对称加密，输出 keys-backup-<ts>.gpg
```

把产物上传到与 DB 备份**不同**的地点（不同账号 / 不同地域 / 离线介质）。
理由：MASTER_KEY 一旦丢，所有 TG session 都解不出来；DB 备份和 MASTER_KEY 必须分开存。

### 1.4 网络与传输

- **HTTPS**：前端必须走 https。任意可拿到 LAN/中间环节的人都能拿到 cookie 里的 JWT。
- **CSP**：默认前端 Nginx 已下发 CSP；若使用自定义反代或 CDN，保持 `default-src 'self'` 起步并按需放行。
- **CORS**：`CORS_ORIGINS` 只放真实前端域名，不要 `*`。
- **TG 出口代理**：要么 VPS 在能直连 TG 的网络，要么走自有可信代理；不要用公开 SOCKS5。

---

## 2. 已知风险与当前缓解

以下项目是已识别风险及当前缓解方式；生产环境仍需按第 1 节完成 HTTPS、强密钥与备份隔离。

### 2.1 CSRF：已实现 header gate + double-submit token

**现状**：后端写操作除 cookie 外还要求前端附带 `X-Requested-With: telepilot-ui`
以及 `X-CSRF-Token`；前端先从 `/api/auth/csrf` 获取 JS 可读 `csrf_token` cookie，
再把同值写入请求头，后端校验两侧一致。过渡期仍接受旧缓存页面使用的 `telebot-ui` 自定义头。

**风险范围**：低。攻击路径仍需要：
1. 用户在登录态访问到一个**与本站同源**的被注入页面（XSS 或子域名失控），
2. 或浏览器存在可绕过同源/头部约束的高危漏洞。

**缓解措施**：
- 不嵌入第三方 iframe；CSP 严格化。
- 子域名最小化，避免 `*.your-domain.com` 共享 cookie。
- Web 端写操作均要求受控自定义头 + double-submit token + `withCredentials`。
- 后端对缺失 gate 头或 token 不一致的写请求直接拒绝。

### 2.2 MASTER_KEY 轮换

**现状**：已提供 `python -m app.scripts.rekey`，可把库内 Fernet 密文字段用旧
`MASTER_KEY` 解密后再用新 `MASTER_KEY` 加密。脚本支持 `--dry-run`，生产执行前必须先验证。

```bash
python -m app.scripts.rekey --old "$OLD_MASTER_KEY" --new "$NEW_MASTER_KEY" --dry-run
python -m app.scripts.rekey --old "$OLD_MASTER_KEY" --new "$NEW_MASTER_KEY"
```

覆盖字段：账号 API ID/API Hash/session、代理密码、LLM API Key、通知 Bot Token、账号 Bot Token、
Web TOTP secret，以及 `account_bot_transfer_notice:*` 系统设置内的交互/转账 Bot Token。

**风险范围**：中低。计划内轮换可平滑完成；若确认 `MASTER_KEY` 与数据库备份同时泄露，
攻击者可能已解开旧密文，仍需按 §3.3 评估是否强制重绑账号与轮换第三方 token。

### 2.3 pending_totp 已迁到 Redis

**现状**：登录第一步通过后，后端在 Redis 中写入 5 分钟 TTL 的 `auth:pending_totp:*`
挂起状态，cookie 只保存随机 token；第二步用 token 换正式 JWT。旧实现残留的 `pending_totp`
cookie 会在新流程中主动清理。

**风险**：5 分钟窗口内若用户机器被劫持（恶意浏览器扩展 / 物理接触），攻击者仍可能复用该
token，但服务端 TTL 和 Redis 删除让窗口更短，也便于主动作废。

**缓解**：
- HttpOnly：JS 偷不到（要绕需要更深层的浏览器漏洞）。
- SameSite=Lax：阻断 CSRF。
- 5 分钟 TTL：远小于一次正常登录耗时。
- Redis 端保存状态：cookie 不再承载用户名和已通过密码标志。

### 2.4 登录安全套件：通知 Bot OTP、TOTP 与服务器恢复码

**现状**：登录安全套件默认关闭。管理员可在「系统设置 → 用户与管理 → 登录安全套件」里开启通知 Bot OTP 防爆破或 TOTP 登录验证。TOTP 分成两步：先绑定验证器密钥，再打开“登录验证”开关；关闭开关不会删除密钥，只是不再要求登录时输入 TOTP。

Web 登录密码失败达到阈值后，下一次正确密码不会直接签发 cookie，而是先通过已启用的通知 Bot 发送 6 位登录验证码。通知 Bot 不可用、Telegram 不通或 TOTP 无法使用时，管理员可以 SSH 到服务器生成一次性恢复码。

```bash
# 本地/开发环境
make auth-recovery

# 生产容器内，按实际 compose 服务名执行
docker compose exec web python -m app.scripts.auth_recovery

# 指定用户和有效期
docker compose exec web python -m app.scripts.auth_recovery --username admin --ttl 900
```

恢复码只打印一次，数据库只保存哈希；登录时仍必须输入正确密码。恢复码成功使用一次后立即失效，过期后也会被拒绝。

**推荐顺序**：
1. 先确认服务器 SSH / 容器执行权限可用，必要时能运行 `make auth-recovery`。
2. 再确保至少有一个通知 Bot 已启用并能收到测试消息。
3. 绑定 TOTP 密钥后，先用当前浏览器确认验证码可验证，再打开 TOTP 登录验证开关。
4. 不要在没有恢复码路径的情况下把所有登录都强制绑定二次验证。

---

## 3. 应急 SOP

每条 SOP 都假设「事件已确认」。**先停服 → 再处置 → 最后恢复**。

### 3.1 怀疑某管理员账号被攻陷

```bash
# 1. 让对方立刻下线
curl -X POST https://<host>/api/auth/logout -H "Cookie: ..."   # 当前 session

# 2. 强制改 password_hash 让现有 JWT 失效（等到 JWT 过期或重启服务）
psql "$DATABASE_URL" <<SQL
UPDATE web_user SET password_hash = '!INVALIDATED' WHERE username = '<目标>';
SQL

# 3. 翻审计日志看异常操作
psql "$DATABASE_URL" -c "
  SELECT ts, action, target, detail FROM audit_log
  WHERE user_id = (SELECT id FROM web_user WHERE username='<目标>')
  ORDER BY ts DESC LIMIT 200;"

# 4. 若该账号曾绑定 TOTP，建议同时让管理员重新生成 secret
```

### 3.2 怀疑某 TG 账号 session 被盗

```bash
# 1. UI：账号详情 → 暂停（防止机器人继续主动发消息）
curl -X POST https://<host>/api/accounts/<aid>/pause

# 2. 让 worker 在 TG 端撤销这个 session
#    最稳的做法是删账号；删的过程会调用 client.log_out()
curl -X DELETE https://<host>/api/accounts/<aid>

# 3. 用户重新走 /accounts/new 绑定向导，会签发一个新 session 字符串
```

### 3.3 .env 泄露 / MASTER_KEY 泄露

> 若只是计划内轮换或怀疑 `.env` 暴露但没有证据表明数据库也泄露，优先走 rekey 平滑轮换。
> 若确认 `MASTER_KEY` 与数据库备份同时落入攻击者手中，旧 session / Bot Token 可能已经被解密，
> rekey 只能保护后续数据，仍应考虑强制重绑账号与轮换第三方 token。

```bash
# 1. 立即停服
docker compose stop

# 2. 先把当前 .env 备份到只有你自己可读的地方（重要：保留旧 MASTER_KEY，
#    因为 DB 里所有 session_enc/api_hash_enc/totp_secret_enc 都是用它加密的）
cp .env /root/secure-store/env.<incident-ts>
chmod 600 /root/secure-store/env.<incident-ts>

# 3. 生成新密钥
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 4. 先用旧/新 MASTER_KEY 做 dry-run，确认所有密文字段可解
docker compose run --rm web python -m app.scripts.rekey \
  --old '<旧 MASTER_KEY>' \
  --new '<新 MASTER_KEY>' \
  --dry-run

# 5. 执行重钥；脚本遇到任何无法解密字段会回滚，不会半写
docker compose run --rm web python -m app.scripts.rekey \
  --old '<旧 MASTER_KEY>' \
  --new '<新 MASTER_KEY>'

# 6. 编辑 .env：用新值覆盖 MASTER_KEY、JWT_SECRET、POSTGRES_PASSWORD
vi .env
chmod 600 .env

# 7. 如果确认 DB + MASTER_KEY 已同时泄露，重钥后还要让所有 TG 账号重新走绑定向导。
#    否则可跳过这一步。两条路径任选：
#
#    A) 全清重来（推荐，最干净）：
psql "$DATABASE_URL" -c "TRUNCATE account, audit_log, runtime_log, rate_limit_event CASCADE;"
#
#    B) 保留账号元信息，只清 session：
psql "$DATABASE_URL" -c "UPDATE account
  SET session_enc='', api_id_enc='', api_hash_enc='', status='login_required';"

# 8. JWT_SECRET 已换 → 所有 web 用户的 cookie 自动失效，下次登录强制重输密码

# 9. 写一条入侵审计
psql "$DATABASE_URL" -c "
  INSERT INTO audit_log (ts, user_id, action, target, detail)
  VALUES (now(), NULL, 'security.master_key_rotated', 'system',
          '{\"reason\":\"<事件说明>\"}'::jsonb);"

# 10. 启服
docker compose start
```

### 3.4 数据库泄露但 MASTER_KEY 没泄露

DB 里 session/api_hash/totp_secret 都是 Fernet 密文，**只要 MASTER_KEY 没一起泄**就还能用。

```bash
# 1. 把所有管理员账号强制重置（防止密码哈希被离线撞）
psql "$DATABASE_URL" -c "UPDATE web_user SET password_hash='!INVALIDATED';"

# 2. 紧急轮换 JWT_SECRET（让现存 cookie 全失效）
sed -i.bak 's/^JWT_SECRET=.*/JWT_SECRET=<新值>/' .env
docker compose restart web

# 3. 立刻确认 MASTER_KEY 没在同一个泄露包里；若同泄 → 走 §3.3
```

### 3.5 整机被入侵 / 物理接触

按最严流程：

1. 立刻断网。
2. 镜像取证（如有需要）。
3. 在新机器上重建：跑 §1 一次性配置 → 用最近一次干净的 DB 备份恢复 → 走 §3.3 强制密钥轮换 →
   通知所有 TG 账号持有人重新绑定。

---

## 4. 日常巡检建议

| 频率 | 检查项 | 命令 / 位置 |
| --- | --- | --- |
| 每天 | `audit_log` 是否有异常 action（login fail 集中、`account.delete`、`humanize.update` 异常） | `psql -c "SELECT ... FROM audit_log WHERE ts > now()-interval '1 day' AND action LIKE '%fail%';"` |
| 每周 | 备份还原演练（在隔离机器） | `bash deploy/backup.sh && bash deploy/restore.sh` |
| 按需 | 插件 lint 规则升级后或完成批量插件迁移后，跑一次存量回填 | `python -m app.scripts.lint_existing_plugins --dry-run`（确认 diff）→ `python -m app.scripts.lint_existing_plugins` |
| 每月 | 跑一次 `bash deploy/backup-keys.sh`，更新异地 .gpg | 把旧 .gpg 销毁前确认新 .gpg 能成功解密 |
| 每季 | 复盘是否仍接受 §2 中三项风险；V1.5 来了就按计划修 | 在本文件末尾加 changelog |

---

## 5. 反模式（不要做）

- ❌ 把 `.env` 提交到 git（即使是私有仓库）。
- ❌ 在 docker-compose.yml 里硬编码密码（即便加了 `.gitignore` 也容易漏）。
- ❌ 用 `--no-verify` / `--no-gpg-sign` 跳过任何安全检查来「先把功能跑起来」。
- ❌ 把 MASTER_KEY 和 DB 备份放在同一个云盘 / 同一台机器。
- ❌ 多管理员复用同一个 web_user（每人单独账号，方便审计追溯）。
- ❌ 在公开聊天 / 截屏里暴露 cookie / token / api_hash。

---

## 6. 应急响应工单模板

当发生安全事件时，使用此模板记录处置过程：

```markdown
## 安全事件 #<编号>

**发现时间**：YYYY-MM-DD HH:MM UTC
**发现人**：<姓名/ID>
**事件类型**：[ ] 账号攻陷 [ ] 密钥泄露 [ ] 数据库泄露 [ ] 其他

### 事件描述
<简述发生了什么>

### 影响范围
- 受影响账号：<列表>
- 受影响数据：<列表>
- 潜在泄露信息：<列表>

### 处置步骤
1. [ ] 停服（时间：____）
2. [ ] 执行 SOP §3.X（具体步骤：____）
3. [ ] 验证修复（时间：____）
4. [ ] 恢复服务（时间：____）

### 根因分析
<事后填写>

### 改进措施
<事后填写>

### 完成时间
YYYY-MM-DD HH:MM UTC
```

---

## Changelog

- **2026-05-06** —— Sprint 4 Wave 3：开源向润色，新增应急响应工单模板。
- **2026-05-03** —— Sprint 2 #1：初稿，覆盖一次性配置、三项已知接受风险、五条应急 SOP。
