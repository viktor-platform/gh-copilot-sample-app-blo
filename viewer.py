from dataclasses import asdict
import json
from pathlib import Path

from etabs_model import ComboResult, platform_note
from shed import COMBO_LABELS, GEOMETRY_FIELDS, LOAD_FIELDS, ROOF_COMBO, SECTIONS, Shed, build_model, load_totals


WEB = Path(__file__).resolve().parent / "web"


def model_payload(shed: Shed, results: dict[str, ComboResult] | None = None) -> dict[str, object]:
    return {
        **asdict(build_model(shed)),
        "inputs": asdict(shed),
        "length": shed.length,
        "sections": {kind: asdict(section) for kind, section in SECTIONS.items()},
        "combinations": results,
        "combo_labels": COMBO_LABELS,
        "roof_combo": ROOF_COMBO,
        "load_totals": load_totals(shed),
    }


def render_view(shed: Shed, results: dict[str, ComboResult] | None = None) -> str:
    data = model_payload(shed, results)
    data["fields"] = [asdict(spec) for spec in (*GEOMETRY_FIELDS, *LOAD_FIELDS)]
    data["solver_note"] = platform_note()
    payload = json.dumps(data, allow_nan=False).replace("<", "\\u003c")
    template = (WEB / "index.html").read_text()
    script = (WEB / "scene.bundle.js").read_text()
    css = (WEB / "style.css").read_text()
    return template.replace("/* STYLE */", css).replace("/* MODEL */", payload).replace("/* SCENE */", script)
