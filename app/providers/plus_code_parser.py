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
    address: Optional[str] = None


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


async def resolve_plus_code(value: str, reference: Optional[Coordinate] = None) -> list[PlusCodeResult]:
    """Resolve Google copied short-code + locality text using a city reference."""
    import re
    from app.config import settings
    from app.providers.nominatim_provider import get_nominatim_provider, normalize_japanese_address, PREF_REGEX

    value = value.replace("＋", "+").strip()
    parts = value.split(maxsplit=1)
    initial = parse_plus_code(value, reference)
    if reference is not None or initial.error or initial.latitude is not None:
        return [initial]
    if len(parts) < 2:
        return [PlusCodeResult(question="短 Plus Code 缺少地区，请粘贴 Google 复制的完整文本（例如 CPF8+Q7 东广岛市 广岛县），或输入完整编码。")]
    if settings.MAPS_PROVIDER != "nominatim":
        return [PlusCodeResult(question="短编码包含地区，但城市查询服务尚未配置，请提供完整编码或参照坐标。")]

    locality = normalize_japanese_address(parts[1])
    city_match = re.search(r"([^\s]+?(?:市|郡))", re.sub(PREF_REGEX, " ", locality))
    prefecture_match = re.search(PREF_REGEX, locality)
    refs = await get_nominatim_provider().search(parts[1], locality_only=True)
    # Enforce explicit Japanese city/prefecture matches even if search is fuzzy.
    if city_match:
        refs = [r for r in refs if city_match.group(0) in (r.address or "")]
    if prefecture_match:
        refs = [r for r in refs if prefecture_match.group(0) in (r.address or "")]
    if not refs:
        return [PlusCodeResult(question="未能找到编码后面的城市，请补充市、县或提供完整 Plus Code。")]
    resolved: dict[str, PlusCodeResult] = {}
    for ref in refs:
        result = parse_plus_code(parts[0], Coordinate(latitude=ref.latitude, longitude=ref.longitude))
        if result.error or result.full_code is None:
            continue
        result.address = f"{parts[1]}；按城市参照还原 {result.full_code}，取编码区域中心 [© OpenStreetMap contributors]"
        resolved.setdefault(result.full_code, result)
    results = list(resolved.values())
    if not results:
        return [PlusCodeResult(question="城市参照无法还原此编码，请提供完整 Plus Code。")]
    # Common Google 4+2 short code with an explicit city AND prefecture can be
    # dispatched only when all city matches agree on one sufficiently fine code.
    if (len(results) == 1 and city_match and prefecture_match
            and parts[0].index("+") == 4 and olc.decode(results[0].full_code).codeLength >= 10):
        results[0].question = None
    else:
        for result in results:
            result.question = "按地区还原了 Plus Code，请确认候选目的地；同名城市或较短编码可能产生不同地点。"
    return results
