"""
Steam Market API client.

Provides methods to fetch:
- Price overview (lowest_price, median_price, volume)
- Buy orders (highest_buy_order, buy_order_graph)

Supports SOCKS5/HTTP proxy for regions where Steam is blocked.
"""
import re
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import aiohttp
from aiohttp_socks import ProxyConnector

from src.bottm.config import Currency, STEAM_CURRENCY_CODES, config

logger = logging.getLogger(__name__)


@dataclass
class PriceOverview:
    """Steam market price overview."""
    success: bool
    lowest_price: Optional[float] = None
    median_price: Optional[float] = None
    volume: Optional[int] = None
    currency: Currency = Currency.RUB


@dataclass
class BuyOrderInfo:
    """Steam market buy order information."""
    success: bool
    highest_buy_order: Optional[float] = None  # In currency units (not cents)
    lowest_sell_order: Optional[float] = None
    buy_order_count: int = 0
    sell_order_count: int = 0
    # Raw order graph: [[price_cents, cumulative_qty, "desc"], ...]
    buy_order_graph: Optional[list] = None

    def count_orders_above_price(self, price: float) -> int:
        """
        Count how many buy orders are at prices ABOVE the given price.

        This tells you how many orders need to fill before yours.
        Steam's graph shows cumulative quantities at each price level.
        """
        if not self.buy_order_graph:
            return 0

        price_cents = int(price * 100)
        orders_above = 0

        # Graph is sorted by price descending (highest first)
        # Each entry: [price_cents, cumulative_qty, description]
        # cumulative_qty is total orders at this price AND ABOVE
        for entry in self.buy_order_graph:
            if len(entry) >= 2:
                level_price = entry[0]  # in cents
                cumulative_qty = entry[1]  # orders at this price and above

                if level_price > price_cents:
                    # This price level is above our target
                    orders_above = cumulative_qty
                else:
                    # We've reached prices at or below our target
                    break

        return orders_above


@dataclass
class PriceHistory:
    """Steam market price history statistics."""
    success: bool
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    avg_price: Optional[float] = None
    median_price: Optional[float] = None
    percentile_20: Optional[float] = None  # 20% of sales were below this price
    percentile_25: Optional[float] = None  # 25% of sales were below this price
    total_sales: int = 0
    days_analyzed: int = 30


def parse_steam_price(price_str: str) -> Optional[float]:
    """
    Parse Steam price string to float.

    Examples:
    - "1 234,56 pуб." -> 1234.56
    - "12,34€" -> 12.34
    - "1,234.56€" -> 1234.56
    """
    if not price_str:
        return None

    # Remove HTML entities and currency symbols
    price_str = price_str.replace("&#8364;", "").replace("€", "")
    price_str = price_str.replace("pуб.", "").replace("руб.", "").replace("₽", "")
    price_str = price_str.replace("$", "").replace("USD", "")
    price_str = price_str.strip()

    # Handle different number formats
    # Russian: "1 234,56" (space as thousand sep, comma as decimal)
    # European: "1.234,56" (dot as thousand sep, comma as decimal)
    # US: "1,234.56" (comma as thousand sep, dot as decimal)

    # Remove spaces
    price_str = price_str.replace(" ", "").replace("\xa0", "")

    # Count dots and commas to determine format
    dots = price_str.count(".")
    commas = price_str.count(",")

    if commas == 1 and dots == 0:
        # "1234,56" -> European/Russian decimal
        price_str = price_str.replace(",", ".")
    elif commas == 1 and dots >= 1:
        # "1.234,56" -> European thousand separator
        price_str = price_str.replace(".", "").replace(",", ".")
    elif dots == 1 and commas >= 1:
        # "1,234.56" -> US format
        price_str = price_str.replace(",", "")
    elif commas > 1:
        # "1,234,567" -> US thousand separators, no decimal
        price_str = price_str.replace(",", "")

    try:
        return float(price_str)
    except ValueError:
        logger.warning(f"Failed to parse price: {price_str}")
        return None


class SteamMarketAPI:
    """Steam Community Market API client."""

    BASE_URL = "https://steamcommunity.com/market"
    APP_ID = 730  # CS:GO/CS2

    def __init__(
        self,
        currency: Currency = Currency.RUB,
        proxy_url: Optional[str] = None,
        proxy_list: Optional[list[str]] = None,
        requests_per_proxy: int = 15,
    ):
        """
        Initialize Steam Market API client.

        Args:
            currency: Currency for prices
            proxy_url: Single proxy URL (e.g., "socks5://host:port" or "socks5://user:pass@host:port")
            proxy_list: List of proxy URLs for rotation
            requests_per_proxy: Rotate proxy after this many requests (default: 15)
        """
        self.currency = currency
        self._session: Optional[aiohttp.ClientSession] = None
        self._item_nameid_cache: dict[str, int] = {}

        # Proxy rotation support
        if proxy_list:
            self._proxy_list = proxy_list
        elif proxy_url:
            self._proxy_list = [proxy_url]
        elif config.proxy.enabled and config.proxy.url:
            self._proxy_list = [config.proxy.url]
        else:
            self._proxy_list = []

        self._current_proxy_idx = 0
        self._requests_count = 0
        self._requests_per_proxy = requests_per_proxy
        self._session_lock = asyncio.Lock()  # Lock for thread-safe session operations

    def _get_current_proxy(self) -> Optional[str]:
        """Get current proxy URL."""
        if not self._proxy_list:
            return None
        return self._proxy_list[self._current_proxy_idx % len(self._proxy_list)]

    async def _rotate_proxy(self, reason: str = ""):
        """Switch to next proxy (DON'T close session during parallel requests)."""
        if not self._proxy_list:
            return

        # DON'T close session - just mark it for rotation on next _get_session
        # This prevents "Connector is closed" errors during parallel requests
        self._session = None  # Mark for re-creation with new proxy

        # Move to next proxy
        self._current_proxy_idx = (self._current_proxy_idx + 1) % len(self._proxy_list)
        self._requests_count = 0

        proxy = self._get_current_proxy()
        proxy_display = proxy.split('@')[-1] if proxy and '@' in proxy else proxy
        reason_msg = f" ({reason})" if reason else ""
        logger.info(f"Will rotate to proxy [{self._current_proxy_idx + 1}/{len(self._proxy_list)}]: {proxy_display}{reason_msg}")

    async def _handle_rate_limit(self, status_code: int) -> bool:
        """Handle rate limit (429) by rotating proxy. Returns True if rotated."""
        if status_code == 429 and self._proxy_list and len(self._proxy_list) > 1:
            await self._rotate_proxy(reason="429 rate limit")
            return True
        return False

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            # Check if we need to rotate proxy
            if self._proxy_list and self._requests_count >= self._requests_per_proxy:
                await self._rotate_proxy()

            if self._session is None or self._session.closed:
                connector = None
                proxy = self._get_current_proxy()
                if proxy:
                    connector = ProxyConnector.from_url(proxy)
                    proxy_display = proxy.split('@')[-1] if '@' in proxy else proxy
                    logger.info(f"Using proxy: {proxy_display}")

                self._session = aiohttp.ClientSession(
                    connector=connector,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                )

            self._requests_count += 1
            return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def check_proxy(self, proxy_url: str, timeout: float = 10, check_steam_api: bool = True) -> bool:
        """
        Check if proxy is working (basic + Steam API test).

        Args:
            proxy_url: Proxy URL to test
            timeout: Timeout in seconds
            check_steam_api: Also test Steam Market API (checks for 429 errors)

        Returns:
            True if proxy works and NOT rate limited, False otherwise
        """
        try:
            connector = ProxyConnector.from_url(proxy_url)
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as session:
                # Test 1: Basic connectivity
                async with session.get(f"{self.BASE_URL}/") as response:
                    if response.status != 200:
                        logger.debug(f"Proxy failed basic test: status {response.status}")
                        return False

                # Test 2: Steam Market API (check for rate limit)
                if check_steam_api:
                    # Try to get price for a common item
                    test_item = "AK-47 | Redline (Field-Tested)"
                    encoded_name = quote(test_item)
                    api_url = f"{self.BASE_URL}/priceoverview/"
                    params = {
                        'appid': self.APP_ID,
                        'market_hash_name': test_item,
                        'currency': STEAM_CURRENCY_CODES.get(self.currency, 5)
                    }

                    async with session.get(api_url, params=params) as response:
                        if response.status == 429:
                            logger.debug(f"Proxy has rate limit (429)")
                            return False
                        elif response.status != 200:
                            logger.debug(f"Proxy API test failed: status {response.status}")
                            return False

                return True

        except asyncio.TimeoutError:
            logger.debug(f"Proxy timeout")
            return False
        except Exception as e:
            logger.debug(f"Proxy check error: {e}")
            return False

    async def check_all_proxies(self) -> list[str]:
        """
        Check all proxies and return only working ones.

        Returns:
            List of working proxy URLs
        """
        if not self._proxy_list:
            return []

        working = []
        total = len(self._proxy_list)

        for i, proxy in enumerate(self._proxy_list, 1):
            proxy_display = proxy.split('@')[-1] if '@' in proxy else proxy
            logger.info(f"Checking proxy [{i}/{total}]: {proxy_display}...")

            if await self.check_proxy(proxy):
                logger.info(f"  OK")
                working.append(proxy)
            else:
                logger.warning(f"  FAILED")

        # Update proxy list to only working ones
        self._proxy_list = working
        logger.info(f"Working proxies: {len(working)}/{total}")

        return working

    async def get_price_overview(self, market_hash_name: str, max_retries: int = 3) -> PriceOverview:
        """
        Get price overview for an item (with automatic proxy rotation on 429).

        Args:
            market_hash_name: Steam market hash name (e.g., "AK-47 | Redline (Field-Tested)")
            max_retries: Maximum number of retries with proxy rotation (default: 3)

        Returns:
            PriceOverview with lowest_price, median_price, volume
        """
        currency_code = STEAM_CURRENCY_CODES[self.currency]

        url = f"{self.BASE_URL}/priceoverview/"
        params = {
            "appid": self.APP_ID,
            "currency": currency_code,
            "market_hash_name": market_hash_name,
        }

        for attempt in range(max_retries):
            try:
                session = await self._get_session()
                async with session.get(url, params=params) as response:
                    if response.status == 429:
                        # Rate limit - rotate proxy and retry
                        await self._handle_rate_limit(response.status)
                        logger.warning(f"Rate limit (429) for price_overview, rotating proxy... (attempt {attempt + 1}/{max_retries})")
                        await self._rotate_proxy(reason="rate_limit")

                        if attempt < max_retries - 1:
                            await asyncio.sleep(2)  # Small delay before retry
                            continue
                        else:
                            logger.error(f"Max retries reached for price_overview: {market_hash_name}")
                            return PriceOverview(success=False)

                    elif response.status != 200:
                        await self._handle_rate_limit(response.status)
                        logger.error(f"Steam API error: {response.status}")
                        return PriceOverview(success=False)

                    data = await response.json()

                    if not data.get("success"):
                        return PriceOverview(success=False)

                    return PriceOverview(
                        success=True,
                        lowest_price=parse_steam_price(data.get("lowest_price", "")),
                        median_price=parse_steam_price(data.get("median_price", "")),
                        volume=int(data.get("volume", "0").replace(",", "")) if data.get("volume") else None,
                        currency=self.currency,
                    )
            except Exception as e:
                logger.error(f"Failed to get price overview (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue

        return PriceOverview(success=False)

    async def _get_item_nameid(self, market_hash_name: str) -> Optional[int]:
        """
        Get Steam's internal item_nameid by parsing the market listing page.

        This ID is required for the itemordershistogram endpoint.
        """
        if market_hash_name in self._item_nameid_cache:
            return self._item_nameid_cache[market_hash_name]

        session = await self._get_session()
        encoded_name = quote(market_hash_name)
        url = f"{self.BASE_URL}/listings/{self.APP_ID}/{encoded_name}"

        try:
            async with session.get(url) as response:
                if response.status != 200:
                    await self._handle_rate_limit(response.status)
                    logger.error(f"Failed to load market page: {response.status}")
                    return None

                html = await response.text()

                # Look for ItemActivityTicker.Start( nameid )
                # or Market_LoadOrderSpread( nameid )
                patterns = [
                    r"Market_LoadOrderSpread\(\s*(\d+)\s*\)",
                    r"ItemActivityTicker\.Start\(\s*(\d+)\s*\)",
                ]

                for pattern in patterns:
                    match = re.search(pattern, html)
                    if match:
                        nameid = int(match.group(1))
                        self._item_nameid_cache[market_hash_name] = nameid
                        return nameid

                logger.warning(f"Could not find item_nameid for: {market_hash_name}")
                return None

        except Exception as e:
            logger.error(f"Failed to get item_nameid: {e}")
            return None

    async def get_buy_orders(self, market_hash_name: str, max_retries: int = 3) -> BuyOrderInfo:
        """
        Get buy order information for an item (with automatic proxy rotation on 429).

        Args:
            market_hash_name: Steam market hash name
            max_retries: Maximum number of retries with proxy rotation (default: 3)

        Returns:
            BuyOrderInfo with highest_buy_order and other order data
        """
        item_nameid = await self._get_item_nameid(market_hash_name)

        if item_nameid is None:
            return BuyOrderInfo(success=False)

        currency_code = STEAM_CURRENCY_CODES[self.currency]

        url = f"{self.BASE_URL}/itemordershistogram"
        params = {
            "country": "RU",
            "language": "russian",
            "currency": currency_code,
            "item_nameid": item_nameid,
            "two_factor": 0,
        }

        for attempt in range(max_retries):
            try:
                session = await self._get_session()
                async with session.get(url, params=params) as response:
                    if response.status == 429:
                        # Rate limit - rotate proxy and retry
                        await self._handle_rate_limit(response.status)
                        logger.warning(f"Rate limit (429) for buy_orders, rotating proxy... (attempt {attempt + 1}/{max_retries})")
                        await self._rotate_proxy(reason="rate_limit")

                        if attempt < max_retries - 1:
                            await asyncio.sleep(2)  # Small delay before retry
                            continue
                        else:
                            logger.error(f"Max retries reached for buy_orders: {market_hash_name}")
                            return BuyOrderInfo(success=False)

                    elif response.status != 200:
                        logger.error(f"Orders histogram error: {response.status}")
                        return BuyOrderInfo(success=False)

                    data = await response.json()

                    if not data.get("success"):
                        return BuyOrderInfo(success=False)

                    # highest_buy_order and lowest_sell_order are in cents
                    highest_buy = data.get("highest_buy_order")
                    lowest_sell = data.get("lowest_sell_order")
                    buy_graph = data.get("buy_order_graph", [])

                    return BuyOrderInfo(
                        success=True,
                        highest_buy_order=int(highest_buy) / 100 if highest_buy else None,
                        lowest_sell_order=int(lowest_sell) / 100 if lowest_sell else None,
                        buy_order_count=len(buy_graph),
                        sell_order_count=len(data.get("sell_order_graph", [])),
                        buy_order_graph=buy_graph,
                    )

            except Exception as e:
                logger.error(f"Failed to get buy orders (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue

        return BuyOrderInfo(success=False)

    async def get_price_history(self, market_hash_name: str, days: int = 7) -> PriceHistory:
        """
        Get price history from Steam listing page.

        Parses the var line1=[[date, price, volume], ...] data embedded in the page.
        Note: Prices are returned in USD (Steam's default for history graph).

        Args:
            market_hash_name: Steam market hash name
            days: Number of days of history to analyze (default 7 for recent data)

        Returns:
            PriceHistory with min, max, avg, median, and percentiles
        """
        from datetime import datetime, timedelta

        session = await self._get_session()
        encoded_name = quote(market_hash_name)
        url = f"{self.BASE_URL}/listings/{self.APP_ID}/{encoded_name}"

        try:
            async with session.get(url) as response:
                if response.status != 200:
                    await self._handle_rate_limit(response.status)
                    logger.error(f"Failed to load market page for history: {response.status}")
                    return PriceHistory(success=False)

                html = await response.text()

                # Look for price history data: var line1=[[date, price, volume], ...]
                line1_match = re.search(r'var line1=(\[\[.*?\]\]);', html, re.DOTALL)

                if not line1_match:
                    logger.warning(f"No price history found for: {market_hash_name}")
                    return PriceHistory(success=False)

                data_str = line1_match.group(1)

                # Extract entries with dates: ["MMM DD YYYY HH: +0", price, "volume"]
                entries = re.findall(r'\["([A-Za-z]{3} \d{1,2} \d{4})[^"]*",(\d+\.?\d*),"\d+"', data_str)

                if not entries:
                    logger.warning(f"Could not parse price history for: {market_hash_name}")
                    return PriceHistory(success=False)

                # Filter to last N days
                cutoff_date = datetime.now() - timedelta(days=days)
                recent_prices = []

                for date_str, price_str in entries:
                    try:
                        # Parse date: "Jan 01 2024"
                        entry_date = datetime.strptime(date_str, "%b %d %Y")
                        if entry_date >= cutoff_date:
                            recent_prices.append(float(price_str))
                    except (ValueError, TypeError):
                        continue

                if not recent_prices:
                    # Fallback: use last 50 entries if date parsing failed
                    recent_prices = [float(p) for _, p in entries[-50:]]

                if not recent_prices:
                    return PriceHistory(success=False)

                # Calculate statistics
                sorted_prices = sorted(recent_prices)
                n = len(sorted_prices)

                min_price = sorted_prices[0]
                max_price = sorted_prices[-1]
                avg_price = sum(sorted_prices) / n
                median_price = sorted_prices[n // 2]

                # Percentiles (20th and 25th)
                percentile_20 = sorted_prices[int(n * 0.20)] if n >= 5 else min_price
                percentile_25 = sorted_prices[int(n * 0.25)] if n >= 4 else min_price

                return PriceHistory(
                    success=True,
                    min_price=min_price,
                    max_price=max_price,
                    avg_price=avg_price,
                    median_price=median_price,
                    percentile_20=percentile_20,
                    percentile_25=percentile_25,
                    total_sales=n,
                    days_analyzed=days,
                )

        except Exception as e:
            logger.error(f"Failed to get price history: {e}")
            return PriceHistory(success=False)

    async def get_full_market_info(self, market_hash_name: str) -> dict:
        """
        Get complete market information for an item.

        Returns dict with price_overview and buy_orders.
        """
        # Run both requests concurrently
        price_task = self.get_price_overview(market_hash_name)
        orders_task = self.get_buy_orders(market_hash_name)

        price_overview, buy_orders = await asyncio.gather(price_task, orders_task)

        return {
            "market_hash_name": market_hash_name,
            "price_overview": price_overview,
            "buy_orders": buy_orders,
        }


# Convenience function for testing
async def test_steam_api():
    """Test Steam Market API with a sample item."""
    api = SteamMarketAPI(currency=Currency.RUB)

    test_items = [
        "AK-47 | Redline (Field-Tested)",
        "AWP | Asiimov (Field-Tested)",
    ]

    try:
        for item in test_items:
            print(f"\n{'='*60}")
            print(f"Item: {item}")
            print("="*60)

            info = await api.get_full_market_info(item)

            po = info["price_overview"]
            print(f"\nPrice Overview:")
            print(f"  Lowest price: {po.lowest_price} {po.currency.value}")
            print(f"  Median price: {po.median_price} {po.currency.value}")
            print(f"  Volume (24h): {po.volume}")

            bo = info["buy_orders"]
            print(f"\nBuy Orders:")
            print(f"  Highest buy order: {bo.highest_buy_order} {api.currency.value}")
            print(f"  Lowest sell order: {bo.lowest_sell_order} {api.currency.value}")
            print(f"  Buy order levels: {bo.buy_order_count}")
            print(f"  Sell order levels: {bo.sell_order_count}")

            if po.median_price and bo.highest_buy_order:
                diff = ((po.median_price - bo.highest_buy_order) / po.median_price) * 100
                print(f"\n  Gap (median vs buy order): {diff:.2f}%")

            await asyncio.sleep(1)  # Rate limiting
    finally:
        await api.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_steam_api())
