# 全球化 / 15 语言方案

> 目标：Windows Store 全球发布，软件支持全球主要 15 种语言。
> 现状：前端是单文件 Vue 3 SPA，登录页/隐私页服务端渲染，AI 提示词以中文为主，
> 数据库里的科目/考点是中文。**这不是“把文案翻译一下”就能完成的事**，需要分阶段做。

## 一、先定产品定位（最关键）

当前产品是「初三中考 7 科」：语文、数学、英语、物理、化学、历史、道德与法治。
海外用户上传的作业未必属于这套课程。全球化必须先选：

| 定位 | 说明 | 需要做什么 |
|---|---|---|
| A. 海外华人 / 中文课程 | 用户仍学中国课程 | 只做 UI 翻译 + 中文课程提示词 |
| B. 通用中学 AI 拍照辅导（推荐） | 用户上传自己的作业，AI 按当地课程讲解 | 课程/科目泛化、语言自适应、AI 提示词改造 |
| C. 分市场课程适配 | IB / IGCSE / A-Level / 美国 Common Core 等 | 每个市场单独做内容、题库、考点体系 |

**建议：以 B 为全球化底座，zh-CN 保留“中考模式”。**  
如果只做 A，15 种语言的产品意义不大；如果做 C，工作量是另一个产品，不建议第一版就铺开。

## 二、15 个 locale 建议

按语言使用人数 + Microsoft Store 主要市场：

1. `en` English
2. `zh-CN` 简体中文
3. `es` Español
4. `hi` हिन्दी
5. `ar` العربية（RTL）
6. `pt-BR` Português (Brasil)
7. `ru` Русский
8. `ja` 日本語
9. `de` Deutsch
10. `fr` Français
11. `ko` 한국어
12. `it` Italiano
13. `tr` Türkçe
14. `vi` Tiếng Việt
15. `id` Bahasa Indonesia

补充：`zh-TW` 繁體中文可以作为第 16 个 locale，或替换 `it`/`tr`，按 Store 数据决定。  
每个语言只做一个主地区变体，避免 15 种语言变成 40 个 locale。

## 三、技术架构

### 1. 前端 i18n

- 新增 `static/locales/{locale}.json`，按模块分 key：`common`、`login`、`app`、`subjects`、`errors`、`pdf`。
- 使用轻量 i18n 层（`vue-i18n` 或自研 `t(key, params)`），不要在每个页面写 15 份文案。
- 语言选择：
  - 优先级：用户手动选择 → 账号保存的 locale → Cookie → `Accept-Language` → `en`。
  - 顶部/登录页加语言切换；切换后写入账号和 Cookie。
- 日期、数字、货币、复数用 `Intl.DateTimeFormat` / `Intl.NumberFormat` / `Intl.PluralRules`；阿拉伯语、俄语复数规则必须用 ICU。
- 静态 HTML 用 `data-i18n`，Vue 模板用 `t('key')`；JS 里的提示、错误、确认框全部走 key。
- **字符串提取**：先冻结功能，写脚本扫描中文文案并生成 key 清单；之后做 pseudo-locale（如 `en-XA`）检查漏翻和布局。

### 2. RTL（阿拉伯语）

- `dir="rtl"` 动态设置；CSS 用逻辑属性（`margin-inline`、`padding-inline`、`inset-inline`）。
- Tailwind 的 `ml/mr/left/right` 要逐步换成 `ms/me/start/end`，或加 `rtl:` 变体。
- 箭头、返回图标、进度方向要镜像；数学公式、图表、题目图片不镜像。
- 登录页、列表、详情、对话、PDF 全部做 RTL 截图回归。

### 3. 服务端 / 登录页 / 错误消息

- 登录页、隐私页是服务端渲染：按 `?lang=` / Cookie / `Accept-Language` 输出对应语言，避免先闪中文。
- API 不返回中文句子，改为返回 `code + params`（例如 `quota_exhausted: {kind:"chat"}`），由前端翻译；邮件/短信由后端按 locale 选模板。
- 用户表增加 `locale`、`curriculum`（`cn_zhongkao` / `generic` / 未来 `ib` 等）。
- 数据库科目从中文改为 **canonical key**（`math`、`physics`、`chinese`…），显示时翻译；老数据做一次映射迁移。

### 4. AI 语言与课程

- 所有 AI 请求带 `locale`、`curriculum`。
- 提示词保留英文/中文母版，在末尾加硬性指令：
  - 用 UI 语言写 `analysis`、对话、变式题；
  - `clean_text` 保持原题语言；
  - 数学公式继续用 LaTeX；
  - 按用户所在课程作答，不要默认中国中考。
- 15 种语言不建议各写一套完整提示词：先用 **英文母版 + locale 指令**，对 en/es/pt/fr/de/ja/ko/ar/hi 做重点评测；中文保留中文母版。
- 题目识别增加 `subject_key`（canonical）和 `topic`（本地语言自由文本）；`tag` 不再依赖中文「科目-考点」。
- 建立每种语言 20~50 道评测题，检查：语言是否正确、数学是否正确、是否出现中文残留、是否按当地课程举例。

### 5. 字体 / PDF / 动画

- Web 用系统字体栈兜底；为一致性可自托管 Noto Sans（拉丁/西里尔）+ Noto Sans Arabic/Devanagari + Noto Sans CJK 子集。
- **PDF 是最大的技术债**：现在 ReportLab 只内嵌 Noto Sans SC，阿拉伯语/印地语没有文字整形（shaping），RTL 也会错。
  - 建议改成 **Chromium HTML → PDF**：已有 Puppeteer/Chromium 导出经验，能处理 RTL、连写、CJK、Devanagari。
  - 若坚持 ReportLab：只能先支持拉丁/中日韩，阿拉伯/印地语 PDF 延后。
- AI 生成的 SVG/HTML 动画：提示词要求按 locale 输出文字，浏览器字体负责渲染；RTL 需要单独测试。

### 6. 登录 / 短信 / 邮件

- 当前注册依赖阿里云 PNVS + 中国短信模板，海外手机号/模板/签名都成问题。
- 全球化建议：
  - 增加 **邮箱 + 密码 / 邮箱验证码** 作为全球主路径；
  - 或接 OAuth（Microsoft / Google / Apple）；
  - 短信只保留 +86 或接 Twilio/MessageBird 等国际短信，按国家配置签名/模板；
  - 邮件用 SendGrid/Resend 等事务邮件，配 SPF/DKIM，模板按 locale。
- 这会影响注册转化和成本，必须在上架前定。

### 7. 支付 / Store

- Microsoft Store 内购已接入；`getDetails` 会返回本地货币价格，继续使用。
- 15 个语言对应 Store listing 的多语言描述、截图、隐私政策；Store 按市场定价。
- 额度/会员状态是账号级，和语言无关。

## 四、法律与基础设施（全球化真正的门槛）

1. **数据跨境**：题目照片/对话会发给 DeepSeek（中国）。欧盟/英国用户涉及 GDPR 跨境传输，需 SCC/DPA/同意，或改用区域 AI 供应商。建议做 **LLM provider abstraction**，按地区路由（EU/US 用 Azure OpenAI/其他，CN 用 DeepSeek），至少先做法律评估。
2. **未成年人**：COPPA（美国 13 岁以下）、GDPR-K（欧盟）、中国《儿童个人信息网络保护规定》；需要年龄门、监护人同意、家长删除通道。
3. **隐私政策/服务条款**：15 种语言版本，最好由本地法律顾问审阅；至少英文版不能是机翻。
4. **数据驻留/延迟**：当前生产在深圳单机；全球用户加载和 AI 调用延迟高。静态资源上 CDN；API 可按区域部署；图片存对象存储。
5. **Microsoft Store 合规**：年龄分级、隐私政策、支持联系方式、退款/订阅说明、账号注销（已完成）。

## 五、分阶段实施

### Phase 0：定方向（1~2 周）
- 定 A/B/C 定位；定 15 个 locale；定 AI 供应商/数据跨境方案。
- 冻结功能，盘点全部中文文案；建立术语表（错题、经典题、变式题、考点、去红笔、重做卷等）。

### Phase 1：i18n 底座 + 英文（2~3 周）
- 前端 i18n 层、语言切换、`Intl`、登录页/隐私页服务端 locale。
- API 错误码化；用户表加 locale/curriculum；科目 canonical key。
- AI 请求带 locale；英文母版提示词；英文评测集。
- 先上 en + zh-CN，内部测试。

### Phase 2：核心 6 语言（3~4 周）
- es、pt-BR、fr、de、ja、ko；RTL 预研。
- 机器翻译 + 母语审校；pseudo-locale、截图回归。
- 登录/邮件/PDF 拉丁+CJK 路径。

### Phase 3：15 语言 + RTL + 全球登录（4~6 周）
- ar、hi、ru、it、tr、vi、id；RTL、Devanagari、西里尔字体。
- Chromium PDF 替换 ReportLab；国际短信/OAuth/邮箱。
- Store listing 15 语言；隐私政策本地化。

### Phase 4：市场适配（持续）
- 按市场做课程/考点/示例；不是翻译问题，是内容运营问题。

## 六、成本与风险（粗略）

- 15 语言 UI 翻译：约 1 万~2 万词，专业翻译约 $10k~$25k；AI 初翻 + 母语审校约 $3k~$8k。
- 法律/隐私本地化：$3k~$10k（不含律师费）。
- 工程：单人约 3~6 个月；两人可压缩到 2~3 个月。
- 最大风险不是翻译，而是：**课程不匹配、AI 数据跨境、PDF/RTL、全球登录**。

## 七、建议

**不要一次性硬上 15 种语言。** 先把 i18n 架构、英文、AI locale、RTL/PDF 做对，再按 Store 数据逐批开语言。  
如果目标是“Store 列表显示 15 种语言”，可以先做 Store listing 本地化；App UI 先支持 en + zh-CN + 4 个主要语言，避免半成品翻译影响口碑。

## 八、现在就能开始的 5 件事

1. 确认定位：A 海外华人 / B 通用中学辅导 / C 分市场课程。
2. 确认 15 个 locale 和首发语言（建议 en、zh-CN、es、pt-BR、ja）。
3. 冻结文案，跑字符串盘点，建术语表。
4. 把用户表加 `locale`、`curriculum`，AI 请求带 locale。
5. 决定 AI 跨境方案（DeepSeek 全球 / 区域供应商）和全球登录方式（邮箱/OAuth/国际短信）。
