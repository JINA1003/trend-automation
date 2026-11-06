"""주간 인사이트 — 토요일 09:00 #인사이트로 보낸다. `python3 trend.py insight [--no-send]`

① 위키 연결 발견: 링크로 이어져 있지 않지만 이어져야 할 개념 쌍 3개
② 반대 의견: 최근 내린 결정 하나에 대한 반대 근거
③ 질문 하나에 답: 지난주 Discord에서 나온 질문 · 위키의 [조사필요] 중 하나를 조사해 답

후보는 결정론적으로 모아 넘기고(이 파일), 고르기와 쓰기는 claude -p가 한다(prompt_insight.md).
결과: 03.보고서/<날짜>-insight/{insight.json, report.md}.
쓰기 권한은 절대 경로(`Edit(//…)`)로 준다 — `--add-dir`로 볼트 전체를 연 세션에서는 `./` 규칙이 맞지 않아 거부됐다(2026-11-03). 공개 웹에는 올리지 않는다(업무 결정 · 위키가 섞임).
"""
import datetime as dt
import json
import re
import subprocess

import trend

VAULT = trend.VAULT
WIKI = VAULT / "wiki"
LINK = re.compile(r"\[\[([^\]|#]+)")
DATED_ROW = re.compile(r"^\|\s*(D-\d{3})?\s*([^|]*?)\s*\|.*?(\d{4}-\d{2}-\d{2})", re.M)


def wiki_graph():
    """개념 페이지(topics · projects · ideas · entities)의 제목 · 태그 · 나가는 링크."""
    pages = {}
    for folder in ("topics", "projects", "ideas", "entities"):
        for p in sorted((WIKI / folder).glob("*.md")):
            text = p.read_text(errors="ignore")
            body = text.split("\n---", 2)[-1]
            tags = re.search(r"^tags:\s*\[(.*?)\]", text, re.M)
            title = re.search(r"^# (.+)$", text, re.M)
            pages[p.stem] = {"folder": folder, "title": title.group(1) if title else p.stem,
                             "tags": [t.strip() for t in tags.group(1).split(",")] if tags else [],
                             "links": sorted({l.split("/")[-1].strip() for l in LINK.findall(body)} - {p.stem})}
    return pages


def recent_decisions(since):
    """work 프로젝트의 결정 기록 · 진행 현황 결정 표에서 최근 날짜가 적힌 행."""
    out = []
    for f in sorted(VAULT.glob("work/*/00. 결정 기록.md")) + sorted(VAULT.glob("work/*/00. 진행 현황.md")):
        for line in f.read_text(errors="ignore").splitlines():
            if not line.startswith("|") or "---" in line[:6]:
                continue
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", line)
            if dates and max(dates) >= since and len(line) > 40:
                out.append({"file": str(f.relative_to(VAULT)), "row": line[:600]})
    return out[-40:]


def open_questions(since_ts):
    qs = []
    for name, cid in trend.channels().items():
        try:
            for m in trend.discord("GET", f"/channels/{cid}/messages?limit=100"):
                if (m["author"]["id"] == trend.os.environ["DISCORD_OWNER_ID"] and "?" in m["content"]
                        and m["timestamp"] >= since_ts):
                    qs.append({"origin": f"Discord #{name} {m['timestamp'][:10]}", "q": m["content"][:400]})
        except Exception as e:  # noqa: BLE001 — 질문 수집이 안 되면 위키 쪽만 쓴다
            trend.log(f"WARN insight questions #{name}: {e}")
    for p in sorted(WIKI.rglob("*.md")):
        if p.name in ("log.md", "lint-report.md"):
            continue
        for line in p.read_text(errors="ignore").splitlines():
            if "[조사필요]" in line:
                qs.append({"origin": f"wiki/{p.relative_to(WIKI)}", "q": line.strip()[:400]})
    return qs


def run(send=True):
    date = trend.today()
    now = dt.date.fromisoformat(date)
    out = trend.REPORTS / f"{date}-insight"
    out.mkdir(parents=True, exist_ok=True)
    (out / "insight.json").unlink(missing_ok=True)
    hist = trend.load_json(trend.STATE / "insight.json", {"pairs": [], "decisions": [], "questions": []})
    cand = {
        "date": date,
        "wiki_pages": wiki_graph(),
        "recent_decisions": recent_decisions((now - dt.timedelta(days=21)).isoformat()),
        "questions": open_questions((now - dt.timedelta(days=8)).isoformat()),
        "already_used": hist,  # 지난 인사이트에서 쓴 쌍 · 결정 · 질문은 다시 고르지 않는다
    }
    trend.save_json(out / "candidates.json", cand)
    prompt = ((trend.HERE / "prompt_insight.md").read_text().replace("{DATE}", date)
              .replace("{CANDIDATES}", str(out / "candidates.json")).replace("{VAULT}", str(VAULT))
              .replace("{DIRECTION}", (trend.HERE / "insight_direction.md").read_text())
              .replace("{PERIOD}", trend.period(date)))
    for attempt in (1, 2):
        trend.log(f"claude -p insight attempt {attempt}")
        r = subprocess.run(["timeout", "45m", "claude", "-p", prompt, "--setting-sources", "user", "--strict-mcp-config",
                            "--add-dir", str(VAULT), "--allowedTools", f"Read,Glob,Grep,WebFetch,WebSearch,Edit(/{out}/insight.json)",
                            "--permission-prompts", "none", "--max-turns", "120", "--output-format", "json"],
                           cwd=out, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        (out / "claude.log").write_text(r.stdout[-20000:] + "\n--- stderr\n" + r.stderr[-5000:])
        if (out / "insight.json").exists():
            break
    else:
        raise RuntimeError(f"insight 생성 실패 (exit {r.returncode}), 로그: {out / 'claude.log'}")
    ins = json.loads((out / "insight.json").read_text())
    (out / "report.md").write_text(render(ins, date))
    hist["pairs"] += [[c.get("a"), c.get("b")] for c in ins.get("connections", [])]
    hist["decisions"] += [ins.get("counter", {}).get("decision", "")]
    hist["questions"] += [ins.get("question", {}).get("q", "")]
    trend.save_json(trend.STATE / "insight.json", {k: v[-60:] for k, v in hist.items()})
    if send:
        trend.say_long(ins["discord"], channel=trend.channels()["인사이트"])
    trend.log(f"insight {date} done → {out}")


def render(ins, date):
    md = [f"---\n상태: 기록\n공개범위: 내부\n---\n# 주간 인사이트 — {date}", "", "## 1. 위키 연결 발견", ""]
    for n, c in enumerate(ins.get("connections", []), 1):
        md += [f"### 1.{n} {c.get('a')} ↔ {c.get('b')}", "", c.get("why", ""), "", f"- 제안: {c.get('suggest', '')}", ""]
    c = ins.get("counter", {})
    md += ["## 2. 반대 의견", "", f"**결정.** {c.get('decision', '')} ({c.get('source', '')})", "", c.get("summary", ""), ""]
    md += [f"- {p}" for p in c.get("counterpoints", [])] + ["", f"**판단.** {c.get('verdict', '')}", ""]
    md += ["출처: " + " · ".join(c.get("sources", [])), ""]
    q = ins.get("question", {})
    md += ["## 3. 질문 하나에 답", "", f"**질문.** {q.get('q', '')} ({q.get('origin', '')})", "", q.get("answer", ""), ""]
    md += ["출처: " + " · ".join(q.get("sources", [])), ""]
    if q.get("wiki_page"):
        md += [f"- 위키 페이지 제안: `{q['wiki_page']}` (저장은 사용자 확인 뒤)", ""]
    return "\n".join(md)
