from dataclasses import dataclass
from typing import TypedDict


Point = tuple[float, float, float]
ROOF_COMBO = "SHED-ROOF-SERVICE"
WIND_COMBO = "SHED-WIND-SERVICE"
COMBINATIONS = {
    ROOF_COMBO: ("SHED-DEAD", "SHED-ROOF-LIVE"),
    WIND_COMBO: ("SHED-DEAD", "SHED-WIND"),
}
COMBO_LABELS = {ROOF_COMBO: "Roof · D + Lr", WIND_COMBO: "Wind · D + W"}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    unit: str
    minimum: float
    maximum: float
    step: float


GEOMETRY_FIELDS = (
    Field("span", "Clear span", "m", 6, 30, 0.5),
    Field("bay_length", "Bay length", "m", 3, 10, 0.5),
    Field("bays", "Number of bays", "", 1, 6, 1),
    Field("eave_height", "Eave height", "m", 3, 12, 0.5),
    Field("rise", "Ridge rise", "m", 0.8, 6, 0.1),
    Field("panels", "Panels per roof slope", "", 2, 5, 1),
)
LOAD_FIELDS = (
    Field("roof_dead", "Roof superimposed dead load", "kPa", 0, 3, 0.05),
    Field("roof_live", "Roof live load", "kPa", 0, 5, 0.05),
    Field("wall_wind", "Side wall wind pressure", "kPa", 0, 5, 0.05),
    Field("roof_uplift", "Roof wind uplift", "kPa", 0, 5, 0.05),
)


@dataclass(frozen=True)
class Shed:
    span: float = 16.0
    bay_length: float = 5.0
    bays: int = 3
    eave_height: float = 5.0
    rise: float = 3.0
    panels: int = 3
    roof_dead: float = 0.2
    roof_live: float = 0.6
    wall_wind: float = 0.75
    roof_uplift: float = 0.6
    wind_side: str = "+X"

    @classmethod
    def from_values(cls, values: dict[str, object]) -> "Shed":
        arguments = {}
        for spec in (*GEOMETRY_FIELDS, *LOAD_FIELDS):
            value = values.get(spec.key)
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise ValueError(f"Enter a number for {spec.label.lower()}.")
            if not spec.minimum <= value <= spec.maximum:
                raise ValueError(f"{spec.label} must be between {spec.minimum:g} and {spec.maximum:g}.")
            if spec.key in {"bays", "panels"}:
                if int(value) != value:
                    raise ValueError(f"{spec.label} must be a whole number.")
                value = int(value)
            else:
                value = float(value)
            arguments[spec.key] = value
        side = values.get("wind_side")
        if side not in ("+X", "-X"):
            raise ValueError("Select a wind direction.")
        return cls(**arguments, wind_side=side)

    @property
    def length(self) -> float:
        return self.bays * self.bay_length


@dataclass(frozen=True)
class Section:
    name: str
    depth: float
    width: float
    web: float
    flange: float = 0.0


SECTIONS = {
    "column": Section("UC 254×254×73", 0.2541, 0.2546, 0.0086, 0.0142),
    "chord": Section("UB 305×165×40", 0.3034, 0.1650, 0.0060, 0.0102),
    "purlin": Section("UB 305×165×40", 0.3034, 0.1650, 0.0060, 0.0102),
    "web": Section("CHS 88.9×5", 0.0889, 0.0889, 0.005),
    "roof_brace": Section("CHS 114.3×5", 0.1143, 0.1143, 0.005),
    "side_brace": Section("CHS 114.3×5", 0.1143, 0.1143, 0.005),
}


@dataclass(frozen=True)
class Member:
    start: int
    end: int
    kind: str


class RoofLoad(TypedDict):
    joint: int
    dead: float
    live: float
    uplift: float


class WallLoad(TypedDict):
    member: int
    line: float


@dataclass(frozen=True)
class Model:
    points: list[Point]
    members: list[Member]
    bases: list[int]
    roof: list[RoofLoad]
    wind: list[WallLoad]


def build_model(shed: Shed) -> Model:
    points: list[Point] = []
    members: list[Member] = []
    bases: list[int] = []
    roof: list[RoofLoad] = []
    wind: list[WallLoad] = []

    def point(x: float, y: float, z: float) -> int:
        points.append((x, y, z))
        return len(points) - 1

    def member(start: int, end: int, kind: str) -> int:
        members.append(Member(start, end, kind))
        return len(members) - 1

    panel_count = 2 * shed.panels
    panel_width = shed.span / panel_count
    top_lines: list[list[int]] = []
    base_lines: list[list[int]] = []
    windward = 0 if shed.wind_side == "+X" else 1
    wind_sign = 1 if windward == 0 else -1

    for station in range(shed.bays + 1):
        y = station * shed.bay_length
        width = shed.bay_length * (0.5 if station in (0, shed.bays) else 1.0)
        base = [point(0.0, y, 0.0), point(shed.span, y, 0.0)]
        bases.extend(base)
        base_lines.append(base)
        top = [
            point(i * panel_width, y, shed.eave_height + shed.rise * min(i, panel_count - i) / shed.panels)
            for i in range(panel_count + 1)
        ]
        top_lines.append(top)
        bottom = [top[0]]
        bottom.extend(point(i * panel_width, y, shed.eave_height) for i in range(1, panel_count))
        bottom.append(top[-1])

        for side, eave in enumerate((top[0], top[-1])):
            column = member(base[side], eave, "column")
            if side == windward and shed.wall_wind:
                wind.append({"member": column, "line": wind_sign * shed.wall_wind * width})

        for i in range(panel_count):
            member(top[i], top[i + 1], "chord")
            member(bottom[i], bottom[i + 1], "chord")

        for i in range(1, panel_count):
            member(bottom[i], top[i], "web")
        for i in range(1, panel_count - 1):
            if i < shed.panels:
                member(top[i], bottom[i + 1], "web")
            else:
                member(bottom[i], top[i + 1], "web")

        for i, joint in enumerate(top):
            area = panel_width * width * (0.5 if i in (0, panel_count) else 1.0)
            roof.append({
                "joint": joint,
                "dead": -shed.roof_dead * area,
                "live": -shed.roof_live * area,
                "uplift": shed.roof_uplift * area,
            })

        if station:
            for near, far in zip(top_lines[-2], top):
                member(near, far, "purlin")

    near_top, far_top = top_lines[-2:]
    near_base, far_base = base_lines[-2:]
    for start, end in ((0, shed.panels), (shed.panels, panel_count)):
        member(near_top[start], far_top[end], "roof_brace")
        member(far_top[start], near_top[end], "roof_brace")
    for side, edge in enumerate((0, panel_count)):
        member(near_base[side], far_top[edge], "side_brace")
        member(far_base[side], near_top[edge], "side_brace")

    return Model(points, members, bases, roof, wind)


def load_totals(shed: Shed) -> dict[str, float]:
    area = shed.span * shed.length
    sign = 1 if shed.wind_side == "+X" else -1
    return {
        "plan_area_m2": area,
        "roof_dead_kn": shed.roof_dead * area,
        "roof_live_kn": shed.roof_live * area,
        "roof_uplift_kn": shed.roof_uplift * area,
        "wall_wind_kn": sign * shed.wall_wind * shed.eave_height * shed.length,
    }
