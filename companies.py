"""
회사 채용 페이지 직접 수집기.

지원하는 채용 시스템: Workday, Greenhouse, Lever, Ashby, SmartRecruiters, iCIMS,
Phenom, Radancy(TalentBrew), SAP SuccessFactors(Career Site Builder), Eightfold, Apple.

각 회사마다
  1) companies.yaml 의 sources (확인된 주소)를 먼저 시도하고
  2) 실패하면 careers 페이지를 열어 어떤 시스템인지 자동으로 찾아서 시도합니다.
찾은 결과는 state에 저장해 다음 실행부터 재사용합니다.
"""
import html as htmllib
import json
import re
import time
from urllib.parse import urlparse, urljoin

import requests

TIMEOUT = 30
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}

# 회사 사이트 검색에 쓰는 키워드 (제목 필터는 job_watcher가 따로 적용)
SEARCH_TERMS = ["RF", "antenna", "EMC", "EMI", "microwave", "radar",
                "electromagnetic", "electronic warfare", "phased array", "RFIC"]


def _clean_loc(loc):
    parts, seen = [], set()
    for p in re.split(r"\s*/\s*", loc or ""):
        p = re.sub(r"\s*~.*$", "", p.strip())                       # Workday 사업장 주소 제거
        p = re.sub(r"^(US|USA)-([A-Z]{2})-([A-Za-z][A-Za-z .']+?)(-[A-Z0-9]{2,4})?$",
                   lambda m: f"{m.group(3).title()}, {m.group(2)}", p)       # US-MA-TEWKSBURY-TB1
        p = re.sub(r"^USA\s+([A-Z]{2})\s+(.+)$", lambda m: f"{m.group(2)}, {m.group(1)}", p)
        p = re.sub(r"^United States-([A-Za-z ]+)-(.+)$", lambda m: f"{m.group(2)}, {m.group(1)}", p)
        p = re.sub(r",\s*(United States( of America)?|USA|US)$", "", p)
        if p and p.lower() not in seen:
            seen.add(p.lower())
            parts.append(p)
    return " / ".join(parts)


class Found:
    """수집한 공고 한 건 (job_watcher.Job 으로 변환됨)"""
    __slots__ = ("id", "title", "location", "url", "posted", "description")

    def __init__(self, id, title, location, url, posted="", description=""):
        self.id, self.title, self.location = str(id), title or "", _clean_loc(location)
        self.url, self.posted, self.description = url or "", (posted or "")[:10], description or ""


def _txt(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()


def _get(url, **kw):
    kw.setdefault("timeout", TIMEOUT)
    h = dict(HEADERS)
    h.update(kw.pop("headers", {}) or {})
    return requests.get(url, headers=h, **kw)


def _post(url, **kw):
    kw.setdefault("timeout", TIMEOUT)
    h = dict(HEADERS)
    h.update(kw.pop("headers", {}) or {})
    return requests.post(url, headers=h, **kw)


def _dedupe(items):
    seen, out = set(), []
    for f in items:
        if f.id in seen:
            continue
        seen.add(f.id)
        out.append(f)
    return out


# --------------------------------------------------------------------------
# 채용 시스템별 수집기. 모두 (list[Found]) 를 반환하고, 연결 자체가 안 되면 예외를 던짐.
# --------------------------------------------------------------------------
def fetch_workday(url, title_ok):
    u = urlparse(url)
    tenant = u.hostname.split(".")[0]
    parts = [p for p in u.path.split("/") if p]
    parts = [p for p in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    site = parts[0]
    base = f"{u.scheme}://{u.hostname}"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    hdr = {"Content-Type": "application/json", "Accept": "application/json"}
    found, ok, raw = [], False, 0
    for term in SEARCH_TERMS:
        offset = 0
        while offset < 400:
            r = _post(api, headers=hdr, json={"appliedFacets": {}, "limit": 20,
                                               "offset": offset, "searchText": term})
            if r.status_code != 200:
                raise RuntimeError(f"Workday HTTP {r.status_code}")
            ok = True
            data = r.json()
            posts = data.get("jobPostings") or []
            raw += len(posts)
            for p in posts:
                title = p.get("title", "")
                if not title_ok(title):
                    continue
                path = p.get("externalPath", "")
                loc = p.get("locationsText", "")
                if re.search(r"\d+\s+Locations?", loc or ""):
                    loc = _workday_locations(base, tenant, site, path, hdr) or loc
                found.append(Found(path or title, title, loc, f"{base}/en-US/{site}{path}",
                                   _workday_date(p.get("postedOn", ""))))
            total = data.get("total") or 0
            offset += 20
            if not posts or (total and offset >= total):
                break
            time.sleep(0.3)
    if not ok:
        raise RuntimeError("Workday 응답 없음")
    if raw == 0:
        raise RuntimeError("Workday 결과 0건 (사이트 이름 확인 필요)")
    return _dedupe(found)


def _workday_locations(base, tenant, site, path, hdr):
    try:
        r = _get(f"{base}/wday/cxs/{tenant}/{site}{path}", headers=hdr)
        info = r.json().get("jobPostingInfo", {})
        locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
        return " / ".join(x for x in locs if x)
    except Exception:
        return ""


def _workday_date(text):
    from datetime import datetime, timedelta
    t = (text or "").lower()
    now = datetime.now()
    if "today" in t:
        return now.strftime("%Y-%m-%d")
    if "yesterday" in t:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\+?\s+days?", t)
    if m:
        return (now - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    return ""


def fetch_greenhouse(slug, title_ok):
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if r.status_code != 200:
        raise RuntimeError(f"Greenhouse HTTP {r.status_code}")
    if not r.json().get("jobs"):
        raise RuntimeError("Greenhouse 공고 0건")
    return [Found(d.get("id"), d.get("title"), (d.get("location") or {}).get("name", ""),
                  d.get("absolute_url"), d.get("updated_at", ""))
            for d in r.json().get("jobs", []) if title_ok(d.get("title", ""))]


def fetch_lever(slug, title_ok):
    r = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if r.status_code != 200 or not isinstance(r.json(), list):
        raise RuntimeError(f"Lever HTTP {r.status_code}")
    if not r.json():
        raise RuntimeError("Lever 공고 0건")
    out = []
    from datetime import datetime
    for d in r.json():
        if not title_ok(d.get("text", "")):
            continue
        cats = d.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        ts = d.get("createdAt")
        out.append(Found(d.get("id"), d.get("text"), " / ".join(x for x in locs if x),
                         d.get("hostedUrl"),
                         datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else ""))
    return out


def fetch_ashby(slug, title_ok):
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if r.status_code != 200:
        raise RuntimeError(f"Ashby HTTP {r.status_code}")
    if not r.json().get("jobs"):
        raise RuntimeError("Ashby 공고 0건")
    out = []
    for d in r.json().get("jobs", []):
        if not title_ok(d.get("title", "")):
            continue
        locs = [d.get("location", "")] + [x.get("location", "") for x in d.get("secondaryLocations") or []]
        out.append(Found(d.get("id") or d.get("jobUrl"), d.get("title"),
                         " / ".join(x for x in locs if x), d.get("jobUrl"), d.get("publishedAt", "")))
    return out


def fetch_smartrecruiters(company, title_ok):
    out, offset = [], 0
    while offset < 2000:
        r = _get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                 params={"limit": 100, "offset": offset})
        if r.status_code != 200:
            raise RuntimeError(f"SmartRecruiters HTTP {r.status_code}")
        data = r.json()
        items = data.get("content") or []
        for d in items:
            if not title_ok(d.get("name", "")):
                continue
            loc = d.get("location") or {}
            where = ", ".join(x for x in [loc.get("city"), loc.get("region")] if x)
            out.append(Found(d.get("id"), d.get("name"), where,
                             f"https://jobs.smartrecruiters.com/{company}/{d.get('id')}",
                             d.get("releasedDate", "")))
        offset += 100
        if offset >= (data.get("totalFound") or 0) or not items:
            break
    return out


def fetch_icims(base, title_ok):
    base = base.rstrip("/")
    out, ok, raw = [], False, 0
    for term in SEARCH_TERMS:
        for page in range(0, 10):
            r = _get(f"{base}/jobs/search", params={"ss": 1, "searchKeyword": term,
                                                     "in_iframe": 1, "pr": page})
            if r.status_code != 200:
                raise RuntimeError(f"iCIMS HTTP {r.status_code}")
            ok = True
            links = re.findall(r'href="((?:https?://[^"]+)?/jobs/(\d+)/[^"]+/job[^"]*)"[^>]*>(.*?)</a>', r.text, re.S)
            links = [(h, i, t) for h, i, t in links]
            if not links:
                break
            raw += len(links)
            for href, jid, inner in links:
                title = _txt(inner)
                if not title or not title_ok(title):
                    continue
                pos = r.text.find(href)
                chunk = r.text[pos:pos + 2500]
                if not href.startswith("http"):
                    href = base + href
                m = re.search(r'Location[s]?\s*</span>\s*(?:<span[^>]*>)?\s*([^<]+)', chunk)
                loc = _txt(m.group(1)) if m else ""
                if not loc or loc.lower().startswith("job"):
                    m = re.search(r'\b((?:US|USA)-[A-Z]{2}-[A-Za-z .\'-]+)', chunk)
                    loc = m.group(1) if m else ""
                if not loc:
                    m = re.search(r'\b([A-Z][A-Za-z .\'-]+,\s*(?:[A-Z]{2}|Virginia|Maryland|Massachusetts|California|District of Columbia)\b)', _txt(chunk))
                    loc = m.group(1) if m else ""
                if loc.startswith(title):
                    loc = loc[len(title):].strip()
                out.append(Found(jid, title, loc, href.split("?")[0]))
            if len(links) < 10:
                break
            time.sleep(0.3)
    if not ok:
        raise RuntimeError("iCIMS 응답 없음")
    if raw == 0:
        raise RuntimeError("iCIMS 결과 0건 (형식 확인 필요)")
    return _dedupe(out)


def _find_json_after(text, key):
    i = text.find(key)
    if i < 0:
        return None
    j = text.find("{", i + len(key))
    if j < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[j:])
        return obj
    except ValueError:
        return None


def fetch_phenom(base, title_ok):
    base = base.rstrip("/")
    out, ok, raw = [], False, 0
    for term in SEARCH_TERMS:
        for start in range(0, 200, 10):
            r = _get(f"{base}/search-results", params={"keywords": term, "from": start, "s": 1})
            if r.status_code != 200:
                raise RuntimeError(f"Phenom HTTP {r.status_code}")
            obj = _find_json_after(r.text, '"eagerLoadRefineSearch"')
            if obj is None:
                raise RuntimeError("Phenom 형식 아님")
            ok = True
            jobs = ((obj.get("data") or {}).get("jobs")) or []
            raw += len(jobs)
            for d in jobs:
                title = d.get("title", "")
                if not title_ok(title):
                    continue
                loc = d.get("location") or ", ".join(
                    x for x in [d.get("city"), d.get("state")] if x)
                multi = d.get("multi_location") or []
                if multi:
                    loc = " / ".join([loc] + [m for m in multi if isinstance(m, str)])
                jid = d.get("jobId") or d.get("reqId") or title
                url = d.get("applyUrl") or f"{base}/job/{jid}"
                out.append(Found(jid, title, loc, url, d.get("postedDate", "")))
            total = obj.get("totalHits") or 0
            if not jobs or start + 10 >= total:
                break
            time.sleep(0.3)
    if not ok:
        raise RuntimeError("Phenom 응답 없음")
    if raw == 0:
        raise RuntimeError("Phenom 결과 0건")
    return _dedupe(out)


def fetch_radancy(base, title_ok):
    base = base.rstrip("/")
    out, ok, raw = [], False, 0
    hdr = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
    for term in SEARCH_TERMS:
        for page in range(1, 11):
            r = _get(f"{base}/search-jobs/results", headers=hdr, params={
                "ActiveFacetID": 0, "CurrentPage": page, "RecordsPerPage": 100,
                "Distance": 50, "RadiusUnitType": 0, "Keywords": term, "Location": "",
                "ShowRadius": "False", "IsPagination": "True" if page > 1 else "False",
                "SearchResultsModuleName": "Search Results",
                "SearchFiltersModuleName": "Search Filters",
                "SortCriteria": 0, "SortDirection": 0, "SearchType": 5, "ResultsType": 0})
            if r.status_code != 200:
                raise RuntimeError(f"Radancy HTTP {r.status_code}")
            try:
                body = r.json().get("results", "")
            except ValueError:
                raise RuntimeError("Radancy 형식 아님")
            ok = True
            items = re.findall(r'<a[^>]+href="((?:/[a-z]{2}(?:-[a-z]{2})?)?/job/[^"]+)"[^>]*data-job-id="(\d+)"[^>]*>(.*?)</a>', body, re.S)
            if not items:
                items = [(h, i, inner) for i, h, inner in re.findall(
                    r'<a[^>]+data-job-id="(\d+)"[^>]*href="((?:/[a-z]{2}(?:-[a-z]{2})?)?/job/[^"]+)"[^>]*>(.*?)</a>', body, re.S)]
            raw += len(items)
            for href, jid, inner in items:
                m = re.search(r"<h\d[^>]*>(.*?)</h\d>", inner, re.S)
                title = _txt(m.group(1) if m else inner)
                if not title_ok(title):
                    continue
                m = re.search(r'class="[^"]*job-location[^"]*"[^>]*>(.*?)</span>', inner, re.S)
                loc = _txt(m.group(1)) if m else ""
                m = re.search(r'class="[^"]*job-date-posted[^"]*"[^>]*>(.*?)</span>', inner, re.S)
                out.append(Found(jid, title, loc, base + href, _us_date(_txt(m.group(1)) if m else "")))
            if len(items) < 100:
                break
            time.sleep(0.3)
    if not ok:
        raise RuntimeError("Radancy 응답 없음")
    if raw == 0:
        raise RuntimeError("Radancy 결과 0건 (형식 확인 필요)")
    return _dedupe(out)


def _us_date(s):
    from datetime import datetime
    for fmt in ("%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def fetch_successfactors(base, title_ok):
    base = base.rstrip("/")
    out, ok, raw = [], False, 0
    for term in SEARCH_TERMS:
        for start in range(0, 500, 25):
            r = _get(f"{base}/search/", params={"q": term, "startrow": start})
            if r.status_code != 200:
                raise RuntimeError(f"SuccessFactors HTTP {r.status_code}")
            links = re.findall(r'<a[^>]+href="(/job/[^"]+)"[^>]*class="[^"]*jobTitle-link[^"]*"[^>]*>(.*?)</a>', r.text, re.S)
            links += re.findall(r'<a[^>]+class="[^"]*jobTitle-link[^"]*"[^>]*href="(/job/[^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
            if "jobTitle-link" not in r.text and start == 0 and not ok:
                if "searchResults" not in r.text and "search-results" not in r.text:
                    raise RuntimeError("SuccessFactors 형식 아님")
            ok = True
            seen_here = set()
            raw += len(links)
            for href, inner in links:
                if href in seen_here:
                    continue
                seen_here.add(href)
                title = _txt(inner)
                if not title_ok(title):
                    continue
                pos = r.text.find(href)
                chunk = r.text[pos:pos + 3000]
                m = re.search(r'class="[^"]*jobLocation[^"]*"[^>]*>(.*?)</span>', chunk, re.S)
                loc = _txt(m.group(1)) if m else ""
                if not loc:  # URL에 도시-주가 들어 있음: /job/Lexington-...-MA-02421/123/
                    seg = href.strip("/").split("/")
                    loc = seg[1].replace("-", " ") if len(seg) > 2 else ""
                m = re.search(r'class="[^"]*jobDate[^"]*"[^>]*>(.*?)</span>', chunk, re.S)
                jid = re.findall(r"/(\d+)/?$", href)
                out.append(Found(jid[0] if jid else href, title, loc, base + href,
                                 _us_date(_txt(m.group(1))) if m else ""))
            if len(seen_here) < 25:
                break
            time.sleep(0.3)
    if not ok:
        raise RuntimeError("SuccessFactors 응답 없음")
    if raw == 0:
        raise RuntimeError("SuccessFactors 결과 0건")
    return _dedupe(out)


def _ef_positions(obj):
    """Eightfold 응답에서 공고 목록 찾기 (v2: positions, PCSX: data.positions)"""
    if not isinstance(obj, dict):
        return [], 0
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    items = data.get("positions") or data.get("jobs") or []
    return items, data.get("count") or data.get("total") or 0


def fetch_eightfold(conf, title_ok):
    from datetime import datetime
    host, domain = conf["host"], conf["domain"]
    hdr = {"Accept": "application/json", "Referer": f"https://{host}/careers"}
    endpoints = [("pcsx", f"https://{host}/api/pcsx/search", 10),
                 ("v2", f"https://{host}/api/apply/v2/jobs", 100)]
    errors = []
    for kind, api, size in endpoints:
        out, raw = [], 0
        try:
            for term in SEARCH_TERMS:
                for start in range(0, 600, size):
                    params = {"domain": domain, "query": term, "start": start, "sort_by": "relevance"}
                    if kind == "v2":
                        params["num"] = size
                    else:
                        params["location"] = ""
                    r = _get(api, headers=hdr, params=params)
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    items, count = _ef_positions(r.json())
                    raw += len(items)
                    for d in items:
                        title = d.get("name") or d.get("title") or ""
                        if not title_ok(title):
                            continue
                        locs = d.get("locations") or d.get("standardizedLocations") or [d.get("location", "")]
                        ts = d.get("postedTs") or d.get("t_create") or d.get("t_update")
                        url = d.get("canonicalPositionUrl") or d.get("positionUrl") or f"/careers/job/{d.get('id')}"
                        if url.startswith("/"):
                            url = f"https://{host}{url}"
                        out.append(Found(d.get("id") or d.get("displayJobId"), title,
                                         " / ".join(x for x in locs if isinstance(x, str) and x), url,
                                         datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if isinstance(ts, (int, float)) else ""))
                    if len(items) < size or (count and start + size >= count):
                        break
                    time.sleep(0.3)
            if raw == 0:
                raise RuntimeError("결과 0건")
            return _dedupe(out)
        except Exception as e:
            errors.append(f"{kind} {str(e)[:60]}")
    raise RuntimeError("Eightfold " + ", ".join(errors))


def fetch_apple(_, title_ok):
    out, ok, raw = [], False, 0
    for term in SEARCH_TERMS:
        for page in range(1, 11):
            r = _get("https://jobs.apple.com/en-us/search",
                     params={"search": term, "sort": "relevance", "location": "united-states-USA",
                             "page": page})
            if r.status_code != 200:
                raise RuntimeError(f"Apple HTTP {r.status_code}")
            text = r.text
            m = re.search(r'__staticRouterHydrationData\s*=\s*JSON\.parse\((".*?")\);', text, re.S)
            results = []
            if m:
                try:
                    data = json.loads(json.loads(m.group(1)))
                    results = _find_key(data, "searchResults") or []
                except ValueError:
                    results = []
            if not results and '"postingTitle"' not in text and not ok:
                raise RuntimeError("Apple 형식을 읽지 못함")
            ok = True
            if not results:  # 예비: HTML 안의 JSON 조각을 직접 찾기
                for chunk in re.findall(r'\{[^{}]*"postingTitle"[^{}]*\}', text.replace('\\"', '"')):
                    try:
                        results.append(json.loads(chunk))
                    except ValueError:
                        pass
            raw += len(results)
            for d in results:
                title = d.get("postingTitle", "")
                if not title_ok(title):
                    continue
                locs = d.get("locations") or []
                loc = " / ".join(", ".join(x for x in [l.get("city") or l.get("name"), l.get("stateProvince")] if x)
                                 for l in locs if isinstance(l, dict))
                pid = d.get("positionId") or d.get("id") or d.get("reqId")
                slug = d.get("transformedPostingTitle") or ""
                out.append(Found(pid, title, loc,
                                 f"https://jobs.apple.com/en-us/details/{pid}/{slug}",
                                 (d.get("postDateInGMT") or d.get("postingDate") or "")[:10]))
            if len(results) < 20:
                break
            time.sleep(0.5)
    if raw == 0:
        raise RuntimeError("Apple 결과 0건 (형식 확인 필요)")
    return _dedupe(out)


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], list):
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r:
                return r
    return None


def fetch_jobsyn(origin, title_ok):
    """DirectEmployers(.jobs) 사이트. 예: sandia.jobs"""
    origin = origin.replace("https://", "").strip("/")
    hdr = {"x-origin": origin, "Accept": "application/json", "Origin": f"https://{origin}",
           "Referer": f"https://{origin}/"}
    out, raw = [], 0
    for term in SEARCH_TERMS:
        for page in range(1, 11):
            r = _get("https://prod-search-api.jobsyn.org/api/v1/solr/search",
                     headers=hdr, params={"q": term, "page": page})
            if r.status_code != 200:
                raise RuntimeError(f"DirectEmployers HTTP {r.status_code}")
            data = r.json()
            jobs = data.get("jobs") or (data.get("data") or {}).get("jobs") or []
            raw += len(jobs)
            for d in jobs:
                title = d.get("title_exact") or d.get("title") or ""
                if not title_ok(title):
                    continue
                city = d.get("city_exact") or d.get("city") or ""
                state = d.get("state_short") or d.get("state_exact") or d.get("state") or ""
                loc = d.get("location_exact") or ", ".join(x for x in [city, state] if x)
                guid = d.get("guid") or d.get("id") or d.get("reqid")
                slug = lambda x: re.sub(r"[^a-z0-9]+", "-", (x or "").lower()).strip("-")
                url = d.get("url") or f"https://{origin}/{slug(city)}-{slug(state)}/{slug(title)}/{guid}/job/"
                out.append(Found(guid, title, loc, url, (d.get("date_new") or d.get("date_added") or "")[:10]))
            pages = ((data.get("pagination") or {}).get("total_pages")) or 0
            if not jobs or page >= pages:
                break
            time.sleep(0.3)
    if raw == 0:
        raise RuntimeError("DirectEmployers 결과 0건")
    return _dedupe(out)


FETCHERS = {
    "workday": fetch_workday, "greenhouse": fetch_greenhouse, "lever": fetch_lever,
    "ashby": fetch_ashby, "smartrecruiters": fetch_smartrecruiters, "icims": fetch_icims,
    "phenom": fetch_phenom, "radancy": fetch_radancy, "successfactors": fetch_successfactors,
    "eightfold": fetch_eightfold, "apple": fetch_apple, "jobsyn": fetch_jobsyn,
}


# --------------------------------------------------------------------------
# 자동 탐지: 채용 페이지 HTML에서 어떤 시스템을 쓰는지 찾기
# --------------------------------------------------------------------------
def discover(url):
    """careers 페이지에서 찾은 후보 source 목록 [{type: value}, ...]"""
    try:
        r = _get(url, allow_redirects=True)
    except requests.RequestException:
        return []
    text, final = r.text, r.url
    host = urlparse(final).hostname or ""
    cands = []

    def add(kind, val):
        item = {kind: val}
        if item not in cands:
            cands.append(item)

    if host.endswith("myworkdayjobs.com"):
        add("workday", final)
    for t, wd, site in re.findall(
            r'https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)', text):
        if site.lower() not in ("wday", "en-us", "job"):
            add("workday", f"https://{t}.{wd}.myworkdayjobs.com/{site}")
    for slug in re.findall(r'(?:boards|job-boards)(?:-api)?\.greenhouse\.io/(?:v1/boards/|embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)', text):
        if slug not in ("embed", "v1"):
            add("greenhouse", slug)
    for slug in re.findall(r'jobs\.lever\.co/([A-Za-z0-9_.-]+)', text):
        add("lever", slug)
    for slug in re.findall(r'jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)', text):
        add("ashby", slug)
    for cid in re.findall(r'(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)', text):
        add("smartrecruiters", cid)
    icims = [h for h in re.findall(r'https?://([a-z0-9-]+\.icims\.com)', text) if not h.startswith("www.")]
    if host.endswith("icims.com"):
        icims.insert(0, host)
    for h in icims:
        add("icims", f"https://{h}")
    m = re.search(r'([a-z0-9-]+\.eightfold\.ai)', host if host.endswith("eightfold.ai") else text)
    d = re.search(r'["\']?domain["\']?\s*[:=]\s*["\']([a-z0-9.-]+\.[a-z]{2,})["\']', text)
    if (m or "/api/apply/v2" in text) and d:
        add("eightfold", {"host": host if not m else m.group(1), "domain": d.group(1)})
    if "phenompeople" in text or "phApp" in text or "eagerLoadRefineSearch" in text:
        path = urlparse(final).path
        pm = re.match(r"(/[a-z]{2,6}/[a-z]{2})(?:/|$)", path)
        add("phenom", f"https://{host}{pm.group(1) if pm else '/us/en'}")
    if "talentbrew" in text or "tbcdn." in text or "/search-jobs" in text:
        add("radancy", f"https://{host}")
    if "rmkcdn.successfactors.com" in text or "jobTitle-link" in text or "career5.successfactors" in text:
        add("successfactors", f"https://{host}")
    # 링크된 채용 하위 페이지도 한 단계만 따라가 봄
    if not cands:
        for href in re.findall(r'href="([^"]+)"', text):
            if re.search(r'(search-jobs|job-search|/jobs/?$|careers/search|openings|opportunities)', href, re.I):
                sub = urljoin(final, href)
                if sub != url and urlparse(sub).hostname:
                    return discover_once(sub)
    def rank(c):
        k, v = next(iter(c.items()))
        if k == "workday":
            site = v.rsplit("/", 1)[-1].lower()
            return 0 if re.search(r"career|external|search|jobs", site) else 1
        return 0
    cands.sort(key=rank)
    return cands


def discover_once(url):
    try:
        r = _get(url, allow_redirects=True)
    except requests.RequestException:
        return []
    text = r.text
    cands = []
    for t, wd, site in re.findall(
            r'https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)', text):
        cands.append({"workday": f"https://{t}.{wd}.myworkdayjobs.com/{site}"})
    for slug in re.findall(r'(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)', text):
        cands.append({"greenhouse": slug})
    for slug in re.findall(r'jobs\.lever\.co/([A-Za-z0-9_.-]+)', text):
        cands.append({"lever": slug})
    for h in re.findall(r'https?://([a-z0-9-]+\.icims\.com)', text):
        cands.append({"icims": f"https://{h}"})
    return cands


# --------------------------------------------------------------------------
def run_company(comp, title_ok, cache):
    """회사 하나 수집. (결과 목록, 상태 dict) 반환"""
    name = comp["name"]
    tried, errors = [], []
    candidates = list(comp.get("sources") or [])
    if cache.get(name) and cache[name] not in candidates:
        candidates.append(cache[name])
    discovered = False
    idx = 0
    while True:
        if idx >= len(candidates):
            if discovered or not comp.get("careers"):
                break
            discovered = True
            for url in comp["careers"]:
                for c in discover(url):
                    if c not in candidates:
                        candidates.append(c)
            if idx >= len(candidates):
                break
        src = candidates[idx]
        idx += 1
        kind, val = next(iter(src.items()))
        key = f"{kind}:{val if isinstance(val, str) else json.dumps(val)}"
        if key in tried or kind not in FETCHERS:
            continue
        tried.append(key)
        try:
            found = FETCHERS[kind](val, title_ok)
            cache[name] = src
            return found, {"name": name, "group": comp.get("group", ""), "status": "ok",
                           "via": kind, "source": val if isinstance(val, str) else json.dumps(val),
                           "count": len(found)}
        except Exception as e:  # 다음 후보로
            errors.append(f"{kind}: {str(e)[:80]}")
            if cache.get(name) == src:
                cache.pop(name, None)
    return [], {"name": name, "group": comp.get("group", ""), "status": "fail",
                "via": "", "source": "", "count": 0,
                "error": "; ".join(errors[-4:]) or "채용 시스템을 찾지 못함"}
