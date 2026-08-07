"""Unit conversion and physical constants.

SymPy's physics.units powers dimensional conversion (convert_to), with a
curated alias layer for common names (mph, km, GB, °C...) and a physical
constants table. Temperature (°C/°F/K) is handled explicitly since sympy
treats Celsius as an offset scale.
"""

from __future__ import annotations

import re

from sympy.physics import units as u

#: alias -> (sympy unit expression as string, is_temperature)
_UNITS: dict[str, tuple[str, bool]] = {
    # length
    "m": ("meter", False), "meter": ("meter", False), "meters": ("meter", False),
    "km": ("kilometer", False), "kilometer": ("kilometer", False),
    "cm": ("centimeter", False), "centimeter": ("centimeter", False),
    "mm": ("millimeter", False), "millimeter": ("millimeter", False),
    "um": ("micrometer", False), "micron": ("micrometer", False),
    "nm": ("nanometer", False), "nanometer": ("nanometer", False),
    "mi": ("mile", False), "mile": ("mile", False), "miles": ("mile", False),
    "yd": ("yard", False), "yard": ("yard", False),
    "ft": ("foot", False), "foot": ("foot", False), "feet": ("foot", False),
    "in": ("inch", False), "inch": ("inch", False),
    "au": ("astronomical_unit", False), "ly": ("lightyear", False),
    "lightyear": ("lightyear", False), "parsec": ("parsec", False),
    # mass
    "g": ("gram", False), "gram": ("gram", False), "grams": ("gram", False),
    "kg": ("kilogram", False), "kilogram": ("kilogram", False),
    "mg": ("milligram", False), "milligram": ("milligram", False),
    "ug": ("microgram", False), "microgram": ("microgram", False),
    "lb": ("pound", False), "pound": ("pound", False), "lbs": ("pound", False),
    "oz": ("ounce", False), "ounce": ("ounce", False),
    "ton": ("metric_ton", False), "metric_ton": ("metric_ton", False),
    # time
    "s": ("second", False), "sec": ("second", False), "second": ("second", False),
    "seconds": ("second", False),
    "ms": ("millisecond", False), "millisecond": ("millisecond", False),
    "min": ("minute", False), "minute": ("minute", False),
    "h": ("hour", False), "hr": ("hour", False), "hour": ("hour", False),
    "hours": ("hour", False),
    "day": ("day", False), "days": ("day", False),
    "week": ("week", False), "year": ("year", False), "yr": ("year", False),
    # speed
    "m/s": ("meter/second", False), "km/h": ("kilometer/hour", False),
    "kph": ("kilometer/hour", False), "mph": ("mile/hour", False),
    "fps": ("foot/second", False), "knot": ("knot", False),
    "mach": ("mach", False), "c": ("speed_of_light", False),
    # energy / power
    "j": ("joule", False), "joule": ("joule", False),
    "kj": ("kilojoule", False), "kilojoule": ("kilojoule", False),
    "cal": ("calorie", False), "calorie": ("calorie", False),
    "kcal": ("kilocalorie", False), "kilocalorie": ("kilocalorie", False),
    "kwh": ("kilowatt_hour", False), "kwh_": ("kilowatt_hour", False),
    "wh": ("watt_hour", False), "ev": ("eV", False), "electronvolt": ("eV", False),
    "w": ("watt", False), "watt": ("watt", False),
    "kw": ("kilowatt", False), "kilowatt": ("kilowatt", False),
    "mw": ("megawatt", False), "megawatt": ("megawatt", False),
    "hp": ("horsepower", False), "horsepower": ("horsepower", False),
    # force / pressure
    "n": ("newton", False), "newton": ("newton", False),
    "pa": ("pascal", False), "pascal": ("pascal", False),
    "kpa": ("kilopascal", False), "kilopascal": ("kilopascal", False),
    "mpa": ("megapascal", False), "megapascal": ("megapascal", False),
    "bar": ("bar", False), "atm": ("atm", False), "torr": ("torr", False),
    "psi": ("pound_force/inch**2", False),
    # temperature
    "k": ("kelvin", True), "kelvin": ("kelvin", True),
    "celsius": ("celsius", True), "degc": ("celsius", True),
    "°c": ("celsius", True), "f": ("fahrenheit", True),
    "fahrenheit": ("fahrenheit", True), "degf": ("fahrenheit", True),
    "°f": ("fahrenheit", True),
    # volume
    "l": ("liter", False), "liter": ("liter", False), "liters": ("liter", False),
    "ml": ("milliliter", False), "milliliter": ("milliliter", False),
    "gal": ("gallon", False), "gallon": ("gallon", False),
    "quart": ("quart", False), "pint": ("pint", False),
    "cup": ("cup", False), "floz": ("fluid_ounce", False),
    # area
    "ha": ("hectare", False), "hectare": ("hectare", False),
    "acre": ("acre", False), "sqft": ("foot**2", False),
    "m2": ("meter**2", False), "km2": ("kilometer**2", False),
    # data
    "b": ("byte", False), "byte": ("byte", False), "bytes": ("byte", False),
    "kb": ("kilobyte", False), "mb": ("megabyte", False),
    "gb": ("gigabyte", False), "tb": ("terabyte", False),
    "kib": ("kibibyte", False), "mib": ("mebibyte", False),
    "gib": ("gibibyte", False),
    # frequency
    "hz": ("hertz", False), "hertz": ("hertz", False),
    "khz": ("kilohertz", False), "mhz": ("megahertz", False),
    "ghz": ("gigahertz", False),
}

#: aliases for units sympy lacks — built as composites of sympy quantities
_COMPOSITES: dict[str, str] = {
    "horsepower": "745.6998715822702*watt",
    "mach": "340.3*meter/second",
    "knot": "0.5144444444444445*meter/second",
    "gallon": "3.785411784*liter",
    "quart": "0.946352946*liter",
    "pint": "0.473176473*liter",
    "cup": "0.2365882365*liter",
    "fluid_ounce": "0.0295735295625*liter",
    "calorie": "4.184*joule",
    "kilocalorie": "4184*joule",
    "kilowatt_hour": "3.6*megajoule",
    "watt_hour": "3600*joule",
    "rydberg": "10973731.568160/meter",
    "wien_displacement_constant": "2.897771955e-3*meter*kelvin",
    "pound_force": "4.4482216152605*newton",
    "kilobyte": "kilo*byte",
    "megabyte": "mega*byte",
    "gigabyte": "giga*byte",
    "terabyte": "tera*byte",
    "metric_ton": "1000*kilogram",
    "kilopascal": "kilo*pascal",
    "megapascal": "mega*pascal",
    "kilojoule": "kilo*joule",
    "megajoule": "mega*joule",
    "kilowatt": "kilo*watt",
    "megawatt": "mega*watt",
    "kilohertz": "kilo*hertz",
    "megahertz": "mega*hertz",
    "gigahertz": "giga*hertz",
    "milligram": "milli*gram",
    "microgram": "micro*gram",
    "millisecond": "milli*second",
    "millimeter": "milli*meter",
    "centimeter": "centi*meter",
    "micrometer": "micro*meter",
    "nanometer": "nano*meter",
    "milliliter": "milli*liter",
}

#: physical constants: name -> (sympy expr, description, numeric target unit,
#:    fallback value when sympy cannot resolve the number)
_CONSTANTS: dict[str, tuple[str, str, str, float | None]] = {
    "speed_of_light": ("speed_of_light", "c = 299792458 m/s", "meter/second", None),
    "planck": ("planck", "h = 6.62607015e-34 J·s", "joule*second", None),
    "hbar": ("hbar", "ℏ = h/2π", "joule*second", 1.054571817e-34),
    "avogadro": ("avogadro_constant", "N_A = 6.02214076e23 /mol", "1/mole", None),
    "boltzmann": ("boltzmann_constant", "k_B = 1.380649e-23 J/K", "joule/kelvin", None),
    "elementary_charge": ("elementary_charge", "e = 1.602176634e-19 C", "coulomb", None),
    "electron_mass": ("electron_rest_mass", "m_e = 9.1093837015e-31 kg", "kilogram", None),
    "proton_mass": ("proton_mass", "m_p = 1.67262192369e-27 kg", "kilogram", 1.67262192369e-27),
    "neutron_mass": ("neutron_mass", "m_n = 1.67492749804e-27 kg", "kilogram", 1.67492749804e-27),
    "gravitational_constant": ("gravitational_constant", "G = 6.67430e-11 m³/(kg·s²)", "meter**3/(kilogram*second**2)", 6.67430e-11),
    "gravity": ("acceleration_due_to_gravity", "g = 9.80665 m/s² (standard)", "meter/second**2", None),
    "gas_constant": ("molar_gas_constant", "R = 8.314462618 J/(mol·K)", "joule/(mole*kelvin)", 8.314462618),
    "stefan_boltzmann": ("stefan_boltzmann_constant", "σ = 5.670374419e-8 W/(m²·K⁴)", "watt/(meter**2*kelvin**4)", 5.670374419e-8),
    "faraday": ("faraday_constant", "F = 96485.33212 C/mol", "coulomb/mole", None),
    "atomic_mass": ("atomic_mass_constant", "1 u = 1.66053906660e-27 kg", "kilogram", None),
    "rydberg": ("rydberg", "R∞ = 10973731.568160 /m", "1/meter", 10973731.568160),
    "bohr_radius": ("bohr_radius", "a₀ = 5.29177210903e-11 m", "meter", 5.29177210903e-11),
    "electron_volt": ("eV", "1 eV = 1.602176634e-19 J", "joule", None),
    "astronomical_unit": ("astronomical_unit", "1 au = 149597870700 m", "meter", None),
    "lightyear": ("lightyear", "1 ly = 9.4607304725808e15 m", "meter", None),
    "parsec": ("parsec", "1 pc ≈ 3.0857e16 m", "meter", 3.0856775814913673e16),
    "wien": ("wien_displacement_constant", "b = 2.897771955e-3 m·K", "meter*kelvin", 2.897771955e-3),
}

#: temperature conversions (sympy treats Celsius as an offset — handle manually)
def _temp_convert(value: float, src: str, dst: str) -> float:
    if src == "kelvin" and dst == "celsius":
        return value - 273.15
    if src == "celsius" and dst == "kelvin":
        return value + 273.15
    if src == "kelvin" and dst == "fahrenheit":
        return (value - 273.15) * 9 / 5 + 32
    if src == "fahrenheit" and dst == "kelvin":
        return (value - 32) * 5 / 9 + 273.15
    if src == "celsius" and dst == "fahrenheit":
        return value * 9 / 5 + 32
    if src == "fahrenheit" and dst == "celsius":
        return (value - 32) * 5 / 9
    raise ValueError("incompatible temperature scales")


def convert(value: float, from_unit: str, to_unit: str) -> dict:
    """Convert `value` from one unit to another (dimensional analysis)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"value must be numeric, got {value!r}"}

    fu = from_unit.strip().lower().replace(" ", "")
    tu = to_unit.strip().lower().replace(" ", "")
    fsrc = _UNITS.get(fu)
    tsrc = _UNITS.get(tu)
    if fsrc is None or tsrc is None:
        missing = fu if fsrc is None else tu
        return {"ok": False,
                "error": f"unknown unit '{missing}'. Known: {', '.join(sorted({k for k,_ in _UNITS.items() if len(k)<6}))}"}

    # temperature special case
    if fsrc[1] or tsrc[1]:
        if not (fsrc[1] and tsrc[1]):
            return {"ok": False, "error": "cannot mix temperature and non-temperature units"}
        try:
            result = _temp_convert(value, fsrc[0], tsrc[0])
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "value": round(result, 6), "unit": to_unit.strip(),
                "from": f"{value} {from_unit.strip()}"}

    # sympy dimensional conversion
    try:
        from_expr = value * _parse_unit(fsrc[0])
        to_expr = _parse_unit(tsrc[0])
        result = u.convert_to(from_expr, to_expr)
        numeric = float(result.evalf(10) / to_expr.evalf(10))
    except Exception as exc:
        return {"ok": False, "error": f"conversion failed: {exc}"}
    return {"ok": True, "value": round(numeric, 6), "unit": to_unit.strip(),
            "from": f"{value} {from_unit.strip()}"}


# ── safe unit-expression parser (no eval; grammar: number|name with * / **) ─

_TOKEN_RE = re.compile(r"\s*(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|[A-Za-z_]\w*|\*\*|[/\*])")


def _parse_unit(expr: str):
    """Parse a unit expression string into a sympy quantity. Restricted
    grammar only: numbers, names, `*`, `/`, `**`. Names resolve against the
    sympy units module or the composites table — never user code."""
    toks, pos = _TOKEN_RE.findall(expr), 0

    def peek() -> str | None:
        return toks[pos] if pos < len(toks) else None

    def atom():
        nonlocal pos
        t = peek()
        if t is None:
            raise ValueError("unexpected end of unit expression")
        if t == "**":
            raise ValueError("unexpected '**'")
        if t.replace(".", "", 1).isdigit():
            pos += 1
            return float(t)
        if t in _COMPOSITES:
            pos += 1
            return _parse_unit(_COMPOSITES[t])
        if hasattr(u, t):
            pos += 1
            return getattr(u, t)
        raise ValueError(f"unknown unit name '{t}'")

    def power():
        nonlocal pos
        base = atom()
        if peek() == "**":
            pos += 1
            exp_t = peek()
            if exp_t is None or not exp_t.replace(".", "", 1).isdigit():
                raise ValueError("bad exponent")
            pos += 1
            return base ** float(exp_t)
        return base

    def product():
        nonlocal pos
        val = power()
        while peek() in ("*", "/"):
            op = toks[pos]
            pos += 1
            rhs = power()
            val = val * rhs if op == "*" else val / rhs
        return val

    result = product()
    if pos != len(toks):
        raise ValueError(f"trailing tokens in unit expression: {toks[pos:]}")
    return result


def constants(name: str | None = None) -> dict:
    """Look up physical constants by name (or list all)."""
    if not name:
        return {"ok": True, "constants": [
            {"name": k, "value": v[1]} for k, v in sorted(_CONSTANTS.items())]}
    key = name.strip().lower()
    entry = _CONSTANTS.get(key)
    if entry is None:
        close = [k for k in _CONSTANTS if key in k]
        return {"ok": False, "error": f"unknown constant '{name}'",
                "suggestions": close[:5]}
    sym, desc, target, fallback = entry
    val = fallback
    if val is None:
        try:
            expr = _parse_unit(sym)
            tgt = _parse_unit(target)
            val = float(u.convert_to(expr, tgt).evalf(12) / tgt.evalf(12))
        except Exception:
            val = None
    return {"ok": True, "name": key, "description": desc,
            "value": val, "symbol": sym}


def _si_of(expr):
    """Best-effort SI decomposition for a constant's numeric value."""
    try:
        from sympy.physics.units.systems.si import SI
        return SI._get_dimensional_expr(expr)
    except Exception:
        return expr


def list_units() -> dict:
    """List every supported unit alias grouped by dimension."""
    return {"ok": True,
            "count": len(_UNITS),
            "units": sorted({k: v[0] for k, v in _UNITS.items()}.keys())}
