"""Комнаты, датчики воздуха и исполнительные устройства из таблицы."""
from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class Room:
    name: str
    floor: int
    sensors: tuple
    radiators: tuple = ()
    floor_climate: str = ""


SLOTS = {f"room_{i}": 1 if i <= 4 else 2 for i in range(1, 11)}
GROUPS = {"home": tuple(SLOTS)}
for level in (1, 2):
    GROUPS[f"floor{level}"] = tuple(k for k, floor in SLOTS.items() if floor == level)
GROUPS.update({k: (k,) for k in SLOTS})
GROUP_NAMES = {"home": "Весь дом", "floor1": "Первый этаж", "floor2": "Второй этаж"}


def average(values):
    valid = [x for x in values if x is not None]
    return round(mean(valid), 2) if valid else None


HYSTERESIS_C = 1.0


def demands(targets, readings, previous, hysteresis=HYSTERESIS_C):
    """Start a hysteresis interval below target; keep heating up to the target."""
    result = {}
    for key, temperature in readings.items():
        target = targets[key]
        result[key] = temperature is not None and temperature < target and (
            temperature <= target - hysteresis
            or (previous.get(key, False) and temperature < target)
        )
    return result


def outputs(heat, rooms, floor, boiler):
    wanted = {}
    for k, room in rooms.items():
        for radiator in room.radiators:
            wanted[radiator] = heat[k]
        if room.floor_climate:
            wanted[room.floor_climate] = heat[k]
    wanted[floor] = any(heat[k] for k in GROUPS["floor1"])
    wanted[boiler] = any(heat.values())
    return wanted
