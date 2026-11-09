"""AI 트렌드 · 스킬 보고 자동화. 월~금 = 매일 판(daily): 한 주에 판 하나를 두고 매일 새 소식 · 스킬 3~5건을 더한다,
금요일 = 팀 공유용 종합 호(weekly, 그 주 매일 판 + 사용자 제보). 옛 화 · 목 호(trends · skills)는 일정에서 뺐다.

  python3 trend.py collect [--date D]       후보 수집 → state/<D>/candidates.json · snapshot.json · weekly.json
  python3 trend.py generate daily|weekly    수집 → claude -p 작성 → 검증 → 렌더 (03.보고서/<그 주 월요일>-daily/ · <D>-weekly/)
  python3 trend.py deliver [kind]           오늘 만든 보고서를 Discord로 보낸다. 없거나 부실하거나 kind가 다르면 실패 알림
  python3 trend.py insight [--no-send]      주간 인사이트(연결 · 반대 의견 · 질문) → #인사이트 (토 09:00)
  python3 trend.py poll                     Discord 메시지를 읽고 명령 · 대화를 처리한다 (1분 주기)
  python3 trend.py discord-setup            봇이 든 채널과 내 사용자 ID를 찾아 .env에 적는다
  python3 trend.py alert "메시지"           Discord로 실패 알림
  python3 trend.py selftest                 급상승 규칙 · 렌더 자기검사

설정은 ~/.config/trend-automation/.env (DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID(#브리핑), DISCORD_INSIGHT_CHANNEL_ID(#인사이트),
DISCORD_ADMIN_CHANNEL_ID(#관리, 볼트 작업), DISCORD_OWNER_ID, HC_URL).
설계: ../01.설계/설계안.md
"""
import csv
import datetime as dt
import html
import io
import json
import os
import re
import statistics as st
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
# 코드는 Desktop/code, 보고서 · 문서는 볼트(Desktop/Obsidian/Second_Brain)에 있다
VAULT = HERE.parent.parent / "Obsidian" / "Second_Brain"
REPORTS = VAULT / "work" / "trend-automation" / "03.보고서"
# 브리핑 · 사용자 제보는 외부 정보를 정리한 것이라 볼트 raw-sources로 들어가 위키에 편입된다. 인사이트는 대상이 아니다
RAW = VAULT / "raw-sources"
RAW_NAME = {"daily": "데일리", "trends": "트렌드노트", "skills": "스킬도감"}
LINKLY = "https://raw.githubusercontent.com/LinklyAI/best-skills/main/data/latest/"
MARKETPLACE = "https://raw.githubusercontent.com/anthropics/claude-plugins-official/main/.claude-plugin/marketplace.json"
CHANGELOG = "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
UA = {"User-Agent": "Mozilla/5.0 (trend-automation)"}

# 급상승 규칙의 보정 손잡이. 2026-09-03~10-29 9주 백테스트에서 주당 1~6건 (설계안 §3).
SMOOTH = 500        # 작은 수의 노이즈 급등(1→10)과 0 나누기를 막는 평활값
RATIO = 2.5         # (지난주+SMOOTH)/(직전 3주 평균+SMOOTH)
MIN_GAIN = 1500     # 지난주 − 직전 3주 평균. 절대 순위 하한 대신 증가량 하한 — 순위 낮은 급등을 살린다
COOLDOWN_DAYS = 28  # 한 번 소개한 저장소는 4주간 다시 올리지 않는다
MIRROR_OWNERS = 3   # 같은 저장소 이름이 이만큼 많은 소유자에게 있으면 복제본 무리로 보고 원본 하나만 남긴다
SKILL_WORDS = re.compile(r"claude|skill|agent|mcp|llm|\bai\b|gpt|codex|prompt", re.I)

KST = dt.timezone(dt.timedelta(hours=9))


def today():
    return dt.datetime.now(KST).date().isoformat()


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def log(msg):
    line = f"[{dt.datetime.now(KST):%F %T}] {msg}"
    print(line, file=sys.stderr)
    (STATE / "logs").mkdir(parents=True, exist_ok=True)
    with open(STATE / "logs" / f"{today()}.log", "a") as f:
        f.write(line + "\n")


def soft(name, fn, errors):
    """보조 출처는 깨져도 보고를 막지 않는다. 오류는 candidates에 남겨 보고서 끝에 적힌다."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — 출처별 실패를 모으는 것이 목적
        errors.append(f"{name}: {e}")
        log(f"WARN {name}: {e}")
        return []


# ---------- 스킬 ----------

def csv_rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def surge(rows, reported, now):
    """skills.sh raw 행 → (급상승, 신규 진입, 제외 사유 수). 단위는 저장소 — 설치 수가 저장소 묶음 단위라서."""
    repos, by_pair = {}, defaultdict(set)
    for r in rows:
        if "/" in r["source"]:  # 「skills」처럼 흔한 저장소 이름이 겹치는 건 복제가 아니다. 저장소 이름+스킬 이름이 같아야 복제
            by_pair[(r["source"].split("/")[1].lower(), r["name"].lower())].add(r["source"])
        if not r.get("weekly_installs"):
            continue
        w = [int(x) for x in r["weekly_installs"].split("|") if x != ""]
        if len(w) >= 4:
            repos.setdefault(r["source"], (w, r["name"], int(r.get("installs") or 0)))

    mirrors = set()
    for srcs in by_pair.values():
        srcs = srcs & repos.keys()
        if len({s.split("/")[0] for s in srcs}) >= MIRROR_OWNERS:
            keep = max(srcs, key=lambda s: repos[s][2])
            mirrors |= srcs - {keep}

    same_count = defaultdict(list)  # 한 소유자의 여러 저장소가 같은 주 설치 수가 똑같으면 조작 의심
    for src, (w, _, _) in repos.items():
        same_count[src.split("/")[0]].append(w[-1])
    suspicious = {o for o, v in same_count.items() if Counter(v).most_common(1)[0][1] >= 3}

    hits, new, skipped = [], [], Counter()
    for src, (w, name, total) in repos.items():
        owner = src.split("/")[0]
        if src in mirrors:
            skipped["복제본"] += 1
            continue
        if owner in suspicious:
            skipped["조작의심"] += 1
            continue
        last_day = reported.get(src)
        if last_day and (now - dt.date.fromisoformat(last_day)).days < COOLDOWN_DAYS:
            skipped["쿨다운"] += 1
            continue
        base, last = st.mean(w[-4:-1]), w[-1]
        item = {"repo": src, "skill": name, "last_week": last, "prev3_avg": round(base),
                "all_time": total, "weekly": w[-5:]}
        if base == 0 and last >= MIN_GAIN:
            new.append(item)
        elif (last + SMOOTH) / (base + SMOOTH) >= RATIO and last - base >= MIN_GAIN:
            item["ratio"] = round(last / base, 1) if base else None
            hits.append(item)
    hits.sort(key=lambda x: x["last_week"] - x["prev3_avg"], reverse=True)
    return hits, new, dict(skipped)


def linkly_fresh():
    """LinklyAI는 서드파티 단일 출처라, 48시간 넘게 멈췄으면 보고를 막고 알린다."""
    data = json.loads(fetch("https://api.github.com/repos/LinklyAI/best-skills/commits?path=data/latest/rankings&per_page=1"))
    when = dt.datetime.fromisoformat(data[0]["commit"]["committer"]["date"].replace("Z", "+00:00"))
    age = (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 3600
    if age > 48:
        raise RuntimeError(f"LinklyAI 데이터가 {age:.0f}시간째 갱신되지 않음")


def top_lists(prev_top):
    pick = lambda rows, n, cols: [{c: r.get(c, "") for c in cols} for r in rows[:n]]
    best = csv_rows(fetch(LINKLY + "rankings/best-100.csv"))
    base = ["rank", "skill", "vendor", "url", "repo_url", "install", "category"]
    top20 = pick(best, 20, base + ["wis"])
    for r in top20:  # 비교할 이전 스냅숏이 없으면 '새로 진입'을 말할 수 없다
        r["new_in_top20"] = (r["url"] not in prev_top) if prev_top else None
    return {
        "best_top20": top20,
        "rising_stars": pick(csv_rows(fetch(LINKLY + "rankings/rising-stars.csv")), 10, base + ["first_seen_days"]),
        "social_buzz": pick(csv_rows(fetch(LINKLY + "rankings/social-buzz.csv")), 10, base + ["x_mentions_7d", "hn_hits_7d"]),
    }


def marketplace_diff(prev):
    plugins = json.loads(fetch(MARKETPLACE))["plugins"]
    names = {p["name"] for p in plugins}
    added = [] if not prev else [
        {k: p.get(k) for k in ("name", "description", "category", "homepage")} for p in plugins if p["name"] not in prev]
    return added, sorted(names)


def github_trending():
    page = fetch("https://github.com/trending?since=weekly")
    out = []
    for block in page.split('<article class="Box-row">')[1:]:
        m = re.search(r'href="/([\w.-]+/[\w.-]+)/stargazers"', block)
        s = re.search(r"([\d,]+) stars this week", block)
        d = re.search(r'<p class="col-9[^>]*>\s*(.*?)\s*</p>', block, re.S)
        desc = html.unescape(re.sub(r"<[^>]+>", "", d.group(1))).strip() if d else ""
        if m and s and SKILL_WORDS.search(m.group(1) + " " + desc):
            out.append({"repo": m.group(1), "stars_week": int(s.group(1).replace(",", "")), "description": desc[:200]})
    return out


# ---------- AI 트렌드 ----------

def feed_items(url, label, since):
    root = ET.fromstring(fetch(url).encode())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = []
    for it in root.iter("item"):
        items.append((it.findtext("title"), it.findtext("link"), it.findtext("pubDate")))
    for it in root.findall("a:entry", ns):
        link = it.find("a:link", ns)
        items.append((it.findtext("a:title", namespaces=ns), link.get("href") if link is not None else "",
                      it.findtext("a:updated", namespaces=ns) or it.findtext("a:published", namespaces=ns)))
    out = []
    for title, link, date in items:
        try:
            when = parse_date(date)
        except (TypeError, ValueError):
            continue
        if when >= since:
            out.append({"source": label, "title": (title or "").strip(), "url": link, "date": when.date().isoformat()})
    return out


def parse_date(s):
    from email.utils import parsedate_to_datetime
    s = s.strip()
    d = parsedate_to_datetime(s) if s[:3].isalpha() else dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def page_links(url, pattern, prev, label):
    """RSS가 없는 공식 페이지는 링크 목록의 차분으로 새 글을 찾는다. 첫 실행은 맨 앞 8개."""
    links = list(dict.fromkeys(re.findall(pattern, fetch(url))))
    new = [l for l in links if l not in prev] if prev else links[:8]
    base = re.match(r"https://[^/]+", url).group(0)
    return [{"source": label, "url": base + l} for l in new], links


def changelog_since(last_seen):
    text = fetch(CHANGELOG)
    parts = re.split(r"^## ", text, flags=re.M)[1:]
    out = []
    for p in parts:
        ver = p.split("\n", 1)[0].strip()
        if ver == last_seen or len(out) >= (30 if last_seen else 5):
            break
        out.append({"version": ver, "notes": p.split("\n", 1)[1].strip()[:1500]})
    return out


def hacker_news(since):
    """지난 기간 100점 이상 + 최근 3일 40점 이상(아직 점수가 덜 쌓인 새 글을 놓치지 않으려고)."""
    seen, out = set(), []
    recent = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).timestamp())
    for q in ["Claude", "Anthropic", "Claude Code", "MCP", "AI agent", "LLM", "OpenAI", "Gemini"]:
        url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=20&query="
               + urllib.parse.quote(q) + f"&numericFilters=points%3E100,created_at_i%3E{int(since.timestamp())}")
        url2 = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=20&query="
                + urllib.parse.quote(q) + f"&numericFilters=points%3E40,created_at_i%3E{recent}")
        for h in json.loads(fetch(url))["hits"] + json.loads(fetch(url2))["hits"]:
            if h["objectID"] not in seen:
                seen.add(h["objectID"])
                out.append({"title": h["title"], "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                            "points": h["points"], "comments": h.get("num_comments"), "date": h["created_at"][:10],
                            "hn": f"https://news.ycombinator.com/item?id={h['objectID']}"})
    return sorted(out, key=lambda x: -x["points"])[:35]


def trending_days(now, days=7):
    """GitHub 일간 트렌딩 기록(bonfy/github-trending)으로 지난 7일 중 며칠 올랐는지 센다. AI 관련만."""
    seen = {}
    for k in range(1, days + 1):
        d = (now - dt.timedelta(days=k)).isoformat()
        try:
            text = fetch(f"https://raw.githubusercontent.com/bonfy/github-trending/master/{d}.md")
        except Exception:  # noqa: BLE001 — 빠진 날은 건너뛴다
            continue
        for a, b, desc in re.findall(r"^\* \[([\w.-]+) / ([\w.-]+)\]\([^)]*\):(.*)$", text, re.M):
            repo = f"{a}/{b}"
            if SKILL_WORDS.search(repo + " " + desc):
                e = seen.setdefault(repo, {"repo": repo, "days": 0, "description": desc.strip()[:200]})
                e["days"] += 1
    return sorted(seen.values(), key=lambda e: -e["days"])[:15]


def star_risers(now):
    """스킬 뒤 저장소(별 상위 100) 중 지난 7일 별 증가가 큰 것. 오래된 대형 저장소가 다시 뜨는 것을 잡는다."""
    base = "https://raw.githubusercontent.com/LinklyAI/best-skills/main/data/"
    cur = {r["repo"]: r for r in csv_rows(fetch(LINKLY + "rankings/top-repos.csv"))}
    old = {r["repo"]: int(r["stars"]) for r in csv_rows(fetch(f"{base}{(now - dt.timedelta(days=7)).isoformat()}/rankings/top-repos.csv"))}
    out = [{"repo": k, "stars": int(r["stars"]), "stars_7d": int(r["stars"]) - old[k], "topics": r.get("topics", "")}
           for k, r in cur.items() if k in old]
    return sorted(out, key=lambda e: -e["stars_7d"])[:10]


def show_hn(since):
    """직접 써 볼 수 있는 새 도구. Show HN 중 AI · 에이전트 관련, 점수 50 이상."""
    seen, out = set(), []
    for q in ["Claude", "MCP", "agent", "LLM", "AI"]:
        url = ("https://hn.algolia.com/api/v1/search?tags=show_hn&hitsPerPage=15&query=" + urllib.parse.quote(q)
               + f"&numericFilters=points%3E50,created_at_i%3E{int(since.timestamp())}")
        for h in json.loads(fetch(url))["hits"]:
            if h["objectID"] not in seen:
                seen.add(h["objectID"])
                out.append({"title": h["title"], "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                            "points": h["points"], "hn": f"https://news.ycombinator.com/item?id={h['objectID']}"})
    return sorted(out, key=lambda x: -x["points"])[:12]


WATCH_ORGS = ["anthropics", "openai", "vercel-labs", "google-gemini", "modelcontextprotocol", "huggingface",
              "ollama", "langchain-ai", "microsoft", "simonw"]  # 릴리스를 지켜보는 조직. 화제 저장소는 따로 더한다


def gh_api(path):
    """GitHub API. gh 로그인 토큰을 쓴다(비인증은 시간당 60회라 모자란다)."""
    tok = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    req = urllib.request.Request("https://api.github.com" + path,
                                 headers={**UA, "Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {tok}"} if tok else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def github_releases(since, hot_repos):
    """지켜보는 조직의 활발한 저장소(별 1,000 이상, 최근 push)와 화제 저장소의 지난 기간 릴리스.
    블로그 · 칼럼보다 먼저 나오는 1차 공시다(예: vercel-labs/agent-desktop v1.0 → 칼럼은 며칠 뒤)."""
    repos = dict.fromkeys(hot_repos)
    for org in WATCH_ORGS:
        try:
            for r in gh_api(f"/orgs/{org}/repos?sort=pushed&per_page=20"):
                if r["stargazers_count"] >= 1000 and r["pushed_at"] >= since.isoformat()[:10]:
                    repos.setdefault(r["full_name"], None)
        except Exception as e:  # noqa: BLE001 — 사용자 계정(simonw) 등은 /users로 다시
            try:
                for r in gh_api(f"/users/{org}/repos?sort=pushed&per_page=20"):
                    if r["stargazers_count"] >= 1000 and r["pushed_at"] >= since.isoformat()[:10]:
                        repos.setdefault(r["full_name"], None)
            except Exception:  # noqa: BLE001
                log(f"WARN releases {org}: {e}")
    out = []
    for full in list(repos)[:80]:
        try:
            for rel in gh_api(f"/repos/{full}/releases?per_page=3"):
                if rel.get("draft") or not rel.get("published_at") or rel["published_at"] < since.isoformat()[:19]:
                    continue
                tag = rel["tag_name"]
                out.append({"repo": full, "tag": tag, "date": rel["published_at"][:10], "name": rel.get("name") or tag,
                            "url": rel["html_url"], "prerelease": rel.get("prerelease"),
                            "major": bool(re.search(r"(?:^|[^\d.])v?[1-9]\d*\.0(?:\.0)?$", tag)),  # 1.0 · 2.0.0 (패치 제외)
                            "minor": bool(re.search(r"\.\d+\.0$", tag)),  # x.y.0 새 기능 릴리스
                            "notes": (rel.get("body") or "")[:700]})
        except Exception as e:  # noqa: BLE001
            log(f"WARN releases {full}: {e}")
    out.sort(key=lambda r: r["date"], reverse=True)
    out.sort(key=lambda r: (not r["major"], not r["minor"]))  # 큰 버전 → 기능 릴리스 → 패치, 각각 최신순
    return out[:40]


def last_edition(kind, before):
    """같은 호의 직전 정기 보고 날짜(주 1회라 5일 이상 앞선 것). 이 날짜 이후에 나온 소식만 새 소식이다.
    같은 주에 다시 만든 시험본 · rerun은 기준이 되지 않는다. 매일 판은 오늘 전 마지막 갱신(또는 옛 화 · 목 호)이 기준이다."""
    if kind == "daily":
        ds = sorted([p.name for p in REPORTS.glob("20*-daily/20*-*-*") if p.is_dir() and p.name < before]
                    + [p.name[:10] for k in ("trends", "skills") for p in REPORTS.glob(f"20*-{k}") if p.name[:10] < before])
        return ds[-1] if ds else (dt.date.fromisoformat(before) - dt.timedelta(days=7)).isoformat()
    cut =(dt.date.fromisoformat(before) - dt.timedelta(days=5)).isoformat()
    ds = sorted(p.name[:10] for p in REPORTS.glob(f"20*-{kind}") if p.name[:10] <= cut)
    return ds[-1] if ds else (dt.date.fromisoformat(before) - dt.timedelta(days=7)).isoformat()


def collect(date):
    now = dt.date.fromisoformat(date)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
    # 차분 기준은 직전 보고 무렵(5일 이상 전)의 스냅숏. 같은 날 재시도 · rerun이 서로의 새 소식을 지우지 않게.
    olds = sorted(p for p in STATE.glob("20*/snapshot.json") if p.parent.name <= (now - dt.timedelta(days=5)).isoformat())
    prev = load_json(olds[-1], {}) if olds else {}
    reported = load_json(STATE / "reported.json", {})
    errors = []

    linkly_fresh()  # 핵심 출처 — 실패하면 그대로 예외
    sk_rows = csv_rows(fetch(LINKLY + "raw/skills-sh.csv"))
    hits, new, skipped = surge(sk_rows, reported, now)
    weekly = {}
    for r in sk_rows:  # 보고서 그림(주간 설치 막대)용. LLM에게는 넘기지 않는다
        if r.get("weekly_installs"):
            weekly.setdefault(r["source"].lower(), [int(x) for x in r["weekly_installs"].split("|") if x != ""])
    save_json(STATE / date / "weekly.json", weekly)
    tops = top_lists(set(prev.get("best_top20", [])))
    added, mp_names = soft("marketplace", lambda: marketplace_diff(set(prev.get("marketplace", []))), errors) or ([], prev.get("marketplace", []))

    news = []
    for line in (HERE / "sources.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            url, label = line.split(None, 1)
            news += soft(label, lambda u=url, l=label: feed_items(u, l, since), errors)
    official_new, links = [], {}
    for key, url, pat, label in [("anthropic_news", "https://www.anthropic.com/news", r'href="(/news/[a-z0-9-]+)"', "Anthropic News"),
                                 ("claude_blog", "https://claude.com/blog", r'href="(/blog/[a-z0-9-]+)"', "Claude Blog")]:
        got = soft(label, lambda u=url, p=pat, l=label, k=key: page_links(u, p, set(prev.get(k, [])), l), errors)
        if got:
            official_new += got[0]
            links[key] = got[1]
    cl = soft("changelog", lambda: changelog_since(prev.get("changelog")), errors)

    cand = {
        "date": date,
        "skills": {"surge": hits[:10], "new_entrants": new[:10], "excluded": skipped, **tops,
                   "marketplace_added": added, "github_trending": soft("github trending", github_trending, errors),
                   "trending_days_7d": soft("trending archive", lambda: trending_days(now), errors),
                   "star_risers_7d": soft("star risers", lambda: star_risers(now), errors)},
        "trends": {"official_new": official_new, "claude_code_changelog": cl,
                   "hacker_news": soft("hn", lambda: hacker_news(since), errors), "feeds": news,
                   "practice_signals": {"show_hn": soft("show hn", lambda: show_hn(since), errors)},
                   "github_releases": [], "hot_repos": []},
        "covered_recently": load_json(STATE / "covered.json", [])[-80:],
        "source_errors": errors,
    }
    hot = [r["repo"] for r in (cand["skills"]["star_risers_7d"] or [])[:15]] + [r["repo"] for r in (cand["skills"]["trending_days_7d"] or [])[:15]]
    cand["trends"]["hot_repos"] = (cand["skills"]["star_risers_7d"] or [])[:10] + (cand["skills"]["trending_days_7d"] or [])[:10]
    cand["trends"]["github_releases"] = soft("github releases", lambda: github_releases(since, hot), errors)
    save_json(STATE / date / "candidates.json", cand)
    snap = {"best_top20": [r["url"] for r in tops["best_top20"]], "marketplace": mp_names,
            **{k: prev.get(k, []) for k in ("anthropic_news", "claude_blog")}, **links,
            "changelog": cl[0]["version"] if cl else prev.get("changelog")}
    save_json(STATE / date / "snapshot.json", snap)
    log(f"collect {date}: surge {len(hits)} new {len(new)} excluded {skipped} news {len(news)} errors {len(errors)}")
    return STATE / date / "candidates.json"


# ---------- 작성 · 검증 ----------

ENV_FILE = Path.home() / ".config" / "trend-automation" / ".env"
EDITION = {"daily": "AI 데일리", "trends": "AI 트렌드 노트", "skills": "스킬 도감", "weekly": "트렌드 + AI 종합"}
MIN_ITEMS = {"daily": 3, "trends": 2, "skills": 3, "weekly": 5}
WEEKDAY = "월화수목금토일"


def norm(s):
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def drop_stale(items, since):
    """원문 날짜가 직전 호보다 앞선 소식은 뺀다(`stale`). 가이드 · 스킬은 날짜가 없어도 된다."""
    for it in items.get("trends", []):
        pub = it.get("published", "")
        if not re.match(r"\d{4}-\d{2}-\d{2}$", pub or "") or pub < since:
            it["verified"], it["stale"] = False, True
    return items


def verify(items):
    """LLM의 '확인했다'는 검증이 아니다. 설치 명령이 그 출처 문서 본문에 글자 그대로 있어야 통과."""
    cache = {}

    def text_of(url):
        if url not in cache:
            try:
                cache[url] = norm(fetch(url).replace('\\"', '"'))
            except Exception:  # noqa: BLE001
                cache[url] = None
        return cache[url]

    for it in items.get("skills", []):
        body = text_of(it.get("install_source_url", ""))
        it["verified"] = bool(body and it.get("install") and norm(it["install"]) in body)
    for it in items.get("trends", []):
        it["verified"] = bool(it.get("source_url") and text_of(it["source_url"]))
    for it in items.get("guides", []):  # 설치 명령이 있으면 스킬과 같은 대조, 없으면 출처가 열리는지만
        if it.get("install"):
            body = text_of(it.get("install_source_url", ""))
            it["verified"] = bool(body and norm(it["install"]) in body)
        else:
            it["verified"] = bool(it.get("source_url") and text_of(it["source_url"]))
    return items


def og_image(url):
    m = re.match(r"https://github\.com/([\w.-]+/[\w.-]+)", url or "")
    if m:  # GitHub는 저장소마다 카드 이미지를 만들어 준다
        return f"https://opengraph.githubassets.com/1/{m.group(1)}"
    try:
        page = fetch(url)[:400000]
    except Exception:  # noqa: BLE001 — 그림이 없으면 그림 없이 간다
        return None
    m = (re.search(r'og:image["\']\s+content=["\']([^"\']+)', page)
         or re.search(r'content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:image["\']', page))
    return html.unescape(m.group(1)) if m and m.group(1).startswith("http") else None


def enrich(items, weekly):
    for it in items.get("skills", []):
        it["image"] = og_image(it.get("repo_url") or it.get("install_source_url"))
        w = weekly.get((it.get("repo") or "").lower())
        if w and len(w) >= 4:
            it["weekly"] = w[-8:]
    for it in items.get("trends", []) + items.get("guides", []):
        it["image"] = og_image(it.get("repo_url") or it.get("source_url"))
    return items


def period(date):
    """「10월 다섯째 주」. 주는 월요일에 시작하고, 그 주 목요일이 속한 달로 센다."""
    d = dt.date.fromisoformat(date)
    thu = d - dt.timedelta(days=d.weekday()) + dt.timedelta(days=3)
    return f"{thu.month}월 {['첫째', '둘째', '셋째', '넷째', '다섯째'][(thu.day - 1) // 7]} 주"


def direction_text():
    p = HERE / "direction.md"
    return p.read_text() if p.exists() else "(아직 없음)"


def week_editions(date):
    """이번 주(월요일부터 date까지) 매일 · 화 · 목 호 폴더, 날짜순."""
    d = dt.date.fromisoformat(date)
    mon = (d - dt.timedelta(days=d.weekday())).isoformat()
    eds = sorted((p for k in ("daily", "trends", "skills") for p in REPORTS.glob(f"20*-{k}")
                  if mon <= p.name[:10] <= date and (p / "items.json").exists()), key=lambda p: p.name)
    if not eds:
        raise RuntimeError(f"이번 주({mon}~) 호가 없음")
    return eds


def week_dir(date):
    """매일 판 폴더. 한 주에 하나(그 주 월요일 이름)를 금요일 종합 전까지 매일 갱신한다. 토 · 일에 만들면 다음 주 판으로 간다."""
    d = dt.date.fromisoformat(date) + dt.timedelta(days=2)
    return REPORTS / f"{(d - dt.timedelta(days=d.weekday())).isoformat()}-daily"


def generate(kind):
    date = today()
    out = week_dir(date) if kind == "daily" else REPORTS / f"{date}-{kind}"
    work = out / date if kind == "daily" else out  # 매일 판은 그날 새 항목을 하위 폴더에 받아 그 주 판에 더한다
    work.mkdir(parents=True, exist_ok=True)
    (work / "items.json").unlink(missing_ok=True)  # 지난 결과가 남아 있으면 실패를 성공으로 오인한다
    report = (HERE / "prompt_report.md").read_text()
    if kind == "weekly":  # 새로 수집하지 않는다. 그 주 매일 · 화 · 목 호에서 검증을 통과한 항목이 재료다
        eds = week_editions(date)
        got = [json.loads((p / "items.json").read_text()) for p in eds]
        cand = out / "candidates.json"
        save_json(cand, {**{k: [i for g in got for i in g.get(k, []) if i.get("verified")] for k in ("trends", "guides", "skills")},
                         "source_errors": []})
        report = (HERE / "prompt_weekly.md").read_text().replace("{RULES}", "## 반드시 지킬 것" + report.split("## 반드시 지킬 것", 1)[1])
        weekly_dir = max(STATE.glob("20*/weekly.json")).parent
    else:
        cand = collect(date)
        weekly_dir = STATE / date
    prompt = (report.replace("{KIND}", kind).replace("{CANDIDATES}", str(cand))
              .replace("{DATE}", date).replace("{DIRECTION}", direction_text())
              .replace("{EDITION}", EDITION[kind]).replace("{PERIOD}", period(date))
              .replace("{SINCE}", last_edition(kind, date)))
    for attempt in (1, 2):
        log(f"claude -p {kind} attempt {attempt}")
        r = subprocess.run(
            ["timeout", "45m", "claude", "-p", prompt, "--setting-sources", "user", "--strict-mcp-config",
             "--allowedTools", "Read,Glob,Grep,WebFetch,WebSearch,Write", "--permission-prompts", "none",
             "--max-turns", "120", "--output-format", "json"],
            cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        (work / "claude.log").write_text(r.stdout[-20000:] + "\n--- stderr\n" + r.stderr[-5000:])
        if r.returncode == 0 and (work / "items.json").exists():
            break
    else:
        raise RuntimeError(f"claude -p 실패 (exit {r.returncode}), 로그: {work / 'claude.log'}")
    items = json.loads((work / "items.json").read_text())
    items.setdefault("skills", [])
    items.setdefault("trends", [])
    items.setdefault("guides", [])
    items = verify(items)
    if kind != "weekly":  # 종합 호는 지난 소식을 모으는 호라 날짜로 거르지 않는다
        items = drop_stale(items, last_edition(kind, date))
    items = enrich(items, load_json(weekly_dir / "weekly.json", {}))
    items["source_errors"] = json.loads(cand.read_text())["source_errors"]
    new = items
    if kind == "daily":  # 오늘 새 항목을 앞에 두고 그 주 판에 더한다. 같은 날 다시 만들면 그날 것만 바꾼다
        save_json(work / "items.json", new)
        week = load_json(out / "items.json", {})
        for k in ("trends", "guides", "skills"):
            for it in new[k]:
                it["day"] = date
        items = {**new, **{k: new[k] + [i for i in week.get(k, []) if i.get("day") != date] for k in ("trends", "guides", "skills")}}
    save_json(out / "items.json", items)
    render(items, kind, date, out)
    reported = load_json(STATE / "reported.json", {})
    covered = load_json(STATE / "covered.json", [])
    for it in new["skills"]:
        if it["verified"]:
            if it.get("repo"):
                reported[it["repo"]] = date
            covered.append(it["name"])
    covered += [t["title"] for t in new["trends"] + new["guides"] if t["verified"]]
    save_json(STATE / "reported.json", reported)
    save_json(STATE / "covered.json", covered[-200:])
    save_json(STATE / "status.json", {"last_generate": date, "kind": kind, "dir": str(out),
                                      "items": len(plates(new, kind))})
    if kind in RAW_NAME:  # 종합 호는 매일 판을 다시 묶은 것이라 raw로 넣지 않는다. 매일 판 raw는 주마다 한 파일(편입 전까지 갱신)
        to_raw(f"AI브리핑_{out.name[:10]}_{RAW_NAME[kind]}.md",
               re.sub(r"^!\[[^\]]*\]\(chart-\d+\.svg\)\n\n?", "", (out / "report.md").read_text(), flags=re.M))  # 막대 그림은 보고서 폴더에만 있다
    log(f"generate {kind} done → {out}")


def ingested(name):
    return (VAULT / "wiki" / "sources" / (name[:-3] + ".md")).exists()


def to_raw(name, text, append=False):
    """raw-sources에 원본 사본을 쓰고 위키 편입 요청을 requests.md에 올린다.
    raw는 불변이다 — 이미 위키에 편입된(sources 페이지가 있는) 파일은 건드리지 않는다. 편입 전이면 재생성 · 추가를 허용한다."""
    if ingested(name):
        log(f"raw 편입됨, 건너뜀: {name}")
        return
    p = RAW / name
    p.write_text(p.read_text() + text if append and p.exists() else text)
    subprocess.run(["python3", str(VAULT / "scripts" / "gen_raw_manifest.py")], capture_output=True)
    req = HERE / "requests.md"
    line = f"위키 편입: raw-sources/{name}"
    if line not in req.read_text():
        req.write_text(req.read_text().replace("## 대기\n", f"## 대기\n- [{today()}] {line} — sources 페이지 + 도구맵 · 개념 페이지로 옮기기 + relations.py로 관계 후보 · alternativeTo · disagreesWith 제안\n", 1))


def to_raw_report(user_text, reply, channel):
    """사용자 제보 한 건(제보 + 봇 조사 답)을 그날의 제보 원본에 덧붙인다. 그날 파일이 이미 편입됐으면 _2, _3…"""
    d = today()
    name = next(n for n in (f"AI브리핑_제보_{d}{s}.md" for s in ["", *(f"_{i}" for i in range(2, 50))]) if not ingested(n))
    head = "" if (RAW / name).exists() else (f"# AI 브리핑 — 사용자 제보와 조사 답 ({d})\n\n"
        "Discord에서 사용자가 단건으로 준 AI 소식 · 스킬과, 브리핑 봇(Claude)이 웹을 찾아 답한 내용이다.\n")
    now = dt.datetime.now().strftime("%H:%M")
    block = f"\n## {user_text.splitlines()[0][:60]}\n\n*{d} {now} · #{channel}*\n\n**제보**\n\n> " + user_text.replace("\n", "\n> ") + f"\n\n**조사 답**\n\n{reply}\n"
    to_raw(name, head + block, append=True)


# ---------- 렌더 ----------

LABEL = {"surge": "급상승", "new": "신규 진입", "top": "상위권", "plugin": "공식 마켓 신규", "github": "GitHub 트렌딩"}
def ko_date(iso):
    try:
        d = dt.date.fromisoformat(iso)
        return f"{d.month}월 {d.day}일"
    except (TypeError, ValueError):
        return ""


TLABEL = {"release": "출시 · 업데이트", "tool": "도구 사용법", "practice": "실무 · 관점"}
CLAIM = {"official": "공식 발표", "external": "외부 측정 · 보도", "opinion": "의견 · 칼럼"}


def esc(s):
    return html.escape(str(s or ""))


def inline(s):
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc(s))
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)


def short_url(u):
    return re.sub(r"^https?://(www\.)?", "", u).rstrip("/")[:60]


def week_chart(w, title):
    """주간 설치 막대. 마지막 막대(지난주)에 표지 화살표를 단다 — 도감의 '식별 포인트'."""
    W, H, top, bottom, left = 520, 190, 34, 26, 8
    n, peak = len(w), max(max(w), 1)
    bw = (W - left * 2) / n
    base = st.mean(w[-4:-1])
    y = lambda v: top + (H - top - bottom) * (1 - v / peak)
    bars = []
    for i, v in enumerate(w):
        x = left + i * bw + bw * 0.18
        cls = "bar last" if i == n - 1 else "bar"
        label = "지난주" if i == n - 1 else f"{n - 1 - i}주 전"
        bars.append(f'<rect class="{cls}" x="{x:.1f}" y="{y(v):.1f}" width="{bw * 0.64:.1f}" height="{H - bottom - y(v):.1f}">'
                    f"<title>{label} {v:,}</title></rect>")
    by = y(base)
    lx = left + (n - 1) * bw + bw / 2
    ratio = f" · {w[-1] / base:.1f}배" if base else ""
    note = (f'<line class="avg" x1="{left}" x2="{W - left}" y1="{by:.1f}" y2="{by:.1f}"/>'
            f'<text class="cap" x="{left + 2}" y="{by - 6:.1f}">직전 3주 평균 {base:,.0f}</text>' if base else "")
    return (f'<svg class="weeks" viewBox="0 0 {W} {H}" role="img"><title>{esc(title)} 주간 설치 {n}주</title>'
            '<style>.bar{fill:#9AA79D}.bar.last{fill:#E9C64B;stroke:#1D241F;stroke-width:1}.avg{stroke:#1D241F;stroke-dasharray:3 4;stroke-width:1}'
            '.mark{stroke:#1D241F;stroke-width:1.4;fill:none}.cap,.lab{font:500 12px "Pretendard","Noto Sans KR",sans-serif;fill:#3E4A42}'
            '.lab{font-weight:700;fill:#1D241F}.axis{stroke:#B9C2B9}'
            '@media (prefers-color-scheme:dark){.bar{fill:#4E5A52}.bar.last{stroke:#E9C64B}.avg,.mark{stroke:#DDE3DC}.cap{fill:#AEB8AF}.lab{fill:#EEF1EC}.axis{stroke:#3A433D}}</style>'
            f'<line class="axis" x1="{left}" x2="{W - left}" y1="{H - bottom}" y2="{H - bottom}"/>{"".join(bars)}{note}'
            f'<path class="mark" d="M{lx - 70:.1f} 16 H{lx - 8:.1f} L{lx:.1f} {max(y(w[-1]) - 6, 22):.1f}"/>'
            f'<text class="lab" x="{lx - 74:.1f}" y="20" text-anchor="end">지난주 {w[-1]:,}{ratio}</text>'
            f'<text class="cap" x="{left}" y="{H - 8}">{n - 1}주 전</text><text class="cap" x="{W - left}" y="{H - 8}" text-anchor="end">지난주</text></svg>')


def plates(items, kind):
    """화요일 호는 소식 다음에 실무 가이드가 붙는다. 종합 호는 소식 → 스킬 → 가이드. 스킬은 _s, 가이드는 _g 표시로 구분한다."""
    ps = [i for i in items["trends"] if i["verified"]] if kind != "skills" else []
    if kind != "trends":
        ps += [dict(s, _s=True) for s in items["skills"] if s["verified"]]
    if kind != "skills":
        ps += [dict(g, _g=True) for g in items.get("guides", []) if g["verified"]]
    return ps


def all_items(items, kind):
    return (items["trends"] if kind != "skills" else []) + (items["skills"] if kind != "trends" else []) \
        + (items.get("guides", []) if kind != "skills" else [])


def render(items, kind, date, out):
    d = dt.date.fromisoformat(date)
    title = f"{EDITION[kind]} — {d.month}월 {d.day}일 ({WEEKDAY[d.weekday()]})"
    items["_dateline"] = f"{d.year}년 {d.month}월 {d.day}일 {WEEKDAY[d.weekday()]}요일 · {len(plates(items, kind))}건"
    ps = plates(items, kind)
    dropped = [(i.get("name") or i.get("title")) + (" (지난 호 이전 소식)" if i.get("stale") else "")
               for i in all_items(items, kind) if not i["verified"]]
    for f in out.glob("chart-*.svg"):
        f.unlink()
    (out / "report.html").write_text(build_html(items, kind, title, ps, dropped))
    (out / "report.md").write_text(build_md(items, kind, title, ps, dropped, out))


def md_item(p, n, kind, out=None):
    """도판 한 장의 md. out이 있으면 막대 그림 파일을 쓰고 링크한다(Obsidian용). 없으면 그림 없이(Discord용)."""
    md = []
    if p.get("_s"):
        md += [f"## {n}. {p['name']}", "", f"*{LABEL.get(p.get('category'), '')}*", ""]
        if p.get("image"):
            md += [f"![{p['name']}]({p['image']})", ""]
        md += ["### 무엇인가", "", p["what"], "", "### 왜 지금", "", p["why_now"], ""]
        if p.get("weekly") and out:
            (out / f"chart-{n}.svg").write_text(week_chart(p["weekly"], p["name"]))
            md += [f"![주간 설치]({'chart-%d.svg' % n})", ""]
        md += ["### 설치", "", "```bash", p["install"], "```", "", "### 활용법", ""]
        md += [f"{k}. {u}" for k, u in enumerate(p.get("usage", []), 1)] + [""]
        if p.get("diagram"):
            md += ["```mermaid", p["diagram"], "```", ""]
        if p.get("prompt_example"):
            md += ["### 예시 프롬프트", "", "```text", p["prompt_example"], "```", ""]
        if p.get("caution"):
            md += [f"> **주의.** {p['caution']}", ""]
        srcs = dict.fromkeys(filter(None, [p.get("install_source_url"), p.get("repo_url")]))
    elif p.get("_g"):
        md += [f"## {n}. {p['title']}", "", "*실무 가이드*", ""]
        if p.get("image"):
            md += [f"![{p['title']}]({p['image']})", ""]
        md += [f"**이런 분께.** {p.get('audience', '')}", "", f"**읽고 나면.** {p.get('outcome', '')}", "", p["what"], ""]
        md += ["### 설치 · 설정", ""] + (["```bash", p["install"], "```", ""] if p.get("install") else [])
        md += [f"{k}. {u}" for k, u in enumerate(p.get("setup", []), 1)] + [""]
        ho = p.get("hands_on") or {}
        md += ["### 실습", "", ho.get("goal", ""), ""] + [f"{k}. {u}" for k, u in enumerate(ho.get("steps", []), 1)] + [""]
        if p.get("diagram"):
            md += ["```mermaid", p["diagram"], "```", ""]
        if p.get("prompts"):
            md += ["### 바로 쓰는 프롬프트", ""]
            for pr in p["prompts"]:
                md += [f"**{pr.get('label', '')}**", "", "```text", pr.get("text", ""), "```", ""]
        if p.get("troubleshooting"):
            md += ["### 안 될 때", ""] + [f"- **{t.get('problem', '')}** → {t.get('fix', '')}" for t in p["troubleshooting"]] + [""]
        if p.get("caution"):
            md += [f"> **주의.** {p['caution']}", ""]
        srcs = dict.fromkeys(filter(None, [p.get("source_url"), p.get("install_source_url")] + p.get("sources", [])))
    else:
        md += [f"## {n}. {p['title']}", "", f"*{TLABEL.get(p.get('category'), '')} · {CLAIM.get(p.get('claim_type'), '')}"
               + (f" · 원문 {ko_date(p.get('published'))}" if p.get("published") else "") + "*", ""]
        if p.get("image"):
            md += [f"![{p['title']}]({p['image']})", ""]
        md += [p["summary"], ""]
        if p.get("diagram"):
            md += ["```mermaid", p["diagram"], "```", ""]
        md += [f"### {'우리 팀에게는' if kind == 'weekly' else '그래서 무엇을 하나'}", "", p["so_what"], ""] + [f"- {h}" for h in p.get("how_to", [])] + [""]
        srcs = dict.fromkeys([p["source_url"]] + p.get("sources", []))
    md += ["출처: " + " · ".join(srcs), ""]
    return md


def build_md(items, kind, title, ps, dropped, out):
    md = [f"# {title}", "", items.get("headline", ""), ""]
    md += [f"{n}. **{p.get('name') or p.get('title')}**{' (실무 가이드)' if p.get('_g') else ''} — {p.get('one_line', '')}"
           for n, p in enumerate(ps, 1)] + [""]
    for n, p in enumerate(ps, 1):
        md += md_item(p, n, kind, out)
    md += ["---", ""] + memo_lines(items, dropped)
    return "\n".join(md)


def memo_lines(items, dropped):
    lines = []
    if dropped:
        lines.append("- 검증에 실패해 뺀 항목(설치 명령이 출처 본문에 없거나 출처가 열리지 않음): " + ", ".join(map(str, dropped)))
    lines += [f"- 출처 오류: {e}" for e in items.get("source_errors", [])]
    return lines + ["- 스킬 순위 데이터: Best Skills (https://github.com/LinklyAI/best-skills) by @BlueeonY — CC BY 4.0",
                    "- 그림은 각 출처의 공개 미리보기 이미지(Open Graph)다. 막대 그림은 skills.sh 주간 설치 수로 그렸다.",
                    "- '검증'은 설치 명령이 공식 문서 · README 본문에 글자 그대로 있는지 확인했다는 뜻이다. 실행해 본 것은 아니다."]


def figure(p, name):
    if not p.get("image") or short_url(p["image"]).startswith("github.githubassets.com"):
        return ""  # Gist 기본 미리보기는 내용이 아니라 호스트 로고다
    return (f'<figure class="specimen"><img src="{esc(p["image"])}" alt="{esc(name)} 미리보기 이미지" loading="lazy" '
            f'onerror="this.closest(\'figure\').remove()"><figcaption>출처 미리보기 · {esc(short_url(p["image"]).split("/")[0])}</figcaption></figure>')


def rail_links(urls):
    return "".join(f'<li><a href="{esc(u)}">{esc(short_url(u))}</a></li>' for u in dict.fromkeys(filter(None, urls)))


def build_html(items, kind, title, ps, dropped):
    idx, body, gidx = [], [], []
    for n, p in enumerate(ps, 1):
        name = p.get("name") or p.get("title")
        if kind in ("weekly", "daily") and p.get("_s") and not any(b.startswith('<header class="part" id="skills"') for b in body):
            body.append('<header class="part" id="skills"><h2>이번 주 써 볼 도구</h2><p>팀이 이번 주에 깔아서 덕을 볼 것.</p></header>')
        if p.get("_g") and not gidx:
            body.append('<header class="part" id="guides"><h2>실무 가이드</h2>'
                        '<p>이번 주 직접 해 볼 만한 것. 설치부터 실습, 막힐 때까지.</p></header>')
        when = f'<span class="when">{esc(ko_date(p.get("published")))}</span>' if p.get("published") else ""
        (gidx if p.get("_g") else idx).append(f'<li><a href="#p{n}"><span class="tag">{n}</span><span class="nm">{esc(name)}</span>'
                   f'<span class="ol">{inline(p.get("one_line", ""))}{when}</span></a></li>')
        dg = p.get("diagram") or ""
        if dg.count("-->") >= 4:  # 본문 단이 좁아 옆으로 긴 흐름도는 글자가 작아진다 — 세로로 돌린다
            dg = re.sub(r"^(\s*)(flowchart|graph) LR", r"\1\2 TD", dg, count=1)
        diagram = f'<figure class="diagram"><pre class="mermaid">{esc(dg)}</pre></figure>' if dg else ""
        if p.get("_g"):
            ho = p.get("hands_on") or {}
            cmd = (f'<div class="cmd"><pre><code>{esc(p["install"])}</code></pre><button type="button" class="copy">복사</button></div>'
                   if p.get("install") else "")
            prompts = "".join(f'<p class="plabel">{inline(pr.get("label", ""))}</p><div class="cmd"><pre class="prompt"><code>'
                              f'{esc(pr.get("text", ""))}</code></pre><button type="button" class="copy">복사</button></div>'
                              for pr in p.get("prompts", []))
            trouble = "".join(f'<dt>{inline(t.get("problem", ""))}</dt><dd>{inline(t.get("fix", ""))}</dd>' for t in p.get("troubleshooting", []))
            main = (figure(p, name) + f'<p class="summary">{inline(p["what"])}</p>'
                    + "<h3>설치 · 설정</h3>" + cmd
                    + ('<ol class="steps">' + "".join(f"<li>{inline(u)}</li>" for u in p["setup"]) + "</ol>" if p.get("setup") else "")
                    + f'<h3>실습</h3><p>{inline(ho.get("goal", ""))}</p>'
                    + '<ol class="steps">' + "".join(f"<li>{inline(u)}</li>" for u in ho.get("steps", [])) + "</ol>"
                    + diagram
                    + (f"<h3>바로 쓰는 프롬프트</h3>{prompts}" if prompts else "")
                    + (f'<h3>안 될 때</h3><dl class="trouble">{trouble}</dl>' if trouble else "")
                    + (f'<p class="caution"><strong>주의.</strong> {inline(p["caution"])}</p>' if p.get("caution") else ""))
            rail = (f'<p class="cls">실무 가이드</p><dl class="who"><dt>이런 분께</dt><dd>{inline(p.get("audience", ""))}</dd>'
                    f'<dt>읽고 나면</dt><dd>{inline(p.get("outcome", ""))}</dd></dl>'
                    + ('<p class="vf">설치 명령을 출처 본문과 대조함</p>' if p.get("install") else "")
                    + f'<ul>{rail_links([p.get("source_url"), p.get("install_source_url")] + p.get("sources", []))}</ul>')
        elif p.get("_s"):
            w = p.get("weekly")
            stats = ""
            if w:
                base = st.mean(w[-4:-1])
                stats = (f'<dl><dt>지난주 설치</dt><dd>{w[-1]:,}</dd><dt>직전 3주 평균</dt><dd>{base:,.0f}</dd>'
                         + (f'<dt>배수</dt><dd>{w[-1] / base:.1f}배</dd>' if base else "") + "</dl>")
            main = (figure(p, name) + f'<h3>무엇인가</h3><p>{inline(p["what"])}</p><h3>왜 지금</h3><p>{inline(p["why_now"])}</p>'
                    + (f'<figure class="chart">{week_chart(w, name)}</figure>' if w else "")
                    + f'<h3>설치</h3><div class="cmd"><pre><code>{esc(p["install"])}</code></pre>'
                      f'<button type="button" class="copy">복사</button></div>'
                    + "<h3>활용법</h3><ol class=\"steps\">" + "".join(f"<li>{inline(u)}</li>" for u in p.get("usage", [])) + "</ol>"
                    + diagram
                    + (f'<h3>예시 프롬프트</h3><pre class="prompt"><code>{esc(p["prompt_example"])}</code></pre>' if p.get("prompt_example") else "")
                    + (f'<p class="caution"><strong>주의.</strong> {inline(p["caution"])}</p>' if p.get("caution") else ""))
            rail = (f'<p class="cls">{esc(LABEL.get(p.get("category"), ""))}</p>{stats}'
                    f'<p class="vf">설치 명령을 출처 본문과 대조함</p><ul>{rail_links([p.get("repo_url"), p.get("install_source_url")])}</ul>')
        else:
            claim = p.get("claim_type", "official")
            prov = f'<span class="prov">{esc(CLAIM.get(claim, ""))}</span>' if claim != "official" else ""
            paras = [x for x in p["summary"].split("\n\n") if x.strip()]  # 종합 호 기사는 무슨 일 · 왜 중요 · 숫자 세 단락
            main = (figure(p, name) + f'<p class="summary {esc(claim)}">{prov}{inline(paras[0])}</p>'
                    + "".join(f"<p>{inline(x)}</p>" for x in paras[1:]) + diagram
                    + f'<h3>{"우리 팀에게는" if kind == "weekly" else "그래서 무엇을 하나"}</h3><p>{inline(p["so_what"])}</p>'
                    + ("<ul class=\"steps\">" + "".join(f"<li>{inline(h)}</li>" for h in p["how_to"]) + "</ul>" if p.get("how_to") else ""))
            rail = (f'<p class="cls">{"머리기사" if kind == "weekly" and n == 1 else esc(TLABEL.get(p.get("category"), ""))}</p>'
                    + (f'<p class="when">원문 {esc(ko_date(p.get("published")))}</p>' if p.get("published") else "")
                    + f'<p class="claim {esc(claim)}">{esc(CLAIM.get(claim, ""))}</p>'
                    f'<ul>{rail_links([p["source_url"]] + p.get("sources", []))}</ul>')
        body.append(f'<article class="plate" id="p{n}"><header><span class="tag">{n}</span><h2>{esc(name)}</h2></header>'
                    f'<div class="cols"><div class="main">{main}</div><aside class="rail">{rail}</aside></div></article>')
    memo = "".join(f"<li>{inline(m[2:])}</li>" for m in memo_lines(items, dropped))
    css = (HERE / "report.css").read_text()
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{css}</style></head><body>
<header class="mast"><h1>{esc(title.split(" — ")[0])}</h1><p class="dateline">{esc(items.get("_dateline", ""))}</p><p class="lede">{inline(items.get("headline", ""))}</p>
<nav aria-label="도판 목록"><ol class="index">{"".join(idx)}</ol>{
  f'<h2 class="index-part">실무 가이드</h2><ol class="index">{"".join(gidx)}</ol>' if gidx else ""}</nav></header>
<main>{"".join(body)}</main>
<footer class="memo"><h2>수집 메모</h2><ul>{memo}</ul></footer>
<script>
document.querySelectorAll('.copy').forEach(b=>b.addEventListener('click',async()=>{{
  try{{await navigator.clipboard.writeText(b.previousElementSibling.innerText);b.textContent='복사됨';}}
  catch(e){{b.textContent='복사 안 됨';}}
  setTimeout(()=>b.textContent='복사',1600);}}));
const io=new IntersectionObserver(es=>es.forEach(e=>{{if(e.isIntersecting){{e.target.classList.add('seen');io.unobserve(e.target);}}}}),{{threshold:.5}});
document.querySelectorAll('.chart').forEach(c=>io.observe(c));
</script>
<script type="module">
if(document.querySelector('.mermaid')){{
  const dark=matchMedia('(prefers-color-scheme: dark)').matches;
  try{{const m=(await import('https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs')).default;
    m.initialize({{startOnLoad:false,theme:'base',flowchart:{{useMaxWidth:false}},themeVariables:{{edgeLabelBackground:dark?'#1B201C':'#FBFCF9',fontFamily:'Pretendard, Noto Sans KR, sans-serif',fontSize:'14px',
      primaryColor:dark?'#232A25':'#FBFCF9',primaryBorderColor:dark?'#DDE3DC':'#1D241F',primaryTextColor:dark?'#EEF1EC':'#1D241F',
      lineColor:dark?'#AEB8AF':'#3E4A42',secondaryColor:dark?'#232A25':'#E6EAE3',tertiaryColor:dark?'#161A17':'#F1F3EE'}}}});
    await m.run({{querySelector:'.mermaid'}});}}catch(e){{document.documentElement.classList.add('no-mermaid');}}
}}
</script></body></html>"""


# ---------- Discord ----------

def discord(method, path, payload=None, files=None):
    url = "https://discord.com/api/v10" + path
    headers = {"Authorization": "Bot " + os.environ["DISCORD_BOT_TOKEN"], "User-Agent": "DiscordBot (trend-automation, 1)"}
    if files:
        boundary = "trendboundary7MA4YWxk"
        body = io.BytesIO()
        parts = [("payload_json", "application/json", None, json.dumps(payload).encode())]
        parts += [(f"files[{i}]", "application/octet-stream", Path(p).name, Path(p).read_bytes()) for i, p in enumerate(files)]
        for name, ctype, fname, data in parts:
            disp = f'form-data; name="{name}"' + (f'; filename="{fname}"' if fname else "")
            body.write(f"--{boundary}\r\nContent-Disposition: {disp}\r\nContent-Type: {ctype}\r\n\r\n".encode())
            body.write(data + b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())
        data, headers["Content-Type"] = body.getvalue(), f"multipart/form-data; boundary={boundary}"
    else:
        data = json.dumps(payload).encode() if payload is not None else None
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        return json.loads(body) if body else None


REPLY = {}  # poll이 지금 답하는 채널. 비어 있으면 #브리핑


def channels():
    """채널 이름 → ID. #브리핑은 보고 · 실패 알림, #인사이트는 인사이트 보고, #관리는 볼트 작업. 모두 대화를 받는다."""
    out = {"브리핑": os.environ["DISCORD_CHANNEL_ID"]}
    if os.environ.get("DISCORD_INSIGHT_CHANNEL_ID"):
        out["인사이트"] = os.environ["DISCORD_INSIGHT_CHANNEL_ID"]
    if os.environ.get("DISCORD_ADMIN_CHANNEL_ID"):
        out["관리"] = os.environ["DISCORD_ADMIN_CHANNEL_ID"]
    return out


def say(text, files=None, embeds=None, channel=None):
    payload = {"content": text[:1990], "allowed_mentions": {"parse": []}}
    if embeds:
        payload["embeds"] = embeds[:10]
    cid = channel or REPLY.get("cid") or os.environ["DISCORD_CHANNEL_ID"]
    return discord("POST", f"/channels/{cid}/messages", payload, files)


def say_long(text, files=None, channel=None):
    """Discord 한 메시지는 2,000자. 문단 경계로 나눠 보내고, 첨부는 마지막에 붙인다."""
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) > 1800 and cur:
            chunks.append(cur)
            cur = ""
        cur += para[:1800] + "\n\n"
    chunks.append(cur)
    for i, c in enumerate(chunks):
        say(c.strip() or "(빈 답)", files if i == len(chunks) - 1 else None, channel=channel)


def alert(msg):
    say(f"⚠️ **trend-automation 실패**\n{msg}", channel=os.environ["DISCORD_CHANNEL_ID"])




def deliver(want=None):
    s = load_json(STATE / "status.json", {})
    if s.get("last_generate") != today():
        raise RuntimeError(f"오늘({today()}) 생성된 보고서가 없음. 마지막 생성: {s.get('last_generate')}")
    if want and s["kind"] != want:  # 금요일: 종합 생성이 실패했을 때 07:00 매일 판을 종합 대신 보내지 않는다
        raise RuntimeError(f"오늘 마지막으로 만든 호가 {EDITION[s['kind']]}라 {EDITION[want]}를 보내지 않음")
    out, kind = Path(s["dir"]), s["kind"]
    items = json.loads((out / "items.json").read_text())
    d = dt.date.fromisoformat(today())
    web = soft("publish", lambda: publish(out, kind), errs := [])
    # 메시지 본문은 편집자(LLM)가 direction.md의 형식 규칙대로 써 둔다 — 형식 피드백이 코드 수정 없이 반영된다
    text = items.get("discord") or "\n".join(
        [f"**{EDITION[kind]} — {period(today())}**", items.get("headline", ""), ""]
        + [f"{n}. **{p.get('name') or p.get('title')}**: {p.get('one_line', '')}" for n, p in enumerate(plates(items, kind), 1)]
        + ["", "{WEB}"])
    say(text.replace("{WEB}", web or "").strip())
    if errs:
        alert("웹 게시 실패: " + "; ".join(errs))
    if s["items"] < MIN_ITEMS[kind]:
        alert(f"보고서가 부실함 (검증 통과 {s['items']}건). {out / 'claude.log'} 확인")
    if os.environ.get("HC_URL"):
        fetch(os.environ["HC_URL"])
    log(f"deliver {out.name}")


PAGES_DIR = Path.home() / ".local" / "share" / "trend-automation" / "pages"


def publish(out, kind):
    """GitHub Pages 저장소에 보고서 html을 올리고 주소를 돌려준다. 저장소가 준비되지 않았으면 None."""
    if not (PAGES_DIR / ".git").exists():
        return None
    cfg = load_json(PAGES_DIR / "site.json", {})
    dst = PAGES_DIR / out.name
    dst.mkdir(exist_ok=True)
    (dst / "index.html").write_text((out / "report.html").read_text())
    reports = sorted((d.name for d in PAGES_DIR.iterdir() if (d / "index.html").exists()), reverse=True)
    (PAGES_DIR / "index.html").write_text(
        '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>AI 브리핑</title><body style="font:17px/1.8 sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem">'
        "<h1>AI 브리핑</h1><ul>" + "".join(f'<li><a href="{r}/">{r}</a></li>' for r in reports) + "</ul></body></html>")
    git = lambda *a: subprocess.run(["git", "-C", str(PAGES_DIR), *a], check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    git("add", "-A")
    if subprocess.run(["git", "-C", str(PAGES_DIR), "diff", "--cached", "--quiet"]).returncode:
        git("commit", "-m", f"report: {out.name}")
        git("push", "-q", "origin", "HEAD")
    return f"{cfg['base_url'].rstrip('/')}/{out.name}/"


HELP = ("말로 하면 됩니다. 방향 · 피드백은 다음 호에 반영하고, 궁금한 건 조사해서 답합니다.\n"
        "바로 실행하는 명령: `status` · `rerun daily|weekly` · `add-source <RSS URL> <이름>` · `reset`(대화 새로 시작) · `help`")


def poll():
    owner = os.environ["DISCORD_OWNER_ID"]
    seen = load_json(STATE / "poll.json", {})
    seen = seen if "last_id" not in seen else {}  # 옛 형식(채널 하나)은 버린다 — 그 채널은 지워졌다
    for name, cid in channels().items():
        last = seen.get(cid)
        msgs = discord("GET", f"/channels/{cid}/messages?limit=50" + (f"&after={last}" if last else ""))
        if not last:  # 처음 보는 채널은 과거 메시지를 처리하지 않는다
            seen[cid] = msgs[0]["id"] if msgs else "0"
            save_json(STATE / "poll.json", seen)
            continue
        REPLY.update(cid=cid, name=name)
        for m in sorted(msgs, key=lambda m: int(m["id"])):
            seen[cid] = m["id"]
            save_json(STATE / "poll.json", seen)
            if m["author"]["id"] != owner:  # 봇 자신과 다른 사람의 메시지는 처리하지 않는다
                continue
            handle(m["content"].strip(), name, [(a["url"], a["filename"]) for a in m.get("attachments", [])])
    REPLY.clear()


def handle(text, channel, files=()):
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.lower(), arg.strip()
    log(f"poll msg #{channel} {text[:60]}")
    try:
        if cmd == "status":
            say("```json\n" + json.dumps(load_json(STATE / "status.json", {}), ensure_ascii=False, indent=1) + "\n```")
        elif cmd == "rerun" and arg in EDITION:
            rerun(arg)
        elif cmd == "add-source" and arg:
            url, _, label = arg.partition(" ")
            feed_items(url, label or url, dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30))  # 읽히는 피드인지 먼저 확인
            with open(HERE / "sources.txt", "a") as f:
                f.write(f"{url} {label or url}\n")
            say(f"✅ 출처 추가: {label or url}")
        elif cmd == "reset":
            st = load_json(STATE / "chat.json", {})
            st.pop(channel, None)
            save_json(STATE / "chat.json", st)
            say(f"#{channel} 대화를 새로 시작합니다. 방향 메모는 그대로 둡니다.")
        elif cmd == "help" or not (text or files):
            say(HELP if text else "메시지 본문이 비어 있습니다. 봇 설정에서 Message Content Intent가 켜져 있는지 확인하세요.")
        else:
            chat(text, channel, files)
    except Exception as e:  # noqa: BLE001 — 한 메시지의 실패를 알리고 다음 메시지로
        log(f"ERROR poll {e}")
        say(f"⚠️ 처리 실패: {e}")


def rerun(kind):
    say(f"⏳ {EDITION[kind]} 다시 만드는 중 (5~30분)")
    generate(kind)
    deliver()


NOTES = {"브리핑": "direction.md", "인사이트": "insight_direction.md"}  # 채널마다 방향 메모가 따로 있다
# 대화가 쓸 수 있는 파일. 어느 채널에서든 두 메모를 고치고, 코드 · 일정 변경은 requests.md에 적어 두면 PC의 Claude Code 세션이 처리한다
WRITABLE = [*NOTES.values(), "requests.md"]


def attach(files):
    """Discord 첨부를 state/attachments/에 내려받고 경로 목록을 돌려준다. claude -p가 Read로 이미지를 본다."""
    d = STATE / "attachments"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for url, name in files:
        p = d / f"{dt.datetime.now(KST):%Y%m%d-%H%M%S}-{Path(name).name}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
            p.write_bytes(r.read())
        out.append(p)
    return out


def chat(text, channel="브리핑", files=()):
    """대화 한 턴 = 새 claude -p 프로세스 하나. 맥락은 채널마다 --resume으로 잇는다.
    쓰기는 #브리핑 · #인사이트면 메모 파일만, #관리면 볼트 안 Edit · Write만 허용한다(Bash 없음 — 커밋 · push는 사용자 몫)."""
    discord("POST", f"/channels/{channels()[channel]}/typing")
    state = load_json(STATE / "chat.json", {})
    state = state if "session_id" not in state else {"브리핑": state["session_id"]}
    if paths := attach(files):
        text += "\n\n첨부 파일(Read로 열어 볼 것):\n" + "\n".join(f"- {p}" for p in paths)
    if channel == "관리":
        cwd, system = VAULT, (HERE / "prompt_admin.md").read_text().replace("{TODAY}", today())
        tools = ["Read,Glob,Grep,WebFetch,WebSearch", f"Edit(/{VAULT}/**)", f"Write(/{VAULT}/**)"]
    else:
        note = NOTES[channel]
        recent = sorted(REPORTS.glob("20*-*/report.md"))[-4:]
        cwd, system = HERE, ((HERE / "prompt_chat.md").read_text().replace("{TODAY}", today())
                             .replace("{REPORTS}", "\n".join(f"- {p}" for p in recent) or "(아직 없음)")
                             .replace("{CHANNEL}", channel).replace("{NOTE}", note))
        tools = ["Read,Glob,Grep,WebFetch,WebSearch", *(f"Edit(./{m})" for m in WRITABLE)]
    # 메시지는 stdin으로 넘긴다 — 인자로 넘기면 "-"로 시작하는 글이 옵션으로 읽혀 실패한다
    cmd = ["timeout", "20m", "claude", "-p", "--setting-sources", "user", "--strict-mcp-config", "--append-system-prompt", system,
           "--allowedTools", ",".join(tools), "--add-dir", str(REPORTS), str(STATE / "attachments"),
           "--permission-prompts", "none", "--max-turns", "40", "--output-format", "json"]
    if state.get(channel):
        cmd += ["--resume", state[channel]]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, input=text)
    try:
        res = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude -p exit {r.returncode}: {r.stderr[-300:]}") from None
    state[channel] = res.get("session_id")
    save_json(STATE / "chat.json", state)
    reply = res.get("result") or "(답 없음)"
    actions = re.findall(r"^ACTION: rerun (trends|skills)\s*$", reply, re.M)
    clean = re.sub(r"^ACTION: .*$", "", reply, flags=re.M).strip()
    say_long(clean)
    if re.search(r"^ACTION: 제보\s*$", reply, re.M):
        to_raw_report(text, clean, channel)
    for kind in dict.fromkeys(actions):
        rerun(kind)


def discord_setup():
    """봇이 들어간 서버의 글 채널 하나를 고르고, 그 채널에 처음 글을 쓴 사람을 주인으로 적는다."""
    me = discord("GET", "/users/@me")
    guilds = discord("GET", "/users/@me/guilds")
    perms = 64 + 1024 + 2048 + 16384 + 32768 + 65536  # 반응 · 채널 보기 · 쓰기 · 링크 미리보기 · 첨부 · 기록 읽기
    invite = f"https://discord.com/oauth2/authorize?client_id={me['id']}&scope=bot&permissions={perms}"
    if not guilds:
        sys.exit(f"봇이 아직 어느 서버에도 없다. 이 주소를 열어 서버에 초대한 뒤 다시 실행:\n{invite}")
    chans = [c for g in guilds for c in discord("GET", f"/guilds/{g['id']}/channels") if c["type"] == 0]
    pick = next((c for c in chans if re.search(r"trend|트렌드|ai|report|보고", c["name"], re.I)), chans[0])
    os.environ["DISCORD_CHANNEL_ID"] = pick["id"]
    print(f"채널: #{pick['name']} ({pick['id']}). 다른 채널을 쓰려면 이름에 'trend'를 넣고 다시 실행")
    hello = say("연결 확인 중입니다. 이 채널에 아무 말이나 한 줄 보내 주세요 (5분 안에).")
    for _ in range(60):
        time.sleep(5)
        msgs = discord("GET", f"/channels/{pick['id']}/messages?after={hello['id']}&limit=10")
        human = [m for m in msgs if not m["author"].get("bot")]
        if human:
            m = human[-1]
            break
    else:
        sys.exit("5분 안에 메시지가 없었다. 다시 실행")
    values = {"DISCORD_CHANNEL_ID": pick["id"], "DISCORD_OWNER_ID": m["author"]["id"]}
    lines = [l for l in ENV_FILE.read_text().splitlines() if l.split("=", 1)[0] not in values]
    ENV_FILE.write_text("\n".join(lines + [f"{k}={v}" for k, v in values.items()]) + "\n")
    save_json(STATE / "poll.json", {"last_id": m["id"]})
    if not m["content"]:
        say("받았는데 본문이 비어 있습니다. Developer Portal → Bot → Message Content Intent를 켜 주세요. 켜기 전엔 대화가 안 됩니다.")
        sys.exit("Message Content Intent가 꺼져 있다")
    say(f"연결 완료. 주인: {m['author']['username']}.\n" + HELP)
    print(f"완료: 주인 {m['author']['username']} ({m['author']['id']})")


# ---------- 자기검사 ----------

def selftest():
    row = lambda src, w, inst=0: {"source": src, "name": src if "skills" in src else src.split("/")[-1],
                                  "installs": str(inst), "weekly_installs": "|".join(map(str, w))}
    now = dt.date(2026, 10, 30)
    rows = [row("a/flat", [5000] * 8), row("b/surge", [100, 100, 900, 1000, 1100, 1000, 4000]),
            row("c/tiny", [1, 1, 1, 1, 0, 0, 50]), row("d/new", [0, 0, 0, 0, 0, 0, 2500]),
            row("o1/superpowers", [0, 0, 0, 100, 100, 100, 9000], 10), row("o2/superpowers", [0, 0, 0, 100, 100, 100, 9000], 5),
            row("o3/superpowers", [0, 0, 0, 100, 100, 100, 9000], 99), row("e/cool", [0, 0, 0, 100, 100, 100, 9000]),
            row("g1/skills", [10] * 7), row("g2/skills", [10] * 7), row("g3/skills", [100, 100, 100, 100, 100, 100, 5000]),
            row("s/x1", [7, 7, 7, 7, 7, 7, 7777]), row("s/x2", [7, 7, 7, 7, 7, 7, 7777]), row("s/x3", [7, 7, 7, 7, 7, 7, 7777])]
    hits, new, skipped = surge(rows, {"e/cool": "2026-10-20"}, now)
    got = {h["repo"] for h in hits}
    assert got == {"b/surge", "o3/superpowers", "g3/skills"}, got   # 급등만, 복제본 중 원본(최다 설치)만
    assert [n["repo"] for n in new] == ["d/new"], new                # 이력 없는 진입은 따로
    assert skipped == {"복제본": 2, "조작의심": 3, "쿨다운": 1}, skipped
    svg = week_chart([100, 100, 900, 1000, 1100, 1000, 4000], "x")
    assert "지난주 4,000 · 3.9배" in svg and "6주 전</text>" in svg and svg.count("<rect") == 7, svg
    items = {"headline": "h", "skills": [
        {"name": "<b>", "verified": True, "what": "w **강조**", "why_now": "y", "install": "npx a <b>", "usage": ["u"],
         "weekly": [1, 2, 3, 4, 50], "one_line": "o"},
        {"name": "dropped", "verified": False}], "trends": []}
    h = build_html(items, "skills", "T", plates(items, "skills"), ["dropped"])
    assert "npx a &lt;b&gt;" in h and "<strong>강조</strong>" in h and 'id="p1"' in h and 'id="p2"' not in h, h
    assert "dropped" in h  # 뺀 항목은 수집 메모에 남는다
    assert [week_dir(d).name for d in ("2026-11-09", "2026-11-13", "2026-11-14", "2026-11-15")] == \
        ["2026-11-09-daily", "2026-11-09-daily", "2026-11-16-daily", "2026-11-16-daily"]  # 토 · 일은 다음 주 판
    assert period("2026-10-30") == "10월 다섯째 주" and period("2026-11-03") == "11월 첫째 주" and period("2026-09-28") == "10월 첫째 주"
    items = {"headline": "h", "trends": [{"title": "t", "verified": True, "summary": "s", "so_what": "w", "source_url": "u"}],
             "guides": [{"title": "g", "verified": True, "what": "x", "install": "npx g", "setup": ["a"], "audience": "누구",
                         "outcome": "결과", "hands_on": {"goal": "목표", "steps": ["1"]}, "prompts": [{"label": "L", "text": "P <x>"}],
                         "troubleshooting": [{"problem": "안 됨", "fix": "고침"}], "source_url": "u"}]}
    ps = plates(items, "trends")
    h = build_html(items, "trends", "T", ps, [])
    assert [bool(p.get("_g")) for p in ps] == [False, True] and 'id="guides"' in h and "<dt>안 됨</dt>" in h and "P &lt;x&gt;" in h, h
    items["skills"] = [{"name": "s", "verified": True, "what": "w", "why_now": "n", "install": "npx s", "usage": []}]
    ps = plates(items, "weekly")
    h = build_html(items, "weekly", "T", ps, [])
    assert [p.get("title") or p.get("name") for p in ps] == ["t", "s", "g"] and 'id="skills"' in h and "<h3>왜 지금</h3>" in h, h
    t = drop_stale({"trends": [{"title": "a", "published": "2026-10-26", "verified": True},
                               {"title": "b", "published": "2026-11-02", "verified": True},
                               {"title": "c", "verified": True}]}, "2026-10-30")
    assert [x["verified"] for x in t["trends"]] == [False, True, False] and ko_date("2026-11-02") == "11월 2일"
    maj = lambda tag: bool(re.search(r"(?:^|[^\d.])v?[1-9]\d*\.0(?:\.0)?$", tag))
    assert maj("v1.0.0") and maj("2.0") and maj("pkg@3.0.0") and not maj("v1.0.2") and not maj("v0.0.15") and not maj("v1.1.0")
    print("selftest ok")


def poll_down_long(e, minutes=30):
    """폴링 실패가 30분 넘게 이어졌을 때만 True(→ 알림). 처음 한 번만 알리고, 성공하면 기록을 지운다."""
    f = STATE / "poll_fail.json"
    st = load_json(f, {})
    now = dt.datetime.now(KST)
    first = dt.datetime.fromisoformat(st.get("since", now.isoformat()))
    alert_now = (now - first).total_seconds() > minutes * 60 and not st.get("alerted")
    save_json(f, {"since": first.isoformat(), "alerted": st.get("alerted") or alert_now, "last": str(e)[:200]})
    return alert_now


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "collect":
            print(collect(sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--date" else today()))
        elif cmd == "generate" and sys.argv[2:3] and sys.argv[2] in EDITION:
            generate(sys.argv[2])
        elif cmd == "deliver":
            deliver(sys.argv[2] if len(sys.argv) > 2 else None)
        elif cmd == "poll":
            poll()
            (STATE / "poll_fail.json").unlink(missing_ok=True)
        elif cmd == "insight":
            import insight
            insight.run(send="--no-send" not in sys.argv)
        elif cmd == "discord-setup":
            discord_setup()
        elif cmd == "alert":
            alert(" ".join(sys.argv[2:]))
        elif cmd == "selftest":
            selftest()
        else:
            sys.exit(__doc__)
    except Exception as e:
        log(f"ERROR {cmd}: {e}")
        if cmd == "poll" and not poll_down_long(e):
            raise  # 1분마다 도는 폴링의 일시적 네트워크 오류는 알리지 않는다
        if cmd in ("generate", "deliver", "poll", "insight") and os.environ.get("DISCORD_CHANNEL_ID"):
            try:
                alert(f"`{' '.join(sys.argv[1:])}` — {e}" + (" (30분 넘게 계속 실패 중)" if cmd == "poll" else ""))
            except Exception as e2:  # noqa: BLE001
                log(f"ERROR alert 전송도 실패: {e2}")
        raise
