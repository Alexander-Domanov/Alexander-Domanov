#!/usr/bin/env python3
"""Generate every image used by the profile README from public GitHub data.

Design constraints this file obeys on purpose:

* **stdlib only** — no pip install step, so the workflow cannot rot when a
  dependency publishes a breaking release.
* **own assets only** — nothing is loaded from a third-party widget service,
  so a dead Vercel deployment can never put a broken frame on the profile.
* **light + dark variants** — GitHub renders READMEs in both themes; each
  figure is emitted twice and the README selects with <picture>.
* **deterministic apart from the date stamp** — the same input produces byte
  identical output, so the daily workflow only commits real change.

Usage:
    python3 scripts/profile_svg.py                     # write assets/out/
    python3 scripts/profile_svg.py --dry-run           # report, write nothing
    GITHUB_TOKEN=... python3 scripts/profile_svg.py    # raise API rate limit
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
SANS = "system-ui,-apple-system,sans-serif"
SHIPPED_DAYS = 120          # how far back the "last shipped" board looks
SHIPPED_ROWS = 5


# --------------------------------------------------------------------------
# tiny SVG helpers
# --------------------------------------------------------------------------
def esc(value) -> str:
    return escape(str(value), {'"': "&quot;"})


def txt(x, y, s, size, fill, mono=True, anchor="start", weight=None,
        ls=None, opacity=None) -> str:
    parts = [f'x="{x:.1f}"', f'y="{y:.1f}"',
             f'font-family="{MONO if mono else SANS}"', f'font-size="{size}"',
             f'fill="{fill}"']
    if anchor != "start":
        parts.append(f'text-anchor="{anchor}"')
    if weight:
        parts.append(f'font-weight="{weight}"')
    if ls:
        parts.append(f'letter-spacing="{ls}"')
    if opacity is not None:
        parts.append(f'opacity="{opacity}"')
    return f'<text {" ".join(parts)}>{esc(s)}</text>'


def char_w(size, mono=True) -> float:
    return size * (0.60 if mono else 0.515)


def fit(s, size, max_px, mono=True):
    """Trim to the longest prefix that fits, with a trailing ellipsis."""
    s = " ".join(str(s).split())
    if char_w(size, mono) * len(s) <= max_px:
        return s
    keep = max(1, int(max_px / char_w(size, mono)) - 1)
    return s[:keep].rstrip(" ,;:·-") + "…"


def svg(width, height, palette, body, label, grid=True) -> str:
    defs = ""
    background = f'<rect width="{width}" height="{height}" fill="{palette["panel"]}"/>'
    if grid:
        defs = (f'<defs><pattern id="g" width="20" height="20" patternUnits="userSpaceOnUse">'
                f'<path d="M20 0H0V20" fill="none" stroke="{palette["grid"]}" stroke-width="1"/>'
                f'</pattern></defs>')
        background += f'<rect width="{width}" height="{height}" fill="url(#g)"/>'
    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{esc(label)}">\n'
            f'{defs}{background}\n{body}\n</svg>\n')


# --------------------------------------------------------------------------
# GitHub data (public endpoints)
# --------------------------------------------------------------------------
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def get(url: str):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "profile-svg-generator",
        **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
    })
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.load(resp)


def get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "profile-svg-generator"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_profile(login: str) -> dict:
    return get(f"{API}/users/{login}")


def fetch_repos(login: str) -> list:
    out, page = [], 1
    while page <= 5:
        batch = get(f"{API}/users/{login}/repos?per_page=100&sort=pushed&page={page}")
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return [r for r in out if not r.get("fork")]


def fetch_contributions(login: str) -> dict:
    """Daily contribution counts from the public calendar.

    The calendar tooltips are the only unauthenticated place these numbers are
    published, and they are exact: their sum matches the yearly total GitHub
    prints above the graph.
    """
    html = get_text(f"https://github.com/users/{login}/contributions")
    by_component = {}
    for m in re.finditer(
            r'id="contribution-day-component-(\d+-\d+)".*?'
            r'<tool-tip[^>]*>([^<]*)</tool-tip>', html, re.S):
        by_component[m.group(1)] = m.group(2).strip()
    pairs = re.findall(
        r'data-date="(\d{4}-\d{2}-\d{2})"[^>]*?id="contribution-day-component-(\d+-\d+)"',
        html, re.S)
    date_by_component = {component: iso for iso, component in pairs}
    if not date_by_component:
        # attribute order is not guaranteed by GitHub; try the other direction
        date_by_component = dict(re.findall(
            r'id="contribution-day-component-(\d+-\d+)"[^>]*?data-date="(\d{4}-\d{2}-\d{2})"',
            html, re.S))
    counts = {}
    for component, iso in date_by_component.items():
        raw = by_component.get(component, "")
        m = re.match(r"^(\d+)\s+contribution", raw)
        counts[date.fromisoformat(iso)] = int(m.group(1)) if m else 0
    if not counts:
        raise RuntimeError("could not parse the contribution calendar")
    return counts


WEEKDAY_NOISE = re.compile(
    r"\s*\((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[^)]*\)\s*$", re.I)


def clean_message(raw: str) -> str:
    """First line of a commit message, minus the date noise some editors append."""
    line = (raw or "").split("\n")[0].strip()
    return WEEKDAY_NOISE.sub("", line).strip()


def fetch_shipped(login: str, repos: list, days: int = SHIPPED_DAYS) -> list:
    """Own, non-merge commits from the last `days`, newest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items = []
    for repo in repos[:12]:
        if repo["name"].lower() == login.lower():
            continue          # the profile repo itself is not news
        pushed = repo.get("pushed_at")
        if not pushed:
            continue
        if datetime.fromisoformat(pushed.replace("Z", "+00:00")) < cutoff:
            continue
        try:
            commits = get(f"{API}/repos/{repo['full_name']}/commits"
                          f"?author={login}&per_page=20")
        except urllib.error.HTTPError:
            continue
        for c in commits:
            msg = clean_message(c["commit"]["message"])
            when = datetime.fromisoformat(c["commit"]["author"]["date"].replace("Z", "+00:00"))
            if when < cutoff or not msg or msg.lower().startswith("merge "):
                continue
            items.append({"repo": repo["name"], "date": when.date(), "message": msg,
                          "sha": c["sha"][:7]})
    items.sort(key=lambda i: (i["date"], i["sha"]), reverse=True)
    seen_msgs, by_repo = set(), {}
    for it in items:
        if it["message"] in seen_msgs:
            continue
        seen_msgs.add(it["message"])
        by_repo.setdefault(it["repo"], []).append(it)
    strong = re.compile(r"^(feat|fix|perf|refactor|test|ci|build|chore|style|revert)[(:]")
    # one line per repository first, so the board shows breadth rather than one
    # repo's five commits from a single afternoon — and prefer a commit that
    # says what was built over a README touch-up.
    picked = []
    for repo, commits in sorted(by_repo.items(), key=lambda kv: kv[1][0]["date"], reverse=True):
        best = next((c for c in commits if strong.match(c["message"])), commits[0])
        picked.append(best)
        if len(picked) == SHIPPED_ROWS:
            break
    picked.sort(key=lambda i: i["date"], reverse=True)
    return picked


def fetch_languages(repos: list) -> Counter:
    cutoff = datetime.now(timezone.utc) - timedelta(days=730)
    total = Counter()
    for repo in repos:
        pushed = repo.get("pushed_at")
        if not pushed:
            continue
        if datetime.fromisoformat(pushed.replace("Z", "+00:00")) < cutoff:
            continue
        try:
            for lang, size in get(f"{API}/repos/{repo['full_name']}/languages").items():
                total[lang] += size
        except urllib.error.HTTPError:
            continue
    return total


# --------------------------------------------------------------------------
# aggregates
# --------------------------------------------------------------------------
def month_keys(n=12, today=None):
    today = today or date.today()
    keys, y, m = [], today.year, today.month
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))


def aggregate(counts: dict) -> dict:
    months = month_keys(12)
    by_month = Counter()
    for day, n in counts.items():
        key = f"{day.year:04d}-{day.month:02d}"
        if key in months and n:
            by_month[key] += 1
    active_days = sorted(d for d, n in counts.items() if n)
    longest = run = 0
    for prev, cur in zip(active_days, active_days[1:]):
        run = run + 1 if (cur - prev).days == 1 else 0
        longest = max(longest, run)
    return {
        "months": [(k, by_month.get(k, 0)) for k in months],
        "contributions": sum(counts.values()),
        "active_days": len(active_days),
        "longest_run": longest + 1 if active_days else 0,
        "tracked_days": len(counts),
        "last_active": active_days[-1].isoformat() if active_days else None,
    }


# --------------------------------------------------------------------------
# figures
# --------------------------------------------------------------------------
def fig_hero(p, theme, stats, profile, stamp) -> str:
    L = theme["labels"]
    W, H = 900, 250
    body = [f'<rect x="8.5" y="8.5" width="{W-17}" height="{H-17}" fill="none" '
            f'stroke="{p["line_strong"]}"/>']
    body.append(txt(28, 40, f'{L["hero_kicker"]} / REV {stamp}', 11, p["dim"], ls=3))
    body.append(txt(28, 98, profile["name"].upper(), 30, p["text"]))
    body.append(txt(28, 128, L["hero_role"], 13, p["accent"]))
    body.append(f'<circle cx="30" cy="156" r="2.5" fill="{p["accent"]}"/>'
                f'<line x1="30" y1="156" x2="220" y2="156" stroke="{p["line_strong"]}"/>')
    body.append(txt(230, 160, L["hero_note_1"], 11, p["dim"]))
    body.append(f'<circle cx="30" cy="182" r="2.5" fill="{p["accent"]}"/>'
                f'<line x1="30" y1="182" x2="220" y2="182" stroke="{p["line_strong"]}"/>')
    body.append(txt(230, 186, L["hero_note_2"], 11, p["dim"]))
    body.append(f'<circle cx="30" cy="208" r="2.5" fill="{p["warm"]}"/>'
                f'<line x1="30" y1="208" x2="220" y2="208" stroke="{p["line_strong"]}"/>')
    body.append(txt(230, 212, L["hero_status"], 11, p["warm"]))
    # title block, bottom right
    bx, by, bw, bh = 560, 150, 324, 68
    body.append(f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" fill="{p["panel"]}" '
                f'stroke="{p["line_strong"]}"/>')
    cells = L["titleblock"]
    for row in range(2):
        for col in range(2):
            key, value = cells[row * 2 + col]
            cx, cy = bx + col * 162, by + row * 34
            body.append(f'<line x1="{cx+162}" y1="{cy}" x2="{cx+162}" y2="{cy+34}" '
                        f'stroke="{p["line"]}"/>' if col == 0 else "")
            body.append(f'<line x1="{bx}" y1="{cy+34}" x2="{bx+bw}" y2="{cy+34}" '
                        f'stroke="{p["line"]}"/>' if row == 0 else "")
            body.append(txt(cx + 12, cy + 17, key, 8, p["dim"], ls=1.5))
            body.append(txt(cx + 12, cy + 30, value, 12, p["text"]))
    body.append(f'<line x1="28" y1="228" x2="520" y2="228" stroke="{p["line_strong"]}"/>')
    body.append(f'<line x1="28" y1="223" x2="28" y2="233" stroke="{p["line_strong"]}"/>')
    body.append(f'<line x1="520" y1="223" x2="520" y2="233" stroke="{p["line_strong"]}"/>')
    body.append(txt(28, 222, f'{L["hero_axis"]} · {stats["active_days"]} ACTIVE DAYS', 10, p["dim"], ls=1.6))
    return svg(W, H, p, "\n".join(x for x in body if x), "profile title block")


def fig_shipped(p, theme, shipped, stats) -> str:
    W = 900
    rows = max(1, len(shipped))
    H = 96 + rows * 32
    body = [txt(28, 34, theme["labels"]["fig_shipped"], 11, p["dim"], ls=2.2)]
    y = 60
    if not shipped:
        body.append(txt(28, y + 12, "no public commits in the recent window", 12.5, p["dim"], mono=False))
    for i, item in enumerate(shipped):
        body.append(f'<line x1="28" y1="{y - 18}" x2="872" y2="{y - 18}" stroke="{p["line"]}"/>')
        body.append(txt(28, y, item["date"].strftime("%Y-%m-%d"), 11, p["faint"]))
        body.append(txt(120, y, fit(item["repo"], 12, 150), 12, p["accent"]))
        body.append(txt(285, y, fit(item["message"], 12.5, 560, mono=False), 12.5, p["text"], mono=False))
        y += 32
    body.append(f'<line x1="28" y1="{y - 18}" x2="872" y2="{y - 18}" stroke="{p["line"]}"/>')
    body.append(txt(28, H - 16, f'LAST ACTIVE DAY {stats["last_active"] or "—"} · '
                                f'{stats["contributions"]} CONTRIBUTIONS / {stats["active_days"]} DAYS',
                    10, p["dim"], ls=1.2))
    return svg(W, H, p, "\n".join(body), "last shipped public commits")


def fig_load(p, theme, stats) -> str:
    W, H = 900, 210
    months = stats["months"]
    top = max((v for _, v in months), default=0) or 1
    axis_max = max(5, -(-top // 5) * 5)      # round the axis up to a clean top line
    body = [txt(76, 34, theme["labels"]["fig_load"], 11, p["dim"], ls=2.2),
            txt(840, 34, theme["labels"]["axis_unit"], 9, p["faint"], anchor="end", ls=1.2)]
    body.append(f'<line x1="76" y1="150" x2="840" y2="150" stroke="{p["line_strong"]}"/>')
    for value in (axis_max, axis_max // 2):
        y = 150 - 76 * value / axis_max
        body.append(f'<line x1="76" y1="{y:.1f}" x2="840" y2="{y:.1f}" '
                    f'stroke="{p["line"]}" stroke-dasharray="3 5"/>')
        body.append(txt(68, y + 3, value, 9, p["faint"], anchor="end"))
    body.append(txt(68, 153, "0", 9, p["faint"], anchor="end"))
    step = (840 - 76) / len(months)
    for i, (key, value) in enumerate(months):
        x = 76 + i * step
        hgt = 4 if not value else 10 + value / axis_max * 66
        fill = p["zero"] if not value else p["accent"]
        opacity = "0.5" if not value else "0.92"
        body.append(f'<rect x="{x + 4:.1f}" y="{150 - hgt:.1f}" width="{step - 22:.1f}" '
                    f'height="{hgt:.1f}" fill="{fill}" opacity="{opacity}"/>')
        body.append(txt(x + (step - 14) / 2 - 2, 168, key[5:], 10, p["dim"], anchor="middle"))
    body.append(txt(76, 196, f'TOTAL {stats["contributions"]} CONTRIBUTIONS · '
                             f'{stats["active_days"]} ACTIVE DAYS · '
                             f'{theme["labels"]["footer_caveat"]}', 11, p["dim"]))
    return svg(W, H, p, "\n".join(body), "active days per month")


def fig_stats(p, stats, profile) -> str:
    W, H = 900, 104
    cells = [("CONTRIBUTIONS / 12 MO", f'{stats["contributions"]}'),
             ("ACTIVE DAYS", f'{stats["active_days"]}'),
             ("LONGEST RUN", f'{stats["longest_run"]} D'),
             ("PUBLIC REPOS", f'{profile["public_repos"]}'),
             ("FOLLOWERS", f'{profile["followers"]}')]
    step = W / len(cells)
    body = []
    for i, (label, value) in enumerate(cells):
        x = i * step
        if i:
            body.append(f'<line x1="{x:.1f}" y1="20" x2="{x:.1f}" y2="{H-20}" stroke="{p["line"]}"/>')
        body.append(txt(x + 26, 52, value, 26, p["text"]))
        body.append(txt(x + 26, 74, label, 9, p["dim"], ls=1.6))
    return svg(W, H, p, "\n".join(body), "profile statistics")


def fig_stack(p, theme, languages) -> str:
    W, H = 900, 156
    top = languages.most_common(5)
    total = sum(languages.values()) or 1
    body = [txt(28, 34, theme["labels"]["fig_stack"], 11, p["dim"], ls=2.2)]
    x, bar_y, bar_h = 28, 56, 30
    for i, (lang, size) in enumerate(top):
        width = (W - 56) * size / total
        body.append(f'<rect x="{x:.1f}" y="{bar_y}" width="{max(width, 2):.1f}" height="{bar_h}" '
                    f'fill="{p["bar"][i % len(p["bar"])]}" stroke="{p["panel"]}" stroke-width="1.5"/>')
        if width > 70:
            body.append(txt(x + 10, bar_y + 20, f'{size/total*100:.0f}%', 12,
                            p["page"] if i == 0 else p["text"]))
        x += width
    lx = 28
    for i, (lang, size) in enumerate(top):
        body.append(f'<rect x="{lx}" y="108" width="10" height="10" fill="{p["bar"][i % len(p["bar"])]}" '
                    f'stroke="{p["line_strong"]}" stroke-width="1"/>')
        body.append(txt(lx + 18, 117, lang, 12, p["text"], mono=False))
        lx += 18 + char_w(12, False) * len(lang) + 34
    body.append(txt(872, 148, f'TOP {len(top)} OF {len(languages)} LANGUAGES · BYTES OF CODE', 9,
                    p["faint"], anchor="end", ls=1.2))
    return svg(W, H, p, "\n".join(body), "languages by bytes")


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", default="Alexander-Domanov")
    ap.add_argument("--theme", default=str(ROOT / "themes" / "blueprint.json"))
    ap.add_argument("--out", default=str(ROOT / "assets" / "out"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    theme = json.loads(Path(args.theme).read_text(encoding="utf-8"))
    out = Path(args.out)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    profile = fetch_profile(args.login)
    repos = fetch_repos(args.login)
    counts = fetch_contributions(args.login)
    shipped = fetch_shipped(args.login, repos)
    languages = fetch_languages(repos)
    stats = aggregate(counts)

    figures = {
        "hero": lambda p: fig_hero(p, theme, stats, profile, stamp),
        "fig-shipped": lambda p: fig_shipped(p, theme, shipped, stats),
        "fig-stats": lambda p: fig_stats(p, stats, profile),
        "fig-load": lambda p: fig_load(p, theme, stats),
        "fig-stack": lambda p: fig_stack(p, theme, languages),
    }

    written = 0
    for name, build in figures.items():
        for variant in ("dark", "light"):
            content = build(theme["palette"][variant])
            path = out / f"{name}-{variant}.svg"
            old = path.read_text(encoding="utf-8") if path.exists() else None
            if old != content:
                written += 1
                if not args.dry_run:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "login": args.login,
        "stats": stats,
        "shipped": [{**s, "date": s["date"].isoformat()} for s in shipped],
        "languages": dict(languages.most_common(10)),
    }
    if not args.dry_run:
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"contributions={stats['contributions']} active_days={stats['active_days']} "
          f"longest_run={stats['longest_run']} repos={profile['public_repos']} "
          f"followers={profile['followers']}")
    print(f"shipped rows={len(shipped)} top languages={[l for l, _ in languages.most_common(5)]}")
    print(f"files updated={written}{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        print(f"GitHub API error: {exc.code} {exc.reason} for {exc.url}", file=sys.stderr)
        sys.exit(2)
