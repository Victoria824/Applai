# Applai — 全自动求职投递工具（原型 v0.1）

浏览器插件原型：验证"官网直投"链路。用户在公司官网职位页打开插件，
插件自动识别 ATS（Greenhouse / Lever / Ashby / Workday），用个人画像
自动填写申请表单；遇到验证码则把任务丢进"待人工处理"队列，并通过
手机推送（ntfy）提醒用户。

## 设计原则

- **只做官网直投**：不碰 LinkedIn / Indeed 账号体系，避免封号风险。
- **默认半自动**：v0.1 只自动"填写"，提交按钮永远由用户点。
  `autoSubmit` 开关默认关闭，打开后也只在"无验证码 + 无缺失字段"时才提交。
- **验证码隔离**：检测到 reCAPTCHA / hCaptcha / Turnstile 即停止，
  任务进 `needs_manual` 队列，手机推送提醒。
- **简历上传不代劳**：浏览器安全限制下插件无法静默设置 file input，
  插件会高亮简历上传框并提示用户手动选择文件。

## 目录结构

```
job-apply-tool/
├── README.md
├── extension/
│   ├── manifest.json          # MV3
│   ├── profile.sample.json    # 个人画像示例
│   ├── popup/                 # 插件弹窗 UI
│   │   ├── popup.html
│   │   ├── popup.css
│   │   └── popup.js
│   ├── content/               # 注入到职位页的脚本
│   │   ├── ats.js             # ATS 识别
│   │   ├── fill.js            # 表单自动填写引擎
│   │   ├── captcha.js         # 验证码检测
│   │   └── bridge.js          # popup <-> content 消息路由
│   └── background/
│       └── sw.js              # 队列存储 + ntfy 手机推送
└── test/
    ├── package.json
    └── fill.test.js           # jsdom 填写链路测试
```

## 快速开始

### 1. 加载插件（Chrome / Edge）

1. 打开 `chrome://extensions`，开启右上角"开发者模式"
2. "加载已解压的扩展程序" → 选择 `extension/` 目录
3. 固定插件图标到工具栏

### 2. 完成第一步：求职画像

点击插件图标 → "第一步：求职画像" → "编辑"，按表单填写：

- **基本信息**：姓名、邮箱、电话、现居地、LinkedIn/GitHub/个人网站
- **简历**：选择 PDF/DOC 文件（只保存在本机浏览器，不上传服务器）
- **求职偏好**：目标职位、行业、工作年限、期望地点、远程偏好、
  期望薪资（区间+币种）、工作许可、是否需要签证担保、通用求职信

旧版 JSON 画像会自动迁移为新结构；"高级：JSON"保留给直接编辑。

### 3. 配置手机推送（ntfy，免费免注册）

1. 手机安装 ntfy App（iOS / Android 均有）
2. 在 ntfy 里订阅一个自定主题，例如 `victoria-apply-9f3k2`
3. 在插件"设置"里填入同一主题名 → 点"测试推送"，手机应收到通知

原理：遇到验证码时插件后台向 `https://ntfy.sh/<主题>` POST 一条消息，
手机 App 订阅了该主题即实时收到。主题名用随机字符串，相当于密码。

### 4. 验证投递链路（建议顺序）

1. 打开一个 Greenhouse 职位页（如 `boards.greenhouse.io/xxx/jobs/yyy`）
2. 点插件图标 → 确认识别出 `greenhouse`
3. 点"自动填写表单" → 观察字段被填入（绿色描边=已填，红色描边=需人工）
4. 手动选择简历文件 → 检查无误后手动点提交
5. 打开一个带验证码的申请页 → 点"自动填写" → 应提示验证码，
   任务进入"待人工"队列，手机收到推送

### 5. 运行自动化测试

```bash
cd test && npm install && npm test
```

测试用 jsdom 构造仿 Greenhouse / Lever / Workday / Ashby 表单，验证：
同义词匹配、富签名、自定义下拉框、radio 组、file input 标记、
验证码中断等 39 项。

## 填写引擎 v2（2026-10-05）

- **富 label 签名**：每个字段的签名拼自 `label[for]`、`aria-label`、
  `aria-labelledby`、祖先 label、前一个兄弟 label、name、id、placeholder、
  fieldset legend、Workday `data-automation-id` 容器、Ashby `data-field-path`
  包裹层。
- **同义词词典匹配**：规范字段 key → 中英同义词表，最长匹配获胜
  （`first name` 不会被 `name` 抢走）；排除词表防止 `company name`
  被当成人名。
- **自定义下拉框驱动**：Workday / react-select / Oracle JET 等非原生下拉，
  通过指针事件序列 + React `__reactProps.onClick` 兜底完成点选。
- **radio 单选题**：是否类问题（工作许可、签证担保）支持 radio 组作答。
- 填写流程改为异步（`bridge.js` 已适配）；测试 39 项全过
  （`cd test && npm install && npm test`）。

## 开源致谢

本项目站在以下开源项目的肩膀上（均为 MIT 协议，已按条款保留原作者信息）：

- [vesaias/JobNavigator](https://github.com/vesaias/JobNavigator) —
  富签名、 synonym 匹配、ANSWER_SCHEMA 等设计思想；
  `content/lib/match.js` 移植自其 `extension/lib/autofill_match.js`。
- [strelov1/freehire](https://github.com/strelov1/freehire) —
  `content/lib/combobox.js` 经 JobNavigator 移植，原作者 freehire。
- 另调研了 [Liam-Frost/AutoApply](https://github.com/Liam-Frost/AutoApply)
  （PolyForm Noncommercial 协议——仅学习思路，未复用代码）和
  [simonfong6/auto-apply](https://github.com/simonfong6/auto-apply)。

## 路线图

- **v0.2**：队列自动跑批（定时打开职位页、按规则自动填）、投递状态追踪、
  JD 匹配度打分 + 简历措辞 tailor（LLM）。
- **v0.3**：移动端 App（React Native / PWA）：只做仪表盘、待人工队列审批、
  推送触达，不做表单填写。
- **产品化**：多用户账号隔离、频率控制与反检测、服务端队列。

## 已知限制（原型阶段诚实清单）

- Workday 是重 JS 的 SPA，原型只做识别 + 通用字段尝试，大概率进待人工队列。
- 部分公司用自定义表单系统，label 匹配兜底可能漏字段 → 以红色描边提示。
- EEO / 多样性问卷默认不自动作答，标为"需人工确认"。
- 自动提交默认关闭；即使开启也不绕过验证码。
