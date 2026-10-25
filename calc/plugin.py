import ast
import math
import operator

import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Miles/1.0)"}

BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
FUNCTIONS = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
    "atan": math.atan, "log": math.log10, "ln": math.log, "exp": math.exp, "abs": abs, "round": round,
    "floor": math.floor, "ceil": math.ceil, "min": min, "max": max, "factorial": math.factorial,
    "radians": math.radians, "degrees": math.degrees,
}
CONSTANTS = {"pi": math.pi, "e": math.e}
LIMIT = 1e300


def evaluate(node):
    if isinstance(node, ast.Expression):
        return evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in CONSTANTS:
        return CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in UNARY:
        return UNARY[type(node.op)](evaluate(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in BINARY:
        left, right = evaluate(node.left), evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("That exponent is too large.")
        result = BINARY[type(node.op)](left, right)
        if isinstance(result, (int, float)) and abs(result) > LIMIT:
            raise ValueError("That number is too large.")
        return result
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords:
        args = [evaluate(a) for a in node.args]
        if node.func.id == "factorial" and args and args[0] > 1000:
            raise ValueError("That factorial is too large.")
        return FUNCTIONS[node.func.id](*args)
    raise ValueError("I can only do plain arithmetic and common maths functions.")


def tidy(number: float) -> float | int:
    if isinstance(number, int) or float(number).is_integer() and abs(number) < 1e15:
        return int(number)
    return float(f"{number:.10g}")


UNITS: dict[str, tuple[str, float]] = {}


def define(category: str, base: float, names: str) -> None:
    for name in names.split(","):
        UNITS[name.strip()] = (category, base)


define("length", 0.001, "mm,millimeter,millimetre")
define("length", 0.01, "cm,centimeter,centimetre")
define("length", 1, "m,meter,metre")
define("length", 1000, "km,kilometer,kilometre")
define("length", 0.0254, "in,inch,inche")
define("length", 0.3048, "ft,foot,feet")
define("length", 0.9144, "yd,yard")
define("length", 1609.344, "mi,mile")
define("mass", 1e-6, "mg,milligram")
define("mass", 0.001, "g,gram")
define("mass", 1, "kg,kilogram,kilo")
define("mass", 1000, "t,tonne,metric ton")
define("mass", 0.028349523125, "oz,ounce")
define("mass", 0.45359237, "lb,pound,lbs")
define("mass", 6.35029318, "st,stone")
define("volume", 0.001, "ml,milliliter,millilitre")
define("volume", 1, "l,liter,litre")
define("volume", 0.00492892159375, "tsp,teaspoon")
define("volume", 0.01478676478125, "tbsp,tablespoon")
define("volume", 0.0295735295625, "floz,fluid ounce")
define("volume", 0.2365882365, "cup")
define("volume", 0.473176473, "pint")
define("volume", 0.946352946, "quart")
define("volume", 3.785411784, "gal,gallon")
define("speed", 1, "m/s,mps")
define("speed", 1000 / 3600, "km/h,kph,kmh")
define("speed", 0.44704, "mph")
define("speed", 0.514444, "knot,kt")
define("data", 0.125, "bit")
define("data", 1, "b,byte")
define("data", 1e3, "kb,kilobyte")
define("data", 1e6, "mb,megabyte")
define("data", 1e9, "gb,gigabyte")
define("data", 1e12, "tb,terabyte")
define("data", 1024, "kib")
define("data", 1024**2, "mib")
define("data", 1024**3, "gib")
define("time", 0.001, "ms,millisecond")
define("time", 1, "s,sec,second")
define("time", 60, "min,minute")
define("time", 3600, "h,hr,hour")
define("time", 86400, "day")
define("time", 604800, "week")
define("area", 1, "m2,square meter,square metre")
define("area", 1e-4, "cm2,square centimeter")
define("area", 1e6, "km2,square kilometer")
define("area", 10000, "ha,hectare")
define("area", 4046.8564224, "acre")
define("area", 0.09290304, "ft2,square foot,square feet")
TEMPERATURES = {"c": "c", "celsius": "c", "f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}


def find_unit(name: str) -> tuple[str, float] | None:
    key = " ".join(name.lower().replace("°", "").replace("degrees", "").replace("degree", "").split())
    for candidate in (key, key.removesuffix("s"), key.removesuffix("es")):
        if candidate in UNITS:
            return UNITS[candidate]
    return None


def find_temperature(name: str) -> str | None:
    key = " ".join(name.lower().replace("°", "").replace("degrees", "").replace("degree", "").split())
    return TEMPERATURES.get(key) or TEMPERATURES.get(key.removesuffix("s"))


def convert_temperature(value: float, source: str, target: str) -> float:
    kelvin = {"c": value + 273.15, "f": (value - 32) * 5 / 9 + 273.15, "k": value}[source]
    return {"c": kelvin - 273.15, "f": (kelvin - 273.15) * 9 / 5 + 32, "k": kelvin}[target]


def register(api):
    @api.tool
    def calculate(expression: str) -> dict:
        """Work out a maths expression exactly. Always use this for arithmetic instead of doing sums in your head.

        Supports + - * / // % ** and brackets, plus sqrt, sin, cos, tan, log (base 10), ln, exp, abs, round, floor,
        ceil, min, max, factorial, pi and e. For a percentage, write it out, like 0.15 * 80.

        Args:
            expression: The expression, like "(12.5 + 7) * 3" or "sqrt(144)".
        """
        try:
            result = evaluate(ast.parse(expression.strip().replace("^", "**").replace("×", "*").replace("÷", "/"), mode="eval"))
        except ZeroDivisionError:
            return {"error": "Can't divide by zero."}
        except (SyntaxError, ValueError, TypeError, OverflowError) as e:
            return {"error": f"Couldn't work that out: {e}"}
        return {"expression": expression, "result": tidy(result)}

    @api.tool
    def convert_units(value: float, from_unit: str, to_unit: str) -> dict:
        """Convert between units of length, mass, volume, speed, area, time, data size and temperature.

        Args:
            value: The amount to convert.
            from_unit: The unit it is in, like "miles", "kg", "fahrenheit" or "GB".
            to_unit: The unit to convert to.
        """
        source_temp, target_temp = find_temperature(from_unit), find_temperature(to_unit)
        if source_temp and target_temp:
            result = convert_temperature(value, source_temp, target_temp)
        else:
            source, target = find_unit(from_unit), find_unit(to_unit)
            if not source:
                return {"error": f"I don't know the unit {from_unit!r}."}
            if not target:
                return {"error": f"I don't know the unit {to_unit!r}."}
            if source[0] != target[0]:
                return {"error": f"Can't convert {source[0]} to {target[0]}."}
            result = value * source[1] / target[1]
        return {"result": tidy(result), "text": f"{tidy(value)} {from_unit} is {tidy(result)} {to_unit}"}

    @api.tool
    async def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict:
        """Convert money using today's exchange rate.

        Args:
            amount: How much money.
            from_currency: Three letter code of the currency it is in, like USD or INR.
            to_currency: Three letter code of the currency to convert to.
        """
        source, target = from_currency.strip().upper(), to_currency.strip().upper()
        if source == target:
            return {"result": tidy(amount), "text": f"{tidy(amount)} {source}"}
        try:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                r = await client.get(
                    "https://api.frankfurter.dev/v1/latest",
                    params={"base": source, "symbols": target, "amount": amount},
                )
            if r.status_code == 404:
                return {"error": f"I don't have rates for {source} to {target}. Use three letter codes like USD or EUR."}
            r.raise_for_status()
        except httpx.HTTPError as e:
            return {"error": f"Rate lookup failed: {e}"}
        data = r.json()
        result = data["rates"][target]
        return {"result": tidy(result), "rate_date": data["date"], "text": f"{tidy(amount)} {source} is {tidy(result)} {target}"}
