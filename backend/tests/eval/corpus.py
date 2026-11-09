"""The synthetic corpus and the gold question set of the evaluation harness.

Everything is generated here, so the corpus is small, free of real content and reproducible:

* depots.pdf      a text PDF (eight pages, one depot each, exact facts and look-alike filler)
* march-scan.pdf  three scanned pages (a picture of printed text, no text layer): read by Tesseract
* handbook.docx   a Word file with headings, a table and an embedded architecture diagram
* release.md      a Markdown file with headings, a list and a table

A standalone image is NOT an accepted source type in phase 1 (pdf, docx, txt, md), so the "diagram
image" is the picture inside handbook.docx.

Each gold item names the sources it uses, the exact phrases ("needles") a correct retrieval must put
in front of the model, and what the published answer is expected to cite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tests import analysis_files as af

# eight depots, one page each: the same vocabulary on every page, so a question must be answered by the
# right page and not just by any page that mentions parcels
DEPOTS = [
    (
        "Berlin",
        "4200",
        "Staffing at the Berlin depot rose by six people after the spring hiring round.",
    ),
    ("Lisbon", "3100", "The Lisbon depot closed early twice because of storms."),
    ("Madrid", "5100", "The Madrid depot added a third sorting belt in April."),
    ("Vienna", "2800", "The Vienna depot lost two delivery vans to a recall."),
    ("Oslo", "1900", "The Oslo depot paused night shifts during the ice warning."),
    ("Prague", "3600", "The Prague depot moved its returns desk to the east bay."),
    ("Rome", "4700", "The Rome depot trained twelve temporary packers."),
    ("Paris", "6100", "The Paris depot opened a second loading dock on the river side."),
]
_FILLER = (
    "Shift leads at the {c} depot reviewed loading bays, sorting belts and the returns desk each "
    "morning. Drivers collected routes from the {c} dispatch office and scanned every parcel at the "
    "gate. Customers in the {c} area could follow their deliveries in the tracking system, and the "
    "{c} supervisors wrote a short summary of delays at the end of every week."
)


def _wrap(text: str, width: int = 82) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return [*lines, line] if line else lines


def depots_pdf() -> bytes:
    streams = []
    for city, parcels, note in DEPOTS:
        lines = [
            f"{city} depot report for May.",
            f"The {city} depot handled {parcels} parcels in May.",
            note,
            *_wrap(_FILLER.format(c=city) + " " + _FILLER.format(c=city)),
        ]
        ops = ["BT /F1 11 Tf 72 740 Td 14 TL"]
        ops += [f"({line}) Tj T*" for line in lines]
        ops.append("ET")
        streams.append("\n".join(ops).encode("latin-1"))
    return af._pdf_package(streams)


SCAN_PAGES = [
    af.CLEAN_SCAN_LINES,
    [
        "Maintenance schedule for the sorting equipment",
        "Conveyor belts are inspected every second Monday.",
        "The label printers were replaced after repeated jams.",
        "Forklift batteries are charged overnight in the east bay.",
        "A spare scanner is kept at the dispatch office.",
        "Cleaning of the loading docks happens before each shift.",
        "Fire extinguishers were checked by the safety officer.",
        "The maintenance budget stayed within the agreed limit.",
    ],
    [
        "Training plan for new employees",
        "New drivers complete two days of route practice.",
        "Packers learn the handling rules for fragile items.",
        "Every trainee receives a printed guide to the tracking system.",
        "A mentor is assigned to each new employee for four weeks.",
        "Training records are signed by the shift leader.",
        "Refresher courses are offered every spring.",
        "Feedback from trainees is collected in a short survey.",
    ],
]


def sources() -> dict[str, bytes]:
    return {
        "depots.pdf": depots_pdf(),
        "march-scan.pdf": af.pdf_from_images([af.page_image(lines) for lines in SCAN_PAGES]),
        "handbook.docx": af.rich_docx(figure=af.diagram_png()),
        "release.md": af.MARKDOWN_SAMPLE.encode(),
    }


# the text the scanned page really contains: the reference for the OCR accuracy figure
SCAN_TRUTH = " ".join(" ".join(lines) for lines in SCAN_PAGES)
SCAN_NAME = "march-scan.pdf"

DOCUMENT_ITEMS = [
    ("heading", 1, "Launch plan"),
    ("paragraph", 0, "The launch is scheduled for Friday at noon."),
    ("paragraph", 0, "The budget is forty thousand dollars."),
    ("heading", 2, "Risks"),
    ("paragraph", 0, "Vendor delays could slip the schedule."),
]


@dataclass(frozen=True)
class Gold:
    id: str
    task: str  # the task kind this item exercises: qa, summarize, compare, figure, review, diagram
    instruction: str
    sources: tuple[str, ...] = ()
    needles: tuple[str, ...] = ()  # phrases the retrieved evidence must contain
    cites: tuple[str, ...] = ()  # source names the published answer must cite
    expect_figure: bool = False  # a figure passage must be cited
    expect_diagram: bool = False  # a diagram block must be published and render
    document: bool = False  # the open document is included
    extra: dict = field(default_factory=dict)


GOLD: tuple[Gold, ...] = (
    Gold(
        "qa-berlin",
        "qa",
        "How many parcels did the Berlin depot handle in May?",
        ("depots.pdf",),
        ("4200 parcels",),
        ("depots.pdf",),
    ),
    Gold(
        "qa-lisbon-storms",
        "qa",
        "Why did the Lisbon depot close early?",
        ("depots.pdf",),
        ("because of storms",),
        ("depots.pdf",),
    ),
    Gold(
        "qa-scan-orders",
        "qa",
        "How many orders did the warehouse process in March?",
        ("march-scan.pdf",),
        ("twelve thousand",),
        ("march-scan.pdf",),
    ),
    Gold(
        "qa-scan-delays",
        "qa",
        "What caused the delivery delays?",
        ("march-scan.pdf",),
        ("shortage of drivers",),
        ("march-scan.pdf",),
    ),
    Gold(
        "qa-md-owner",
        "qa",
        "Who owns the deploy step?",
        ("release.md",),
        ("Bob",),
        ("release.md",),
    ),
    Gold(
        "qa-md-restart",
        "qa",
        "What happens in the deploy step?",
        ("release.md",),
        ("restarts the workers",),
        ("release.md",),
    ),
    Gold(
        "qa-two-sources",
        "qa",
        "How many parcels did the Lisbon depot handle, and what did the warehouse process in March?",
        ("depots.pdf", "march-scan.pdf"),
        ("3100 parcels", "twelve thousand"),
        ("depots.pdf", "march-scan.pdf"),
    ),
    Gold(
        "figure-handbook",
        "figure",
        "What does the architecture diagram in the handbook show?",
        ("handbook.docx",),
        ("Client",),
        ("handbook.docx",),
        expect_figure=True,
    ),
    Gold(
        "summarize-release",
        "summarize",
        "Summarize the release process.",
        ("release.md",),
        (),
        ("release.md",),
    ),
    Gold(
        "compare-depots-release",
        "compare",
        "Compare what the depot report and the release process say about how work is organised.",
        ("depots.pdf", "release.md"),
        (),
        ("depots.pdf", "release.md"),
    ),
    Gold(
        "review-launch-plan",
        "review",
        "Review this document and list its risks.",
        (),
        (),
        (),
        document=True,
    ),
    Gold(
        "diagram-release",
        "diagram",
        "Draw a flowchart of the build and deploy steps.",
        ("release.md",),
        ("compile the sources",),
        ("release.md",),
        expect_diagram=True,
    ),
    Gold(
        "diagram-architecture",
        "diagram",
        "Draw the architecture shown in the handbook diagram.",
        ("handbook.docx",),
        ("Client",),
        ("handbook.docx",),
        expect_diagram=True,
        expect_figure=True,
    ),
)
