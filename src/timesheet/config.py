"""Every knob the reconstruction has.

A `Config` is per *person*: the web service builds one from the signed-in user's
saved settings, the CLI builds one from the environment. Defaults are deliberately
neutral — an 8h day, a 08:30 start, English labels — so a fresh install produces
something sensible before anyone touches a setting.

Labels come from `i18n`, so the same deployment can render one person's sheet in
English and another's in Dutch. Keyword *markers* (leave, standup, desk bookings)
are matched across every locale at once: whoever runs the calendar decides what
language the subjects are in, and that is rarely the reader's.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

from . import i18n

# Bump whenever the reconstruction *logic* changes (day model, summariser, rota,
# effort estimate). Cached weeks stamp this in their meta; the store treats a
# mismatch as a cache miss, so a deploy auto-recomputes stale weeks — no manual
# cache clearing. v2: dropped the template on-call rota; single-phrase summaries.
# v3: 08:30 day start (earlier only if the day's activity says so).
# v4: PR reviews count as effort (background refresh), not just commits.
# v5: PRs opened + issues authored count too; varied admin labels.
# v6: daily cap raised 12h -> 14h.
# v7: exclude future days + cap today at 'now'; weekends when worked; past-midnight rollover.
# v8: standup blocks use the real cleaned calendar subject instead of a static label.
# v9: removed the 14h daily cap; skip empty days (no commits & no meetings).
# v10: a full-day busy calendar event owns its day (leave is reported as leave).
# v11: admin blocks can be relabelled from real email/Teams activity.
# v12: email (sent AND received) and chat activity COUNT as time, clipped against
#      meetings and capped per source, and get their own labelled blocks.
RECONSTRUCT_VERSION = 12


@dataclass(frozen=True)
class SignalSpec:
    """How one stream of point-in-time events converts into minutes.

    Events are clustered into sessions — a run with no gap longer than
    `gap_minutes` — and each session credits `lead_minutes` (the unseen work
    before its first event) plus its own real span. One isolated message is
    therefore `lead_minutes`, and a burst of forty in ten minutes is ten minutes
    plus the lead-in, not forty messages' worth. `cap_minutes` bounds the day.
    """
    kind: str
    gap_minutes: int
    lead_minutes: int
    cap_minutes: int


@dataclass(frozen=True)
class Config:
    locale: str = i18n.DEFAULT_LOCALE
    label_overrides: dict[str, str] = field(default_factory=dict)

    tz: str = "UTC"
    workdays: tuple[int, ...] = (0, 1, 2, 3, 4)          # Mon-Fri: the floored "core" days
    day_start: str = "08:30"                             # normal; earlier if activity shows it
    earliest_start_floor: str = "06:00"                  # but never open the day before this
    # Work past midnight belongs to the day it started: activity before this hour is
    # credited to the previous calendar day (a 01:00 commit is last night's work).
    day_rollover_hour: int = 5
    snap_minutes: int = 15                               # round edges to :00/:15/:30/:45

    # The day length is DRIVEN by estimated effort (git-hours of the day's commits,
    # plus correspondence and GitHub activity) + meetings + rota, not a flat target:
    # a heavy day shows more than 8h so a prolific week isn't flattened to 40h.
    # Floored so quiet days still read ~8h; the floor applies to core days only.
    min_day_minutes: int = 480                           # floor: a normal 8h day
    max_day_minutes: int = 840                           # kept for compat; unused since v9
    admin_floor_minutes: int = 30                        # always a little admin
    session_gap_minutes: int = 120                       # git-hours: new session after a 2h gap
    first_commit_minutes: int = 120                      # git-hours: lead-in before the 1st commit

    # Block-length hygiene so a thin-meeting day doesn't render one giant block.
    min_block_minutes: int = 30
    max_focus_minutes: int = 120
    max_admin_minutes: int = 90
    include_after_hours: bool = False                    # drop evening/weekend commit sessions

    # Morning on-call / standby. Off by default: most people don't have one.
    rota_enabled: bool = False
    rota_minutes: int = 75

    # Calendar classification. Markers are cross-locale (see i18n).
    standup_markers: tuple[str, ...] = i18n.STANDUP_MARKERS
    drop_show_as: tuple[str, ...] = ("free", "tentative")
    drop_all_day: bool = True
    drop_subject_markers: tuple[str, ...] = i18n.DROP_MARKERS

    # A full-day calendar event marked BUSY (or out-of-office) OWNS its whole day.
    # HR systems push leave into the calendar as exactly that, and on such a day
    # there is nothing to reconstruct: the sheet must report the leave, not pad 8h
    # of admin around a standup invite that was declined in practice. All-day items
    # that are merely 'free' (desk bookings) are unaffected.
    full_day_owns_day: bool = True
    full_day_show_as: tuple[str, ...] = ("busy", "oof")
    full_day_minutes: int = 480                          # booked as a normal 8h day
    full_day_max_span: int = 60                          # sanity bound on a multi-day event
    leave_markers: tuple[str, ...] = i18n.LEAVE_MARKERS
    leave_blank_subjects: tuple[str, ...] = i18n.BLANK_SUBJECTS

    # Commit-backed focus blocks: i18n category key -> keywords (first match wins).
    focus_rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("security",   ("security", "compliance", "rbac", "secret", "vuln", "cve", "oauth")),
        ("monitoring", ("monitor", "observability", "grafana", "prometheus", "thanos",
                        "alert", "metrics")),
        ("cicd",       ("ci", "cd", "pipeline", "workflow", "release", "docs build")),
        ("docs",       ("docs", "runbook", "readme", "adr")),
    )

    # GitHub work that leaves no default-branch commit — reviews (senior work),
    # plus PRs opened and issues authored. Credited per item (NOT via the commit
    # git-hours lead-in, which would over-count a point event), capped per day so a
    # busy-but-not-coding day stays believable. Reviews need a per-PR REST fan-out so
    # they run in the background refresh only; PRs/issues are one search each (fast).
    include_reviews: bool = True
    include_authored: bool = True
    review_minutes_each: int = 12
    pr_minutes_each: int = 6
    issue_minutes_each: int = 6
    noncommit_cap_minutes: int = 240                     # at most 4h/day of review+PR+issue credit

    # Correspondence. Sent mail and chat messages are actions with a time on them;
    # received mail is triage, worth much less per item and capped hard, because
    # mail arrives whether or not anyone was at their desk.
    include_email: bool = True
    include_chat: bool = True
    email_sent: SignalSpec = SignalSpec("email_sent", gap_minutes=30, lead_minutes=6,
                                        cap_minutes=150)
    email_received: SignalSpec = SignalSpec("email_received", gap_minutes=30, lead_minutes=2,
                                            cap_minutes=60)
    chat: SignalSpec = SignalSpec("chat", gap_minutes=20, lead_minutes=4, cap_minutes=150)
    # Correspondence inside a meeting is not extra work — it is the meeting. Sessions
    # are clipped against the day's calendar anchors before any of it is credited.
    clip_comms_to_free_time: bool = True

    # Optional AI label polish via an OpenAI-compatible endpoint. Empty model =>
    # AI off and the deterministic summariser is used. Applied ONLY in the
    # background refresh, never in a web request: a model can take 10-20s a call.
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: float = 30.0
    # A reasoning model with effort "low" keeps a one-line label to a few seconds.
    # Blank if a model rejects the field. Sent only when non-empty.
    llm_reasoning_effort: str = "low"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_model and self.llm_base_url)

    @property
    def strings(self) -> i18n.Locale:
        return i18n.get(self.locale)

    @property
    def labels(self) -> i18n.Labels:
        """Sheet labels for this config: the locale's, with user overrides on top."""
        return i18n.Labels(i18n.get(self.locale), self.label_overrides or {})

    def focus_category(self, key: str) -> str:
        """A focus_rules key -> the label for it, in this config's locale."""
        return self.strings.focus_categories.get(key, self.labels.focus_project)

    def with_settings(self, settings: dict) -> Config:
        """Overlay one user's saved settings. Unknown keys are ignored, so a
        setting removed from the model does not break an account that still has it."""
        known = set(self.__dataclass_fields__)
        patch = {k: v for k, v in (settings or {}).items() if k in known}
        for seq in ("workdays",):                     # JSON gives lists; the model wants tuples
            if isinstance(patch.get(seq), list):
                patch[seq] = tuple(patch[seq])
        return replace(self, **patch)

    @staticmethod
    def from_env(env: dict | None = None) -> Config:
        e = env or os.environ
        def _b(k, d): return e.get(k, str(d)).lower() in ("1", "true", "yes", "on")
        def _i(k, d): return int(e.get(k, d))
        def _f(k, d): return float(e.get(k, d))
        return Config(
            locale=e.get("LOCALE", i18n.DEFAULT_LOCALE),
            tz=e.get("TZ", "UTC"),
            day_start=e.get("DAY_START", "08:30"),
            earliest_start_floor=e.get("EARLIEST_START_FLOOR", "06:00"),
            day_rollover_hour=_i("DAY_ROLLOVER_HOUR", 5),
            min_day_minutes=_i("MIN_DAY_MINUTES", 480),
            max_day_minutes=_i("MAX_DAY_MINUTES", 840),
            rota_enabled=_b("ROTA_ENABLED", False),
            rota_minutes=_i("ROTA_MINUTES", 75),
            include_after_hours=_b("INCLUDE_AFTER_HOURS", False),
            include_reviews=_b("INCLUDE_REVIEWS", True),
            include_authored=_b("INCLUDE_AUTHORED", True),
            include_email=_b("INCLUDE_EMAIL", True),
            include_chat=_b("INCLUDE_CHAT", True),
            llm_base_url=e.get("LLM_BASE_URL", ""),
            llm_api_key=e.get("LLM_API_KEY", ""),
            llm_model=e.get("LLM_MODEL", ""),
            llm_timeout=_f("LLM_TIMEOUT", 30.0),
            llm_reasoning_effort=e.get("LLM_REASONING_EFFORT", "low"),
        )
