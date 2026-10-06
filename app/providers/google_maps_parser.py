import ipaddress
import re
import socket
from urllib.parse import parse_qs, unquote, urljoin, urlparse
import httpx
from app.providers.base import PlaceCandidateItem, SSRFSecurityError

ALLOWED_DOMAINS = {
    "maps.google.com",
    "www.google.com",
    "google.com",
    "goo.gl",
    "maps.app.goo.gl",
    "google.co.jp",
    "maps.google.co.jp",
}

# Coordinate regex matching both integer and floating point degrees [-90..90, -180..180]
COORD_REGEX = r"^(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$"
PROTOBUF_COORD_REGEX = r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)"


def is_allowed_hostname(hostname: str) -> bool:
    if not hostname:
        return False
    hostname = hostname.lower()
    return any(hostname == d or hostname.endswith("." + d) for d in ALLOWED_DOMAINS)


def verify_ip_security(hostname: str, port: int = 443) -> None:
    try:
        addr_info = socket.getaddrinfo(hostname, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFSecurityError(f"DNS resolution failed for {hostname}: {e}")

    for item in addr_info:
        ip_str = item[4][0]
        ip = ipaddress.ip_address(ip_str)
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local or ip.is_multicast:
            raise SSRFSecurityError(f"Security check rejected IP {ip_str} for {hostname}")


def validate_url_security(url_str: str) -> tuple[str, int]:
    parsed = urlparse(url_str)
    if parsed.scheme not in ("http", "https"):
        raise SSRFSecurityError(f"Disallowed scheme '{parsed.scheme}': only http and https are permitted.")
    hostname = parsed.hostname
    if not hostname or not is_allowed_hostname(hostname):
        raise SSRFSecurityError(f"Host '{hostname}' is not in allowed map domains.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    verify_ip_security(hostname, port)
    return hostname, port


def extract_coordinates_from_url(url: str) -> tuple[float, float, str] | None:
    """
    Extracts verified destination coordinates from Google Maps URLs.
    CRITICAL RESTRICTIONS:
    1. 'll=lat,lon' alone represents map center view, NOT destination.
    2. In route URLs (/dir/), explicit destination/daddr is required. Never pick the first
       !3d!4d because in directions URLs the first coordinate pair is the route ORIGIN/start.
    3. If multiple distinct !3d!4d exist without unambiguous destination proof, do not guess.
    4. Supports integer and float coordinates.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    title = "Google Maps Location"
    match_place = re.search(r"/place/([^/@?]+)", url)
    if match_place:
        title = unquote(match_place.group(1)).replace("+", " ")

    is_dir_route = "/dir/" in parsed.path or parsed.path.startswith("/maps/dir")

    # Priority 1: Explicit destination query parameters in routes or search
    for param_name in ["destination", "daddr"]:
        if param_name in qs:
            val = qs[param_name][0]
            match_coords = re.match(COORD_REGEX, val)
            if match_coords:
                lat = float(match_coords.group(1))
                lon = float(match_coords.group(2))
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    pin_title = title if title != "Google Maps Location" else f"Google Maps Destination ({lat:.4f}, {lon:.4f})"
                    return lat, lon, pin_title

    # Priority 2: Explicit search query (q=lat,lon)
    # (If q has coordinates, it is an explicit destination pin, even if ll is also present)
    if "q" in qs:
        val = qs["q"][0]
        match_coords = re.match(COORD_REGEX, val)
        if match_coords:
            lat = float(match_coords.group(1))
            lon = float(match_coords.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                pin_title = title if title != "Google Maps Location" else f"Google Maps Pin ({lat:.4f}, {lon:.4f})"
                return lat, lon, pin_title

    # If it is a route (/dir/) without explicit destination parameter,
    # DO NOT extract !3d!4d because the first coordinate pair is the route start/origin!
    if is_dir_route:
        return None

    # Priority 3: Non-route Protobuf pinpoint !3d<lat>!4d<lon>
    matches = re.findall(PROTOBUF_COORD_REGEX, url)
    if matches:
        unique_coords = set()
        for lat_s, lon_s in matches:
            lat = float(lat_s)
            lon = float(lon_s)
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                unique_coords.add((lat, lon))

        # Only accept if there is exactly 1 distinct coordinate pair; multiple points means ambiguity
        if len(unique_coords) == 1:
            lat, lon = unique_coords.pop()
            return lat, lon, title
        elif len(unique_coords) > 1:
            return None

    # Notice: 'll=lat,lon' alone is map view center, NOT destination.
    # Viewport center (/@lat,lon) is NOT extracted as destination.
    return None


def is_viewport_only_url(url: str) -> bool:
    """Returns True if the URL contains only map view center (/@... or ll=...) without verified destination pinpoint or place name."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    has_viewport_path = bool(re.search(r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", url))
    has_ll_param = "ll" in qs and "destination" not in qs and "daddr" not in qs and "q" not in qs
    has_coords = extract_coordinates_from_url(url) is not None
    has_place = extract_place_name_from_url(url) is not None

    return (has_viewport_path or has_ll_param) and not has_coords and not has_place


def is_route_without_destination(url: str) -> bool:
    """Returns True if URL is a route (/dir/) but lacks explicit destination parameters."""
    parsed = urlparse(url)
    is_dir_route = "/dir/" in parsed.path or parsed.path.startswith("/maps/dir")
    has_coords = extract_coordinates_from_url(url) is not None
    return is_dir_route and not has_coords


def has_multiple_coordinates(url: str) -> bool:
    """Returns True if URL contains multiple distinct !3d!4d coordinate pairs."""
    matches = re.findall(PROTOBUF_COORD_REGEX, url)
    if matches:
        unique_coords = {(float(lat), float(lon)) for lat, lon in matches if -90 <= float(lat) <= 90 and -180 <= float(lon) <= 180}
        return len(unique_coords) > 1
    return False


def extract_place_name_from_url(url: str) -> str | None:
    """Extracts place name from /place/<name> or non-coordinate query q if present."""
    match_place = re.search(r"/place/([^/@?]+)", url)
    if match_place:
        return unquote(match_place.group(1)).replace("+", " ")
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "q" in qs:
        val = qs["q"][0].strip()
        if not re.match(COORD_REGEX, val):
            return unquote(val).replace("+", " ")
    return None


class MapUrlResolutionResult:
    def __init__(
        self,
        candidate: PlaceCandidateItem | None = None,
        is_viewport_only: bool = False,
        is_route_ambiguous: bool = False,
        has_multiple_locations: bool = False,
        place_name: str | None = None,
        resolved_url: str = "",
        error: str | None = None,
    ):
        self.candidate = candidate
        self.is_viewport_only = is_viewport_only
        self.is_route_ambiguous = is_route_ambiguous
        self.has_multiple_locations = has_multiple_locations
        self.place_name = place_name
        self.resolved_url = resolved_url
        self.error = error


async def resolve_and_parse_map_url(url: str, max_hops: int = 4) -> MapUrlResolutionResult:
    current_url = url
    # 1. Validate initial URL
    validate_url_security(current_url)

    async with httpx.AsyncClient(timeout=6.0, follow_redirects=False) as client:
        for _ in range(max_hops):
            # Validate current URL before evaluating
            validate_url_security(current_url)

            # Check if current URL already has verified coordinates
            coords_data = extract_coordinates_from_url(current_url)
            if coords_data:
                lat, lon, title = coords_data
                cand = PlaceCandidateItem(
                    title=title,
                    address=current_url,
                    latitude=lat,
                    longitude=lon,
                    coord_system="wgs84",
                    source="google_maps_link",
                )
                return MapUrlResolutionResult(candidate=cand, resolved_url=current_url)

            # Follow redirects using urljoin for relative and absolute targets
            try:
                resp = await client.head(current_url, headers={"User-Agent": "Mozilla/5.0 MyYaris/1.0"})
                if resp.is_redirect and "location" in resp.headers:
                    next_url = urljoin(current_url, resp.headers["location"])
                    validate_url_security(next_url)
                    current_url = next_url
                    continue
                # If head doesn't redirect, try get
                resp = await client.get(current_url, headers={"User-Agent": "Mozilla/5.0 MyYaris/1.0"})
                if resp.is_redirect and "location" in resp.headers:
                    next_url = urljoin(current_url, resp.headers["location"])
                    validate_url_security(next_url)
                    current_url = next_url
                    continue
                current_url = str(resp.url)
                validate_url_security(current_url)
                break
            except httpx.HTTPError:
                break
        else:
            # If loop finished with max_hops exhausted while still redirecting, reject safely
            return MapUrlResolutionResult(
                candidate=None,
                resolved_url=current_url,
                error="重定向跳数过多，无法安全解析该链接。",
            )

    # Validate final settled URL before coordinate parsing
    validate_url_security(current_url)

    coords_data = extract_coordinates_from_url(current_url)
    if coords_data:
        lat, lon, title = coords_data
        cand = PlaceCandidateItem(
            title=title,
            address=current_url,
            latitude=lat,
            longitude=lon,
            coord_system="wgs84",
            source="google_maps_link",
        )
        return MapUrlResolutionResult(candidate=cand, resolved_url=current_url)

    # Coordinates could not be extracted; evaluate explicit ambiguity reasons
    viewport = is_viewport_only_url(current_url)
    route_ambiguous = is_route_without_destination(current_url)
    multi_loc = has_multiple_coordinates(current_url)
    place = extract_place_name_from_url(current_url)

    return MapUrlResolutionResult(
        candidate=None,
        is_viewport_only=viewport,
        is_route_ambiguous=route_ambiguous,
        has_multiple_locations=multi_loc,
        place_name=place,
        resolved_url=current_url,
    )
