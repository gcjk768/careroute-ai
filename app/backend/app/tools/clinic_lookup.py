import logging
import threading
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache
from geopy.distance import geodesic

logger = logging.getLogger(__name__)

DATASET_ID = "d_548c33ea2d99e29ec63a7cc9edcccedc"
DATASET_API = "https://api-production.data.gov.sg/v2/public/api/datasets"
CACHE_SECONDS = 60 * 60 * 24


@dataclass
class Clinic:
    name: str
    address: str
    postal: str
    phone: str
    latitude: float
    longitude: float
    programmes: list[str]


class ClinicLookupTool:
    def __init__(self):
        self.cache = TTLCache(
            maxsize=1,
            ttl=CACHE_SECONDS,
        )

        self.clinics: list[Clinic] = []
        self._fetch_lock = threading.Lock()

    def fetch_dataset(self):
        """Download and cache the CHAS clinic dataset."""

        # Return cached dataset if available. One download at a time: two
        # cold-start requests used to fetch the dataset twice.
        with self._fetch_lock:
            if "dataset" in self.cache:
                self.clinics = self.cache["dataset"]
                return self.clinics
            return self._download()

    def _download(self):

        logger.info("Downloading CHAS dataset...")
        logger.info("Requesting temporary download URL...")

        download = requests.get(
            f"https://api-open.data.gov.sg/v1/public/api/datasets/{DATASET_ID}/poll-download",
            timeout=60,
        )
        download.raise_for_status()

        download_url = download.json()["data"]["url"]

        logger.info("Downloading GeoJSON...")

        geojson = requests.get(
            download_url,
            timeout=60,
        ).json()

        features = geojson["features"]

        logger.info("Loaded %d clinics", len(features))

        clinics: list[Clinic] = []

        # Parse every clinic.
        for feature in features:
            props = feature["properties"]
            coords = feature["geometry"]["coordinates"]

            soup = BeautifulSoup(
                props["Description"],
                "html.parser",
            )

            fields = {}

            for row in soup.find_all("tr"):
                header = row.find("th")
                value = row.find("td")

                if header is None or value is None:
                    continue

                fields[header.get_text(strip=True)] = value.get_text(strip=True)

            address = " ".join(
                filter(
                    None,
                    [
                        fields.get("BLK_HSE_NO", ""),
                        fields.get("STREET_NAME", ""),
                        fields.get("BUILDING_NAME", ""),
                    ],
                )
            )

            if fields.get("FLOOR_NO") and fields.get("UNIT_NO"):
                address += f" #{fields['FLOOR_NO']}-{fields['UNIT_NO']}"

            clinic = Clinic(
                name=fields.get("HCI_NAME", ""),
                address=address,
                postal=fields.get("POSTAL_CD", ""),
                phone=fields.get("HCI_TEL", ""),
                latitude=coords[1],
                longitude=coords[0],
                programmes=[
                    x.strip()
                    for x in fields.get(
                        "CLINIC_PROGRAMME_CODE",
                        "",
                    ).split(",")
                    if x.strip()
                ],
            )

            clinics.append(clinic)

        self.clinics = clinics
        self.cache["dataset"] = clinics

        return clinics

    def find_candidate_clinics(
        self,
        latitude: float,
        longitude: float,
        programme: str | None = None,
        limit: int = 5,
    ) -> list[Clinic]:
        """Find the nearest clinics, optionally filtered by programme."""

        # The TTL is on the cache, not on `self.clinics`: checking only the
        # list meant the dataset was never refreshed after the first load.
        if not self.clinics or "dataset" not in self.cache:
            self.fetch_dataset()

        candidates: list[tuple[float, Clinic]] = []

        for clinic in self.clinics:
            if programme and programme not in clinic.programmes:
                continue

            distance = geodesic(
                (latitude, longitude),
                (clinic.latitude, clinic.longitude),
            ).km

            candidates.append((distance, clinic))

        candidates.sort(key=lambda x: x[0])

        return [
            clinic
            for _, clinic in candidates[:limit]
        ]
