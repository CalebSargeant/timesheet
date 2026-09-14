"""timesheet — reconstruct a working week from the signals you already generate.

Calendar, mail, chat and GitHub activity in; a defensible timesheet out. The
reconstruction core is pure Python with no dependencies and no network; AI is
strictly optional and only ever polishes a label."""
from .config import Config
from .model import ActivityEvent, Block, Commit, Day, Meeting
from .reconstruct import reconstruct_day, reconstruct_week

__version__ = "0.2.0"

__all__ = [
    "ActivityEvent",
    "Block",
    "Commit",
    "Config",
    "Day",
    "Meeting",
    "__version__",
    "reconstruct_day",
    "reconstruct_week",
]
