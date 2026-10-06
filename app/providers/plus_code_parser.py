"""Offline Open Location Code decoding; never guess a short code's locality."""
from dataclasses import dataclass
from typing import Optional
from openlocationcode import openlocationcode as olc
from app.schemas.common import Coordinate


@dataclass
class PlusCodeResult:
    full_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    question: Optional[str] = None
    error: Optional[str] = None


def parse_plus_code(value: str, reference: Optional[Coordinate] = None) -> PlusCodeResult:
    # Google often copies a short code followed by a locality; the locality is
    # not itself a reference coordinate and must not be silently ignored.
    parts = value.strip().upper().split(maxsplit=1)
    if not parts or not olc.isValid(parts[0]):
        return PlusCodeResult(error="无效的 Plus Code，请复制含 + 的完整编码。")
    code = parts[0]
    recovered = olc.isShort(code)
    if recovered:
        if reference is None:
            return PlusCodeResult(question="这是短 Plus Code，请提供完整编码或附近的 reference_coordinate 经纬度；城市名称不能直接离线解码。")
        if reference.coord_system.lower() != "wgs84":
            return PlusCodeResult(error="短编码参照坐标必须使用 wgs84。")
        code = olc.recoverNearest(code, reference.latitude, reference.longitude)
    elif not olc.isFull(code):
        return PlusCodeResult(error="Plus Code 不能定位到有效的地球坐标。")
    area = olc.decode(code)
    question = None
    if recovered:
        question = "已按参照位置还原最近匹配的短编码，请确认目的地；参照位置选错可能导致错误地点。"
    elif area.codeLength < 10:
        question = "该编码覆盖较大区域，请确认区域中心是否为目的地，或提供更长的 Plus Code。"
    return PlusCodeResult(code, area.latitudeCenter, area.longitudeCenter, question)
