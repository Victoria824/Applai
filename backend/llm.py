"""llm.py — LLM 语义匹配（OpenAI-compatible API，无 SDK 依赖）

支持任何 OpenAI 格式的服务：OpenAI / DeepSeek / OpenRouter / Ollama 本地等。
配置（环境变量）：
  APPLAI_LLM_API_KEY   必填，否则自动降级为纯规则打分
  APPLAI_LLM_BASE_URL  默认 https://api.openai.com/v1
  APPLAI_LLM_MODEL     默认 gpt-4o-mini
"""
import json
import os
import re
import time

import httpx


def get_config() -> dict:
    return {
        "api_key": os.environ.get("APPLAI_LLM_API_KEY", "").strip(),
        "base_url": (os.environ.get("APPLAI_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
        "model": os.environ.get("APPLAI_LLM_MODEL") or "gpt-4o-mini",
    }


def is_configured() -> bool:
    return bool(get_config()["api_key"])


def _profile_text(p: dict) -> str:
    rows = [
        f"目标职位：{', '.join(p.get('targetTitles') or []) or '未填'}",
        f"工作年限：{p.get('yearsExperience') or '未填'} 年",
        f"期望地点：{', '.join(p.get('preferredLocations') or []) or '未填'}；远程偏好：{p.get('remotePreference') or 'any'}",
        f"行业偏好：{', '.join(p.get('industries') or []) or '未填'}",
        f"期望薪资：{(p.get('salaryMin') or '?')}{(('-' + str(p.get('salaryMax'))) if p.get('salaryMax') else '+')} {p.get('salaryCurrency') or ''}({'annual' if p.get('salaryPeriod') == 'annual' else 'hourly'})".strip(),
        f"工作许可：{p.get('workAuth') or '未填'}；需签证担保：{'是' if p.get('needsSponsorship') else '否'}",
    ]
    return "\n".join(rows)


def _job_text(job: dict) -> str:
    desc = (job.get("description") or "")[:3500]
    return (
        f"标题：{job.get('title') or ''}\n"
        f"公司：{job.get('company') or ''}\n"
        f"地点：{job.get('location') or ''}\n"
        f"描述：{desc or '（无描述）'}"
    )


PROMPT = """你是资深招聘顾问。请评估候选人与职位的匹配度，打分 0-100。

候选人画像：
{profile}

职位：
{job}

只返回 JSON（不要用 markdown 包裹）：
{{"score": 85, "reasons": ["...", "...", "..."]}}

打分标准：90+ 完美匹配；70-89 高度匹配；50-69 部分匹配但有差距；
40-49 勉强相关；<40 不匹配。
reasons 用中文，2-4 条，每条一句话，说清匹配点或差距。"""


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    # 去掉可能的 markdown 包裹
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def llm_score(profile: dict, job: dict, timeout: int = 60) -> tuple[float | None, list]:
    """返回 (分数, 理由列表)；任何失败返回 (None, [])，调用方降级为规则打分。"""
    cfg = get_config()
    if not cfg["api_key"]:
        return None, []
    prompt = PROMPT.format(profile=_profile_text(profile), job=_job_text(job))
    try:
        r = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={
                "model": cfg["model"],
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 400,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        data = _extract_json(content)
        if not data:
            return None, []
        score = float(data.get("score", 0))
        score = max(0.0, min(100.0, score))
        reasons = [str(x) for x in (data.get("reasons") or [])][:4]
        return round(score, 1), reasons
    except Exception:
        return None, []


RESUME_PROMPT = """从下面的简历文本中提取求职者信息，只返回 JSON，不要任何解释。
字段（没有就留空字符串 / 空数组 / null）：
{"firstName": "", "lastName": "", "email": "", "phone": "", "location": "",
 "linkedin": "", "github": "", "website": "",
 "targetTitles": ["从经历推断 1-3 个目标职位"],
 "industries": [], "yearsExperience": 工作年限数字或null,
 "preferredLocations": [], "workAuth": "",
 "summary": "一句话中文总结"}
姓名如果是中文："王小明" → firstName "小明", lastName "王"；英文 "John Smith" → firstName "John", lastName "Smith"。

简历文本：
```
{resume}
```
"""


def llm_parse_resume(text: str, timeout: int = 60) -> dict | None:
    """简历文本 → 结构化字段；失败返回 None，调用方提示用户手动填写。"""
    cfg = get_config()
    if not cfg["api_key"] or not text.strip():
        return None
    try:
        r = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={
                "model": cfg["model"],
                "messages": [{"role": "user", "content": RESUME_PROMPT.replace("{resume}", text[:6000])}],
                "temperature": 0.1,
                "max_tokens": 800,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = _extract_json(r.json()["choices"][0]["message"]["content"])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


KEYWORDS_PROMPT = """你是职业规划师。根据求职者画像，生成用于全网职位搜索的关键词。
只返回 JSON，不要解释：
{"keywords": ["具体的职位头衔或技能关键词", "..."], "note": "一句话职业方向总结"}

要求：
- 5-8 个英文关键词，覆盖目标职位头衔 + 核心技能（如 "AI Engineer", "LLM", "RAG", "PyTorch"）
- 关键词要具体可搜索，避免过于宽泛的词
- 参考画像的目标职位、技能、工作经历

求职者画像：
```
{profile}
```
"""


EMAIL_PROMPT = """你是求职邮件分类器。判断这封邮件是不是求职相关的，并分类。

只返回 JSON，不要任何解释：
{{"kind": "interview|rejection|other", "company": "公司名（没有就空）", "job_title": "职位名（没有就空）", "summary": "一句话中文摘要（20字内）"}}

分类标准：
- interview：面试邀请、OA/笔试邀请、HR 约电话、offer 相关
- rejection：拒信、"unfortunately"、"not moving forward"、"已招到合适人选"
- other：其他（广告、newsletter、非求职邮件）

发件人：{sender}
主题：{subject}
正文：
```
{body}
```
"""


def llm_classify_email(sender: str, subject: str, body: str, timeout: int = 30) -> dict | None:
    """返回 {kind, company, job_title, summary}；失败返回 None。"""
    cfg = get_config()
    if not cfg["api_key"]:
        return None
    prompt = EMAIL_PROMPT.format(sender=sender[:200], subject=subject[:200], body=(body or "")[:1500])
    try:
        r = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={"model": cfg["model"],
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.1, "max_tokens": 200},
            timeout=timeout,
        )
        r.raise_for_status()
        data = _extract_json(r.json()["choices"][0]["message"]["content"])
        if not data or data.get("kind") not in ("interview", "rejection", "other"):
            return None
        return {"kind": data["kind"], "company": str(data.get("company", ""))[:100],
                "job_title": str(data.get("job_title", ""))[:100],
                "summary": str(data.get("summary", ""))[:200]}
    except Exception:
        return None


ACTION_PROMPT = """判断用户是不是想让助手执行操作。只返回 JSON，不要解释。

可执行的操作：
- add_to_queue: 把职位加入投递队列。需要 "jobs": ["职位名 @ 公司名", ...]
- pause_schedule: 暂停自动投递
- resume_schedule: 恢复自动投递
- mark_applied: 标记已手动投递。需要 "jobs": ["职位名 @ 公司名", ...]

如果不是操作意图，返回 {{"action": "none"}}。
如果是，返回 {{"action": "add_to_queue", "jobs": [...]}} 等。

用户消息：{msg}
"""


def llm_detect_action(msg: str, timeout: int = 20) -> dict | None:
    """检测 chatbot 操作意图。返回 {"action": ..., ...} 或 None。"""
    cfg = get_config()
    if not cfg["api_key"]:
        return None
    try:
        r = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={"model": cfg["model"],
                  "messages": [{"role": "user", "content": ACTION_PROMPT.format(msg=msg[:500])}],
                  "temperature": 0.1, "max_tokens": 200},
            timeout=timeout,
        )
        r.raise_for_status()
        data = _extract_json(r.json()["choices"][0]["message"]["content"])
        if not data or data.get("action") not in (
                "none", "add_to_queue", "pause_schedule", "resume_schedule", "mark_applied"):
            return None
        return data
    except Exception:
        return None


TAILOR_PROMPT = """你是简历优化师。根据目标岗位 JD，优化简历的措辞和排序，让它更匹配。

【铁律 - 违反就整段作废】
1. 绝不编造：不许新增公司、职位、时间段、数字、项目。所有事实必须来自原简历。
2. 绝不删除任何一段经历（可以重排顺序，不可以删）。
3. 日期、公司名、职位名、学校原样保留。

【允许做的】
1. 重写 bullet：用 JD 里的关键词改写措辞，突出相关经验；量化成果保留原数字。
2. 调整 bullet 顺序：最相关的放前面。
3. 重写 summary（一句话），贴合目标岗位。
4. skills 列表重排，最相关的放前面（不许加原简历没有的技能）。

只返回 JSON，不要解释：
{{"summary": "一句话",
 "experiences": [{{"company": "", "title": "", "dates": "", "bullets": ["", ""]}}],
 "skills": [""],
 "education": ""}}

原简历文本：
```
{resume}
```

目标岗位 JD：
```
{jd}
```
"""


JOB_PROFILE_PROMPT = """你是资深职业顾问。根据下面这份简历，为求职者生成一份【目标岗位画像】初稿。

要求：
1. targetTitles：从简历经历推断 2-4 个最匹配的目标职位（英文职位名，如 "AI Engineer"）。
2. industries：匹配的行业 2-4 个。
3. yearsExperience：从经历估算工作年限（数字）。
4. preferredLocations：根据现居地推断求职地点；如果在加拿大，首选其所在城市 + Remote。
5. salary：根据职位和年限给合理的薪资范围。加拿大 AI/软件岗按年薪 CAD 估算（如 90000-130000）；如不确定宁可保守。
6. remotePreference：根据简历判断偏好（remote/hybrid/onsite/any），不确定填 any。
7. workAuth：如简历显示在加拿大，填 "PR/Citizen or valid work permit in Canada" 之类；不确定留空。

只返回 JSON，不要解释：
{{"targetTitles": [""], "industries": [""], "yearsExperience": 0,
 "preferredLocations": [""], "salaryMin": 0, "salaryMax": 0,
 "salaryCurrency": "CAD", "salaryPeriod": "annual",
 "remotePreference": "any", "workAuth": "", "reason": "一句话说明推荐逻辑"}}

简历：
```
{resume}
```
"""


def llm_suggest_job_profile(resume_text: str, timeout: int = 60, tries: int = 3) -> dict | None:
    """根据简历生成目标岗位画像初稿。瞬时失败自动重试（最多 tries 次）；最终失败返回 None。"""
    cfg = get_config()
    if not cfg["api_key"] or not resume_text:
        return None
    prompt = JOB_PROFILE_PROMPT.format(resume=resume_text[:5000])
    for attempt in range(tries):
        try:
            r = httpx.post(
                f"{cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
                json={"model": cfg["model"],
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.3, "max_tokens": 800},
                timeout=timeout,
            )
            r.raise_for_status()
            data = _extract_json(r.json()["choices"][0]["message"]["content"])
            if data and data.get("targetTitles"):
                return data
        except Exception:
            pass
        if attempt < tries - 1:
            time.sleep(2)
    return None


def llm_tailor_resume(resume_text: str, jd: str, timeout: int = 90) -> dict | None:
    """按 JD 改写简历文本。返回改写后的结构化内容；失败返回 None。"""
    cfg = get_config()
    if not cfg["api_key"]:
        return None
    prompt = TAILOR_PROMPT.format(
        resume=(resume_text or "")[:5000],
        jd=(jd or "")[:3000])
    try:
        r = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={"model": cfg["model"],
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.3, "max_tokens": 2000},
            timeout=timeout,
        )
        r.raise_for_status()
        data = _extract_json(r.json()["choices"][0]["message"]["content"])
        if not data or not data.get("experiences"):
            return None
        return data
    except Exception:
        return None


def llm_job_keywords(profile: dict, timeout: int = 60) -> dict | None:
    """画像 → 搜索关键词；失败返回 None（调用方用画像 targetTitles 兜底）。"""
    cfg = get_config()
    if not cfg["api_key"]:
        return None
    try:
        prof = _profile_text(profile)
        if len(prof.strip()) < 20:
            return None
        r = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={
                "model": cfg["model"],
                "messages": [{"role": "user", "content": KEYWORDS_PROMPT.replace("{profile}", prof[:3000])}],
                "temperature": 0.3,
                "max_tokens": 400,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = _extract_json(r.json()["choices"][0]["message"]["content"])
        if not isinstance(data, dict):
            return None
        kws = [str(k) for k in (data.get("keywords") or []) if str(k).strip()][:8]
        if not kws:
            return None
        return {"keywords": kws, "note": str(data.get("note") or "")}
    except Exception:
        return None


def llm_chat(system: str, messages: list, timeout: int = 90) -> str | None:
    """通用对话；失败返回 None。messages: [{role, content}]。"""
    cfg = get_config()
    if not cfg["api_key"]:
        return None
    try:
        r = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={
                "model": cfg["model"],
                "messages": [{"role": "system", "content": system}] + messages[-10:],
                "temperature": 0.4,
                "max_tokens": 1200,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None
