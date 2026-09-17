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

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
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
        return f"{norm(self.title)}|{norm(self.company)}|{norm(self.metro)}"


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
    if last and not os.getenv("FORCE_ALL"):
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last)
        if elapsed < timedelta(days=src.get("run_every_days", 1), hours=-2):
            log(f"JSearch: 건너뜀 (마지막 실행 {elapsed.days}일 전, 호출량 절약)")
            return []
    days, pages = window(cfg, state)
    date_posted = "today" if days <= 1 else "3days" if days <= 3 else "week" if days <= 7 else "month"
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    jobs = []
    for metro, m in cfg["metros"].items():
        for q in src["combined_queries"]:
            data = get_json(
                "https://jsearch.p.rapidapi.com/search",
                headers=headers,
                params={"query": f"{q} in {m['search_location']}", "page": 1,
                        "num_pages": min(pages, 3), "date_posted": date_posted, "country": "us"},
            )
            for d in (data or {}).get("data", []) or []:
                loc = ", ".join(x for x in [d.get("job_city"), d.get("job_state")] if x)
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
    state["jsearch_last_run"] = datetime.now(timezone.utc).isoformat()
    log(f"JSearch: {len(jobs)}건")
    return jobs


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
    log(f"Adzuna: 최근 {days}일, 호출 {calls}회")
    log(f"Adzuna: {len(jobs)}건")
    return jobs


def _adzuna_location(loc):
    """Adzuna는 display_name에 주 이름이 없어서 area 목록(US, 주, 카운티, 도시)의 주를 덧붙임."""
    area = [a for a in (loc.get("area") or []) if a]
    name = loc.get("display_name", "")
    state = area[1] if len(area) > 1 else ""
    if state and state.lower() not in name.lower():
        return f"{name}, {state}" if name else state
    return name


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


def fetch_greenhouse(cfg, state):
    src = cfg["sources"]["greenhouse"]
    if not src.get("enabled"):
        return []
    jobs = []
    for slug in src.get("companies", []):
        data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if data is None:
            log(f"  Greenhouse '{slug}': 조회 실패 — slug가 맞는지 확인하세요")
            continue
        for d in data.get("jobs", []):
            jobs.append(Job(
                source=f"Greenhouse/{slug}",
                source_id=str(d.get("id")),
                title=d.get("title") or "",
                company=slug,
                location=(d.get("location") or {}).get("name", ""),
                url=d.get("absolute_url") or "",
                posted=(d.get("updated_at") or "")[:10],
            ))
    log(f"Greenhouse: {len(jobs)}건 (필터 전)")
    return jobs


def fetch_lever(cfg, state):
    src = cfg["sources"]["lever"]
    if not src.get("enabled"):
        return []
    jobs = []
    for slug in src.get("companies", []):
        data = get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
        if data is None or not isinstance(data, list):
            log(f"  Lever '{slug}': 조회 실패 — slug가 맞는지 확인하세요")
            continue
        for d in data:
            cats = d.get("categories") or {}
            locs = cats.get("allLocations") or [cats.get("location", "")]
            created = d.get("createdAt")
            posted = datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d") if created else ""
            jobs.append(Job(
                source=f"Lever/{slug}",
                source_id=str(d.get("id")),
                title=d.get("text") or "",
                company=slug,
                location=" / ".join(x for x in locs if x),
                url=d.get("hostedUrl") or "",
                posted=posted,
                description=d.get("descriptionPlain") or "",
            ))
    log(f"Lever: {len(jobs)}건 (필터 전)")
    return jobs


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


def update_job_store(state, jobs, today):
    """대시보드용: 조건에 맞는 모든 공고를 first_seen / last_seen과 함께 보관."""
    store = state.setdefault("jobs", {})
    for j in jobs:
        rec = store.get(j.dedupe_key)
        if rec:
            rec["last_seen"] = today
            rec["sources"] = sorted(set(rec.get("sources", [])) | {j.source})
            if not rec.get("posted") and j.posted:
                rec["posted"] = j.posted
            rec["flags"] = sorted(set(rec.get("flags", [])) | set(j.flags))
        else:
            store[j.dedupe_key] = {
                "id": j.dedupe_key, "title": j.title, "company": j.company,
                "location": j.location, "metro": j.metro, "url": j.url,
                "posted": j.posted, "sources": [j.source], "flags": sorted(set(j.flags)),
                "remote": j.remote, "first_seen": today, "last_seen": today,
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
    for fn in (fetch_jsearch, fetch_adzuna, fetch_usajobs, fetch_greenhouse, fetch_lever):
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

    update_job_store(state, filtered, today)
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

    if os.getenv("DRY_PRINT"):
        for j in new:
            print(json.dumps(asdict(j) | {"description": j.description[:80]}, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
