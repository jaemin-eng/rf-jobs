#!/usr/bin/env python3
"""
RF / Antenna / EMC Job Watcher
- 여러 소스에서 공고를 모아 지역·키워드로 거르고
- 이전에 본 공고는 제외한 뒤
- 새 공고만 이메일로 보내고 latest_jobs.md / jobs_history.csv에 기록합니다.

필요한 환경변수 (없는 소스는 자동으로 건너뜀):
  RAPIDAPI_KEY                       -> JSearch
  ADZUNA_APP_ID, ADZUNA_APP_KEY      -> Adzuna
  USAJOBS_API_KEY, USAJOBS_EMAIL     -> USAJOBS
  SMTP_USER, SMTP_PASSWORD, EMAIL_TO -> 이메일 알림 (Gmail은 앱 비밀번호)
  SMTP_HOST (기본 smtp.gmail.com), SMTP_PORT (기본 465)
"""
import csv
import html
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml

import companies as company_sources

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
COMPANIES_PATH = ROOT / "companies.yaml"
COMPANY_STATUS_JSON = ROOT / "docs" / "companies.json"
STATE_PATH = ROOT / "data" / "state.json"
HISTORY_PATH = ROOT / "data" / "jobs_history.csv"
LATEST_PATH = ROOT / "latest_jobs.md"
DASHBOARD_JSON = ROOT / "docs" / "jobs.json"
KEEP_DAYS = 60  # 대시보드에 보관할 기간
TIMEOUT = 30
UA = "rf-job-watcher/1.0"

STATE_NAMES = {
    "CA": "california", "MA": "massachusetts", "NH": "new hampshire",
    "DC": "district of columbia", "VA": "virginia", "MD": "maryland",
}


# ----------------------------------------------------------------------------
def title_key(title):
    """같은 공고의 사이트별 표기 차이를 없앰 (예: 'with Security Clearance', '- Onsite')"""
    t = (title or "").lower()
    t = re.sub(r"\bwith (an? )?(active )?(security )?clearance\b.*$", "", t)
    t = re.sub(r"\(\s*skillbridge[^)]*\)", "", t)
    t = re.sub(r"[-–,|]\s*(onsite|on-site|hybrid|remote|telework)\b.*$", "", t)
    t = re.sub(r"\bsr\.?(?=\s)", "senior", t)
    return t.strip()


@dataclass
class Job:
    source: str
    source_id: str
    title: str
    company: str
    location: str
    url: str
    posted: str = ""
    description: str = ""
    metro: str = ""
    remote: bool = False
    flags: list = field(default_factory=list)

    @property
    def dedupe_key(self) -> str:
        norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
        company = norm(self.company)
        for suffix in ("corporation", "corp", "incorporated", "inc", "llc", "company", "co"):
            if company.endswith(suffix) and len(company) > len(suffix) + 2:
                company = company[: -len(suffix)]
                break
        return f"{norm(title_key(self.title))}|{company[:12]}|{norm(self.metro)}"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_json(url, **kw):
    kw.setdefault("timeout", TIMEOUT)
    headers = kw.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, **kw)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == 2:
                log(f"  ! 요청 실패: {url[:80]} ({e})")
                return None
            time.sleep(2)
    return None


# ----------------------------------------------------------------------------
# 지역 판별
# ----------------------------------------------------------------------------
def match_metro(location: str, metros: dict):
    """위치 문자열이 어느 metro에 속하는지 반환 (없으면 None)."""
    loc = (location or "").lower()
    if not loc:
        return None
    for name, m in metros.items():
        state_hit = False
        for st in m["states"]:
            if re.search(rf"\b{st.lower()}\b", loc) or STATE_NAMES[st] in loc:
                state_hit = True
                break
        if name == "Washington DC" and ("washington, d.c" in loc or "washington dc" in loc):
            return name
        if name == "SF Bay Area" and "bay area" in loc:
            return name
        if not state_hit:
            continue
        for city in m["cities"]:
            c = city.lower()
            if c == "washington" and "dc" not in loc and "district of columbia" not in loc:
                continue
            if re.search(rf"\b{re.escape(c)}\b", loc):
                return name
    return None


def is_remote(text: str) -> bool:
    return bool(re.search(r"\bremote\b", (text or "").lower()))


# ----------------------------------------------------------------------------
# 소스들
# ----------------------------------------------------------------------------
def window(cfg, state):
    """(검색할 일수, 페이지 수). 첫 실행이면 백필."""
    if not state.get("backfilled"):
        return cfg.get("backfill_days", 30), cfg.get("backfill_pages", 3)
    return cfg["max_days_old"], 1


def fetch_jsearch(cfg, state):
    src = cfg["sources"]["jsearch"]
    key = os.getenv("RAPIDAPI_KEY")
    if not src.get("enabled") or not key:
        log("JSearch: 건너뜀 (비활성 또는 RAPIDAPI_KEY 없음)")
        return []
    last = state.get("jsearch_last_run")
    # 아직 결과를 한 번도 못 받았으면(설정 확인 중) 하루 제한 없이 다시 시도
    if last and state.get("jsearch_backfilled") and not os.getenv("FORCE_ALL"):
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last)
        if elapsed < timedelta(days=src.get("run_every_days", 1), hours=-6):
            log("JSearch: 건너뜀 (오늘 이미 실행, 월 호출량 절약)")
            return []

    queries = src["rotating_queries"]
    per_day = src.get("queries_per_day", 2)
    turn = state.get("jsearch_turn", 0)
    todays = [queries[(turn + i) % len(queries)] for i in range(per_day)]
    # 아직 한 번도 결과를 받은 적이 없으면 한 달치, 이후엔 검색어 한 바퀴(일수)만큼
    if not state.get("jsearch_backfilled"):
        date_posted = "month"
    else:
        cycle = -(-len(queries) // per_day)
        date_posted = "3days" if cycle <= 3 else "week"
    log(f"JSearch: 오늘 검색어 {todays}, 기간 {date_posted}")

    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    jobs, calls, problems = [], 0, []
    for metro, m in cfg["metros"].items():
        for q in todays:
            query = f"{q} in {m['jsearch_location']}"
            try:
                r = requests.get("https://jsearch.p.rapidapi.com/search-v2", headers=headers, timeout=TIMEOUT,
                                 params={"query": query, "page": 1, "num_pages": 1,
                                         "date_posted": date_posted, "country": "us"})
                calls += 1
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            except (requests.RequestException, ValueError) as e:
                problems.append(f"{query}: {e}")
                continue
            # search-v2: {"data": {"jobs": [...], "cursor": "..."}} (구버전: {"data": [...]})
            raw = body.get("data") if isinstance(body, dict) else None
            data = (raw.get("jobs") if isinstance(raw, dict) else raw) or []
            if r.status_code != 200 or not data:
                msg = body.get("message") or body.get("error") or body.get("status") or r.text[:120]
                problems.append(f"{query} -> HTTP {r.status_code}, {len(data)}건, {msg}")
            for d in data:
                loc = d.get("job_location") or ", ".join(
                    x for x in [d.get("job_city"), d.get("job_state")] if x)
                jobs.append(Job(
                    source=f"JSearch/{d.get('job_publisher') or '?'}",
                    source_id=str(d.get("job_id")),
                    title=d.get("job_title") or "",
                    company=d.get("employer_name") or "",
                    location=loc or ("Remote" if d.get("job_is_remote") else ""),
                    url=d.get("job_apply_link") or "",
                    posted=(d.get("job_posted_at_datetime_utc") or "")[:10],
                    description=d.get("job_description") or "",
                    remote=bool(d.get("job_is_remote")),
                ))
            time.sleep(1)
    for pr in problems[:6]:
        log(f"  JSearch 참고: {pr}")
    state["jsearch_last_run"] = datetime.now(timezone.utc).isoformat()
    state["jsearch_turn"] = (turn + per_day) % len(queries)
    if jobs:
        state["jsearch_backfilled"] = True
    log(f"JSearch: 호출 {calls}회, {len(jobs)}건")
    return jobs


def _adzuna_location(loc):
    """Adzuna는 display_name에 주 이름이 없어서 area 목록(US, 주, 카운티, 도시)의 주를 덧붙임."""
    area = [a for a in (loc.get("area") or []) if a]
    name = loc.get("display_name", "")
    state = area[1] if len(area) > 1 else ""
    if state and state.lower() not in name.lower():
        return f"{name}, {state}" if name else state
    return name


def fetch_adzuna(cfg, state):
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not cfg["sources"]["adzuna"].get("enabled") or not (app_id and app_key):
        log("Adzuna: 건너뜀 (비활성 또는 키 없음)")
        return []
    days, pages = window(cfg, state)
    jobs, calls = [], 0
    for metro, m in cfg["metros"].items():
        for q in cfg["search_queries"]:
            for page in range(1, pages + 1):
                data = get_json(
                    f"https://api.adzuna.com/v1/api/jobs/us/search/{page}",
                    params={"app_id": app_id, "app_key": app_key, "what_phrase": q,
                            "where": m["adzuna_where"],
                            "distance": int(m["radius_miles"] * 1.609),
                            "max_days_old": days,
                            "results_per_page": 50, "content-type": "application/json"},
                )
                calls += 1
                results = (data or {}).get("results", []) or []
                for d in results:
                    jobs.append(Job(
                        source="Adzuna",
                        source_id=str(d.get("id")),
                        title=re.sub(r"<[^>]+>", "", d.get("title") or ""),
                        company=(d.get("company") or {}).get("display_name", ""),
                        location=_adzuna_location(d.get("location") or {}),
                        url=d.get("redirect_url") or "",
                        posted=(d.get("created") or "")[:10],
                        description=d.get("description") or "",
                    ))
                time.sleep(2.6)  # 무료 한도: 분당 25회
                if len(results) < 50:
                    break
    log(f"Adzuna: 최근 {days}일, 호출 {calls}회, {len(jobs)}건")
    return jobs


def fetch_usajobs(cfg, state):
    src = cfg["sources"]["usajobs"]
    key, email = os.getenv("USAJOBS_API_KEY"), os.getenv("USAJOBS_EMAIL")
    if not src.get("enabled") or not (key and email):
        log("USAJOBS: 건너뜀 (비활성 또는 키 없음)")
        return []
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    jobs = []
    for metro in src.get("metros", []):
        m = cfg["metros"][metro]
        for q in cfg["search_queries"]:
            data = get_json(
                "https://data.usajobs.gov/api/search",
                headers=headers,
                params={"Keyword": q, "LocationName": m["search_location"],
                        "Radius": m["radius_miles"], "DatePosted": window(cfg, state)[0],
                        "ResultsPerPage": 100},
            )
            items = (((data or {}).get("SearchResult") or {}).get("SearchResultItems")) or []
            for it in items:
                d = it.get("MatchedObjectDescriptor", {})
                jobs.append(Job(
                    source="USAJOBS",
                    source_id=str(d.get("PositionID") or it.get("MatchedObjectId")),
                    title=d.get("PositionTitle") or "",
                    company=d.get("OrganizationName") or "",
                    location=d.get("PositionLocationDisplay") or "",
                    url=d.get("PositionURI") or "",
                    posted=(d.get("PublicationStartDate") or "")[:10],
                    description=json.dumps(d.get("UserArea", {}))[:3000],
                    flags=["🇺🇸 연방정부(대개 시민권 필요)"],
                ))
            time.sleep(0.5)
    log(f"USAJOBS: {len(jobs)}건")
    return jobs


def _recent(date_str, days):
    if not date_str:
        return True
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")[:19])
        return datetime.now() - dt.replace(tzinfo=None) <= timedelta(days=days + 1)
    except ValueError:
        return True


# ----------------------------------------------------------------------------
# 필터링
# ----------------------------------------------------------------------------
def filter_jobs(jobs, cfg):
    inc = [re.compile(p, re.I) for p in cfg["title_include_patterns"]]
    exc = [re.compile(p, re.I) for p in cfg.get("title_exclude_patterns", [])]
    clr = [re.compile(p, re.I) for p in cfg.get("clearance_patterns", [])]
    out, stats, unmatched = [], {}, []
    for j in jobs:
        if not any(p.search(j.title) for p in inc):
            stats["제목 불일치"] = stats.get("제목 불일치", 0) + 1
            continue
        if any(p.search(j.title) for p in exc):
            continue
        # 여러 지역이 " / "로 묶인 경우 하나씩 확인
        metro = None
        for part in re.split(r"\s*/\s*|;\s*", j.location) or [j.location]:
            metro = match_metro(part, cfg["metros"])
            if metro:
                break
        j.remote = j.remote or is_remote(j.location)
        if not metro:
            if j.remote and cfg.get("include_remote"):
                metro = "Remote"
            else:
                stats["지역 불일치"] = stats.get("지역 불일치", 0) + 1
                if len(unmatched) < 8:
                    unmatched.append(j.location)
                continue
        j.metro = metro
        if any(p.search(j.description) or p.search(j.title) for p in clr):
            j.flags.append("⚠️ 클리어런스/시민권")
        if cfg.get("hide_clearance_jobs") and j.flags:
            continue
        out.append(j)
    log(f"필터 결과: 통과 {len(out)}, 제외 {stats}")
    if unmatched:
        log(f"  지역 불일치 위치 예시: {unmatched}")
    return out


# ----------------------------------------------------------------------------
# 상태 / 출력
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# 회사 직접 연결 + 회사 그룹 표시
# ----------------------------------------------------------------------------
def load_companies():
    if not COMPANIES_PATH.exists():
        return {"companies": [], "tag_only": {}}
    return yaml.safe_load(COMPANIES_PATH.read_text(encoding="utf-8")) or {}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def build_company_index(creg):
    """회사 이름 → (정식 이름, 그룹). 긴 별칭부터 비교."""
    pairs = []
    for c in creg.get("companies", []):
        base = re.split(r"\s*[(/]", c["name"])[0]
        for a in [c["name"], base] + list(c.get("aliases") or []):
            if _norm(a):
                pairs.append((_norm(a), c["name"], c.get("group", "")))
    for group, names in (creg.get("tag_only") or {}).items():
        for n in names:
            pairs.append((_norm(n), n, group))
    pairs.sort(key=lambda x: -len(x[0]))
    return pairs


def classify_company(name, index):
    n = _norm(name)
    if not n:
        return None, ""
    words = re.findall(r"[a-z0-9&]+", (name or "").lower().replace("&", ""))
    joined = {w for w in words} | {a + b for a, b in zip(words, words[1:])}
    for key, canon, group in index:
        if len(key) <= 6:
            if key in joined:
                return canon, group
        elif key in n:
            return canon, group
    return None, ""


def title_matcher(cfg):
    inc = [re.compile(p, re.I) for p in cfg["title_include_patterns"]]
    exc = [re.compile(p, re.I) for p in cfg.get("title_exclude_patterns", [])]
    return lambda t: any(p.search(t or "") for p in inc) and not any(p.search(t or "") for p in exc)


def fetch_companies(cfg, state):
    from concurrent.futures import ThreadPoolExecutor
    creg = load_companies()
    comps = creg.get("companies", [])
    if not comps:
        return []
    ok_title = title_matcher(cfg)
    cache = state.setdefault("company_sources", {})
    results, statuses = [], []

    def work(comp):
        t0 = time.time()
        found, st = company_sources.run_company(comp, ok_title, cache)
        st["seconds"] = round(time.time() - t0)
        return comp, found, st

    with ThreadPoolExecutor(max_workers=6) as ex:
        for comp, found, st in ex.map(work, comps):
            inside = [f for f in found if any(match_metro(x, cfg["metros"])
                                               for x in re.split(r"\s*/\s*", f.location or ""))]
            st["in_metro"] = len(inside)
            others = [f.location or "(위치 없음)" for f in found if f not in inside]
            st["other_locations"] = sorted(set(others), key=others.index)[:4]
            statuses.append(st)
            for f in found:
                results.append(Job(
                    source=f"회사/{st['via']}", source_id=f"{comp['name']}:{f.id}",
                    title=f.title, company=comp["name"], location=f.location,
                    url=f.url, posted=f.posted, description=f.description))
    okc = [s for s in statuses if s["status"] == "ok"]
    log(f"회사 직접 연결: {len(okc)}/{len(statuses)}곳 성공, RF 관련 공고 {len(results)}건(지역 필터 전)")
    for st in statuses:
        if st["status"] == "ok":
            log(f"  ✓ {st['name']}: {st['via']} RF {st['count']}건, 대상 지역 {st['in_metro']}건 "
                f"({st['seconds']}초) 다른 위치 예: {st['other_locations']}")
    for st in statuses:
        if st["status"] != "ok":
            log(f"  ✗ {st['name']}: {st.get('error', '')}")
    state["company_status"] = statuses
    return results


def fetch_adzuna_companies(cfg, state):
    """모든 등록 회사를 Adzuna에서 회사 이름으로 한 번 더 검색 (직접 연결 실패 대비)"""
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key) or not cfg["sources"]["adzuna"].get("company_sweep", True):
        return []
    days, _ = window(cfg, state)
    jobs, calls = [], 0
    for comp in load_companies().get("companies", []):
        if comp.get("adzuna") is False:
            continue
        cname = re.split(r"\s*[(/]", comp["name"])[0].strip()
        data = get_json("https://api.adzuna.com/v1/api/jobs/us/search/1", params={
            "app_id": app_id, "app_key": app_key, "company": cname,
            "what_or": "RF antenna EMC EMI microwave radar electromagnetic RFIC MMIC",
            "max_days_old": max(days, 14), "results_per_page": 50,
            "content-type": "application/json"})
        calls += 1
        for d in (data or {}).get("results", []) or []:
            jobs.append(Job(
                source="Adzuna", source_id=str(d.get("id")),
                title=re.sub(r"<[^>]+>", "", d.get("title") or ""),
                company=(d.get("company") or {}).get("display_name", ""),
                location=_adzuna_location(d.get("location") or {}),
                url=d.get("redirect_url") or "", posted=(d.get("created") or "")[:10],
                description=d.get("description") or ""))
        time.sleep(2.6)
    log(f"Adzuna 회사명 검색: 호출 {calls}회, {len(jobs)}건")
    return jobs


def write_company_status(state):
    COMPANY_STATUS_JSON.parent.mkdir(exist_ok=True)
    counts = {}
    for rec in state.get("jobs", {}).values():
        if rec.get("company_key"):
            counts[rec["company_key"]] = counts.get(rec["company_key"], 0) + 1
    rows = []
    for st in state.get("company_status", []):
        rows.append(dict(st, listed=counts.get(st["name"], 0)))
    COMPANY_STATUS_JSON.write_text(json.dumps(
        {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "companies": rows},
        ensure_ascii=False, indent=0), encoding="utf-8")


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"seen": {}}


def save_state(state):
    # 90일 지난 기록은 정리
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    keep = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    state["jobs"] = {k: v for k, v in state.get("jobs", {}).items() if v["last_seen"] >= keep}
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False))


def rekey_store(state, index):
    """회사 이름 표기가 바뀌어도 같은 공고가 두 번 나오지 않도록 저장된 공고를 다시 묶음."""
    old = state.get("jobs", {})
    new = {}
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    for rec in old.values():
        # 회사 채용 페이지에서만 보이던 공고가 1주일 넘게 사라졌으면 정리 (마감)
        if rec.get("last_seen", "") < week_ago and all(
                x.startswith(("회사/", "Greenhouse", "Lever")) for x in rec.get("sources", [])):
            continue
        canon, group = classify_company(rec.get("company", ""), index)
        if canon:
            rec["company"], rec["company_key"], rec["group"] = canon, canon, group
        key = Job("", "", rec["title"], rec["company"], "", "", metro=rec.get("metro", "")).dedupe_key
        rec["id"] = key
        if key in new:
            cur = new[key]
            cur["first_seen"] = min(cur["first_seen"], rec["first_seen"])
            cur["last_seen"] = max(cur["last_seen"], rec["last_seen"])
            cur["sources"] = sorted(set(cur.get("sources", [])) | set(rec.get("sources", [])))
            cur["flags"] = sorted(set(cur.get("flags", [])) | set(rec.get("flags", [])))
            if any(x.startswith("회사/") for x in rec.get("sources", [])):
                cur["url"], cur["location"] = rec["url"], rec["location"]
        else:
            new[key] = rec
    state["jobs"] = new


def update_job_store(state, jobs, today, index=None):
    """대시보드용: 조건에 맞는 모든 공고를 first_seen / last_seen과 함께 보관."""
    rekey_store(state, index or [])
    store = state.setdefault("jobs", {})
    for j in jobs:
        canon, group = classify_company(j.company, index or [])
        rec = store.get(j.dedupe_key)
        if rec:
            rec["last_seen"] = today
            rec["sources"] = sorted(set(rec.get("sources", [])) | {j.source})
            if not rec.get("posted") and j.posted:
                rec["posted"] = j.posted
            rec["flags"] = sorted(set(rec.get("flags", [])) | set(j.flags))
            rec["group"], rec["company_key"] = group, canon
            if j.source.startswith("회사/"):
                rec["url"], rec["location"] = j.url, j.location  # 회사 공식 페이지 정보를 우선
        else:
            store[j.dedupe_key] = {
                "id": j.dedupe_key, "title": j.title, "company": j.company,
                "location": j.location, "metro": j.metro, "url": j.url,
                "posted": j.posted, "sources": [j.source], "flags": sorted(set(j.flags)),
                "remote": j.remote, "first_seen": today, "last_seen": today,
                "group": group, "company_key": canon,
            }


def write_dashboard_json(state):
    DASHBOARD_JSON.parent.mkdir(exist_ok=True)
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "jobs": sorted(state.get("jobs", {}).values(),
                       key=lambda r: (r["first_seen"], r["posted"]), reverse=True),
    }
    DASHBOARD_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8")


def append_history(jobs):
    HISTORY_PATH.parent.mkdir(exist_ok=True)
    new_file = not HISTORY_PATH.exists()
    with HISTORY_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["found_date", "metro", "title", "company", "location",
                        "posted", "source", "flags", "url"])
        today = datetime.now().strftime("%Y-%m-%d")
        for j in jobs:
            w.writerow([today, j.metro, j.title, j.company, j.location,
                        j.posted, j.source, "; ".join(j.flags), j.url])


def group(jobs):
    g = {}
    for j in sorted(jobs, key=lambda x: (x.metro, x.posted), reverse=False):
        g.setdefault(j.metro, []).append(j)
    for v in g.values():
        v.sort(key=lambda x: x.posted, reverse=True)
    return g


def write_markdown(jobs):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# 새 RF/Antenna/EMC 공고 — {now}", "", f"총 **{len(jobs)}건**", ""]
    for metro, items in group(jobs).items():
        lines += [f"## {metro} ({len(items)})", "",
                  "| 게시일 | 직무 | 회사 | 위치 | 출처 | 비고 |", "|---|---|---|---|---|---|"]
        for j in items:
            t = j.title.replace("|", "/")
            lines.append(f"| {j.posted} | [{t}]({j.url}) | {j.company} | {j.location} "
                         f"| {j.source} | {' '.join(j.flags)} |")
        lines.append("")
    if not jobs:
        lines.append("이번 실행에서는 새 공고가 없습니다.")
    LATEST_PATH.write_text("\n".join(lines), encoding="utf-8")


def build_email_html(jobs):
    parts = [f"<h2>새 RF / Antenna / EMC 공고 {len(jobs)}건</h2>"]
    for metro, items in group(jobs).items():
        parts.append(f"<h3>{html.escape(metro)} ({len(items)})</h3><table "
                     "style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>")
        for j in items:
            flag = f" <span style='color:#b45309'>{html.escape(' '.join(j.flags))}</span>" if j.flags else ""
            parts.append(
                "<tr><td style='padding:6px 10px;border-bottom:1px solid #ddd'>"
                f"<a href='{html.escape(j.url)}'><b>{html.escape(j.title)}</b></a>{flag}<br>"
                f"{html.escape(j.company)} · {html.escape(j.location)} · "
                f"<span style='color:#666'>{html.escape(j.posted)} · {html.escape(j.source)}</span>"
                "</td></tr>")
        parts.append("</table>")
    return "\n".join(parts)


def send_email(jobs):
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"), os.getenv("EMAIL_TO")
    if not (user and pw and to):
        log("이메일: 건너뜀 (SMTP 설정 없음)")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Job Watcher] 새 RF/Antenna/EMC 공고 {len(jobs)}건"
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText(build_email_html(jobs), "html", "utf-8"))
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    log(f"이메일 전송 완료 → {to}")


# ----------------------------------------------------------------------------
def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    state = load_state()
    first_run = not state["seen"]

    raw = []
    index = build_company_index(load_companies())
    for fn in (fetch_jsearch, fetch_adzuna, fetch_usajobs, fetch_adzuna_companies, fetch_companies):
        try:
            raw += fn(cfg, state)
        except Exception as e:  # 한 소스가 실패해도 나머지는 계속
            log(f"! {fn.__name__} 오류: {e}")

    filtered = filter_jobs(raw, cfg)

    new, batch_keys = [], set()
    today = datetime.now().strftime("%Y-%m-%d")
    for j in filtered:
        keys = {j.dedupe_key, f"{j.source.split('/')[0]}:{j.source_id}"}
        if keys & set(state["seen"]) or j.dedupe_key in batch_keys:
            continue
        batch_keys.add(j.dedupe_key)
        new.append(j)
        for k in keys:
            state["seen"][k] = today

    # 같은 회사 이름을 정식 이름으로 맞춤 (중복 제거가 잘 되도록)
    for j in filtered:
        canon, _ = classify_company(j.company, index)
        if canon and not j.source.startswith("회사/"):
            j.company = canon
    update_job_store(state, filtered, today, index)
    log(f"수집 {len(raw)} → 조건 일치 {len(filtered)} → 새 공고 {len(new)}")

    write_markdown(new)
    if new:
        append_history(new)
        if first_run and os.getenv("SKIP_FIRST_EMAIL"):
            log("첫 실행: 이메일 생략 (SKIP_FIRST_EMAIL)")
        else:
            try:
                send_email(new)
            except Exception as e:
                log(f"! 이메일 오류: {e}")
    if not state.get("backfilled"):
        state["backfilled"] = today
        log("첫 실행(백필) 완료: 다음부터는 새 공고만 확인합니다")
    save_state(state)
    write_dashboard_json(state)
    write_company_status(state)

    if os.getenv("DRY_PRINT"):
        for j in new:
            print(json.dumps(asdict(j) | {"description": j.description[:80]}, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
