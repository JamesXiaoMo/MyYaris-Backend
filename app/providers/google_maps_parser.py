import ipaddress
import re
import socket
from urllib.parse import parse_qs, urlparse
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


def is_allowed_hostname(hostname: str) -> bool:
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


def extract_coordinates_from_url(url: str) -> tuple[float, float] | None:
    # 1. Search for !3d<lat>!4d<lon> (Google Maps place pinpoint)
    match_data = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url)
    if match_data:
        lat = float(match_data.group(1))
        lon = float(match_data.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon

    # 2. Search for query parameters q=lat,lon or destination=lat,lon
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for param_name in ["q", "destination", "ll", "daddr"]:
        if param_name in qs:
            val = qs[param_name][0]
            match_coords = re.match(r"^(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)$", val)
            if match_coords:
                lat = float(match_coords.group(1))
                lon = float(match_coords.group(2))
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return lat, lon

    # 3. Search for /@lat,lon,
    match_at = re.search(r"/@(-?\d+\.\d+),(-?\d+\.\d+)", url)
    if match_at:
        lat = float(match_at.group(1))
        lon = float(match_at.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon

    return None


async def resolve_and_parse_map_url(url: str, max_hops: int = 4) -> PlaceCandidateItem | None:
    current_url = url
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=False) as client:
        for _ in range(max_hops):
            parsed = urlparse(current_url)
            hostname = parsed.hostname
            if not hostname or not is_allowed_hostname(hostname):
                raise SSRFSecurityError(f"Host '{hostname}' is not in allowed map domains.")

            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            verify_ip_security(hostname, port)

            coords = extract_coordinates_from_url(current_url)
            if coords:
                lat, lon = coords
                return PlaceCandidateItem(
                    title="Google Maps Location",
                    address=current_url,
                    latitude=lat,
                    longitude=lon,
                    coord_system="wgs84",
                    source="google_maps_link",
                )

            try:
                resp = await client.head(current_url, headers={"User-Agent": "Mozilla/5.0 MyYaris/1.0"})
                if resp.is_redirect and "location" in resp.headers:
                    current_url = resp.headers["location"]
                    continue
                # If head doesn't redirect, try get
                resp = await client.get(current_url, headers={"User-Agent": "Mozilla/5.0 MyYaris/1.0"})
                if resp.is_redirect and "location" in resp.headers:
                    current_url = resp.headers["location"]
                    continue
                current_url = str(resp.url)
                break
            except httpx.HTTPError:
                break

    coords = extract_coordinates_from_url(current_url)
    if coords:
        lat, lon = coords
        return PlaceCandidateItem(
            title="Google Maps Location",
            address=current_url,
            latitude=lat,
            longitude=lon,
            coord_system="wgs84",
            source="google_maps_link",
        )
    return None
