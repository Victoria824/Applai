# Applai Chrome 应用店上架资料

> 包：`applai-store-<version>.zip`（manifest.json 必须在 zip 根目录）
> 隐私政策：https://victoria824.github.io/Applai/privacy.html
> 费用：$5 一次性，之后更新免费

## 一、商店信息（复制粘贴）

**名称**：Applai — 求职自动投递

**简短描述**（≤132 字符）：
```
在公司官网职位页自动填写申请表，简历一键上传，验证码进待办队列并手机提醒。
```

**完整描述**：
```
Applai 是你的自动求职助手。


打开任意公司官网的职位申请页（支持 Greenhouse / Lever / Ashby / Workday 等主流招聘系统），
点一下"自动填写"，姓名、邮箱、电话、工作经历等信息自动填好，简历自动上传。


上传简历自动识别生成求职画像；每天为你匹配最合适的职位，智能打分排序。


遇到验证码自动转入待人工队列，手机 ntfy 推送提醒，你手动过一下就行。


https://applai-backend.fly.dev/dashboard —— 手机上也能看队列、改画像、跟进投递进度。

数据只保存在你的浏览器和你自己的账号下，可随时删除。隐私政策见官网链接。
```

**类别**：Productivity（生产力工具）

**语言**：中文（主要）

## 二、截图（需自己截 2-3 张，1280x800 或 640x400）

1. 插件 popup 主界面（在演示页 https://victoria824.github.io/Applai/demo/greenhouse-demo.html 上打开 popup 截图）
2. 表单自动填写后的效果（演示页填完表的样子）
3. 网页控制台 https://applai-backend.fly.dev/dashboard（登录后的队列页，手机或桌面截图）

截图要求：真实界面，不要放营销 mockup。

## 三、隐私与数据披露（商店后台问卷，按此填写）

- 是否收集用户数据：是
- 收集内容：求职画像、简历、账号信息、投递记录（见隐私政策）
- 用途：应用功能（自动填表、职位匹配、进度跟踪）
- 是否出售/共享：否
- 远程代码：否（全部代码打包在插件内）

## 四、`<all_urls>` 权限说明（审核可能会问，备答）

> Job application forms live on thousands of different company career sites
> (Greenhouse / Lever / Ashby / Workday hosted domains). The extension needs
> host permission to read and fill the application form on the page the user
> explicitly opens and clicks "autofill" on. It does not collect browsing
> history or run on pages without user action.

## 五、提交步骤（Victoria 操作）

1. 打开 https://chrome.google.com/webstore/devconsole ，用 Google 账号登录
2. 交 $5 注册开发者（一次性，之后更新免费）
3. "新增项" → 上传 `applai-store-<version>.zip`
4. 填写上面的商店信息，上传截图，粘贴隐私政策链接
5. 完成隐私/数据披露问卷，提交审核（首次 1-3 天）
6. 过审后把链接发给朋友，他们点"添加到 Chrome"即可

## 六、发新版（免费）

1. 改 `extension/manifest.json` 里的 `version`（如 0.2.1 → 0.3.0）
2. 重新打包：`cd extension && zip -qr../applai-store-<version>.zip. -x "node_modules/*"`
3. 开发者控制台 → 该项目 → 上传新包 → 提交审核（更新审核通常几小时）
