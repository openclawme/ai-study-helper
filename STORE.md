# 上架 Microsoft Store（PWA / MSIX）清单

> 目标：商店里安装的是 PWA 外壳；日常改网页只部署服务器，**不需要重新提交审核**。

## 已完成（2026-11-17）

- `static/manifest.webmanifest`：名称、图标、`start_url`、`scope`、`standalone`、截图
- `static/sw.js`：只缓存页面壳和 CDN 静态资源；`/api/`、`/media/`、`/anim/` 等个人数据一律绕过
- `static/offline.html`：断网时的离线页
- 192 / 512 / maskable 图标
- 登录页和首页注册 Service Worker；新版本提示「刷新」，不静默替换
- 生产环境验证：
  - `https://goodgoodstudy.cc/manifest.webmanifest` 无错误
  - Chrome `Page.getInstallabilityErrors` 为空
  - 断网导航落到离线页
- 商店截图（演示账号 + 假题，不含真实学生数据）：
  - `static/store-list-1366.png`
  - `static/store-detail-1366.png`

## Partner Center 身份（已拿到）

```text
Package/Identity/Name: 16388WzzT.AI
Package/Identity/Publisher: CN=D8EFFF52-B354-4D66-80F6-88043D7B12CF
PublisherDisplayName: WzzT
Package Family Name: 16388WzzT.AI_4ze4wsqskfapc
Store ID: 9P41XLJWDL44
Store 链接: https://apps.microsoft.com/detail/9P41XLJWDL44
```

Windows 包已用上述身份生成：

- `~/boardtool/storepkg/pwabuilder-windows.zip`（约 8 MB）
- 内含 `我的AI学习助手.msixbundle`（提交 Store 用）
- `我的AI学习助手.sideload.msix` + `install.ps1`（本地测试用；无单独证书文件，
  安装脚本用 `pwainstaller.exe`，Windows 需允许旁加载/开发者模式）
- `classic.appxbundle`（老 Windows）

## 额度 / 付费（全局，账号通用）

- 额度绑定**账号**，网页版和 Store 版登录同一个账号，共享同一份额度。
- Windows Store 只是**购买入口**：Store 安装版能拉起微软收银台；网页版额度用完时
  提示去 Store 版用同一账号购买，买完网页端自动同步。
- Store 包直接使用主域名 `https://goodgoodstudy.cc`，**不需要 `store` 子域名/DNS**。
- `QUOTA_ENFORCED=0` 可临时关闭额度做压测/回滚；正式环境保持 1。
- 上线前已存在的老账号会一次性送 Pro 一年（`store_grandfathered` 标记）。

额度：

| | 免费 | Pro | Pro Max | 加量包 |
|---|---:|---:|---:|---:|
| 价格 | ¥0 | ¥29.9/月 | ¥59.9/月 | ¥12.9 |
| 题目 | 5（终身） | 200/月 | 500/月 | +100 |
| AI 讲解 | 20（终身） | 200/月 | 500/月 | +100 |
| 动画 | 2（终身） | 10/月 | 25/月 | +5 |

- 题目额度按“上传页”扣：一张照片/一页文档 = 1 次，一页多题不额外扣。
- 会员额度每月重置；加量包不随会员到期清零。
- 付费墙使用 Digital Goods API + Payment Request，只在 Microsoft Store 安装版里可用。

Partner Center 的 `InAppOfferToken` 必须与代码里的 SKU 一致：

- `gcl_pro_monthly`（Store ID `9PK4GLJ88T9Q`）
- `gcl_pro_max_monthly`（Store ID `9N3N2MNDT9J5`）
- `gcl_boost`（Store ID `9P8RSXV8TR0V`）

⚠️ **Store ID 不是 Digital Goods API 的 `itemId`**。API 用的是 Partner Center 里
add-on 的 **Product ID / In-app offer token**（开发者填的字符串）。上面三个 `gcl_*`
是代码期望的 token；如果 Partner Center 实际生成的是别的字符串，需要把它们替换到
`STORE_SKUS` 里，网页更新即可，不用重新打 MSIX。

**安全说明**：Microsoft Store 的 Digital Goods API 不提供唯一交易号，当前服务端以
客户端 `listPurchases` + 购买回调入账。正式收费前建议在 Microsoft 侧增加服务端校验，
否则只能防普通用户，防不住刻意伪造请求。

## 上架前你需要准备

1. **Microsoft Partner Center 开发者账号**
   - 个人或公司账号；未成年学生通常需要监护人协助注册。
2. **保留应用名称**
   - 建议中文名：`我的AI学习助手`
   - 英文名可选：`AI Study Helper`
3. **包标识**（Partner Center → 应用 → 产品标识）
   - `Package/Identity/Name`
   - `Package/Identity/Publisher`
   - `Package/Properties/PublisherDisplayName`
4. **隐私政策 URL** 和 **支持 URL/邮箱**
   - 已上线：`https://goodgoodstudy.cc/privacy`
   - 联系邮箱已设置为 `openclawmail@qq.com`（环境变量 `PRIVACY_CONTACT`）
5. **年龄分级问卷**
   - 应用面向初三学生，含未成年人照片/对话，需如实填写数据收集项。
6. **商店文案/关键词/截图**
   - 分类：Education
   - 截图可先用上面两张，建议再补 2~4 张（手机端、AI 对话、导出 PDF）。

## PWABuilder 打包步骤

1. 打开 <https://www.pwabuilder.com>，输入 `https://goodgoodstudy.cc`。
2. 查看报告：manifest、Service Worker、HTTPS 应全部通过。
3. 选择 **Package for Stores → Windows**。
4. 填入 Partner Center 里的 Package ID / Publisher / Publisher Display Name。
5. 下载 `.msix` / `.msixupload`。
6. 在**干净的 Windows 10/11 虚拟机**（不装 Python/Node）安装测试：
   - 首次启动、登录、上传、AI 对话、导出 PDF
   - 断网显示离线页
   - 卸载/重装
7. Partner Center → New product → 上传包 → 填写 Store listing → 提交审核。

## 更新规则

| 改动 | 是否要重新提审 |
|---|---|
| 网页代码、样式、接口、AI 功能 | **不要**，部署服务器即可 |
| Service Worker / manifest 内容 | 不要（但改 `SHELL_CACHE` 版本，确保缓存刷新） |
| 应用名、图标、`start_url`、权限、包标识 | 要，重新打 MSIX |
| 隐私政策、年龄分级 | 在 Partner Center 更新，通常要审核 |

**注意**：导航请求已设为网络优先，用户下次打开会拿最新 HTML；静态资源 stale-while-revalidate。改 `sw.js` 的 `SHELL_CACHE` 版本可强制清理旧缓存。

## 已完成的产品能力

1. **用户自助注销账号**
   「修改密码 → 注销账号」可永久删除账号、题目、照片、对话、动画和额度记录；
   会提示用户先取消 Microsoft Store 订阅。
2. **公开隐私政策页**
   `https://goodgoodstudy.cc/privacy`，覆盖数据收集、第三方、保存期限、删除方式、
   未成年人保护和联系方式。

## 还需要你提供

- Partner Center 上传两个包并创建私有飞行测试；
- 商店文案、年龄分级、全球市场定价。

用户自助注销和 `/privacy` 页面已经完成，不需要再重复实现。
