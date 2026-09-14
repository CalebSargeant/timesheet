"""User-facing strings, per locale.

Everything a human reads — column headers, block labels, weekday names, the
delivery message — resolves through here, so one person's sheet can be English
while another's is Dutch on the same deployment.

Two kinds of string live here and they must not be confused:

  * **Labels** are written onto the sheet. They follow the *reader's* locale
    (the manager's, set per user), because that is who has to make sense of the
    row.
  * **Markers** are matched against calendar subjects to decide what a thing IS
    (leave, a standup, a desk booking). They are matched across EVERY locale at
    once, never just the active one: a Dutch "Verlof" lands in the calendar of
    an English-speaking user whenever HR is Dutch, and reading it as an ordinary
    meeting would reconstruct a day off into eight hours of invented work.

Adding a locale means adding one `Locale` below. Nothing else needs to know.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields

DEFAULT_LOCALE = "en"


@dataclass(frozen=True)
class Locale:
    code: str
    name: str

    days: tuple[str, ...]
    months: tuple[str, ...]          # index 1..12; [0] is unused padding

    # Sheet columns, in order.
    col_date: str
    col_from: str
    col_to: str
    col_duration: str
    col_project: str
    col_task: str
    total: str

    # Block labels.
    standup_project: str
    meeting_project: str
    focus_project: str
    admin_project: str
    leave_project: str
    leave_task: str
    rota_project: str
    rota_task: str
    review_project: str
    pr_project: str
    issue_project: str
    email_project: str
    chat_project: str

    # Rotated through so a quiet day is not a column of identical rows.
    admin_tasks: tuple[str, ...]
    # Focus categories, keyed by the rule name in Config.focus_rules.
    focus_categories: dict[str, str]

    # Period filter.
    period_this_week: str
    period_last_week: str
    period_this_month: str
    period_last_month: str
    period_to: str                   # the word between two dates in a range

    # Web chrome.
    title: str
    subtitle_hours: str
    download: str
    updated: str
    empty: str
    show: str

    # Delivery.
    mail_subject: str                # {week} {total}
    mail_body: str                   # {greeting} {week} {total} {url} {sender}
    chat_body: str                   # {week} {total} {url}

    # The AI label-polish instruction, when AI is switched on.
    llm_system: str
    llm_prompt: str                  # {items}


EN = Locale(
    code="en",
    name="English",
    days=("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    months=("", "January", "February", "March", "April", "May", "June", "July",
            "August", "September", "October", "November", "December"),
    col_date="Date", col_from="From", col_to="To", col_duration="Duration",
    col_project="Project / client", col_task="Task", total="TOTAL",
    standup_project="Internal",
    meeting_project="Meeting",
    focus_project="Development",
    admin_project="Admin",
    leave_project="Leave",
    leave_task="Leave / away",
    rota_project="On-call",
    rota_task="Checks and standby",
    review_project="Code review",
    pr_project="Pull requests",
    issue_project="Issues / tickets",
    email_project="Correspondence",
    chat_project="Chat / coordination",
    admin_tasks=("Email / chat / GitHub", "Admin", "Coordination", "Documentation"),
    focus_categories={
        "security": "Security",
        "monitoring": "Monitoring",
        "cicd": "CI/CD",
        "docs": "Documentation",
    },
    period_this_week="This week", period_last_week="Last week",
    period_this_month="This month", period_last_month="Last month",
    period_to="to",
    title="Timesheet",
    subtitle_hours="h",
    download="Download .xlsx",
    updated="updated",
    empty="Nothing recorded for this period.",
    show="Show",
    mail_subject="Timesheet week {week} ({total})",
    mail_body=("Hi{greeting},\n\nAttached is the timesheet for the week of {week} "
               "(total {total}).\nAlways up to date at {url}\n\nBest,\n{sender}\n"),
    chat_body="Timesheet for the week of {week}: {total}. Details: {url}",
    llm_system=("You turn technical work into one short, professional task line for a "
                "timesheet. At most 10 words. No commit jargon, no issue numbers, no "
                "quotation marks. Reply with the task line only."),
    llm_prompt=("Summarise this work as one short, professional English task line "
                "(max 10 words), with no commit jargon or issue numbers:\n{items}"),
)


NL = Locale(
    code="nl",
    name="Nederlands",
    days=("Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"),
    months=("", "januari", "februari", "maart", "april", "mei", "juni", "juli",
            "augustus", "september", "oktober", "november", "december"),
    col_date="Datum", col_from="Van", col_to="Tot", col_duration="Duur",
    col_project="Project / klant", col_task="Taak", total="TOTAAL",
    standup_project="Intern",
    meeting_project="Meeting",
    focus_project="Development",
    admin_project="Administratie",
    leave_project="Verlof",
    leave_task="Verlof / afwezig",
    rota_project="Ochtenddienst",
    rota_task="Checks en standby",
    review_project="Code review",
    pr_project="Pull requests",
    issue_project="Issues / tickets",
    email_project="Correspondentie",
    chat_project="Teams / overleg",
    admin_tasks=("Mail / GitHub / Teams", "Administratie", "Afstemming / overleg",
                 "Documentatie / kennisdeling"),
    focus_categories={
        "security": "Security",
        "monitoring": "Monitoring",
        "cicd": "CI/CD",
        "docs": "Documentatie",
    },
    period_this_week="Deze week", period_last_week="Vorige week",
    period_this_month="Deze maand", period_last_month="Vorige maand",
    period_to="tot",
    title="Uren",
    subtitle_hours="u",
    download="Download .xlsx",
    updated="bijgewerkt",
    empty="Geen gegevens voor deze periode.",
    show="Toon",
    mail_subject="Urenstaat week {week} ({total})",
    mail_body=("Hoi{greeting},\n\nBijgevoegd de urenstaat voor week {week} "
               "(totaal {total}).\nAltijd actueel te bekijken op {url}\n\nGroet,\n{sender}\n"),
    chat_body="Urenstaat voor week {week}: {total}. Details: {url}",
    llm_system=("Je vat technisch werk samen tot één korte, professionele Nederlandse "
                "taakregel voor een urenstaat. Maximaal 10 woorden. Geen commit-jargon, "
                "geen issue-nummers, geen aanhalingstekens. Antwoord met alleen de taakregel."),
    llm_prompt=("Vat dit werk samen in één korte, professionele Nederlandse taakregel "
                "(max 10 woorden), zonder commit-jargon of issue-nummers:\n{items}"),
)


LOCALES: dict[str, Locale] = {loc.code: loc for loc in (EN, NL)}


def get(code: str | None) -> Locale:
    """The locale for `code`, falling back to English.

    A bare language subtag is enough: 'nl-NL' and 'NL' both resolve to Dutch, so
    a browser Accept-Language header can be handed straight in.
    """
    if not code:
        return LOCALES[DEFAULT_LOCALE]
    key = code.strip().lower().replace("_", "-").split("-")[0]
    return LOCALES.get(key, LOCALES[DEFAULT_LOCALE])


def choices() -> list[tuple[str, str]]:
    """(code, display name) for every locale, for a settings dropdown."""
    return [(loc.code, loc.name) for loc in LOCALES.values()]


def _union(attr: str) -> tuple[str, ...]:
    """Every locale's value for a label, lowercased — for cross-locale matching."""
    out: set[str] = set()
    for loc in LOCALES.values():
        value = getattr(loc, attr)
        if isinstance(value, str):
            out.add(value.lower())
        else:
            out.update(v.lower() for v in value)
    return tuple(sorted(out))


# --- markers: matched across every locale at once, never just the active one ---

# A day-long busy event whose subject says any of these is an absence.
LEAVE_MARKERS: tuple[str, ...] = (
    # en
    "leave", "holiday", "vacation", "day off", "out of office", "absent", "sick",
    "pto", "annual leave", "parental", "bank holiday", "public holiday",
    # nl
    "verlof", "vakantie", "vrije dag", "feestdag", "afwezig", "ziek", "adv", "atv",
    # de / fr / es — cheap to carry, and a mis-read costs a whole invented day
    "urlaub", "krank", "feiertag", "congé", "vacances", "malade", "férié",
    "vacaciones", "enfermo", "festivo",
)

# A subject that says nothing at all. A published calendar set to "availability
# only" replaces every subject with a bare "Busy", so a whole day blocked out
# with no title is an absence, not an eight-hour meeting called "Busy".
BLANK_SUBJECTS: tuple[str, ...] = (
    "", "busy", "private", "out of office", "no title", "untitled",
    "bezet", "privé", "prive", "geen titel",
    "beschäftigt", "privat", "ocupado", "occupé",
)

# Recurring team check-ins, labelled as internal rather than as a generic meeting.
STANDUP_MARKERS: tuple[str, ...] = (
    "standup", "stand-up", "daily", "scrum", "dagstart", "daily sync",
)

# Calendar noise that is not work: desk bookings, birthdays, lunch.
DROP_MARKERS: tuple[str, ...] = (
    "booking", "desk", "birthday", "lunch", "verjaardag", "geburtstag",
    "anniversaire", "cumpleaños",
)


def label_fields() -> tuple[str, ...]:
    """Names of the Locale fields that are sheet labels — what Config overlays."""
    skip = {"code", "name", "days", "months", "admin_tasks", "focus_categories"}
    return tuple(f.name for f in fields(Locale) if f.name not in skip)


@dataclass(frozen=True)
class Labels:
    """The subset of a Locale that lands on the sheet, after user overrides.

    Split out from Locale so a user can rename 'Admin' to their employer's
    billing code without forking a whole locale.
    """
    locale: Locale
    overrides: dict[str, str] = field(default_factory=dict)

    def __getattr__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError(name)
        over = object.__getattribute__(self, "overrides")
        if name in over:
            return over[name]
        return getattr(object.__getattribute__(self, "locale"), name)
