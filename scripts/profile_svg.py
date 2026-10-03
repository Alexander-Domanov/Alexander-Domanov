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


def human(size) -> str:
    """Bytes as a short human string, for the materials list."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


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
# figures — one drawing set, not a pile of widgets: every figure carries the
# same chrome, its own figure number, and each number is printed exactly once
# across the whole page
# --------------------------------------------------------------------------
FIG_ORDER = ("hero", "fig-shipped", "fig-stats", "fig-load", "fig-stack")


def chrome(W, H, p, theme, key, rev=None, foot=None):
    """Outer frame, numbered caption and revision stamp shared by every figure."""
    n = FIG_ORDER.index(key) + 1
    body = [f'<rect x="8.5" y="8.5" width="{W - 17}" height="{H - 17}" fill="none" '
            f'stroke="{p["line_strong"]}"/>',
            txt(28, 34, f'FIG. {n} — {theme["labels"]["figure_titles"][key]}', 11,
                p["dim"], ls=2.4)]
    if rev:
        body.append(txt(872, 34, f'REV {rev}', 9, p["faint"], anchor="end", ls=1.4))
    body.append(f'<line x1="28" y1="46" x2="{W - 28}" y2="46" stroke="{p["line"]}"/>')
    if foot:
        body.append(f'<line x1="28" y1="{H - 28}" x2="{W - 28}" y2="{H - 28}" '
                    f'stroke="{p["line"]}"/>')
        body.append(txt(28, H - 12, foot, 10, p["dim"], ls=1.4))
    return body


def scale_bar(p, y, x0, x1, months):
    """Drafting ruler. Tick every month, longer tick every quarter."""
    out = [f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" stroke="{p["line_strong"]}"/>']
    for m in range(months + 1):
        x = x0 + (x1 - x0) * m / months
        h = 6 if m % 3 == 0 else 3
        out.append(f'<line x1="{x:.1f}" y1="{y}" x2="{x:.1f}" y2="{y - h}" '
                   f'stroke="{p["line_strong"]}"/>')
    return out


def fig_hero(p, theme, stats, profile, stamp) -> str:
    L = theme["labels"]
    W, H = 900, 250
    body = chrome(W, H, p, theme, "hero", rev=stamp)
    body.append(txt(28, 92, profile["name"].upper(), 30, p["text"]))
    body.append(txt(28, 120, L["hero_role"], 13, p["accent"]))
    for i, (note, colour) in enumerate([(L["hero_note_1"], p["dim"]),
                                        (L["hero_note_2"], p["dim"]),
                                        (L["hero_status"], p["warm"])]):
        y = 150 + i * 24
        body.append(f'<circle cx="30" cy="{y}" r="2.5" fill="{colour}"/>'
                    f'<line x1="30" y1="{y}" x2="220" y2="{y}" stroke="{p["line_strong"]}"/>')
        body.append(txt(230, y + 4, note, 11, colour))
    bx, by, bw, bh = 560, 130, 162, 88
    cells = L["titleblock"]
    body.append(f'<rect x="{bx}" y="{by}" width="{bw * 2}" height="{bh}" fill="{p["panel"]}" '
                f'stroke="{p["line_strong"]}"/>')
    for row in range(2):
        for col in range(2):
            key, value = cells[row * 2 + col]
            cx, cy = bx + col * bw, by + row * 44
            if col == 0:
                body.append(f'<line x1="{cx + bw}" y1="{cy}" x2="{cx + bw}" y2="{cy + 44}" '
                            f'stroke="{p["line"]}"/>')
            if row == 0:
                body.append(f'<line x1="{bx}" y1="{cy + 44}" x2="{bx + bw * 2}" y2="{cy + 44}" '
                            f'stroke="{p["line"]}"/>')
            body.append(txt(cx + 12, cy + 19, key, 8, p["dim"], ls=1.5))
            body.append(txt(cx + 12, cy + 36, value, 12, p["text"]))
    body += scale_bar(p, 226, 28, 520, 12)
    body.append(txt(28, 218, L["hero_axis"], 9, p["dim"], ls=1.3))
    body.append(txt(520, 218, L["hero_axis_end"], 9, p["dim"], anchor="end", ls=1.3))
    return svg(W, H, p, "\n".join(x for x in body if x), "profile title block")


def fig_shipped(p, theme, shipped, stats, rev=None) -> str:
    W = 900
    rows = max(1, len(shipped))
    H = 118 + rows * 32
    foot = f'LAST PUBLIC COMMIT {stats["last_active"] or "—"} · ONE ROW PER REPOSITORY'
    body = chrome(W, H, p, theme, "fig-shipped", rev=rev, foot=foot)
    for x, label, anchor in ((28, "DATE", "start"), (120, "REPOSITORY", "start"),
                             (285, "COMMIT MESSAGE", "start")):
        body.append(txt(x, 70, label, 8.5, p["faint"], ls=1.6, anchor=anchor))
    body.append(f'<line x1="28" y1="78" x2="872" y2="78" stroke="{p["line_strong"]}"/>')
    y = 102
    if not shipped:
        body.append(txt(28, y, "no public commits in the recent window", 12.5, p["dim"],
                        mono=False))
    for item in shipped:
        body.append(txt(28, y, item["date"].strftime("%Y-%m-%d"), 11, p["faint"]))
        body.append(txt(120, y, fit(item["repo"], 12, 150), 12, p["accent"]))
        body.append(txt(285, y, fit(item["message"], 12.5, 560, mono=False), 12.5,
                        p["text"], mono=False))
        body.append(f'<line x1="28" y1="{y + 10}" x2="872" y2="{y + 10}" stroke="{p["line"]}"/>')
        y += 32
    return svg(W, H, p, "\n".join(body), "last shipped public commits")


def fig_load(p, theme, stats, rev=None) -> str:
    W, H = 900, 232
    months = stats["months"]
    top = max((v for _, v in months), default=0) or 1
    axis_max = max(4, -(-top // 4) * 4)      # keep the middle tick at exactly half
    base, tall = 164, 86
    body = chrome(W, H, p, theme, "fig-load", rev=rev, foot=theme["labels"]["footer_caveat"])
    body.append(txt(872, 62, theme["labels"]["axis_unit"], 9, p["faint"], anchor="end", ls=1.2))
    for value in (axis_max, axis_max // 2):
        y = base - tall * value / axis_max
        body.append(f'<line x1="76" y1="{y:.1f}" x2="872" y2="{y:.1f}" stroke="{p["line"]}" '
                    f'stroke-dasharray="3 5"/>')
        body.append(txt(68, y + 3, value, 9, p["faint"], anchor="end"))
    body.append(txt(68, base + 3, "0", 9, p["faint"], anchor="end"))
    body.append(f'<line x1="76" y1="{base}" x2="872" y2="{base}" stroke="{p["line_strong"]}"/>')
    step = (872 - 76) / len(months)
    for i, (key, value) in enumerate(months):
        x = 76 + i * step
        hgt = 4 if not value else 12 + value / axis_max * (tall - 12)
        fill = p["zero"] if not value else p["accent"]
        body.append(f'<rect x="{x + 12:.1f}" y="{base - hgt:.1f}" width="{step - 24:.1f}" '
                    f'height="{hgt:.1f}" fill="{fill}" opacity="'
                    f'{"0.55" if not value else "0.92"}"/>')
        body.append(txt(x + step / 2, 182, key[5:], 10, p["dim"], anchor="middle"))
    return svg(W, H, p, "\n".join(body), "active days per month")


def fig_stack(p, theme, languages, rev=None) -> str:
    W, H = 900, 196
    top = languages.most_common(5)
    total = sum(languages.values()) or 1
    body = chrome(W, H, p, theme, "fig-stack", rev=rev,
                  foot=f'TOP {len(top)} OF {len(languages)} LANGUAGES · BYTES OF CODE')
    x, bar_y, bar_h = 28, 58, 28
    for i, (lang, size) in enumerate(top):
        width = (W - 56) * size / total
        body.append(f'<rect x="{x:.1f}" y="{bar_y}" width="{max(width, 2):.1f}" '
                    f'height="{bar_h}" fill="{p["bar"][i % len(p["bar"])]}" '
                    f'stroke="{p["panel"]}" stroke-width="1.5"/>')
        if width > 70:
            body.append(txt(x + 10, bar_y + 19, f'{size / total * 100:.0f}%', 12,
                            p["page"] if i == 0 else p["text"]))
        x += width
    for i, (lang, size) in enumerate(top):          # bill of materials
        col, row = divmod(i, 3)
        lx, ly = 28 + col * 442, 116 + row * 22
        body.append(f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" '
                    f'fill="{p["bar"][i % len(p["bar"])]}" stroke="{p["line_strong"]}"/>')
        body.append(txt(lx + 18, ly, lang, 12, p["text"], mono=False))
        body.append(txt(lx + 424, ly, f'{size / total * 100:.1f}% · {human(size)}', 10.5,
                        p["dim"], anchor="end"))
    return svg(W, H, p, "\n".join(body), "languages by bytes")


def fig_stats(p, theme, stats, profile, rev=None) -> str:
    W, H = 900, 130
    cells = [("CONTRIBUTIONS / 12 MO", f'{stats["contributions"]}'),
             ("ACTIVE DAYS", f'{stats["active_days"]}'),
             ("LONGEST RUN / DAYS", f'{stats["longest_run"]}'),
             ("PUBLIC REPOS", f'{profile["public_repos"]}'),
             ("FOLLOWERS", f'{profile["followers"]}')]
    step = W / len(cells)
    body = chrome(W, H, p, theme, "fig-stats", rev=rev)
    for i, (label, value) in enumerate(cells):
        x = i * step
        if i:
            body.append(f'<line x1="{x:.1f}" y1="62" x2="{x:.1f}" y2="116" stroke="{p["line"]}"/>')
        body.append(txt(x + 26, 86, value, 26, p["text"]))
        body.append(txt(x + 26, 108, label, 8.5, p["dim"], ls=1.5))
    return svg(W, H, p, "\n".join(body), "profile statistics")


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
        "fig-shipped": lambda p: fig_shipped(p, theme, shipped, stats, stamp),
        "fig-stats": lambda p: fig_stats(p, theme, stats, profile, stamp),
        "fig-load": lambda p: fig_load(p, theme, stats, stamp),
        "fig-stack": lambda p: fig_stack(p, theme, languages, stamp),
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
