import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"

# Today's ingestion behaviour, named so the client and the diagnostic's
# on-demand check can differ without either drifting.
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RETRIES = 3

# The ingestion diagnostic's shared HTTP status table. The category strings are
# written out rather than imported from core: importing core's vocabulary would
# break this plugin at import time on an older core, and core drops any value it
# does not recognise anyway.
#
# 400 and 422 decline because a malformed request is our bug, 429 because rate
# limiting is our polling schedule, and 3xx because a redirect says nothing
# about the source. Nothing here ever stamps UNKNOWN: declining leaves core's
# read-time classification free to do better later, and a stamp does not.
STATUS_CATEGORIES = {
    401: "AUTH_FAILED",
    403: "PERMISSION_DENIED",
    404: "PATH_NOT_FOUND",
}

# Earth Networks also answers HTTP 200 carrying its own `Code` in the body. That
# is a code the *server* sent, so it is layer 5 like any other — but the code
# space belongs to the vendor, which is why it gets its own table rather than
# reusing the one above by reference. The only value the API is documented to
# use is 200 for success, and the space is HTTP-shaped, so the HTTP readings are
# carried over for the codes below and everything else declines.
BODY_CODE_CATEGORIES = {
    401: "AUTH_FAILED",
    403: "PERMISSION_DENIED",
    404: "PATH_NOT_FOUND",
    500: "PROTOCOL_ERROR",
    502: "PROTOCOL_ERROR",
    503: "PROTOCOL_ERROR",
}


def category_for_status(status_code):
    """The diagnostic failure category for an HTTP status, or None where the
    status carries no honest one."""
    if status_code in STATUS_CATEGORIES:
        return STATUS_CATEGORIES[status_code]
    if status_code is not None and 500 <= status_code < 600:
        return "PROTOCOL_ERROR"
    return None


def _raise_for_status(response):
    """``raise_for_status()``, tagging the raised error for the diagnostic.

    The exception is stamped in place rather than wrapped, so the original type
    still matches core's own exception table and the traceback survives. A code
    from the server is proof the server answered, which is what makes every
    category derived from one layer 5.
    """
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        category = category_for_status(e.response.status_code)
        if category:
            e.adl_category = category
            e.adl_layer = 5
        raise


def _to_iso_utc(dt: datetime) -> str:
    """Earth Networks accepts RFC1123 in examples, but ISO-8601 UTC works and
    is cleaner. To send RFC1123 (Mon, 01 Sep 2025 00:00:00) instead, build it
    with dt.strftime('%a, %d %b %Y %H:%M:%S').
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(ISO_UTC)


def kph_to_ms(kph: Optional[float]) -> Optional[float]:
    return None if kph is None else (kph * 1000.0 / 3600.0)


@dataclass
class EarthNetworksConfig:
    base_url: str = "https://owc.enterprise.earthnetworks.com/Data/GetData.ashx"
    provider_id: int = 3  # From your sample (Earth Networks Inc)
    units: str = "metric"  # 'metric' or 'english'
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_factor: float = 0.5
    user_agent: str = "ADL-EarthNetworksClient/1.0"


class EarthNetworksClient:
    """
    Minimal, robust HTTP client for Earth Networks Observations.
    Expected JSON schema:
      Result -> HistoricalObservations -> [ { Observation: {...}, HighLow: {...} } ]
    """

    def __init__(
            self,
            cfg: Optional[EarthNetworksConfig] = None,
            session: Optional[requests.Session] = None,
    ) -> None:
        self.cfg = cfg or EarthNetworksConfig()
        self.session = session or self._build_session()

    def _build_session(self) -> requests.Session:
        sess = requests.Session()
        retry = Retry(
            total=self.cfg.max_retries,
            read=self.cfg.max_retries,
            connect=self.cfg.max_retries,
            backoff_factor=self.cfg.backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        sess.headers.update({"User-Agent": self.cfg.user_agent})
        return sess

    def build_url(
            self,
            station_id: str,
            start_utc: datetime,
            end_utc: datetime,
            *,
            provider_id: Optional[int] = None,
            units: Optional[str] = None,
    ) -> str:
        params = {
            "dt": "dobs",  # historical obs
            "pi": provider_id if provider_id is not None else self.cfg.provider_id,
            "si": station_id,
            "startdatetime": _to_iso_utc(start_utc),
            "enddatetime": _to_iso_utc(end_utc),
            "units": units or self.cfg.units,
        }

        return f"{self.cfg.base_url}?{urlencode(params)}"

    def fetch_raw(
            self,
            station_id: str,
            start_utc: datetime,
            end_utc: datetime,
    ) -> Dict:
        url = self.build_url(station_id, start_utc, end_utc)
        log.debug("EarthNetworks request: %s", url)
        resp = self.session.get(url, timeout=self.cfg.timeout)
        _raise_for_status(resp)
        data = resp.json()

        # A 2xx is not proof of an API response: requests follows redirects, so
        # an error page arrives here as a clean 200 and would otherwise be read
        # as an answer with no observations in it.
        if not isinstance(data, dict):
            raise ValueError("The response carried no result.")

        # EN sometimes returns {"Code":200,...} with payload at data["Result"]
        code = data.get("Code")

        if code:
            try:
                code_int = int(code)
            except (TypeError, ValueError):
                # A code we cannot read is still a failure, just not one we can
                # classify. It used to raise ValueError out of int() here.
                code_int = None

            if code_int != 200:
                error = RuntimeError(
                    f"EarthNetworks returned non-200 Code: {code} {data.get('ErrorMessage')}"
                )

                # Captured while it is still an integer. Core matches on
                # exception type and never on text, so a code stringified into
                # the message is invisible to the whole diagnostic.
                category = BODY_CODE_CATEGORIES.get(code_int)
                if category:
                    error.adl_category = category
                    error.adl_layer = 5

                raise error

        return data

    # ---------- Normalization to ADL's StationRecordModel-like dicts ----------

    def normalize(
            self, data: Dict
    ) -> Tuple[Dict, List[Dict], int]:
        """
        Returns:
          station_meta: dict with basic station metadata
          records: list of {'observation_time': datetime, 'values': {...}} for ingestion
          sources_count: how many observation bundles the response carried

        The count is read off the parsed response before the conversion loop
        below, so a bundle we drop for a missing timestamp still counts as
        something the source offered. It leaves the client by return value
        because the station link it is reported on belongs to the plugin.
        """
        result = data.get("Result") or {}
        station = result.get("Station") or {}
        hist = result.get("HistoricalObservations") or []

        station_meta = {
            "provider_id": station.get("ProviderId"),
            "provider_name": station.get("ProviderName"),
            "station_id": station.get("StationId"),
            "station_name": station.get("StationName"),
            "latitude": station.get("Latitude"),
            "longitude": station.get("Longitude"),
            "elevation_m": station.get("ElevationAboveSeaLevelMeters"),
            "timezone": station.get("TimeZone"),
            "inactive": station.get("Inactive"),
        }

        records: List[Dict] = []
        for item in hist:
            obs = (item or {}).get("Observation") or {}
            # ObservationTimeUtc is ISO with Z
            ts_str: Optional[str] = obs.get("ObservationTimeUtc") or obs.get("ObservationTimeUtcStr")
            if not ts_str:
                continue
            # Normalize time
            # Support both "2025-09-01T00:05:00Z" and "2025-09-01T00:05:00.0000000Z"
            ts_str = ts_str.replace("Z", "+00:00")
            try:
                obs_time = datetime.fromisoformat(ts_str)
            except Exception:
                # Fallback: try trimming fractional seconds
                obs_time = datetime.fromisoformat(ts_str.split(".")[0] + "+00:00")

            # Extract common parameters
            temperature_c = self._unwrap(obs.get("TemperatureC"))
            rh = self._unwrap(obs.get("Humidity"))
            mslp_hpa = self._unwrap(obs.get("PressureSeaLevelMBar"))
            wind_kph = self._unwrap(obs.get("WindSpeedKph"))
            wind_dir = self._unwrap(obs.get("WindDirectionDegrees"))
            rain_day_mm = self._unwrap(obs.get("RainMillimetersDaily"))  # daily accumulation
            gust_kph = self._unwrap(obs.get("WindGustKphHourly"))  # last hour max

            values: Dict[str, Optional[float]] = {
                "air_temperature": temperature_c,
                "relative_humidity": rh,
                "pressure_msl": mslp_hpa,
                "wind_speed": kph_to_ms(wind_kph) if wind_kph is not None else None,
                "wind_direction": wind_dir,
                "precipitation_amount_daily": rain_day_mm,
                "wind_gust": kph_to_ms(gust_kph) if gust_kph is not None else None,
                "dew_point": self._unwrap(obs.get("DewPointC")),
                "wet_bulb_temperature": self._unwrap(obs.get("WetBulbTemperatureC")),
                "visibility": self._unwrap(obs.get("VisibilityKilometers")),
                "altimeter": self._unwrap(obs.get("AltimeterMBar")),
                "solar_radiation": self._unwrap(obs.get("SolarIrradiance")),
            }

            records.append(
                {
                    "observation_time": obs_time,
                    **values,
                }
            )

        return station_meta, records, len(hist)

    @staticmethod
    def _unwrap(node) -> Optional[float]:
        """
        EN sometimes returns:
          - bare numbers: 21.3
          - objects: { "Value": 21.3, "QcApplied": ..., "QcResult": ... }
          - nulls
        This helper extracts the numeric Value regardless of wrapper.
        """
        if node is None:
            return None
        if isinstance(node, (int, float)):
            return float(node)
        if isinstance(node, dict):
            val = node.get("Value")
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None
        return None

    # ---------- High-level convenience ----------

    def get_data(
            self,
            station_id: str,
            start_utc: datetime,
            end_utc: datetime,
    ) -> Tuple[Dict, List[Dict], int]:
        data = self.fetch_raw(station_id, start_utc, end_utc)
        return self.normalize(data)
