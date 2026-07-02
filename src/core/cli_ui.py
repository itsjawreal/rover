from __future__ import annotations

from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_console = Console(soft_wrap=True)

# ── Plain text helpers (used by doctor.py) ───────────────────


def _stringify(value: object) -> str:
    return "" if value is None else str(value)


def box_title(title: str) -> str:
    title = _stringify(title).strip()
    width = max(len(title) + 2, 24)
    top = "+" + "-" * width + "+"
    middle = f"| {title.ljust(width - 1)}|"
    return "\n".join([top, middle, top])


def key_value_block(title: str, rows: list[tuple[str, object]]) -> str:
    label_width = max((len(_stringify(label)) for label, _ in rows), default=8)
    value_width = max((len(_stringify(value)) for _, value in rows), default=8)
    lines = [box_title(title)]
    border = f"+-{'-' * label_width}-+-{'-' * value_width}-+"
    lines.append(border)
    for label, value in rows:
        lines.append(
            f"| {_stringify(label).ljust(label_width)} | {_stringify(value).ljust(value_width)} |"
        )
    lines.append(border)
    return "\n".join(lines)


def table(title: str, headers: list[str], rows: list[list[object]]) -> str:
    widths = [len(_stringify(header)) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(_stringify(cell)))

    def render_row(values: list[object]) -> str:
        cells = [f" {_stringify(value).ljust(widths[index])} " for index, value in enumerate(values)]
        return "|" + "|".join(cells) + "|"

    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    lines = [box_title(title), border, render_row(headers), border]
    for row in rows:
        lines.append(render_row(row))
    lines.append(border)
    return "\n".join(lines)


def bullet_block(title: str, items: list[str]) -> str:
    lines = [box_title(title)]
    if not items:
        lines.append("- none")
    else:
        lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


# ── Rich styled UI ────────────────────────────────────────────

_TAGLINE = "autonomous GitHub contribution agent"


def _resolve_version() -> str:
    try:
        from importlib.metadata import version

        return version("menisik")
    except Exception:
        # running from a source checkout without an installed menisik dist
        return "0.2.0"


_VERSION = _resolve_version()
ENGINE_VERSION = _VERSION

_STATUS_COLOR = {"open": "yellow", "merged": "green", "closed": "red"}
_STATUS_ICON  = {"open": "●", "merged": "✓", "closed": "✗"}


def print_banner(version: str = _VERSION) -> None:
    _console.print()
    _console.print(f"[bold green]♦ menisik {version}[/]  [dim]{_TAGLINE}[/]")
    _console.print()


def print_section(title: str) -> None:
    rule_width = max(0, 58 - len(title))
    _console.print(
        f"[bold cyan]◆ {title} [/][dim cyan]{'─' * rule_width}[/]"
    )
    _console.print()


def print_ok(msg: str) -> None:
    _console.print(f"  [bold green]✓[/]  {msg}")


def print_warn(msg: str) -> None:
    _console.print(f"  [bold yellow]⚠[/]  {msg}")


def print_err(msg: str) -> None:
    _console.print(f"  [bold red]✗[/]  {msg}")


def print_item(msg: str) -> None:
    _console.print(f"  [dim]‣[/]  {msg}")


def print_info(msg: str) -> None:
    _console.print(f"     {msg}")


def print_blank() -> None:
    _console.print()


def _doctor_group_status(check_map: dict[str, object], names: list[str]) -> str:
    statuses = [getattr(check_map[name], "status", "warn") for name in names if name in check_map]
    if not statuses:
        return "warn"
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"


def _doctor_print_row(status: str, name: str, detail: str) -> None:
    label = f"[bold]{name}[/]"
    rendered_detail = f"[dim]{detail}[/]"
    if status == "ok":
        _console.print(f"  [bold green]✓[/]  {label}  {rendered_detail}")
    elif status == "warn":
        _console.print(f"  [bold yellow]⚠[/]  {label}  {rendered_detail}")
    else:
        _console.print(f"  [bold red]✗[/]  {label}  {rendered_detail}")


def _doctor_storage_detail(check_map: dict[str, object]) -> tuple[str, str]:
    status = _doctor_group_status(
        check_map,
        ["storage-mode", "storage-state", "storage-cache", "storage-artifacts", "storage-config"],
    )
    mode_detail = getattr(check_map.get("storage-mode"), "detail", "")
    mode = "persistent"
    if "mode=" in mode_detail:
        mode = mode_detail.split("mode=", 1)[1].split()[0].strip()
    writable = "writable" if status == "ok" else "check permissions"
    return status, f"{mode} · {writable}"


def _doctor_github_detail(check_map: dict[str, object]) -> tuple[str, str]:
    status = _doctor_group_status(check_map, ["github-cli", "github-auth", "github-token"])
    token_detail = getattr(check_map.get("github-token"), "detail", "")
    if "GH_TOKEN" in token_detail:
        detail = "authenticated via GH_TOKEN"
    elif "GITHUB_TOKEN" in token_detail:
        detail = "authenticated via GITHUB_TOKEN"
    elif "gh auth token" in token_detail:
        detail = "authenticated via gh auth"
    elif status == "ok":
        detail = "authenticated"
    else:
        detail = "GitHub auth needs attention"
    return status, detail


def _doctor_workspace_detail(check_map: dict[str, object]) -> tuple[str, str]:
    status = _doctor_group_status(check_map, ["workspace", "entrypoint"])
    entrypoint = getattr(check_map.get("entrypoint"), "detail", "")
    if status == "ok":
        if ".venv" in entrypoint:
            detail = "project root ready · project .venv active"
        else:
            detail = "project root ready · entrypoint looks healthy"
    else:
        detail = "project root ready · check active menisik entrypoint"
    return status, detail


def _doctor_backend_detail(check_map: dict[str, object]) -> tuple[str, str]:
    status = _doctor_group_status(check_map, ["ai-backend", "selected-backend"])
    ai_detail = getattr(check_map.get("ai-backend"), "detail", "")
    runtime = "unknown"
    if "runtime=" in ai_detail:
        runtime = ai_detail.split("runtime=", 1)[1].split()[0].strip()
    elif "backend=" in ai_detail:
        runtime = ai_detail.split("backend=", 1)[1].split()[0].strip()
    availability = "available" if getattr(check_map.get("selected-backend"), "status", "warn") == "ok" else "missing"
    return status, f"{runtime} selected · {availability}"


def _doctor_backend_auth_detail(check_map: dict[str, object]) -> tuple[str, str]:
    check = check_map.get("selected-backend-auth")
    status = getattr(check, "status", "warn")
    detail = getattr(check, "detail", "")
    lowered = detail.lower()
    if "openai_api_key is set" in lowered:
        return status, "ready via OPENAI_API_KEY"
    if "login is active" in lowered or "auth looks ready" in lowered:
        return status, "login active"
    if "not active" in lowered or "run `codex login`" in lowered or "run `claude login`" in lowered:
        return status, "needs login"
    if "not installed" in lowered or "not found" in lowered:
        return status, "CLI missing"
    return status, detail or "needs attention"


def _doctor_openclaw_detail(check_map: dict[str, object]) -> tuple[str, str]:
    status = _doctor_group_status(check_map, ["openclaw-skill", "openclaw-wrapper", "openclaw-mcp", "hermes-mcp"])
    mcp_ok = getattr(check_map.get("openclaw-mcp"), "status", "warn") == "ok"
    skill_ok = getattr(check_map.get("openclaw-skill"), "status", "warn") == "ok"
    wrapper_ok = getattr(check_map.get("openclaw-wrapper"), "status", "warn") == "ok"
    hermes_ok = getattr(check_map.get("hermes-mcp"), "status", "warn") == "ok"
    if mcp_ok and skill_ok and wrapper_ok and hermes_ok:
        return status, "mcp + native ready"
    if mcp_ok:
        return status, "mcp ready"
    if skill_ok or wrapper_ok:
        return status, "native partial"
    return status, "not installed"


def print_styled_doctor(checks: list, *, verbose: bool = False) -> None:
    from src.core.doctor import DoctorCheck

    ok_count = sum(1 for c in checks if c.status == "ok")
    warn_count = sum(1 for c in checks if c.status == "warn")
    fail_count = sum(1 for c in checks if c.status == "fail")

    print_section("Doctor")

    if verbose:
        for check in checks:
            _doctor_print_row(check.status, check.name, check.detail)
    else:
        check_map: dict[str, DoctorCheck] = {check.name: check for check in checks}
        python_detail = getattr(check_map.get("python"), "detail", "Python status unavailable")
        concise_rows = [
            ("python", getattr(check_map.get("python"), "status", "warn"), python_detail.replace("running ", "", 1)),
            ("workspace", *_doctor_workspace_detail(check_map)),
            ("github", *_doctor_github_detail(check_map)),
            ("storage", *_doctor_storage_detail(check_map)),
            ("backend", *_doctor_backend_detail(check_map)),
            ("backend auth", *_doctor_backend_auth_detail(check_map)),
            ("agents", *_doctor_openclaw_detail(check_map)),
        ]

        for name, status, detail in concise_rows:
            _doctor_print_row(status, name, detail)

        covered = {
            "python",
            "workspace",
            "entrypoint",
            "github-cli",
            "github-auth",
            "github-token",
            "storage-mode",
            "storage-state",
            "storage-cache",
            "storage-artifacts",
            "storage-config",
            "ai-backend",
            "codex-cli",
            "claude-cli",
            "codex-auth",
            "agent-runtime",
            "selected-backend",
            "selected-backend-auth",
            "openclaw-skill",
            "openclaw-wrapper",
            "openclaw-mcp",
            "openclaw-legacy",
            "hermes-mcp",
        }
        extras = [check for check in checks if check.name not in covered and check.status in {"warn", "fail"}]
        if extras:
            print_blank()
            print_item("Additional notes")
            for check in extras:
                _doctor_print_row(check.status, check.name, check.detail)

        print_blank()
        print_item("Run `menisik doctor --verbose` for full paths and raw checks.")

    print_blank()
    print_section("Summary")

    overall = "fail" if fail_count else ("warn" if warn_count else "ok")
    overall_color = {"ok": "green", "warn": "yellow", "fail": "red"}[overall]
    _console.print(
        f"  [green]OK {ok_count}[/]   [yellow]WARN {warn_count}[/]   [red]FAIL {fail_count}[/]"
        f"   Overall: [bold {overall_color}]{overall.upper()}[/]"
    )
    print_blank()


def print_styled_prs(rows: list[dict], status_filter: str = "all") -> None:
    if not rows:
        label = f" ({status_filter})" if status_filter != "all" else ""
        print_section(f"Submitted PRs{label}")
        print_item("No PRs found. Run: [bold]menisik owner/repo[/]")
        print_blank()
        return

    counts: dict[str, int] = {}
    for row in rows:
        s = row["status"]
        counts[s] = counts.get(s, 0) + 1
    summary = "  ".join(
        f"[{_STATUS_COLOR.get(s, 'white')}]{_STATUS_ICON.get(s, '?')} {v} {s}[/]"
        for s, v in sorted(counts.items())
    )
    print_section(f"Submitted PRs — {len(rows)} total")
    _console.print(f"  {summary}")
    print_blank()

    t = Table(
        show_header=True,
        header_style="bold dim",
        box=None,
        pad_edge=False,
        show_edge=False,
        row_styles=["", "dim"],
    )
    t.add_column("Status",  width=8,  no_wrap=True)
    t.add_column("Repo",    width=30, no_wrap=True)
    t.add_column("Title",   width=44, no_wrap=True)
    t.add_column("Date",    width=10, no_wrap=True)
    t.add_column("URL",     no_wrap=True)

    for row in rows:
        status = row.get("status", "?")
        color  = _STATUS_COLOR.get(status, "white")
        icon   = _STATUS_ICON.get(status, "?")
        repo   = (row.get("repo_full_name") or "")[:30]
        title  = (row.get("pr_title") or "")
        title  = title[:43] + "…" if len(title) > 43 else title
        date   = (row.get("submitted_at") or "")[:10]
        url    = row.get("pr_url") or ""
        t.add_row(
            Text(f"{icon} {status}", style=color),
            repo,
            title,
            date,
            f"[link={url}][dim]{url}[/][/]",
        )

    _console.print(t)
    print_blank()


def print_styled_report(summaries: list[dict], queued: list[dict]) -> None:
    if not summaries and not queued:
        print_section("Contribution Report")
        print_item("No runs recorded yet.")
        print_blank()
        return

    latest = summaries[0] if summaries else {}

    if latest:
        print_section("Latest run")
        submitted = latest.get("submitted", 0)
        target    = latest.get("target", 0)
        states    = "  ".join(f"[dim]{k}={v}[/]" for k, v in latest.get("state_counts", {}).items()) or "[dim]-[/]"
        color = "green" if submitted >= target > 0 else ("yellow" if submitted > 0 else "red")
        _console.print(f"  Run      [bold]#{latest.get('run_id', '?')}[/]")
        _console.print(f"  Submit   [bold {color}]{submitted} / {target}[/]")
        _console.print(f"  Attempts [dim]{latest.get('attempts', 0)}[/]")
        _console.print(f"  AI calls [dim]{latest.get('ai_calls', 0)}[/]")
        _console.print(f"  States   {states}")
        if latest.get("top_rejections"):
            top = "  ".join(f"[yellow]{r}×{c}[/]" for r, c in latest["top_rejections"][:3])
            _console.print(f"  Reject   {top}")
        print_blank()

    if summaries:
        print_section("Recent runs")
        t = Table(
            show_header=True,
            header_style="bold dim",
            box=None,
            pad_edge=False,
            show_edge=False,
            row_styles=["", "dim"],
        )
        t.add_column("Run",       width=6,  no_wrap=True)
        t.add_column("Submit",    width=8,  no_wrap=True)
        t.add_column("Attempts",  width=9,  no_wrap=True)
        t.add_column("AI calls",  width=9,  no_wrap=True)
        t.add_column("States",    no_wrap=False)
        for s in summaries:
            sub = s.get("submitted", 0)
            tgt = s.get("target", 0)
            color = "green" if sub >= tgt > 0 else ("yellow" if sub > 0 else "white")
            states_str = "  ".join(f"{k}={v}" for k, v in s.get("state_counts", {}).items()) or "-"
            t.add_row(
                f"#{s.get('run_id', '?')}",
                Text(f"{sub}/{tgt}", style=color),
                str(s.get("attempts", 0)),
                str(s.get("ai_calls", 0)),
                states_str,
            )
        _console.print(t)
        print_blank()

        bottlenecks = [
            f"run #{s.get('run_id', '?')}: {s['bottleneck']}"
            for s in summaries if s.get("bottleneck")
        ]
        if bottlenecks:
            print_section("Bottlenecks")
            for b in bottlenecks:
                print_item(b)
            print_blank()

    if queued:
        print_section(f"Queue  ({len(queued)} ready)")
        t2 = Table(
            show_header=True,
            header_style="bold dim",
            box=None,
            pad_edge=False,
            show_edge=False,
            row_styles=["", "dim"],
        )
        t2.add_column("ID",      width=7,  no_wrap=True)
        t2.add_column("Repo",    width=32, no_wrap=True)
        t2.add_column("Pattern", width=26, no_wrap=True)
        t2.add_column("File",    width=28, no_wrap=True)
        t2.add_column("Score",   width=6,  no_wrap=True)
        for opp in queued:
            score = opp.get("acceptance_score", 0)
            score_color = "green" if score >= 100 else ("yellow" if score >= 70 else "white")
            t2.add_row(
                f"#{opp.get('id')}",
                (opp.get("repo_full_name") or "")[:32],
                (opp.get("pattern_type") or "")[:26],
                (opp.get("target_file") or "")[:28],
                Text(str(score), style=score_color),
            )
        _console.print(t2)
        print_blank()

    print_section("Next step")
    if queued:
        print_item("[bold]menisik run[/]   [dim]— consume the strongest queued opportunity[/]")
    elif latest.get("top_rejections"):
        top_reason = latest["top_rejections"][0][0]
        print_item(f"investigate rejection pattern [bold yellow]{top_reason}[/] before widening search")
    else:
        print_item("[bold]menisik run[/]   [dim]— start a new contribution cycle[/]")
    print_blank()


def print_repo_inspect_overview(data: dict[str, object]) -> None:
    _console.print(f"  Repo     [bold]{data.get('repo', '-') }[/]")
    _console.print(f"  URL      [dim]{data.get('url', '-')}[/]")
    _console.print(
        "  Stats    "
        f"[dim]stars={data.get('stars', '-')}  "
        f"forks={data.get('forks', '-')}  "
        f"license={data.get('license', '-')}  "
        f"pushed={data.get('pushed_days_ago', '-')}d ago[/]"
    )
    _console.print(
        "  Surface  "
        f"[dim]files={data.get('files', 0)}  py={data.get('py', 0)}  "
        f"ts={data.get('ts', 0)}  tests={data.get('tests', 0)}[/]"
    )
    lane_match = bool(data.get("lane_match"))
    lane_color = "green" if lane_match else "yellow"
    lane_state = "matched" if lane_match else "not matched"
    _console.print(
        f"  Lane     [bold {lane_color}]{lane_state}[/]  [dim]configured lane `{data.get('lane_name', '-')}`[/]"
    )
    first_pr_friendly = bool(data.get("first_pr_friendly"))
    first_pr_color = "green" if first_pr_friendly else "yellow"
    first_pr_label = str(data.get("first_pr_label", "good fit" if first_pr_friendly else "needs caution"))
    _console.print(
        f"  First PR [bold {first_pr_color}]{first_pr_label}[/]  [dim]{data.get('first_pr_reason', '-') }[/]"
    )
    search_scope = str(data.get("search_scope", "-"))
    targeted_scope = str(data.get("targeted_scope", "-"))
    targeted_color = "green" if targeted_scope == "targeted-ready" else "yellow"
    search_color = "green" if search_scope == "search-ready" else "yellow"
    _console.print(
        f"  Scope    search=[bold {search_color}]{search_scope}[/]  "
        f"targeted=[bold {targeted_color}]{targeted_scope}[/]"
    )
    print_blank()


def print_repo_inspect_description(data: dict[str, object]) -> None:
    description = str(data.get("description", "") or "").strip()
    if description:
        print_section("What Menisik Found")
        print_item("repo purpose and source surface summarized")
        print_info(description)
        print_blank()


def print_repo_inspect_topics(data: dict[str, object]) -> None:
    topics = [str(topic) for topic in (data.get("topics") or [])]
    if topics:
        print_section("Topics")
        print_info(", ".join(topics))
        print_blank()


def print_repo_inspect_scope_notes(data: dict[str, object]) -> None:
    scope_notes = [str(note) for note in (data.get("scope_notes") or [])]
    if scope_notes:
        print_section("Why This Matters")
        for note in scope_notes:
            print_item(note)
        print_blank()


def print_repo_inspect_next_steps(data: dict[str, object]) -> None:
    next_steps = [str(step) for step in (data.get("next_steps") or [])]
    if next_steps:
        print_section("Next Steps")
        for step in next_steps:
            print_item(step)
        print_blank()


def print_styled_repo_inspect(data: dict[str, object]) -> None:
    print_repo_inspect_overview(data)
    print_repo_inspect_description(data)
    print_repo_inspect_topics(data)
    print_repo_inspect_scope_notes(data)
    print_repo_inspect_next_steps(data)


def print_pr_summary(
    pr_url: str,
    pr_title: str,
    repo: str,
    improvement_type: str,
    changed_files: dict,
    rationale: str = "",
) -> None:
    from rich.rule import Rule

    _console.print()
    _console.print(Rule("[bold green] PR Submitted [/]", style="green"))
    _console.print()

    t = Table(box=None, show_header=False, pad_edge=False, show_edge=False, padding=(0, 2))
    t.add_column(style="dim", width=14)
    t.add_column()

    t.add_row("repo",    f"[bold]{repo}[/]")
    t.add_row("title",   f"{pr_title}")
    t.add_row("type",    f"[cyan]{improvement_type}[/]")
    t.add_row("files",   f"[dim]{', '.join(changed_files.keys())}[/]")
    if rationale:
        t.add_row("changes",  f"[dim]{rationale[:120]}[/]")
    t.add_row("url",     f"[link={pr_url}][green]{pr_url}[/][/]")

    _console.print(t)
    _console.print()
    _console.print(Rule(style="dim"))
    _console.print()


def _choose_arrow(prompt: str, options: list[str]) -> str:
    """Arrow-key picker — writes directly to /dev/tty to avoid Rich buffer conflicts."""
    import tty
    import termios

    # Open /dev/tty directly so Rich's stdout buffer doesn't interfere
    try:
        tty_w = open("/dev/tty", "w")
        tty_r = open("/dev/tty", "r")
    except OSError:
        # No TTY (e.g. CI) — fall back to y/n
        _console.print()
        _console.print(f"  [bold]{prompt}[/]  [dim](y/n)[/]", end="  ")
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return options[-1]
        return options[0] if ans in {"y", "yes"} else options[-1]

    n = len(options)
    selected = 0

    def _w(s: str) -> None:
        tty_w.write(s)
        tty_w.flush()

    def _render(first: bool = False) -> None:
        if not first:
            _w(f"\033[{n}A")          # cursor up n lines
        for i, opt in enumerate(options):
            if i == selected:
                _w(f"\r  \033[1;32m› {opt}\033[0m\033[K\n")
            else:
                _w(f"\r    \033[2m{opt}\033[0m\033[K\n")

    # Flush Rich before entering raw mode
    _console.print()
    _console.print(f"  [bold]{prompt}[/]")
    _console.print()
    import sys as _sys
    _sys.stdout.flush()

    _w("\n" * n)          # reserve lines for the picker
    _w(f"\033[{n}A")     # move back up

    fd = tty_r.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        _render(first=True)
        while True:
            ch = tty_r.read(1)
            if ch in ("\r", "\n"):
                break
            if ch == "\x1b":
                seq = tty_r.read(2)
                if seq == "[A" and selected > 0:
                    selected -= 1
                    _render()
                elif seq == "[B" and selected < n - 1:
                    selected += 1
                    _render()
            elif ch == "\x03":
                raise KeyboardInterrupt
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        tty_w.write("\n")
        tty_w.flush()
        tty_w.close()
        tty_r.close()

    return options[selected]


def print_styled_help() -> None:
    commands = [
        ("menisik",                  "status dashboard"),
        ("menisik run",              "search and submit 1 PR"),
        ("menisik run 3",            "submit 3 PRs"),
        ("menisik owner/repo",       "target a specific repo"),
        ("menisik check",            "poll open PRs for updates"),
        ("menisik respond",          "reply to maintainer comments"),
        ("menisik report",           "run history + bottlenecks"),
        ("menisik list-prs",         "all submitted PRs"),
        ("menisik list-prs open",    "filter: open | merged | closed"),
        ("menisik inspect owner/repo", "analyze repo without submitting"),
        ("menisik doctor",           "verify setup"),
        ("menisik doctor --verbose", "show full paths and raw checks"),
        ("menisik setup",            "install / reconfigure environment"),
        ("menisik help",             "show this help"),
    ]

    flags = [
        ("--dry-run",              "generate patch, skip submission"),
        ("--goal bugfix|bug",          "default — concrete failure mode required"),
        ("--goal feature_upgrade|upgrade", "narrow enhancement on maintainer TODO/FIXME"),
        ("--goal feature_add|add",     "issue-backed new feature, strict mode"),
        ("--first-pr",             "bias toward smaller repos for a first PR"),
        ("--override-limits",       "bypass .env contribution filters for this run"),
        ("--claude / --codex",     "override AI backend for this run"),
        ("--count N",              "submit N PRs in one run"),
        ("--version",              "print version and exit"),
    ]

    examples = [
        ("menisik run",                          "auto-find and submit 1 PR"),
        ("menisik run 3",                        "submit 3 PRs in sequence"),
        ("menisik owner/repo",                   "target a pinned repo"),
        ("menisik run --goal upgrade",            "feature upgrade mode"),
        ("menisik run --goal add",               "feature add mode"),
        ("menisik run --first-pr",               "bias toward beginner-friendly repos"),
        ("menisik run owner/repo --override-limits", "target repo while bypassing .env filters"),
        ("menisik run --dry-run",                "preview patch, skip submission"),
        ("menisik run 3 --goal bug --dry-run",   "3 PRs, bugfix mode, dry run"),
    ]

    print_section("Commands")
    for cmd, desc in commands:
        _console.print(
            f"  [bold cyan]{cmd:<30}[/][dim]{desc}[/]"
        )
    print_blank()

    print_section("Flags  (python -m app.builder / low-level)")
    for flag, desc in flags:
        _console.print(
            f"  [bold]{flag:<34}[/][dim]{desc}[/]"
        )
    print_blank()

    print_section("Examples")
    for cmd, desc in examples:
        _console.print(f"  [bold cyan]{cmd}[/]")
        _console.print(f"  [dim]  {desc}[/]")
        print_blank()


def print_status_dashboard() -> None:
    import os
    from src.contrib.contribution_store import ContributionStore

    try:
        store = ContributionStore()
        prs = store.list_pull_requests(limit=200)
        queued = store.queued_opportunities(limit=5)
        summaries = store.latest_run_summaries(limit=1)
    except Exception:
        prs, queued, summaries = [], [], []

    open_count   = sum(1 for p in prs if p.get("status") == "open")
    merged_count = sum(1 for p in prs if p.get("status") == "merged")
    closed_count = sum(1 for p in prs if p.get("status") == "closed")

    owner = os.getenv("GITHUB_OWNER", "")
    backend = os.getenv("AI_BACKEND", "claude")
    lane = os.getenv("CONTRIB_LANE", "general")

    last = summaries[0] if summaries else {}
    last_date = (last.get("created_at") or "")[:10]

    # ── Left panel: status ────────────────────────────────────
    status_lines = Text()
    status_lines.append(f"  ● {open_count} open\n",   style="yellow")
    status_lines.append(f"  ✓ {merged_count} merged\n", style="green")
    status_lines.append(f"  ✗ {closed_count} closed\n", style="red")
    status_lines.append(f"\n")
    if queued:
        status_lines.append(f"  {len(queued)} queued\n", style="dim cyan")
    if last_date:
        status_lines.append(f"  last run  {last_date}\n", style="dim")
    status_lines.append(f"\n")
    status_lines.append(f"  backend  {backend}\n", style="dim")
    status_lines.append(f"  lane     {lane}\n",    style="dim")

    left = Panel(
        status_lines,
        title=f"[bold green]Welcome back, {owner}![/]" if owner else "[bold green]menisik[/]",
        title_align="left",
        border_style="green",
        padding=(0, 1),
    )

    # ── Right panel: quick start ──────────────────────────────
    cmd_lines = Text()
    cmds = [
        ("menisik run",          "search and submit a PR"),
        ("menisik run 3",        "submit 3 PRs"),
        ("menisik owner/repo",   "target a specific repo"),
        ("menisik check",        "poll open PRs + feedback"),
        ("menisik report",       "run history"),
        ("menisik list-prs",     "all submitted PRs"),
        ("menisik inspect repo", "analyze without submitting"),
        ("menisik doctor",       "check setup"),
        ("menisik doctor --verbose", "full diagnostics"),
        ("menisik setup",        "install / reconfigure"),
    ]
    for cmd, desc in cmds:
        cmd_lines.append(f"  {cmd:<24}", style="bold cyan")
        cmd_lines.append(f"{desc}\n", style="dim")

    right = Panel(
        cmd_lines,
        title="[bold cyan]Quick start[/]",
        title_align="left",
        border_style="cyan",
        padding=(0, 1),
    )

    _console.print()
    _console.print(Columns([left, right], equal=True, expand=True))
    _console.print()
