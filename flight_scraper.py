import asyncio
import logging
import re
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)


class FlightScraper:
    """
    Flight scraper using Playwright to search Google Flights.
    Extracts airline, price, times, duration, stops, and booking links.
    """

    BASE_URL = "https://www.google.com/travel/flights"

    def __init__(self):
        self.timeout = 45000  # 45 seconds

    async def search_flights(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str = None,
        trip_type: str = "one_way"
    ) -> list[dict]:
        """
        Main search function. Returns list of flight dicts.
        Each dict: airline, price, departure_time, arrival_time, duration, stops, booking_url
        """
        logger.info(f"Searching: {origin} → {destination} on {departure_date}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-web-security",
                    "--disable-features=VizDisplayCompositor",
                    "--single-process",
                ]
            )

            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="ar-SA",
            )

            page = await context.new_page()

            try:
                flights = await self._scrape_google_flights(
                    page, origin, destination, departure_date, return_date, trip_type
                )
                return flights

            except Exception as e:
                logger.error(f"Scraping error: {e}")
                raise

            finally:
                await browser.close()

    async def _scrape_google_flights(
        self, page, origin, destination, departure_date, return_date, trip_type
    ) -> list[dict]:
        """Scrape Google Flights results."""

        # Build URL
        url = self._build_google_flights_url(
            origin, destination, departure_date, return_date, trip_type
        )
        logger.info(f"Navigating to: {url}")

        await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)

        # Wait for results to load
        try:
            await page.wait_for_selector(
                '[class*="flight"], [data-result], li[class*="pIav2d"], div[jsname]',
                timeout=30000
            )
        except PlaywrightTimeout:
            logger.warning("Timeout waiting for flight selectors, trying fallback")

        # ── 1. Confirm prices are rendered in SAR before reading anything ─────
        await self._wait_for_sar_currency(page)

        # Give extra time for dynamic content to settle after currency confirms
        await asyncio.sleep(2)

        # ── 2. Extract raw flights via cascading strategies ────────────────────
        flights = await self._extract_flights_strategy_1(page)

        if not flights:
            logger.info("Strategy 1 failed, trying strategy 2")
            flights = await self._extract_flights_strategy_2(page)

        if not flights:
            logger.info("Strategy 2 failed, trying strategy 3 (text parsing)")
            flights = await self._extract_flights_strategy_3(page, url)

        logger.info(f"Found {len(flights)} raw flights")

        # ── 3. Drop sponsored / promoted cards ────────────────────────────────
        flights = self._filter_sponsored(flights)
        logger.info(f"{len(flights)} flights remaining after sponsored filter")

        # ── 4. Sort and keep cheapest 3 ───────────────────────────────────────
        flights = self._sort_and_top3(flights)
        logger.info(f"Returning {len(flights)} cheapest flights after sorting")
        return flights

    # ── Currency guard ────────────────────────────────────────────────────────
    async def _wait_for_sar_currency(self, page, retries: int = 5) -> None:
        """
        Poll the page until at least one SAR price token is visible.
        If currency hasn't appeared after `retries` attempts, log a warning
        and continue rather than aborting the whole search.
        """
        # Patterns that confirm SAR prices are rendered
        SAR_PATTERNS = ["SAR", "ريال", "ر.س", "ر.س.", "﷼"]
        CHECK_JS = (
            "() => {"
            "  const body = document.body.innerText || '';"
            "  return " + " || ".join(f"body.includes('{p}')" for p in SAR_PATTERNS) + ";"
            "}"
        )

        for attempt in range(1, retries + 1):
            try:
                found = await page.evaluate(CHECK_JS)
                if found:
                    logger.info("SAR currency confirmed on attempt %d", attempt)
                    return
            except Exception as exc:
                logger.debug("Currency check JS error (attempt %d): %s", attempt, exc)

            logger.debug("SAR not yet visible (attempt %d/%d), waiting 1 s…", attempt, retries)
            await asyncio.sleep(1)

        logger.warning(
            "SAR currency not confirmed after %d attempts — "
            "prices may be in a different currency; proceeding anyway.",
            retries,
        )

    # ── Sponsored-flight filter ───────────────────────────────────────────────
    # Keywords that appear in Google Flights sponsored / promoted cards
    _SPONSORED_TOKENS = frozenset({
        # English labels
        "sponsored", "promoted", "ad", "advertisement",
        # Arabic labels Google Flights uses
        "إعلان", "ممول", "مروّج", "برعاية",
    })

    @classmethod
    def _is_sponsored(cls, flight: dict) -> bool:
        """
        Return True when any field of the flight dict contains a sponsorship
        marker.  Matching is case-insensitive and works on all string fields.
        """
        for value in flight.values():
            if not isinstance(value, str):
                continue
            lower = value.lower()
            if any(token in lower for token in cls._SPONSORED_TOKENS):
                return True
        return False

    @classmethod
    def _filter_sponsored(cls, flights: list[dict]) -> list[dict]:
        """Remove sponsored / promoted flights and log what was dropped."""
        clean, dropped = [], []
        for f in flights:
            (dropped if cls._is_sponsored(f) else clean).append(f)

        if dropped:
            logger.info(
                "Dropped %d sponsored flight(s): %s",
                len(dropped),
                [d.get("airline", "?") for d in dropped],
            )
        return clean

    @staticmethod
    def _parse_price_int(price_raw) -> int:
        """
        Convert any price value to a plain integer for sorting.
        Returns sys.maxsize if the price is missing or unparseable
        so that unknown-price flights sink to the bottom.
        """
        import sys, re
        if price_raw is None:
            return sys.maxsize
        # Strip everything that isn't a digit
        digits = re.sub(r"[^\d]", "", str(price_raw))
        return int(digits) if digits else sys.maxsize

    def _sort_and_top3(self, flights: list[dict]) -> list[dict]:
        """
        1. Attach a numeric `price_int` key to every flight dict.
        2. Sort ascending by that key (cheapest first).
        3. Return the top 3.
        """
        for flight in flights:
            flight["price_int"] = self._parse_price_int(flight.get("price"))

        sorted_flights = sorted(flights, key=lambda f: f["price_int"])

        top3 = sorted_flights[:3]
        logger.info(
            "Top-3 prices: %s",
            [f.get("price", "N/A") for f in top3]
        )
        return top3

    def _build_google_flights_url(
        self, origin, destination, departure_date, return_date, trip_type
    ) -> str:
        """Build Google Flights search URL sorted by price (cheapest first)."""
        dep = departure_date  # e.g., 2025-03-15

        # Common query params: language, region, currency, sort by price
        base_params = "hl=ar&gl=sa&curr=SAR&sort=price"

        if trip_type == "round_trip" and return_date:
            # Round-trip: two legs separated by *, sorted by price
            url = (
                f"https://www.google.com/travel/flights?"
                f"{base_params}"
                f"#flt={origin}.{destination}.{dep}"
                f"*{destination}.{origin}.{return_date}"
                f";c:SAR;e:1;sd:1;t:f;s:0"
            )
        else:
            # One-way, sorted by price
            url = (
                f"https://www.google.com/travel/flights?"
                f"{base_params}"
                f"#flt={origin}.{destination}.{dep}"
                f";c:SAR;e:1;sd:1;t:f;s:0"
            )

        return url

    async def _extract_flights_strategy_1(self, page) -> list[dict]:
        """
        Strategy 1: scrape the 'Best flights' and 'Other flights' sections
        separately, tag each card with its section, combine, and return all
        unique results.  Deduplication is done on (departure_time, arrival_time).
        """
        flights: list[dict] = []

        # ── Section selectors ─────────────────────────────────────────────────
        # Google Flights renders two <ul> / <ol> groups.  The section heading
        # immediately precedes each list; we locate both headings then grab the
        # sibling list so we can label cards accordingly.
        SECTION_MAP = {
            "best":  {
                "headings": [
                    # English
                    "h3:has-text('Best flights')",
                    "div:has-text('Best flights'):not(:has(*))",
                    # Arabic
                    "h3:has-text('أفضل الرحلات')",
                    "div:has-text('أفضل الرحلات'):not(:has(*))",
                ],
                "label": "best",
            },
            "other": {
                "headings": [
                    "h3:has-text('Other flights')",
                    "div:has-text('Other flights'):not(:has(*))",
                    "h3:has-text('رحلات أخرى')",
                    "div:has-text('رحلات أخرى'):not(:has(*))",
                ],
                "label": "other",
            },
        }

        # ── Card-level selectors (tried in order until one yields results) ────
        CARD_SELECTORS = [
            "li.pIav2d",
            "li[class*='flight']",
            "div[class*='yR1fYc']",
            "div[jsname='IWWDBc']",
            "[data-ved] li",
        ]

        async def cards_for_section(section_label: str, heading_selectors: list[str]) -> list[dict]:
            """Return parsed flight dicts for one named section."""
            section_cards: list = []

            for h_sel in heading_selectors:
                try:
                    heading = await page.query_selector(h_sel)
                    if not heading:
                        continue

                    # Walk up until we find a container that also holds the list
                    # then query only *within* that container.
                    container = await heading.evaluate_handle(
                        "(el) => el.closest('div[class]') || el.parentElement"
                    )
                    if not container:
                        continue

                    for c_sel in CARD_SELECTORS:
                        section_cards = await container.query_selector_all(c_sel)
                        if section_cards:
                            logger.info(
                                "Section '%s': %d cards via '%s' (heading '%s')",
                                section_label, len(section_cards), c_sel, h_sel,
                            )
                            break

                    if section_cards:
                        break
                except Exception as exc:
                    logger.debug("Section heading probe failed (%s): %s", h_sel, exc)

            results = []
            for card in section_cards[:10]:
                try:
                    flight = await self._parse_flight_card_v1(card, page)
                    if flight and (flight.get("price") or flight.get("airline")):
                        flight["section"] = section_label
                        results.append(flight)
                except Exception as exc:
                    logger.debug("Card parse error in section '%s': %s", section_label, exc)

            return results

        # ── Scrape both sections ───────────────────────────────────────────────
        best_flights  = await cards_for_section("best",  SECTION_MAP["best"]["headings"])
        other_flights = await cards_for_section("other", SECTION_MAP["other"]["headings"])

        logger.info(
            "Section totals — best: %d, other: %d",
            len(best_flights), len(other_flights),
        )

        # ── Fallback: if section detection failed, grab all cards page-wide ───
        if not best_flights and not other_flights:
            logger.info("Section detection failed — falling back to page-wide card scan")
            for c_sel in CARD_SELECTORS:
                all_cards = await page.query_selector_all(c_sel)
                if all_cards:
                    logger.info("Page-wide fallback: %d cards via '%s'", len(all_cards), c_sel)
                    for card in all_cards[:15]:
                        try:
                            flight = await self._parse_flight_card_v1(card, page)
                            if flight and (flight.get("price") or flight.get("airline")):
                                flight["section"] = "unknown"
                                flights.append(flight)
                        except Exception as exc:
                            logger.debug("Page-wide card error: %s", exc)
                    break
            return flights

        # ── Combine & deduplicate on (departure_time, arrival_time) ──────────
        combined = best_flights + other_flights
        seen: set[tuple] = set()
        for f in combined:
            key = (f.get("departure_time", ""), f.get("arrival_time", ""))
            if key in seen:
                logger.debug("Duplicate flight skipped: %s", key)
                continue
            seen.add(key)
            flights.append(f)

        logger.info(
            "Combined unique flights: %d (best=%d, other=%d, dupes dropped=%d)",
            len(flights), len(best_flights), len(other_flights),
            len(combined) - len(flights),
        )
        return flights

    async def _parse_flight_card_v1(self, card, page) -> dict | None:
        """Parse individual flight card."""
        try:
            text_content = await card.inner_text()
            if not text_content or len(text_content) < 10:
                return None

            flight = self._parse_text_content(text_content)

            # Try to get booking URL
            try:
                link = await card.query_selector("a")
                if link:
                    href = await link.get_attribute("href")
                    if href:
                        if href.startswith("http"):
                            flight["booking_url"] = href
                        else:
                            flight["booking_url"] = f"https://www.google.com{href}"
            except Exception:
                pass

            return flight if flight.get("price") or flight.get("airline") else None

        except Exception as e:
            logger.debug(f"Parse card error: {e}")
            return None

    async def _extract_flights_strategy_2(self, page) -> list[dict]:
        """Strategy 2: Extract using page text content and regex."""
        flights = []

        try:
            # Get all text from the page
            content = await page.content()
            text = await page.inner_text("body")

            flights = self._parse_page_text(text)

        except Exception as e:
            logger.error(f"Strategy 2 error: {e}")

        return flights

    async def _extract_flights_strategy_3(self, page, url) -> list[dict]:
        """Strategy 3: Fallback with simulated flight data structure."""
        logger.info("Using fallback data extraction")

        # Extract what we can from the page title and URL
        flights = []

        try:
            title = await page.title()
            text = await page.inner_text("body")

            # Look for any price patterns
            prices = re.findall(r'[\d,]+\s*(?:ريال|SAR|ر\.س)', text)
            times = re.findall(r'\d{1,2}:\d{2}(?:\s*[صم])?', text)
            airlines = self._find_airlines_in_text(text)

            if prices and times:
                for i, price in enumerate(prices[:5]):
                    flight = {
                        "airline": airlines[i] if i < len(airlines) else "خطوط جوية",
                        "price": self._clean_price(price),
                        "departure_time": times[i * 2] if i * 2 < len(times) else "N/A",
                        "arrival_time": times[i * 2 + 1] if i * 2 + 1 < len(times) else "N/A",
                        "duration": "",
                        "stops": "مباشر",
                        "booking_url": url,
                    }
                    flights.append(flight)

        except Exception as e:
            logger.error(f"Strategy 3 error: {e}")

        return flights

    def _parse_text_content(self, text: str) -> dict:
        """Parse flight info from text content."""
        flight = {
            "airline": "",
            "price": "",
            "departure_time": "",
            "arrival_time": "",
            "duration": "",
            "stops": "مباشر",
            "booking_url": "",
        }

        lines = [line.strip() for line in text.split('\n') if line.strip()]

        for line in lines:
            # Price patterns
            if re.search(r'\d{3,}', line) and any(c in line for c in ['ر', 'SAR', 'ريال', '٪']):
                price_match = re.search(r'[\d,]+', line.replace('٬', ',').replace('٫', '.'))
                if price_match:
                    flight["price"] = price_match.group().replace(',', '')

            # Time patterns (HH:MM)
            time_matches = re.findall(r'\d{1,2}:\d{2}', line)
            if time_matches:
                if not flight["departure_time"]:
                    flight["departure_time"] = time_matches[0]
                elif not flight["arrival_time"] and len(time_matches) > 1:
                    flight["arrival_time"] = time_matches[1]
                elif not flight["arrival_time"]:
                    flight["arrival_time"] = time_matches[0]

            # Duration patterns
            dur_match = re.search(r'(\d+)\s*س(?:اعة)?.*?(\d+)?\s*د(?:قيقة)?', line)
            if dur_match:
                hours = dur_match.group(1)
                minutes = dur_match.group(2) or "00"
                flight["duration"] = f"{hours}س {minutes}د"

            # Airline detection
            airline = self._find_airline_in_line(line)
            if airline and not flight["airline"]:
                flight["airline"] = airline

            # Stops
            if 'توقف' in line or 'stop' in line.lower():
                stops_match = re.search(r'(\d+)\s*توقف', line)
                if stops_match:
                    flight["stops"] = stops_match.group(1)

            if 'مباشر' in line or 'direct' in line.lower() or 'Nonstop' in line:
                flight["stops"] = "مباشر"

        return flight

    def _parse_page_text(self, text: str) -> list[dict]:
        """Parse multiple flights from full page text."""
        flights = []
        
        # Split by common patterns that indicate new flight entries
        sections = re.split(r'(?=\d{1,2}:\d{2}.*\d{1,2}:\d{2})', text)
        
        for section in sections[:8]:
            if len(section) < 20:
                continue
            flight = self._parse_text_content(section)
            if flight.get("price") or flight.get("departure_time"):
                flights.append(flight)
        
        return flights[:5]

    def _find_airlines_in_text(self, text: str) -> list[str]:
        """Find airline names in text."""
        airlines_known = [
            "الخطوط السعودية", "flyadeal", "flynas", "العربية للطيران",
            "الإمارات", "القطرية", "الكويتية", "الخليج",
            "طيران ناس", "طيران أديل", "اتحاد", "فلاي دبي",
            "Turkish Airlines", "Egypt Air", "Oman Air"
        ]
        found = []
        for airline in airlines_known:
            if airline.lower() in text.lower():
                found.append(airline)
        return found

    def _find_airline_in_line(self, line: str) -> str:
        """Find airline name in a single line."""
        airlines_map = {
            "سعودية": "الخطوط السعودية",
            "Saudi": "الخطوط السعودية",
            "flyadeal": "طيران أديل",
            "flynas": "طيران ناس",
            "ناس": "طيران ناس",
            "أديل": "طيران أديل",
            "Emirates": "طيران الإمارات",
            "الإمارات": "طيران الإمارات",
            "Qatar": "القطرية",
            "قطر": "القطرية",
            "Kuwait": "الخطوط الجوية الكويتية",
            "كويت": "الخطوط الجوية الكويتية",
            "Etihad": "الاتحاد للطيران",
            "اتحاد": "الاتحاد للطيران",
            "Turkish": "الخطوط التركية",
            "تركيا": "الخطوط التركية",
            "Egypt": "مصر للطيران",
            "مصر": "مصر للطيران",
            "خليج": "طيران الخليج",
            "Oman": "الطيران العُماني",
            "عُمان": "الطيران العُماني",
            "flydubai": "فلاي دبي",
            "دبي": "فلاي دبي",
            "Jazeera": "طيران الجزيرة",
            "جزيرة": "طيران الجزيرة",
        }

        for key, value in airlines_map.items():
            if key.lower() in line.lower():
                return value
        return ""

    def _clean_price(self, price_str: str) -> str:
        """Clean and format price string."""
        cleaned = re.sub(r'[^\d]', '', price_str)
        return cleaned if cleaned else "غير متاح"
