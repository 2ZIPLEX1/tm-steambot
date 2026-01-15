"""
Account Manager - управление несколькими аккаунтами Steam/CSGO.TM.

Поддерживает:
- Загрузку конфигурации из accounts.json
- Создание клиентов для каждого аккаунта
- Прокси для каждого аккаунта
- Лимиты и ограничения
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from src.logger import get_logger
from src.steam_client import SteamClient
from src.csgotm_client import CsgoTmClient
from src.proxy_manager import ProxyManager

logger = get_logger(__name__)


@dataclass
class AccountConfig:
    """Конфигурация одного аккаунта."""
    name: str
    enabled: bool
    currency: str = 'RUB'  # Валюта Steam (RUB, EUR, USD, ...)

    # Steam credentials
    steam_username: str = ''
    steam_password: str = ''
    steam_api_key: str = ''
    steam_shared_secret: str = ''
    steam_identity_secret: str = ''

    # CSGO.TM credentials
    csgotm_api_key: str = ''

    # Proxy (optional)
    proxy: Optional[str] = None

    # Limits
    max_items: int = 10
    max_price_per_item: float = 1000.0
    total_budget: float = 5000.0


class Account:
    """
    Wrapper для одного аккаунта с клиентами Steam и CSGO.TM.
    """

    def __init__(self, config: AccountConfig):
        """
        Initialize account with config.

        Args:
            config: Account configuration
        """
        self.config = config
        self.name = config.name

        # Clients (будут созданы при логине)
        self.steam_client: Optional[SteamClient] = None
        self.csgotm_client: Optional[CsgoTmClient] = None

        # Session для прокси
        self._session: Optional[requests.Session] = None

        logger.info(f"Account initialized: {self.name}")

    def _create_session_with_proxy(self) -> requests.Session:
        """Create session with proxy if configured."""
        session = requests.Session()

        if self.config.proxy:
            session.proxies = {
                'http': self.config.proxy,
                'https': self.config.proxy,
            }
            logger.info(f"[{self.name}] Proxy configured: {self.config.proxy.split('@')[-1]}")  # Hide credentials

        return session

    def login(self) -> bool:
        """
        Login to Steam and CSGO.TM.

        Returns:
            True if both logins successful
        """
        logger.info(f"[{self.name}] Logging in...")

        try:
            # Create session with proxy
            self._session = self._create_session_with_proxy()

            # Create Steam client with proxy support
            # Try to get proxy_manager from parent AccountManager if available
            proxy_manager = None
            if hasattr(self, '_proxy_manager') and self._proxy_manager:
                proxy_manager = self._proxy_manager
                logger.info(f"[{self.name}] Using ProxyManager with {len(proxy_manager.proxies)} proxies")

            self.steam_client = SteamClient(
                proxy=self.config.proxy,
                proxy_manager=proxy_manager
            )

            # If proxy is set and session already created, use it
            # (for backward compatibility)
            if self.config.proxy and self._session:
                self.steam_client._session = self._session
                self.steam_client._setup_proxy()  # Ensure proxy is configured

            # Try to login with account-specific cookie file
            # This allows each account to have its own cookies
            cookie_file = f"steam_cookies_{self.name}.txt"

            # Try to login (cookies first, then WebAuth)
            # Pass account-specific credentials and cookie file
            if not self.steam_client.login(
                username=self.config.steam_username,
                password=self.config.steam_password,
                shared_secret=self.config.steam_shared_secret,
                cookie_file=cookie_file,
            ):
                logger.error(f"[{self.name}] Login failed")
                return False

            logger.info(f"[{self.name}] ✅ Steam logged in")

            # Skip auto-detection on login to avoid rate limiting
            # User can manually detect currency via GUI button if needed
            logger.info(f"[{self.name}] Currency: {self.config.currency} (use '🔍 Валюта' button to auto-detect)")

            # Create CSGO.TM client with same session (includes proxy)
            self.csgotm_client = CsgoTmClient(
                api_key=self.config.csgotm_api_key,
                session=self._session  # Use same session with proxy
            )

            # Test CSGO.TM connection
            if not self.csgotm_client.ping():
                logger.warning(f"[{self.name}] CSGO.TM ping failed")
            else:
                logger.info(f"[{self.name}] ✅ CSGO.TM connected")

            return True

        except Exception as e:
            logger.error(f"[{self.name}] Login error: {e}")
            return False

    def logout(self):
        """Logout from Steam."""
        if self.steam_client:
            self.steam_client.logout()
            logger.info(f"[{self.name}] Logged out")

    def get_wallet_balance(self) -> float:
        """Get Steam wallet balance."""
        if not self.steam_client:
            return 0.0

        try:
            wallet = self.steam_client.get_wallet_balance()
            return wallet.balance
        except Exception as e:
            logger.error(f"[{self.name}] Failed to get wallet balance: {e}")
            return 0.0

    def get_csgotm_balance(self) -> float:
        """Get CSGO.TM balance."""
        if not self.csgotm_client:
            return 0.0

        try:
            return self.csgotm_client.get_money()
        except Exception as e:
            logger.error(f"[{self.name}] Failed to get CSGO.TM balance: {e}")
            return 0.0

    def is_logged_in(self) -> bool:
        """Check if logged in to Steam."""
        return self.steam_client is not None and self.steam_client._logged_in

    def detect_currency(self) -> Optional[str]:
        """
        Detect account currency from Steam wallet.

        Returns:
            Currency code (e.g., 'RUB', 'USD', 'EUR') or None if failed
        """
        if not self.steam_client or not self.is_logged_in():
            logger.error(f"[{self.name}] Cannot detect currency: not logged in")
            return None

        try:
            import time

            # Add delay to avoid rate limiting
            logger.info(f"[{self.name}] Waiting 3 seconds before currency detection...")
            time.sleep(3)

            # Temporarily bypass proxy for currency detection to avoid rate limiting
            proxy_session = self.steam_client._session if hasattr(self.steam_client, '_session') else None

            if self.config.proxy and proxy_session:
                # Create clean session without proxy for wallet check
                import requests
                clean_session = requests.Session()

                # Copy essential cookies to new session (without proxy context)
                for cookie in proxy_session.cookies:
                    clean_session.cookies.set_cookie(cookie)

                self.steam_client._session = clean_session
                logger.info(f"[{self.name}] Using direct connection (bypassing proxy) for currency detection")

            wallet_info = self.steam_client.get_wallet_balance()

            # Restore proxy session
            if self.config.proxy and proxy_session:
                self.steam_client._session = proxy_session
                logger.debug(f"[{self.name}] Restored proxy session")

            if wallet_info and wallet_info.currency_code:
                detected_currency = wallet_info.currency_code

                # Проверяем надёжность определения
                is_reliable = wallet_info.balance > 0 or wallet_info.currency != 1

                if not is_reliable:
                    logger.warning(f"[{self.name}] ⚠️ Currency detection unreliable (balance=0, default USD)")
                    logger.info(f"[{self.name}] Detected: {detected_currency}, but keeping current: {self.config.currency}")
                    return self.config.currency  # Возвращаем текущую валюту

                logger.info(f"[{self.name}] 🔍 Detected currency: {detected_currency}")

                # Update config
                if self.config.currency != detected_currency:
                    old_currency = self.config.currency
                    self.config.currency = detected_currency
                    logger.info(f"[{self.name}] ✅ Currency updated: {old_currency} → {detected_currency}")

                return detected_currency
            else:
                logger.error(f"[{self.name}] Failed to get wallet info")
                return None
        except Exception as e:
            logger.error(f"[{self.name}] Error detecting currency: {e}")
            # Ensure proxy session is restored even on error
            if self.config.proxy and proxy_session:
                self.steam_client._session = proxy_session
            return None


def load_proxy_manager_from_file(
    proxy_file: str = 'proxies.txt',
    max_requests: int = 15,
    blacklist_duration: int = 30,
    cooldown: int = 60
) -> Optional[ProxyManager]:
    """
    Load ProxyManager from proxy file.

    Args:
        proxy_file: Path to proxy list file
        max_requests: Max requests per proxy before rotation
        blacklist_duration: Blacklist duration in minutes
        cooldown: Cooldown between proxy uses in seconds

    Returns:
        ProxyManager instance or None if no proxies
    """
    proxy_path = Path(proxy_file)
    if not proxy_path.exists():
        logger.debug(f"Proxy file not found: {proxy_file}")
        return None

    proxies = []
    try:
        with open(proxy_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                # Skip comments and empty lines
                if not line or line.startswith('#'):
                    continue

                # Auto-add socks5:// if no protocol specified
                if '://' not in line:
                    line = f'socks5://{line}'

                proxies.append(line)

        if proxies:
            logger.info(f"Loaded {len(proxies)} proxies from {proxy_file}")
            return ProxyManager(
                proxies=proxies,
                max_requests_per_proxy=max_requests,
                blacklist_duration_minutes=blacklist_duration,
                cooldown_seconds=cooldown
            )
        else:
            logger.debug(f"No proxies found in {proxy_file}")
            return None

    except Exception as e:
        logger.error(f"Failed to load proxies: {e}")
        return None


class AccountManager:
    """
    Менеджер для управления несколькими аккаунтами.
    """

    def __init__(self, config_file: str = "accounts.json", proxy_file: str = 'proxies.txt'):
        """
        Initialize account manager.

        Args:
            config_file: Path to accounts configuration file
            proxy_file: Path to proxy list file (optional)
        """
        self.config_file = Path(config_file)
        self.accounts: list[Account] = []

        # Load proxy manager if available
        self.proxy_manager = load_proxy_manager_from_file(proxy_file)

        self._load_accounts()

    def _load_accounts(self):
        """Load accounts from configuration file."""
        if not self.config_file.exists():
            logger.warning(f"Config file not found: {self.config_file}")
            logger.info("Please create accounts.json based on accounts.example.json")
            return

        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                accounts_data = json.load(f)

            for acc_data in accounts_data:
                if not acc_data.get('enabled', True):
                    logger.info(f"Account disabled: {acc_data.get('name')}")
                    continue

                config = AccountConfig(
                    name=acc_data['name'],
                    enabled=acc_data.get('enabled', True),
                    currency=acc_data.get('currency', 'RUB'),
                    steam_username=acc_data['steam']['username'],
                    steam_password=acc_data['steam']['password'],
                    steam_api_key=acc_data['steam']['api_key'],
                    steam_shared_secret=acc_data['steam']['shared_secret'],
                    steam_identity_secret=acc_data['steam']['identity_secret'],
                    csgotm_api_key=acc_data['csgotm']['api_key'],
                    proxy=acc_data.get('proxy'),
                    max_items=acc_data.get('limits', {}).get('max_items', 10),
                    max_price_per_item=acc_data.get('limits', {}).get('max_price_per_item', 1000.0),
                    total_budget=acc_data.get('limits', {}).get('total_budget', 5000.0),
                )

                account = Account(config)
                # Pass proxy_manager to account if available
                if self.proxy_manager:
                    account._proxy_manager = self.proxy_manager
                self.accounts.append(account)

            logger.info(f"Loaded {len(self.accounts)} account(s)")

        except Exception as e:
            logger.error(f"Failed to load accounts: {e}")
            raise

    def get_account(self, name: str) -> Optional[Account]:
        """Get account by name."""
        for account in self.accounts:
            if account.name == name:
                return account
        return None

    def get_all_accounts(self) -> list[Account]:
        """Get all accounts."""
        return self.accounts

    def get_enabled_accounts(self) -> list[Account]:
        """Get all enabled accounts."""
        return [acc for acc in self.accounts if acc.config.enabled]

    def login_all(self) -> dict[str, bool]:
        """
        Login to all enabled accounts.

        Returns:
            Dict of account_name: success status
        """
        results = {}

        for account in self.get_enabled_accounts():
            success = account.login()
            results[account.name] = success

        successful = sum(1 for success in results.values() if success)
        logger.info(f"Login complete: {successful}/{len(results)} accounts logged in")

        return results

    def logout_all(self):
        """Logout from all accounts."""
        for account in self.accounts:
            account.logout()

    def get_total_balance(self) -> dict:
        """
        Get total balance across all accounts.

        Returns:
            Dict with steam_balance and csgotm_balance
        """
        steam_total = 0.0
        csgotm_total = 0.0

        for account in self.get_enabled_accounts():
            steam_total += account.get_wallet_balance()
            csgotm_total += account.get_csgotm_balance()

        return {
            'steam_balance': steam_total,
            'csgotm_balance': csgotm_total,
            'total': steam_total + csgotm_total,
        }

    def __len__(self) -> int:
        """Return number of accounts."""
        return len(self.accounts)

    def __iter__(self):
        """Iterate over accounts."""
        return iter(self.accounts)


# Singleton instance (optional)
# account_manager = AccountManager()
