"""Published growth diagrams are reproducible from measured, attributed snapshots."""

import json
import re
import runpy
from pathlib import Path
from xml.etree import ElementTree


def test_growth_diagrams_match_measurements() -> None:
    root = Path(__file__).resolve().parents[1]
    render = runpy.run_path(str(root / "scripts" / "render_database_growth.py"))["render"]
    charts = json.loads((root / "docs" / "database-growth.json").read_text())
    assert set(charts) == {"sqlite", "psql"}
    for chart in charts.values():
        assert [point["label"] for point in chart["snapshots"]] == [
            str(index) for index in range(1, len(chart["snapshots"]) + 1)
        ]
        generated = render(chart)
        assert not re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}:\d{2}", generated)
        assert (root / "docs" / chart["output"]).read_text() == generated
        svg = ElementTree.fromstring(generated)
        assert svg.find("{http://www.w3.org/2000/svg}title").text == chart["title"]
        assert len(svg.findall(".//{http://www.w3.org/2000/svg}circle")) == (
            3 * len(chart["snapshots"])
        )
        for point in chart["snapshots"]:
            assert point["size_mib"] > 0 and point["landmarks"] > 0
            assert point["references"] > 0 and point["source"]
