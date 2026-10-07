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
        f"期望薪资：{(p.get('salaryMin') or '?')}-{(p.get('salaryMax') or '?')} {p.get('salaryCurrency') or ''}".strip(),
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
