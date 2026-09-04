from dataclasses import dataclass


@dataclass
class IngestResult:
    row: dict  # the people row after the write
    newly_eligible: bool  # False → eligible before this event, or still not eligible
