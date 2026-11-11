# 新用户注册要收集什么信息 — 以 1Password（Ashby）表单为模板

来源：1Password "Account Executive, Enterprise — Future Opportunities" 申请表
（jobs.ashbyhq.com/1password/d91e63b2-abb3-4722-a1ec-280e81604b0b/application，
2026-11-03 实测）。目标：新用户注册时一次问齐，让引擎以后能自动填，
而不是每份申请都留人工。

## 一、已有字段（注册时已收集，引擎已会填）

| 表单项 | 画像字段 | 备注 |
|---|---|---|
| Resume* | resumeFile | 含按岗定制版 |
| Full Name | firstName + lastName | |
| Email | email | |
| Phone Number | phone | 存国际格式（如 +1 区号）更稳 |
| Current Location | location | 决定雇佣实体的关键字段 |
| LinkedIn URL | linkedin | |
| GitHub URL | github | |
| Portfolio URL / Other website | website | 一个 website 字段可覆盖两者 |
| 期望薪资起止 | salaryMin / salaryMax + salaryCurrency | 表单要求无逗号纯数字，引擎填数字即可 |
| 签证担保三态 | needsSponsorship（未设置/不需要/需要） | **必须是用户本人在 UI 里点的事实决定，任何人不得代设** |
| 是否愿意搬迁 | willingToRelocate | |

## 二、新增字段（注册时要补问，否则永远留人工）

按重要性排序：

1. **合法工作国家（多选）authorizedWorkCountries[] — 最关键**
   表单原题："Do you live in, and are you legally authorized to work in the
   countries listed in the job posting?"（本例国家 = United States）。
   现状缺陷：画像里 workAuth 只是一段自由文本，引擎规则是
   "含 canada/authorized 就答 Yes"——**不分国家**。多伦多用户申美国岗时，
   拿加拿大授权去答 Yes 是向雇主作虚假陈述。
   引擎新规则（字段上线后）：岗位国家 ⊆ 已授权国家 → 答 Yes；
   明确未授权 → 答 No；否则留人工。**在字段上线前，工作许可一律留人工，
   不得自动作答。**
2. **X（Twitter）URL — twitter**：表单 Links 区有 X 专栏，画像只有
   linkedin/github/website。
3. **当前公司 currentCompany + 当前职位 currentTitle**：表单有 Current
   Company、Current job title 两栏；画像目前只有简历文本里的经历，
   没有独立字段。
4. **OTE 起止 oteMin / oteMax（销售岗）**：表单单独问 On Target Earnings
   起止，与 base salary 分开；画像只有 base salary。
5. **服务过的客户层级 segments[]（多选：SMB / Commercial / Enterprise /
   Major Accounts / Not Applicable）**：销售岗表单的多选题，画像无此字段。

## 三、每份申请仍留人工的项（不要在注册时问，也不要自动填）

- **利益冲突关系**（亲友是否在该公司）：每家公司都不同，且是敏感事实，
  永远人工回答，引擎不得代答。
- **Why 1Password / Additional Information**：每家公司定制的主观题；
  将来可由按岗改写 AI 起草供用户审，**不得静默自动提交**。
- **What brought you to this job posting（渠道）**：每岗不同，人工选。
- **前雇员 Yes/No**：可从工作经历里**正向匹配**到该雇主时才自动答，
  匹配不到时留人工（简历没写 ≠ 没做过，不得默认答 No）。
- **网络安全 SaaS 经验等岗位专属 Yes/No**：无画像字段对应时留人工。

## 四、永远不得收集、不得自动填的项

- **人口统计调查**（种族、跨性别、性别认同、LGBTQ2S+、残疾、兵役）：
  表单明言自愿、匿名、不影响录用。注册时问 = 逼用户交敏感信息；
  自动填 = 替用户向雇主披露身份。两条都不做。
- **住宿需求（Accommodations）**：敏感健康信息，人工自愿填写。
- **背景调查 "I understand"、隐私政策 "I agree"**：法律同意类勾选，
  必须用户每份申请自己点，引擎不得代点。
- **订阅招聘资讯 "Yes"**：营销 opt-in，不得默认勾选。
- **Pronouns**：沟通偏好，敏感且非必填；如收集只做可选项，缺省留人工。

## 五、落地状态（2026-11-03 已实现）

- 向导：审批岗位画像之后、启动之前新增“必答问题”步骤——合法工作国家、
  雇主担保、现公司、现职位必填；X / OTE / 客户层级可选。保存到画像。
- 画像页：person 表单与 job 表单均已加上述字段；job 表单去掉 workAuth
  自由文本（引擎不再用它作答）。
- 引擎（插件 0.2.14）：工作许可只有岗位国家全在授权清单里才答 Yes，
  否则留人工，绝不自动答 No。
- 未落地（有意排除）：第四节“永远不得收集”项未进必答问题；每岗人工项
  未进注册问题（它们属于具体雇主/岗位）；队列过滤待定。
