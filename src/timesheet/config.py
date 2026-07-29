"""Every knob the manager's 'Uren' format or Caleb's routine needs.

Defaults reproduce the layout of the hand-filled example
(tests/fixtures + 'Uren Voorbeeld Cloud Team.xlsx'): a 07:30 on-call start,
a daily standup, meetings from the calendar, and ~8h days rounded to :15.
Everything here is overridable from env in production (see config.from_env)."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Bump whenever the reconstruction *logic* changes (day model, summariser, rota,
# effort estimate). Cached weeks stamp this in their meta; the store treats a
# mismatch as a cache miss, so a deploy auto-recomputes stale weeks — no manual
# cache clearing. v2: dropped the template on-call rota; single-phrase summaries.
# v3: 08:30 day start (earlier only if the day's activity says so).
# v4: PR reviews count as effort (background refresh), not just commits.
# v5: PRs opened + issues authored count too; varied admin labels.
RECONSTRUCT_VERSION = 5


@dataclass(frozen=True)
class Config:
    tz: str = "Europe/Amsterdam"
    workdays: tuple[int, ...] = (0, 1, 2, 3, 4)          # Mon-Fri
    day_start: str = "08:30"                             # normal start; earlier only if activity shows it
    earliest_start_floor: str = "06:00"                  # but never open the day before this
    snap_minutes: int = 15                               # round edges to :00/:15/:30/:45

    # The day length is DRIVEN by estimated effort (git-hours of the day's commits)
    # + meetings + rota, not a flat target: a heavy coding day shows more than 8h so
    # a prolific week isn't flattened to 40h. Floored so quiet days still read ~8h
    # ("40h on a slow week"); capped so a marathon day stays believable.
    min_day_minutes: int = 480                           # floor: a normal 8h day
    max_day_minutes: int = 720                           # cap: 12h, a long-but-real day
    admin_floor_minutes: int = 30                        # always a little admin
    session_gap_minutes: int = 120                       # git-hours: new session after a 2h gap
    first_commit_minutes: int = 120                      # git-hours: lead-in before the 1st commit

    # Block-length hygiene so a thin-meeting day doesn't render one giant block.
    min_block_minutes: int = 30
    max_focus_minutes: int = 120
    max_admin_minutes: int = 90
    include_after_hours: bool = False                    # drop evening/weekend commit sessions

    # Morning on-call / standby ("ochtenddienst"). Off by default: it was only a
    # placeholder in the example sheet, not real work. Toggle on per person if it is.
    rota_enabled: bool = False
    rota_minutes: int = 75                               # 07:30–08:45 in the example
    rota_project: str = "Ochtenddienst"
    rota_taak: str = "Checks en standby"

    # Meeting classification.
    standup_markers: tuple[str, ...] = ("standup", "daily")
    standup_project: str = "Intern"
    standup_taak: str = "Daily's"
    meeting_project: str = "Meeting"

    # Calendar items to ignore.
    drop_show_as: tuple[str, ...] = ("free", "tentative")
    drop_all_day: bool = True
    drop_subject_markers: tuple[str, ...] = ("booking", "desk", "verjaardag", "birthday", "lunch")

    # Commit-backed focus blocks: keyword -> work category (first match wins).
    focus_rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("Security",     ("security", "compliance", "rbac", "secret", "vuln", "cve", "oauth")),
        ("Monitoring",   ("monitor", "observability", "grafana", "prometheus", "thanos", "alert", "metrics")),
        ("CI/CD",        ("ci", "cd", "pipeline", "workflow", "release", "docs build")),
        ("Documentatie", ("docs", "runbook", "readme", "adr")),
    )
    focus_default_project: str = "Development"
    admin_project: str = "Administratie"
    admin_taak: str = "Mail / GitHub / Teams"
    # Admin/leftover fill rotates through these so quiet days don't render as a column
    # of identical rows. All are honestly-generic components of a normal workday.
    admin_taaks: tuple[str, ...] = (
        "Mail / GitHub / Teams", "Administratie", "Afstemming / overleg",
        "Documentatie / kennisdeling",
    )

    # GitHub work that leaves no default-branch commit — reviews (senior work),
    # plus PRs opened and issues authored. Credited per item (NOT via the commit
    # git-hours lead-in, which would over-count a point event), capped per day so a
    # busy-but-not-coding day stays believable. Reviews need a per-PR REST fan-out so
    # they run in the background refresh only; PRs/issues are one search each (fast).
    include_reviews: bool = True
    include_authored: bool = True
    review_project: str = "Code review"
    pr_project: str = "Pull requests"
    issue_project: str = "Issues / tickets"
    review_minutes_each: int = 12
    pr_minutes_each: int = 6
    issue_minutes_each: int = 6
    noncommit_cap_minutes: int = 240                     # at most 4h/day of review+PR+issue credit

    # Optional AI label polish via the house LiteLLM proxy (OpenAI-compatible).
    # Empty model => AI off and the deterministic summariser is used. Applied ONLY
    # in the background refresh (run.py), never in a web request, because the
    # proxy's models can take 10-20s per call.
    llm_base_url: str = "https://litellm.sargeant.co"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: float = 30.0
    # deepseek-v4-flash is a reasoning model: "low" keeps a one-line label to ~3.6s
    # (vs 5-20s). Blank if a model rejects the field. Sent only when non-empty.
    llm_reasoning_effort: str = "low"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    @staticmethod
    def from_env(env: dict | None = None) -> Config:
        e = env or os.environ
        def _b(k, d): return e.get(k, str(d)).lower() in ("1", "true", "yes", "on")
        def _i(k, d): return int(e.get(k, d))
        def _f(k, d): return float(e.get(k, d))
        return Config(
            tz=e.get("TZ", "Europe/Amsterdam"),
            day_start=e.get("DAY_START", "08:30"),
            earliest_start_floor=e.get("EARLIEST_START_FLOOR", "06:00"),
            min_day_minutes=_i("MIN_DAY_MINUTES", 480),
            max_day_minutes=_i("MAX_DAY_MINUTES", 720),
            rota_enabled=_b("ROTA_ENABLED", False),
            rota_minutes=_i("ROTA_MINUTES", 75),
            include_after_hours=_b("INCLUDE_AFTER_HOURS", False),
            include_reviews=_b("INCLUDE_REVIEWS", True),
            include_authored=_b("INCLUDE_AUTHORED", True),
            llm_base_url=e.get("LITELLM_BASE_URL", "https://litellm.sargeant.co"),
            llm_api_key=e.get("LITELLM_API_KEY", ""),
            llm_model=e.get("LITELLM_MODEL", ""),
            llm_timeout=_f("LITELLM_TIMEOUT", 30.0),
            llm_reasoning_effort=e.get("LITELLM_REASONING_EFFORT", "low"),
        )
