"""Manual listening reminder timing; no playback or completion tracking."""
import random


def random_analysis_minutes(randint=random.randint):
    minutes=int(randint(10,20))
    if not 10 <= minutes <= 20:
        raise ValueError("Reminder interval must be between 10 and 20 minutes.")
    return minutes
