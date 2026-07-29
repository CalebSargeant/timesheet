"""Every knob the manager's 'Uren' format or Caleb's routine needs.

Defaults reproduce the layout of the hand-filled example
(tests/fixtures + 'Uren Voorbeeld Cloud Team.xlsx'): a 07:30 on-call start,
a daily standup, meetings from the calendar, and ~8h days rounded to :15.
Everything here is overridable from env in production (see config.from_env)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    tz: str = "Europe/Amsterdam"
    workdays: tuple[int, ...] = (0, 1, 2, 3, 4)          # Mon–Fri
    day_start: str = "07:30"
    target_minutes: int = 480                            # soft daily target (8:00)
    snap_minutes: int = 15                               # round edges to :00/:15/:30/:45

    # Block-length hygiene so a thin-meeting day doesn't render one giant block.
    min_block_minutes: int = 30
    max_focus_minutes: int = 120
    max_admin_minutes: int = 90
    include_after_hours: bool = False                    # drop evening/weekend commit sessions

    # Morning on-call / standby ("ochtenddienst"). Not calendared -> config.
    rota_enabled: bool = True
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

    session_gap_minutes: int = 120                       # git-hours clustering gap

    @staticmethod
    def from_env(env: dict | None = None) -> Config:
        e = env or os.environ
        def _b(k, d): return e.get(k, str(d)).lower() in ("1", "true", "yes", "on")
        def _i(k, d): return int(e.get(k, d))
        return Config(
            tz=e.get("TZ", "Europe/Amsterdam"),
            day_start=e.get("DAY_START", "07:30"),
            target_minutes=_i("TARGET_MINUTES", 480),
            rota_enabled=_b("ROTA_ENABLED", True),
            rota_minutes=_i("ROTA_MINUTES", 75),
            include_after_hours=_b("INCLUDE_AFTER_HOURS", False),
        )
