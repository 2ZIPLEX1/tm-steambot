"""
Steam client using ValvePython/steam library with HTTP requests.

This implementation replaces steampy with a more reliable approach:
- Uses ValvePython/steam for authentication (supports new Steam API)
- Makes direct HTTP requests to Steam Market API
- Compatible with the same credentials as steampy

Based on the Node.js buyOrders.js implementation.
"""

import json
import time
import re
import base64
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from pathlib import Path

import requests
import hmac
import hashlib
from steam.webauth import WebAuth
from steam.guard import generate_twofactor_code

from config import settings
from src.logger import get_logger

logger = get_logger(__name__)

# CS2 App ID (same as CS:GO)
CS2_APPID = 730


@dataclass
class WalletInfo:
    """Steam wallet information."""

    balance: float
    currency: int
    currency_code: str

    @property
    def balance_cents(self) -> int:
        """Balance in cents."""
        return int(self.balance * 100)


@dataclass
class BuyOrderResult:
    """Result of placing a buy order."""

    success: bool
    order_id: Optional[str] = None
    message: Optional[str] = None


@dataclass
class InventoryItem:
    """Steam inventory item."""

    asset_id: str
    class_id: str
    instance_id: str
    market_hash_name: str
    tradable: bool
    marketable: bool
    amount: int = 1


class SteamClientError(Exception):
    """Custom exception for Steam client errors."""
    pass


class SteamClient:
    """
    Steam Market client using ValvePython/steam for authentication
    and direct HTTP requests for market operations.

    Handles:
    - Authentication with Steam Guard (shared_secret for 2FA)
    - Wallet balance checking
    - Buy order placement and management
    - Inventory access
    - Rate limiting
    """

    # Rate limiting
    REQUEST_DELAY = 3.0  # Seconds between requests
    MAX_RETRIES = 3
    RETRY_DELAY = 5.0

    # Steam API endpoints
    BASE_URL = "https://steamcommunity.com"
    MARKET_URL = f"{BASE_URL}/market"

    def __init__(self, proxy: Optional[str] = None, proxy_manager=None):
        """
        Initialize Steam client.

        Args:
            proxy: Single proxy URL (socks5://user:pass@host:port)
            proxy_manager: ProxyManager instance for automatic proxy rotation
        """
        self._session: Optional[requests.Session] = None
        self._sessionid: Optional[str] = None
        self._last_request_time = 0.0
        self._logged_in = False
        self._use_manual_cookies = False
        self._cookie_file: str = "steam_cookies.txt"  # Default cookie file

        # Proxy support
        self._proxy = proxy
        self._proxy_manager = proxy_manager
        self._current_proxy = proxy  # Track current proxy

    def load_cookies_from_file(self, cookie_file: str = "steam_cookies.txt") -> bool:
        """
        Load cookies from file instead of using WebAuth login.
        This is useful when WebAuth shows captcha or rate limits.

        Cookie file format:
        sessionid=...
        steamLoginSecure=...
        steamCountry=...
        timezoneOffset=...

        Args:
            cookie_file: Path to cookie file

        Returns:
            True if cookies loaded successfully
        """
        cookie_path = Path(cookie_file)
        if not cookie_path.exists():
            logger.warning(f"Cookie file not found: {cookie_file}")
            return False

        logger.info(f"Loading cookies from {cookie_file}...")

        # Create session
        self._session = requests.Session()

        # Set browser-like headers
        self._session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        })

        # Setup proxy if provided
        self._setup_proxy()

        # Read cookies from file
        cookies = {}
        with open(cookie_path, 'r') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    cookies[key.strip()] = value.strip()

        if not cookies:
            logger.error("No cookies found in file")
            return False

        logger.info(f"Loaded cookies: {list(cookies.keys())}")

        # Set cookies for both Steam domains
        for name, value in cookies.items():
            self._session.cookies.set(name, value, domain='store.steampowered.com', path='/')
            self._session.cookies.set(name, value, domain='.steampowered.com', path='/')
            self._session.cookies.set(name, value, domain='steamcommunity.com', path='/')
            self._session.cookies.set(name, value, domain='.steamcommunity.com', path='/')

        # Extract sessionid
        if 'sessionid' in cookies:
            self._sessionid = cookies['sessionid']
        else:
            logger.error("sessionid not found in cookies")
            return False

        # Save identity_secret and shared_secret for confirmations
        if not hasattr(self, '_identity_secret') or not self._identity_secret:
            identity_secret = settings.steam_identity_secret
            if identity_secret:
                self._identity_secret = identity_secret
                logger.debug("identity_secret saved for auto-confirmations")

        if not hasattr(self, '_shared_secret') or not self._shared_secret:
            shared_secret = settings.steam_shared_secret
            if shared_secret:
                self._shared_secret = shared_secret
                logger.debug("shared_secret saved for 2FA codes")

        # Extract SteamID from steamLoginSecure cookie
        if 'steamLoginSecure' in cookies and (not hasattr(self, '_steamid') or not self._steamid):
            # steamLoginSecure format: "steamid||token" or "steamid%7C%7Ctoken" (URL-encoded)
            cookie_value = cookies['steamLoginSecure']
            # Try both separators
            if '%7C%7C' in cookie_value:
                parts = cookie_value.split('%7C%7C')
            elif '||' in cookie_value:
                parts = cookie_value.split('||')
            else:
                parts = []

            if len(parts) >= 1 and parts[0]:
                self._steamid = parts[0]
                logger.debug(f"SteamID extracted from cookie: {self._steamid}")

        self._logged_in = True
        self._use_manual_cookies = True
        logger.info("✅ Cookies loaded successfully!")

        return True

    def _setup_proxy(self):
        """Setup proxy for session."""
        if not self._session:
            return

        proxy_url = self._current_proxy
        if not proxy_url and self._proxy_manager:
            # Get proxy from manager
            proxy_url = self._proxy_manager.get_next_proxy()
            self._current_proxy = proxy_url

        if proxy_url:
            self._session.proxies = {
                'http': proxy_url,
                'https': proxy_url,
            }
            logger.info(f"Using proxy: {proxy_url}")
        else:
            self._session.proxies = {}

    def _rotate_proxy(self, reason: str = "rate_limit"):
        """
        Rotate to next proxy on error.

        Args:
            reason: Reason for rotation (rate_limit, error, etc.)
        """
        if not self._proxy_manager:
            logger.warning("Cannot rotate proxy: ProxyManager not configured")
            return False

        # Mark current proxy as having an issue
        if self._current_proxy:
            if reason == "rate_limit":
                self._proxy_manager.record_rate_limit(self._current_proxy)
            else:
                self._proxy_manager.record_error(self._current_proxy)

        # Get next proxy
        next_proxy = self._proxy_manager.get_next_proxy(skip_unhealthy=True)
        if not next_proxy:
            logger.error("No healthy proxies available!")
            return False

        old_proxy = self._current_proxy
        self._current_proxy = next_proxy

        # Update session
        if self._session:
            self._session.proxies = {
                'http': next_proxy,
                'https': next_proxy,
            }

        logger.info(f"🔄 Proxy rotated: {old_proxy} -> {next_proxy} (reason: {reason})")
        return True

    def _save_cookies_to_file(self, cookie_file: str = "steam_cookies.txt"):
        """
        Save current session cookies to file for future use.
        This allows reusing the session without WebAuth login (avoids captcha).

        Args:
            cookie_file: Path to save cookies
        """
        if not self._session:
            logger.warning("No session to save cookies from")
            return

        try:
            cookie_path = Path(cookie_file)

            # Important cookies to save
            important_cookies = ['sessionid', 'steamLoginSecure', 'steamCountry', 'timezoneOffset']

            cookies_to_save = {}
            for cookie in self._session.cookies:
                if cookie.name in important_cookies:
                    cookies_to_save[cookie.name] = cookie.value

            if not cookies_to_save:
                logger.warning("No important cookies found to save")
                return

            # Write to file
            with open(cookie_path, 'w') as f:
                for name, value in cookies_to_save.items():
                    f.write(f"{name}={value}\n")

            logger.info(f"💾 Saved {len(cookies_to_save)} cookies to {cookie_file}")
            logger.debug(f"Saved cookies: {list(cookies_to_save.keys())}")

        except Exception as e:
            logger.error(f"Failed to save cookies: {e}")

    def _rate_limit(self):
        """Apply rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.REQUEST_DELAY:
            time.sleep(self.REQUEST_DELAY - elapsed)
        self._last_request_time = time.time()

    def _retry_on_error(self, func, *args, **kwargs):
        """Retry function on transient errors."""
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                self._rate_limit()
                return func(*args, **kwargs)
            except requests.exceptions.Timeout:
                wait_time = self.RETRY_DELAY * (attempt + 1)
                logger.warning(f"Request timeout, waiting {wait_time}s...")
                time.sleep(wait_time)
            except requests.exceptions.RequestException as e:
                last_error = e
                if "429" in str(e) or "Too Many Requests" in str(e):
                    # Get the function name that called _retry_on_error
                    import inspect
                    caller_frame = inspect.currentframe().f_back.f_back
                    caller_name = caller_frame.f_code.co_name if caller_frame else "unknown"

                    # Try to rotate proxy if available
                    if self._proxy_manager and attempt < self.MAX_RETRIES - 1:
                        logger.warning(f"Rate limited in {caller_name}(), rotating proxy... (attempt {attempt + 1}/{self.MAX_RETRIES})")
                        if self._rotate_proxy(reason="rate_limit"):
                            # Proxy rotated, retry immediately without waiting
                            logger.info("Retrying with new proxy...")
                            continue
                        else:
                            logger.warning("Failed to rotate proxy, falling back to wait")

                    wait_time = self.RETRY_DELAY * (attempt + 1)
                    logger.warning(f"Rate limited in {caller_name}(), waiting {wait_time}s... (attempt {attempt + 1}/{self.MAX_RETRIES})")
                    time.sleep(wait_time)
                else:
                    raise
            except Exception as e:
                last_error = e
                logger.error(f"Attempt {attempt + 1} failed: {e}")
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY)

        raise SteamClientError(f"Max retries exceeded: {last_error}")

    def login(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        shared_secret: Optional[str] = None,
        cookie_file: Optional[str] = None,
    ) -> bool:
        """
        Login to Steam.

        Priority:
        1. Try loading cookies from file (avoids captcha)
        2. Fall back to WebAuth login with 2FA

        Args:
            username: Steam username (uses settings if not provided)
            password: Steam password (uses settings if not provided)
            shared_secret: Base64-encoded shared secret (uses settings if not provided)
            cookie_file: Path to cookie file (uses 'steam_cookies.txt' if not provided)

        Returns:
            True if login successful
        """
        if self._logged_in and self._session:
            logger.info("Already logged in")
            return True

        # Use settings as defaults
        username = username or settings.steam_username
        password = password or settings.steam_password
        shared_secret = shared_secret or settings.steam_shared_secret
        cookie_file = cookie_file or "steam_cookies.txt"

        # Save cookie file path for later use
        self._cookie_file = cookie_file

        # Try cookies first (to avoid captcha/rate limits)
        if Path(cookie_file).exists():
            logger.info(f"Found {cookie_file}, trying cookie-based login...")
            if self.load_cookies_from_file(cookie_file):
                return True
            else:
                logger.warning("Cookie login failed, falling back to WebAuth...")

        # Fall back to WebAuth login

        try:
            logger.info(f"Logging in as {username}...")

            # Generate 2FA code from shared_secret
            # shared_secret in .env is base64-encoded, so decode it first
            try:
                shared_secret_bytes = base64.b64decode(shared_secret)
                twofactor_code = generate_twofactor_code(shared_secret_bytes)
                logger.info(f"Generated 2FA code: {twofactor_code} (length: {len(twofactor_code)})")
            except Exception as e:
                raise SteamClientError(
                    f"Failed to generate 2FA code. "
                    f"Check that STEAM_SHARED_SECRET is valid base64: {e}"
                )

            # Create WebAuth instance
            wa = WebAuth(username)
            logger.info(f"WebAuth instance created for user: {username}")

            # Attempt login with 2FA
            # Try first without 2FA to get the RSA key and session
            logger.info("Step 1: Initial login attempt...")
            try:
                self._session = wa.login(password=password)
                logger.info("Login successful! Session obtained.")
            except Exception as login_error:
                logger.debug(f"Initial login raised: {type(login_error).__name__}")

                # Import the exception class
                from steam.webauth import TwoFactorCodeRequired

                # Check for rate limiting (HTTP 429)
                if isinstance(login_error, TypeError) and "'NoneType' object is not subscriptable" in str(login_error):
                    raise SteamClientError(
                        "Steam rate limit exceeded (HTTP 429). "
                        "Too many login attempts. Please wait 5-10 minutes before trying again."
                    )

                # If 2FA is required, retry with the code
                if isinstance(login_error, TwoFactorCodeRequired):
                    logger.info(f"2FA required. Retrying with code: {twofactor_code}")
                    try:
                        self._session = wa.login(
                            password=password,
                            twofactor_code=twofactor_code
                        )
                        logger.info("Login with 2FA successful! Session obtained.")
                    except KeyError as key_err:
                        # Handle the 'transfer_parameters' KeyError from Issue #456
                        # https://github.com/ValvePython/steam/issues/456
                        if 'transfer_parameters' in str(key_err):
                            logger.warning(
                                "Encountered 'transfer_parameters' KeyError. "
                                "This is a known issue in steam library, but login was successful."
                            )
                            # The session is already set in wa object, we can use it
                            # even though _finalize_login failed
                            if hasattr(wa, 'session') and wa.session:
                                self._session = wa.session
                                logger.info("Using session from WebAuth object")
                            else:
                                raise SteamClientError(
                                    "Login succeeded but couldn't get session. "
                                    "This is a known issue with steam library. "
                                    "Try updating: pip install --upgrade steam"
                                )
                        else:
                            raise
                    except TwoFactorCodeRequired as e:
                        raise SteamClientError(
                            f"2FA code rejected by Steam. "
                            f"Code used: {twofactor_code}. "
                            f"Please verify your STEAM_SHARED_SECRET is correct."
                        )
                    except Exception as e:
                        error_msg = str(e).lower()
                        if "incorrect login" in error_msg or "invalid" in error_msg:
                            raise SteamClientError("Invalid Steam credentials")
                        elif "too many login failures" in error_msg:
                            raise SteamClientError(
                                "Too many login failures. Wait a few minutes and try again."
                            )
                        else:
                            raise SteamClientError(f"Login with 2FA failed: {e}")
                else:
                    # Not a 2FA error, re-raise
                    error_msg = str(login_error).lower()
                    if "incorrect login" in error_msg or "invalid" in error_msg:
                        raise SteamClientError("Invalid Steam credentials")
                    elif "too many login failures" in error_msg:
                        raise SteamClientError(
                            "Too many login failures. Wait a few minutes and try again."
                        )
                    else:
                        raise SteamClientError(f"Login failed: {login_error}")

            # Get sessionid from cookies
            for cookie in self._session.cookies:
                if cookie.name == 'sessionid':
                    self._sessionid = cookie.value
                    break

            if not self._sessionid:
                raise SteamClientError("Failed to get sessionid from cookies")

            # Debug: log all cookies after login
            logger.debug("Cookies after login:")
            for cookie in self._session.cookies:
                logger.debug(f"  {cookie.name}: {cookie.value[:30]}... (domain: {cookie.domain}, path: {cookie.path})")

            self._logged_in = True
            logger.info("✅ Successfully logged in to Steam!")
            logger.debug(f"SessionID: {self._sessionid[:8]}...")

            # Save identity_secret and shared_secret for confirmations (if not already saved)
            if not hasattr(self, '_identity_secret') or not self._identity_secret:
                identity_secret = settings.steam_identity_secret
                if identity_secret:
                    self._identity_secret = identity_secret
                    logger.debug("identity_secret saved for auto-confirmations")

            if not hasattr(self, '_shared_secret') or not self._shared_secret:
                shared_secret = settings.steam_shared_secret
                if shared_secret:
                    self._shared_secret = shared_secret
                    logger.debug("shared_secret saved for 2FA codes")

            # Get SteamID from session if available
            if not hasattr(self, '_steamid') or not self._steamid:
                # Try to get steamid from cookies or session
                for cookie in self._session.cookies:
                    if cookie.name == 'steamLoginSecure':
                        # steamLoginSecure format: "steamid||token" or "steamid%7C%7Ctoken" (URL-encoded)
                        cookie_value = cookie.value
                        # Try both separators
                        if '%7C%7C' in cookie_value:
                            parts = cookie_value.split('%7C%7C')
                        elif '||' in cookie_value:
                            parts = cookie_value.split('||')
                        else:
                            parts = []

                        if len(parts) >= 1 and parts[0]:
                            self._steamid = parts[0]
                            logger.debug(f"SteamID extracted from cookie: {self._steamid}")
                            break

            # Auto-save cookies for future use (to avoid captcha/rate limits)
            self._save_cookies_to_file(cookie_file=self._cookie_file)

            return True

        except SteamClientError:
            raise
        except Exception as e:
            logger.error(f"Login failed: {e}")
            logger.debug(f"Login error details: {type(e).__name__}: {e}")
            raise SteamClientError(f"Login failed: {e}")

    def logout(self):
        """Logout from Steam."""
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
            self._sessionid = None
            self._logged_in = False
            logger.info("Logged out from Steam")

    def ensure_logged_in(self):
        """Ensure client is logged in."""
        if not self._logged_in or not self._session:
            self.login()

    def get_steamid(self) -> Optional[str]:
        """
        Get Steam ID64 from session cookies.

        The steamLoginSecure cookie has format: steamid||token

        Returns:
            Steam ID64 as string, or None if not found
        """
        self.ensure_logged_in()

        try:
            # Debug: list all cookies
            cookie_names = [c.name for c in self._session.cookies]
            logger.debug(f"Available cookies: {cookie_names}")

            for cookie in self._session.cookies:
                if cookie.name == 'steamLoginSecure':
                    # Cookie format: steamid||token or steamid%7C%7Ctoken (URL-encoded)
                    cookie_value = cookie.value
                    logger.debug(f"steamLoginSecure cookie value (first 50 chars): {cookie_value[:50]}...")

                    # Try both separators
                    if '%7C%7C' in cookie_value:
                        steamid = cookie_value.split('%7C%7C')[0]
                        logger.debug(f"Extracted Steam ID (URL-encoded): {steamid}")
                        return steamid
                    elif '||' in cookie_value:
                        steamid = cookie_value.split('||')[0]
                        logger.debug(f"Extracted Steam ID: {steamid}")
                        return steamid
                    else:
                        logger.warning(f"steamLoginSecure cookie found but no separator found in value")
                        return None

            logger.warning("steamLoginSecure cookie not found in session")
            return None

        except Exception as e:
            logger.error(f"Failed to extract Steam ID: {e}")
            return None

    def _make_request(
        self,
        method: str,
        url: str,
        **kwargs
    ) -> requests.Response:
        """Make an authenticated HTTP request."""
        self.ensure_logged_in()

        headers = kwargs.pop('headers', {})
        headers.update({
            'Referer': self.MARKET_URL,
            'Origin': self.BASE_URL,
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

        # Debug: log cookies being sent
        cookies_debug = []
        for cookie in self._session.cookies:
            cookies_debug.append(f"{cookie.name}={cookie.value[:20]}...")
        logger.debug(f"Request to {url}: Cookies: {', '.join(cookies_debug) if cookies_debug else 'NONE'}")

        if method.upper() == 'POST':
            response = self._session.post(url, headers=headers, **kwargs)
        else:
            response = self._session.get(url, headers=headers, **kwargs)

        response.raise_for_status()
        return response

    def _make_public_request(
        self,
        method: str,
        url: str,
        **kwargs
    ) -> requests.Response:
        """Make a public HTTP request (no login required)."""
        # Create session if not exists
        if self._session is None:
            self._session = requests.Session()
            self._setup_proxy()  # Setup proxy for new session

        headers = kwargs.pop('headers', {})
        headers.update({
            'Referer': self.MARKET_URL,
            'Origin': self.BASE_URL,
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

        if method.upper() == 'POST':
            response = self._session.post(url, headers=headers, **kwargs)
        else:
            response = self._session.get(url, headers=headers, **kwargs)

        response.raise_for_status()
        return response

    def get_wallet_balance(self) -> WalletInfo:
        """
        Get current wallet balance.

        Returns:
            WalletInfo with balance and currency
        """
        self.ensure_logged_in()

        try:
            # Method 1: Try using Steam API endpoint for wallet info
            # This is more reliable than HTML parsing
            logger.debug("Attempting to get wallet balance from Steam API...")

            try:
                url = f"{self.BASE_URL}/steamguard/getjson"
                response = self._retry_on_error(self._make_request, 'GET', url)

                # Check if response is JSON
                if 'application/json' in response.headers.get('content-type', ''):
                    data = response.json()

                    if 'wallet_balance' in data:
                        balance_cents = int(data.get("wallet_balance", 0))
                        balance = balance_cents / 100.0
                        currency = data.get("wallet_currency", 1)

                        currency_map = {
                            1: "USD", 2: "GBP", 3: "EUR", 4: "CHF", 5: "RUB",
                            6: "PLN", 7: "BRL", 8: "JPY", 9: "NOK", 10: "IDR",
                            11: "MYR", 12: "PHP", 13: "SGD", 14: "THB", 15: "VND",
                            16: "KRW", 17: "TRY", 18: "UAH", 19: "MXN", 20: "CAD",
                            21: "AUD", 22: "NZD", 23: "CNY", 24: "INR", 25: "CLP",
                            26: "PEN", 27: "COP", 28: "ZAR", 29: "HKD", 30: "TWD",
                            31: "SAR", 32: "AED", 33: "SEK", 34: "ARS", 35: "ILS",
                            36: "BYN", 37: "KZT", 38: "KWD", 39: "QAR", 40: "CRC",
                            41: "UYU",
                        }
                        currency_code = currency_map.get(currency, "USD")

                        logger.info(f"✅ Wallet balance (API): {balance:.2f} {currency_code}")

                        return WalletInfo(
                            balance=balance,
                            currency=currency,
                            currency_code=currency_code
                        )
            except Exception as api_error:
                logger.debug(f"API method failed: {api_error}, falling back to HTML parsing")

            # Method 2: Try account page first (often more reliable)
            import re
            logger.debug("Attempting to parse wallet from account page...")
            try:
                url = f"{self.BASE_URL}/account/"
                response = self._retry_on_error(self._make_request, 'GET', url)
                html = response.text

                # Look for wallet_balance in account page
                wallet_match = re.search(r'wallet_balance["\']?\s*[:=]\s*(\d+)', html, re.IGNORECASE)
                currency_match = re.search(r'wallet_currency["\']?\s*[:=]\s*(\d+)', html, re.IGNORECASE)

                if wallet_match and currency_match:
                    balance_cents = int(wallet_match.group(1))
                    balance = balance_cents / 100.0
                    currency = int(currency_match.group(1))

                    currency_map = {
                        1: "USD", 2: "GBP", 3: "EUR", 4: "CHF", 5: "RUB",
                        6: "PLN", 7: "BRL", 8: "JPY", 9: "NOK", 10: "IDR",
                        11: "MYR", 12: "PHP", 13: "SGD", 14: "THB", 15: "VND",
                        16: "KRW", 17: "TRY", 18: "UAH", 19: "MXN", 20: "CAD",
                        21: "AUD", 22: "NZD", 23: "CNY", 24: "INR", 25: "CLP",
                        26: "PEN", 27: "COP", 28: "ZAR", 29: "HKD", 30: "TWD",
                        31: "SAR", 32: "AED", 33: "SEK", 34: "ARS", 35: "ILS",
                        36: "BYN", 37: "KZT", 38: "KWD", 39: "QAR", 40: "CRC",
                        41: "UYU",
                    }
                    currency_code = currency_map.get(currency, "USD")

                    logger.info(f"✅ Wallet balance (account page): {balance:.2f} {currency_code}")

                    return WalletInfo(
                        balance=balance,
                        currency=currency,
                        currency_code=currency_code
                    )
            except Exception as account_error:
                logger.debug(f"Account page method failed: {account_error}")

            # Method 3: Parse from Market page HTML
            logger.debug("Attempting to parse wallet balance from Market page HTML...")
            url = f"{self.MARKET_URL}/"
            response = self._retry_on_error(self._make_request, 'GET', url)
            html = response.text

            # Try multiple patterns to find wallet info in the full HTML
            patterns = [
                # Pattern 1: Header wallet balance element (most reliable for logged-in users)
                # Example: <a id="header_wallet_balance">13987,45 руб.</a>
                (r'id=["\']header_wallet_balance["\'][^>]*>([^<]+)</a>', 'header_wallet_balance'),
                # Pattern 2: g_rgWalletInfo = {...}
                (r'g_rgWalletInfo\s*=\s*(\{[^}]+\})', 'g_rgWalletInfo'),
                # Pattern 3: "wallet_balance":12345 or 'wallet_balance':12345
                (r'["\']wallet_balance["\']\s*:\s*(\d+)', 'wallet_balance_json'),
                # Pattern 4: wallet_balance: 12345 (without quotes)
                (r'wallet_balance\s*:\s*(\d+)', 'wallet_balance_plain'),
                # Pattern 5: Look in any object with wallet info
                (r'walletInfo["\']?\s*[:=]\s*(\{[^}]+\})', 'walletInfo_object'),
                # Pattern 6: Search in larger JSON structures
                (r'\{[^}]*"wallet_balance"\s*:\s*(\d+)[^}]*"wallet_currency"\s*:\s*(\d+)[^}]*\}', 'wallet_full_json'),
            ]

            for pattern, pattern_name in patterns:
                logger.debug(f"Trying pattern: {pattern_name}")
                match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)

                if match:
                    logger.info(f"Found wallet data using pattern: {pattern_name}")

                    try:
                        if pattern_name == 'header_wallet_balance':
                            # Parse text like "13987,45 руб." or "$123.45" or "123,45 €"
                            balance_text = match.group(1).strip()
                            logger.debug(f"Raw balance text: '{balance_text}'")

                            # Currency text to code mapping
                            currency_text_map = {
                                'руб': ('RUB', 5),
                                'pуб': ('RUB', 5),  # Cyrillic р
                                'rub': ('RUB', 5),
                                '$': ('USD', 1),
                                'usd': ('USD', 1),
                                '€': ('EUR', 3),
                                'eur': ('EUR', 3),
                                '£': ('GBP', 2),
                                'gbp': ('GBP', 2),
                                'pln': ('PLN', 6),
                                'zł': ('PLN', 6),
                                'brl': ('BRL', 7),
                                'r$': ('BRL', 7),
                                '¥': ('JPY', 8),
                                'jpy': ('JPY', 8),
                                'cny': ('CNY', 23),
                                'yuan': ('CNY', 23),
                                '₸': ('KZT', 37),
                                'kzt': ('KZT', 37),
                                'тңг': ('KZT', 37),  # Казахский символ тенге
                            }

                            # Extract numeric value (handle comma or dot as decimal separator)
                            # Remove currency symbols and text
                            numeric_text = balance_text
                            for curr_text in ['руб.', 'руб', 'pуб.', 'pуб', 'USD', '$', '€', 'EUR', '£', 'GBP', 'zł', 'PLN', 'R$', 'BRL', '¥', 'JPY', 'CNY', '₸', 'KZT', 'тңг']:
                                numeric_text = numeric_text.replace(curr_text, '')

                            # Remove spaces and non-breaking spaces
                            numeric_text = numeric_text.replace(' ', '').replace('\xa0', '').replace('\u202f', '')

                            # Replace comma with dot for decimal separator
                            # But be careful: 1,234.56 vs 1.234,56
                            # If there's both comma and dot, keep the last one as decimal
                            if ',' in numeric_text and '.' in numeric_text:
                                # Determine which is decimal separator (it's the last one)
                                comma_pos = numeric_text.rfind(',')
                                dot_pos = numeric_text.rfind('.')
                                if comma_pos > dot_pos:
                                    # Comma is decimal, dot is thousands separator
                                    numeric_text = numeric_text.replace('.', '').replace(',', '.')
                                else:
                                    # Dot is decimal, comma is thousands separator
                                    numeric_text = numeric_text.replace(',', '')
                            elif ',' in numeric_text:
                                # Only comma - could be decimal or thousands
                                # If there's only one comma and 2 digits after it, it's decimal
                                parts = numeric_text.split(',')
                                if len(parts) == 2 and len(parts[1]) == 2:
                                    numeric_text = numeric_text.replace(',', '.')
                                else:
                                    # Thousands separator
                                    numeric_text = numeric_text.replace(',', '')

                            balance = float(numeric_text.strip())

                            # Determine currency
                            currency_code = 'USD'
                            currency = 1
                            balance_lower = balance_text.lower()
                            for curr_text, (code, num) in currency_text_map.items():
                                if curr_text in balance_lower:
                                    currency_code = code
                                    currency = num
                                    break

                            logger.info(f"✅ Wallet balance (HTML header): {balance:.2f} {currency_code}")

                            return WalletInfo(
                                balance=balance,
                                currency=currency,
                                currency_code=currency_code
                            )
                        elif pattern_name == 'wallet_full_json':
                            # Got both balance and currency
                            balance_cents = int(match.group(1))
                            balance = balance_cents / 100.0
                            currency = int(match.group(2))

                            currency_map = {
                                1: "USD", 2: "GBP", 3: "EUR", 4: "CHF", 5: "RUB",
                                6: "PLN", 7: "BRL", 8: "JPY", 9: "NOK", 10: "IDR",
                                11: "MYR", 12: "PHP", 13: "SGD", 14: "THB", 15: "VND",
                                16: "KRW", 17: "TRY", 18: "UAH", 19: "MXN", 20: "CAD",
                                21: "AUD", 22: "NZD", 23: "CNY", 24: "INR", 25: "CLP",
                                26: "PEN", 27: "COP", 28: "ZAR", 29: "HKD", 30: "TWD",
                                31: "SAR", 32: "AED", 33: "SEK", 34: "ARS", 35: "ILS",
                                36: "BYN", 37: "KZT", 38: "KWD", 39: "QAR", 40: "CRC",
                                41: "UYU",
                            }
                            currency_code = currency_map.get(currency, "USD")

                            logger.info(f"✅ Wallet balance: {balance:.2f} {currency_code}")

                            return WalletInfo(
                                balance=balance,
                                currency=currency,
                                currency_code=currency_code
                            )
                        elif pattern_name in ['g_rgWalletInfo', 'walletInfo_object']:
                            # Parse as JSON object
                            wallet_str = match.group(1)
                            wallet_data = json.loads(wallet_str)

                            balance_cents = int(wallet_data.get("wallet_balance", 0))
                            balance = balance_cents / 100.0
                            currency = wallet_data.get("wallet_currency", 1)

                            currency_map = {
                                1: "USD", 2: "GBP", 3: "EUR", 4: "CHF", 5: "RUB",
                                6: "PLN", 7: "BRL", 8: "JPY", 9: "NOK", 10: "IDR",
                                11: "MYR", 12: "PHP", 13: "SGD", 14: "THB", 15: "VND",
                                16: "KRW", 17: "TRY", 18: "UAH", 19: "MXN", 20: "CAD",
                                21: "AUD", 22: "NZD", 23: "CNY", 24: "INR", 25: "CLP",
                                26: "PEN", 27: "COP", 28: "ZAR", 29: "HKD", 30: "TWD",
                                31: "SAR", 32: "AED", 33: "SEK", 34: "ARS", 35: "ILS",
                                36: "BYN", 37: "KZT", 38: "KWD", 39: "QAR", 40: "CRC",
                                41: "UYU",
                            }
                            currency_code = currency_map.get(currency, "USD")

                            logger.info(f"✅ Wallet balance: {balance:.2f} {currency_code}")

                            return WalletInfo(
                                balance=balance,
                                currency=currency,
                                currency_code=currency_code
                            )
                        else:
                            # Only balance value found, assume USD
                            balance_cents = int(match.group(1))
                            balance = balance_cents / 100.0

                            logger.info(f"✅ Wallet balance (pattern {pattern_name}): {balance:.2f} USD")

                            return WalletInfo(
                                balance=balance,
                                currency=1,
                                currency_code="USD"
                            )
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.warning(f"Failed to parse wallet data from {pattern_name}: {e}")
                        continue

            # If nothing found, log more debug info
            logger.warning("Could not find wallet balance in Steam Market page")

            # Save larger HTML snippet for debugging (20000 chars)
            html_snippet = html[:20000]
            logger.debug(f"HTML snippet (first 20000 chars):\n{html_snippet}")

            # Try to find wallet mentions in specific parts of HTML
            if 'wallet' in html.lower():
                wallet_mentions = re.findall(r'.{0,150}wallet.{0,150}', html.lower(), re.DOTALL)
                logger.debug(f"Found {len(wallet_mentions)} mentions of 'wallet' in HTML")
                for i, mention in enumerate(wallet_mentions[:5]):  # Show first 5
                    logger.debug(f"Wallet mention {i+1}: {mention[:200]}")

            return WalletInfo(balance=0.0, currency=1, currency_code="USD")

        except Exception as e:
            logger.error(f"Failed to get wallet balance: {e}")
            logger.debug(f"Error details: {type(e).__name__}: {e}")
            import traceback
            logger.debug(f"Traceback:\n{traceback.format_exc()}")
            return WalletInfo(balance=0.0, currency=1, currency_code="USD")

    def _get_currency_symbol(self, currency_code: int) -> str:
        """Get currency symbol from Steam currency code."""
        currency_symbols = {
            1: "USD",
            3: "EUR",
            5: "RUB",
            18: "UAH",
            36: "BYN",
            37: "KZT",
        }
        return currency_symbols.get(currency_code, f"CURR{currency_code}")

    def create_buy_order(
        self,
        market_hash_name: str,
        price: float,
        quantity: int = 1,
        currency_code: Optional[int] = None
    ) -> BuyOrderResult:
        """
        Create a buy order on Steam Market.

        Args:
            market_hash_name: Item's market hash name
            price: Price per item in account's currency
            quantity: Number of items to buy
            currency_code: Steam currency code (if None, uses wallet currency)

        Returns:
            BuyOrderResult with order ID if successful
        """
        self.ensure_logged_in()

        # Get wallet currency if not specified
        if currency_code is None:
            wallet_info = self.get_wallet_balance()
            currency_code = wallet_info.currency

        price_cents = int(price * 100)

        try:
            currency_symbol = self._get_currency_symbol(currency_code)
            logger.info(f"Creating buy order: {market_hash_name} @ {price:.2f} {currency_symbol} x{quantity}")

            url = f"{self.MARKET_URL}/createbuyorder/"

            # Steam requires billing_state and save_my_address for Russian region
            form_data = {
                'sessionid': self._sessionid,
                'currency': currency_code,
                'appid': CS2_APPID,
                'market_hash_name': market_hash_name,
                'price_total': price_cents * quantity,  # Total price for all items
                'quantity': quantity,
                'billing_state': '',
                'save_my_address': '0'
            }

            # Add proper headers with Referer
            from urllib.parse import quote
            encoded_name = quote(market_hash_name)
            referer = f"{self.MARKET_URL}/listings/{CS2_APPID}/{encoded_name}"

            headers = {
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'Accept': '*/*',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': referer,
                'Origin': 'https://steamcommunity.com'
            }

            logger.debug(f"Buy order params: currency={currency_code}, price_total={price_cents * quantity}, qty={quantity}")

            # Make request without raise_for_status() because Steam returns 406 for success:22
            self.ensure_logged_in()
            headers_combined = headers.copy()
            headers_combined.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })

            response = self._session.post(url, headers=headers_combined, data=form_data)

            # Log response details for debugging
            logger.debug(f"Response status: {response.status_code}")
            logger.debug(f"Response headers: {dict(response.headers)}")

            # Parse JSON response (even if status is 406)
            try:
                result = response.json()
            except Exception as e:
                logger.error(f"Failed to parse JSON response: {e}")
                logger.error(f"Response text: {response.text}")
                response.raise_for_status()  # Raise the original error
                return BuyOrderResult(success=False, message=f"Invalid response: {e}")

            # Check for success
            if result.get('success') in [1, True, '1']:
                order_id = str(result.get('buy_orderid', 'unknown'))
                logger.info(f"✅ Buy order created: {order_id}")
                return BuyOrderResult(success=True, order_id=order_id)

            # Handle success:22 (needs mobile confirmation)
            elif result.get('success') == 22:
                if result.get('need_confirmation'):
                    confirmation_id = result.get('confirmation', {}).get('confirmation_id')
                    logger.warning(f"⚠️ Order created but needs mobile confirmation: {confirmation_id}")

                    # Check if we have identity_secret and steamid for auto-confirmation
                    if not hasattr(self, '_identity_secret') or not self._identity_secret:
                        logger.warning("identity_secret not set, cannot auto-confirm")
                        return BuyOrderResult(success=False, message="needs-mobile-confirmation-no-secret")

                    if not hasattr(self, '_steamid') or not self._steamid:
                        logger.warning("steamid not set, cannot auto-confirm")
                        return BuyOrderResult(success=False, message="needs-mobile-confirmation-no-steamid")

                    # Try to confirm via Steam Guard Mobile Confirmations
                    logger.info("Attempting to auto-confirm via Steam Guard...")
                    # Reduce initial delay - confirmations appear quickly but expire fast
                    logger.debug("Waiting 2 seconds for confirmation to appear in Steam's system...")
                    time.sleep(2)  # Wait for confirmation to appear in Steam's system

                    try:
                        # Retry logic: try up to 3 times with increasing delays
                        max_retries = 3
                        confirmed_count = 0

                        for retry in range(max_retries):
                            # Use built-in confirmation method
                            confirmed_count = self.confirm_all_market_transactions(
                                identity_secret=self._identity_secret,
                                steamid=self._steamid
                            )

                            if confirmed_count > 0:
                                break  # Success!

                            # No confirmations found yet
                            if retry < max_retries - 1:
                                wait_time = 3 + (retry * 2)  # 3s, 5s, 7s
                                logger.debug(f"No confirmations found (attempt {retry+1}/{max_retries}), waiting {wait_time}s...")
                                time.sleep(wait_time)

                        if confirmed_count > 0:
                            logger.info(f"✅ Confirmed {confirmed_count} market transaction(s)")
                            time.sleep(1)
                            # Verify order was created
                            orders = self.get_buy_orders()
                            for order in orders:
                                if order['item_name'] == market_hash_name:
                                    return BuyOrderResult(success=True, order_id=order.get('id', 'confirmed'))
                            return BuyOrderResult(success=True, message="confirmed-but-not-found-in-list")
                        else:
                            logger.warning("❌ Auto-confirmation failed - confirmations endpoint returned 404")
                            logger.warning("📱 Please confirm the order manually in Steam Mobile app!")
                            logger.info(f"Order created with confirmation_id: {confirmation_id}")
                            # Return success=True with needs-manual-confirmation flag
                            # This allows bot to continue and user can confirm manually
                            return BuyOrderResult(
                                success=True,
                                order_id=confirmation_id,
                                message="needs-manual-confirmation"
                            )

                    except Exception as e:
                        logger.error(f"Failed to auto-confirm order: {e}")
                        import traceback
                        logger.debug(traceback.format_exc())
                        return BuyOrderResult(success=False, message=f"confirmation-error: {str(e)}")
                else:
                    message = result.get('message', 'Unknown confirmation error')
                    return BuyOrderResult(success=False, message=message)

            # Handle success:42 (order created but no ID returned)
            elif result.get('success') == 42:
                logger.warning("Steam returned success:42 - verifying order...")
                # Verify order was created by checking active orders
                orders = self.get_buy_orders()
                for order in orders:
                    if order['item_name'] == market_hash_name:
                        logger.info("✅ Order confirmed via getbuyorders")
                        return BuyOrderResult(success=True, message="confirmed-via-list")

                message = "Order may not have been created (success:42)"
                logger.warning(message)
                return BuyOrderResult(success=False, message=message)

            else:
                message = result.get('message', 'Unknown error')
                logger.warning(f"Buy order failed: {message}")
                logger.debug(f"Full response: {result}")
                return BuyOrderResult(success=False, message=message)

        except requests.exceptions.HTTPError as e:
            # Log full error details for HTTP errors
            logger.error(f"HTTP error creating buy order: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response text: {e.response.text[:500]}")
            return BuyOrderResult(success=False, message=str(e))
        except Exception as e:
            logger.error(f"Failed to create buy order: {e}")
            return BuyOrderResult(success=False, message=str(e))

    def get_market_histogram(self, market_hash_name: str, item_nameid: Optional[int] = None) -> Optional[dict]:
        """
        Get market histogram (buy/sell orders) for an item.

        Args:
            market_hash_name: Item's market hash name
            item_nameid: Item's nameid (if None, will be fetched)

        Returns:
            Dict with highest_buy_order, lowest_sell_order, and order graphs
        """
        # NOTE: This is a public API, no login required!
        # Only need login if we want user-specific currency

        try:
            # Get item_nameid if not provided
            if item_nameid is None:
                # Fetch item page to extract nameid
                from urllib.parse import quote
                encoded_name = quote(market_hash_name)
                listing_url = f"{self.MARKET_URL}/listings/{CS2_APPID}/{encoded_name}"

                response = self._retry_on_error(self._make_public_request, 'GET', listing_url)
                html = response.text

                # Extract item_nameid from JavaScript
                import re
                match = re.search(r'Market_LoadOrderSpread\(\s*(\d+)\s*\)', html)
                if not match:
                    logger.warning(f"Could not find item_nameid for {market_hash_name}")
                    return None

                item_nameid = int(match.group(1))
                logger.debug(f"Found item_nameid: {item_nameid}")

            # Get currency code - use logged in wallet if available, otherwise default to RUB
            currency_code = 5  # Default: RUB
            if self._logged_in:
                try:
                    wallet = self.get_wallet_balance()
                    currency_code = wallet.currency
                except Exception:
                    pass  # Use default RUB if wallet fetch fails

            # Fetch histogram
            url = f"{self.MARKET_URL}/itemordershistogram"
            params = {
                'country': 'RU',
                'language': 'russian',
                'currency': currency_code,
                'item_nameid': item_nameid,
                'two_factor': 0,
            }

            response = self._retry_on_error(self._make_public_request, 'GET', url, params=params)
            data = response.json()

            if not data.get('success'):
                logger.warning(f"Failed to get histogram for {market_hash_name}")
                return None

            # Parse prices (in cents)
            highest_buy = data.get('highest_buy_order')
            lowest_sell = data.get('lowest_sell_order')
            buy_graph = data.get('buy_order_graph', [])
            sell_graph = data.get('sell_order_graph', [])

            return {
                'highest_buy_order': int(highest_buy) / 100 if highest_buy else None,
                'lowest_sell_order': int(lowest_sell) / 100 if lowest_sell else None,
                'buy_order_count': len(buy_graph),
                'sell_order_count': len(sell_graph),
                'buy_order_graph': buy_graph,
                'sell_order_graph': sell_graph,
            }

        except Exception as e:
            logger.error(f"Failed to get market histogram: {e}")
            return None

    def get_buy_orders(self) -> list[dict]:
        """
        Get all active buy orders.

        Returns:
            List of active buy orders
        """
        self.ensure_logged_in()

        try:
            url = f"{self.MARKET_URL}/mylistings/"
            response = self._retry_on_error(self._make_request, 'GET', url)

            # Parse response - might need to parse HTML or JSON
            # This is simplified - actual implementation may differ
            data = response.json() if 'json' in response.headers.get('content-type', '') else {}

            orders = []
            buy_orders = data.get('buy_orders', {})

            for order_id, order_data in buy_orders.items():
                orders.append({
                    "order_id": order_id,
                    "item_name": order_data.get("hash_name", ""),
                    "price": int(order_data.get("price", 0)) / 100.0,
                    "quantity": order_data.get("quantity", 1),
                    "quantity_remaining": order_data.get("quantity_remaining", 1),
                })

            return orders

        except Exception as e:
            logger.error(f"Failed to get buy orders: {e}")
            return []

    def cancel_buy_order(self, order_id: str) -> bool:
        """
        Cancel a buy order.

        Args:
            order_id: Steam order ID

        Returns:
            True if cancelled successfully
        """
        self.ensure_logged_in()

        try:
            logger.info(f"Cancelling buy order: {order_id}")

            url = f"{self.MARKET_URL}/cancelbuyorder/"

            form_data = {
                'sessionid': self._sessionid,
                'buy_orderid': order_id
            }

            response = self._retry_on_error(
                self._make_request,
                'POST',
                url,
                data=form_data
            )

            result = response.json()
            success = result.get('success') in [1, True, '1']

            if success:
                logger.info(f"Buy order cancelled: {order_id}")
            else:
                logger.warning(f"Failed to cancel order: {order_id}")

            return success

        except Exception as e:
            logger.error(f"Failed to cancel buy order: {e}")
            return False

    def get_inventory(self) -> list[InventoryItem]:
        """
        Get Steam inventory for CS2.

        Returns:
            List of inventory items
        """
        self.ensure_logged_in()

        try:
            # Get steamid from session
            # This is simplified - you might need to fetch steamid differently
            url = f"{self.BASE_URL}/my/inventory/json/{CS2_APPID}/2"

            response = self._retry_on_error(self._make_request, 'GET', url)
            data = response.json()

            items = []
            assets = data.get('rgInventory', {})
            descriptions = data.get('rgDescriptions', {})

            # Handle both dict and list formats
            if isinstance(assets, list):
                logger.warning("Inventory returned as list (possibly empty or error format)")
                return []

            if not isinstance(assets, dict):
                logger.error(f"Unexpected assets type: {type(assets)}")
                return []

            for asset_id, asset_data in assets.items():
                classid = asset_data.get('classid')
                instanceid = asset_data.get('instanceid')

                desc_key = f"{classid}_{instanceid}"
                desc = descriptions.get(desc_key, {})

                items.append(InventoryItem(
                    asset_id=asset_id,
                    class_id=classid,
                    instance_id=instanceid,
                    market_hash_name=desc.get('market_hash_name', ''),
                    tradable=desc.get('tradable', False),
                    marketable=desc.get('marketable', False),
                    amount=int(asset_data.get('amount', 1)),
                ))

            logger.info(f"Found {len(items)} items in inventory")
            return items

        except Exception as e:
            logger.error(f"Failed to get inventory: {e}")
            return []

    def _generate_confirmation_key(self, identity_secret: str, tag: str, timestamp: Optional[int] = None) -> str:
        """
        Generate confirmation key for Steam Guard confirmations.

        Args:
            identity_secret: Identity secret from Steam Guard
            tag: Tag for the key ('conf' for fetching, 'allow' for confirming)
            timestamp: Unix timestamp (if None, uses current time)

        Returns:
            Base64 encoded confirmation key
        """
        if timestamp is None:
            timestamp = int(time.time())

        # Decode identity secret from base64
        try:
            key = base64.b64decode(identity_secret)
        except Exception as e:
            logger.error(f"Failed to decode identity_secret: {e}")
            return ""

        # Create buffer: timestamp (8 bytes) + tag (ASCII)
        buffer = timestamp.to_bytes(8, 'big') + tag.encode('ascii')

        # Generate HMAC-SHA1
        mac = hmac.new(key, buffer, hashlib.sha1).digest()

        # Return base64 encoded
        return base64.b64encode(mac).decode('ascii')

    def get_confirmations(self, identity_secret: str, steamid: str) -> list[dict]:
        """
        Get pending Steam Guard confirmations (trades, market orders, etc).

        Args:
            identity_secret: Identity secret from Steam Guard
            steamid: Steam ID64

        Returns:
            List of confirmations
        """
        self.ensure_logged_in()

        try:
            timestamp = int(time.time())
            device_id = f"android:{steamid}"

            # Generate confirmation key
            conf_key = self._generate_confirmation_key(identity_secret, 'conf', timestamp)

            # Use /getlist endpoint (JSON) instead of /conf (HTML)
            # This is what SDA uses and it actually works (returns 200)
            url = "https://steamcommunity.com/mobileconf/getlist"
            params = {
                'p': device_id,
                'a': steamid,
                'k': conf_key,
                't': timestamp,
                'm': 'android',
                'tag': 'conf'
            }

            logger.debug(f"Requesting confirmations: URL={url}")
            logger.debug(f"Params: p={device_id}, a={steamid}, t={timestamp}, k={conf_key[:10]}..., m=android, tag=conf")

            # Make request
            self.ensure_logged_in()
            response = self._session.get(url, params=params, timeout=30)

            logger.debug(f"Response status: {response.status_code}")

            # Parse JSON response
            if response.status_code != 200:
                logger.info(f"No pending confirmations (status: {response.status_code})")
                return []

            try:
                data = response.json()
            except Exception as e:
                logger.error(f"Failed to parse confirmation response: {e}")
                return []

            # Check success
            if not data.get('success'):
                logger.info("No pending confirmations (success=False)")
                return []

            # Get confirmation list
            confirmations = data.get('conf', [])
            if not confirmations:
                logger.info("No pending confirmations")
                return []

            # Parse JSON confirmation data
            # Format: {"id": "...", "nonce": "...", "type": 3, "headline": "..."}
            result = []
            for conf in confirmations:
                result.append({
                    'id': str(conf.get('id', '')),
                    'key': str(conf.get('nonce', '')),  # 'nonce' is the confirmation key
                    'type': int(conf.get('type', 0)),
                    'headline': conf.get('headline', ''),
                    'summary': conf.get('summary', []),
                })

            logger.info(f"Found {len(result)} pending confirmations")
            return result

        except Exception as e:
            logger.error(f"Failed to get confirmations: {e}")
            return []

    def confirm_market_transaction(self, identity_secret: str, steamid: str, conf_id: str, conf_key: str) -> bool:
        """
        Confirm a specific market transaction.

        Args:
            identity_secret: Identity secret from Steam Guard
            steamid: Steam ID64
            conf_id: Confirmation ID
            conf_key: Confirmation key

        Returns:
            True if confirmed successfully
        """
        self.ensure_logged_in()

        try:
            timestamp = int(time.time())
            device_id = f"android:{steamid}"

            # Generate confirmation key for allow operation
            allow_key = self._generate_confirmation_key(identity_secret, 'allow', timestamp)

            url = "https://steamcommunity.com/mobileconf/ajaxop"
            params = {
                'op': 'allow',
                'p': device_id,
                'a': steamid,
                'k': allow_key,
                't': timestamp,
                'm': 'android',
                'tag': 'allow',
                'cid': conf_id,
                'ck': conf_key
            }

            response = self._retry_on_error(
                self._make_request,
                'GET',
                url,
                params=params
            )

            result = response.json()
            success = result.get('success', False)

            if success:
                logger.info(f"✅ Market transaction confirmed: {conf_id}")
            else:
                logger.warning(f"Failed to confirm transaction: {conf_id}")

            return success

        except Exception as e:
            logger.error(f"Failed to confirm transaction: {e}")
            return False

    def confirm_all_market_transactions(self, identity_secret: str, steamid: str) -> int:
        """
        Confirm all pending market transactions (buy orders).

        Args:
            identity_secret: Identity secret from Steam Guard
            steamid: Steam ID64

        Returns:
            Number of transactions confirmed
        """
        try:
            logger.info("Fetching pending confirmations...")
            confirmations = self.get_confirmations(identity_secret, steamid)

            if not confirmations:
                logger.info("No confirmations to process")
                return 0

            # Log all confirmations with their types for debugging
            for conf in confirmations:
                logger.info(f"Confirmation found: id={conf.get('id')}, type={conf.get('type')}, headline={conf.get('headline', 'N/A')[:80]}")

            # Filter market transactions
            # Type 1 = generic confirmation
            # Type 2 = trade offer
            # Type 3 = market listing (selling)
            # Type 12 = market buy order (discovered from logs!)
            market_confirmations = [c for c in confirmations if c['type'] in [1, 2, 3, 12]]

            if not market_confirmations:
                logger.warning(f"No market confirmations found (found {len(confirmations)} confirmations with unsupported types)")
                logger.warning(f"Confirmation types: {[c.get('type') for c in confirmations]}")
                logger.warning(f"Please report this - we may need to add support for these types")
                return 0

            logger.info(f"Found {len(market_confirmations)} market transactions to confirm")

            confirmed_count = 0
            for conf in market_confirmations:
                if self.confirm_market_transaction(identity_secret, steamid, conf['id'], conf['key']):
                    confirmed_count += 1
                    time.sleep(1)  # Small delay between confirmations

            logger.info(f"Confirmed {confirmed_count}/{len(market_confirmations)} market transactions")
            return confirmed_count

        except Exception as e:
            logger.error(f"Failed to confirm all transactions: {e}")
            return 0

    def create_buy_order(
        self,
        market_hash_name: str,
        price_total: int,
        quantity: int = 1,
        billing_state: str = "",
        save_my_address: int = 0
    ) -> Optional[str]:
        """
        Create a buy order on Steam Market.

        Args:
            market_hash_name: Item name
            price_total: Price in cents (e.g., 1000 = 10.00 RUB)
            quantity: Number of items
            billing_state: Billing state (optional)
            save_my_address: Save address flag

        Returns:
            Buy order ID or None if failed
        """
        try:
            logger.info(f"Creating buy order: {market_hash_name} @ {price_total/100:.2f} x{quantity}")

            url = f"{self.BASE_URL}/market/createbuyorder/"

            data = {
                'sessionid': self._session_id,
                'currency': self._get_wallet_currency_code(),
                'appid': CS2_APPID,
                'market_hash_name': market_hash_name,
                'price_total': price_total,
                'quantity': quantity,
                'billing_state': billing_state,
                'save_my_address': save_my_address
            }

            response = self._make_request('POST', url, data=data)
            result = response.json()

            if result.get('success') == 1:
                buy_orderid = result.get('buy_orderid')
                logger.info(f"✅ Buy order created: {buy_orderid}")
                return str(buy_orderid)
            else:
                error_msg = result.get('message', 'Unknown error')
                logger.error(f"Failed to create buy order: {error_msg}")
                return None

        except Exception as e:
            logger.error(f"Error creating buy order: {e}", exc_info=True)
            return None

    def cancel_buy_order(self, buy_orderid: str) -> bool:
        """
        Cancel a buy order.

        Args:
            buy_orderid: Buy order ID

        Returns:
            True if cancelled successfully
        """
        try:
            logger.info(f"Cancelling buy order: {buy_orderid}")

            url = f"{self.BASE_URL}/market/cancelbuyorder/"

            data = {
                'sessionid': self._session_id,
                'buy_orderid': buy_orderid
            }

            response = self._make_request('POST', url, data=data)
            result = response.json()

            if result.get('success') == 1:
                logger.info(f"✅ Buy order cancelled: {buy_orderid}")
                return True
            else:
                logger.error(f"Failed to cancel buy order: {result}")
                return False

        except Exception as e:
            logger.error(f"Error cancelling buy order: {e}", exc_info=True)
            return False

    def get_my_market_orders(self) -> dict:
        """
        Get all active market orders (buy and sell).

        Returns:
            Dict with buy_orders and sell_orders lists
        """
        try:
            url = f"{self.BASE_URL}/market/"
            response = self._make_request('GET', url)

            # Parse HTML for market orders
            # Steam returns orders in JavaScript variables
            html = response.text

            # Extract g_rgWalletInfo (contains currency info)
            wallet_match = re.search(r'g_rgWalletInfo\s*=\s*({.+?});', html, re.DOTALL)

            # Extract g_rgBuyOrder (buy orders)
            buy_orders_match = re.search(r'g_rgBuyOrder\s*=\s*(\[.+?\]);', html, re.DOTALL)

            result = {
                'buy_orders': [],
                'sell_orders': []
            }

            if buy_orders_match:
                try:
                    buy_orders_json = buy_orders_match.group(1)
                    result['buy_orders'] = json.loads(buy_orders_json)
                except Exception as e:
                    logger.error(f"Failed to parse buy orders: {e}")

            return result

        except Exception as e:
            logger.error(f"Error getting market orders: {e}", exc_info=True)
            return {'buy_orders': [], 'sell_orders': []}

    def get_market_history(self, count: int = 100) -> dict:
        """
        Get market purchase history.

        Args:
            count: Number of items to retrieve

        Returns:
            Dict with assets and events
        """
        try:
            url = f"{self.BASE_URL}/market/myhistory/"

            params = {
                'count': count
            }

            response = self._make_request('GET', url, params=params)
            result = response.json()

            if result.get('success'):
                return result
            else:
                logger.error(f"Failed to get market history: {result}")
                return {'assets': [], 'events': []}

        except Exception as e:
            logger.error(f"Error getting market history: {e}", exc_info=True)
            return {'assets': [], 'events': []}

    def _get_wallet_currency_code(self) -> int:
        """Get wallet currency code (internal method)."""
        try:
            wallet = self.get_wallet_balance()
            return wallet.currency
        except:
            # Default to RUB (5) if failed
            return 5

    def __enter__(self):
        """Context manager entry."""
        self.login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.logout()


# Singleton instance
steam_client = SteamClient()
