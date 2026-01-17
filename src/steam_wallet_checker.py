"""
Wrapper для SteamWalletChecker
Использует новый метод проверки баланса через SteamKit2 с 2FA
"""

import os
import sys
from pathlib import Path
from typing import Dict, Optional

# Добавляем путь к SteamWalletChecker
steamwallet_path = Path(__file__).parent.parent / 'SteamWalletChecker'
if str(steamwallet_path) not in sys.path:
    sys.path.insert(0, str(steamwallet_path))

from python_auto_auth import SteamAutoAuthChecker


class SteamWalletBalance:
    """
    Wrapper класс для получения баланса Steam кошелька.
    Совместим с интерфейсом SteamMarketScraper для упрощения миграции.
    """

    # Маппинг кодов валют Steam
    CURRENCY_CODES = {
        1: 'USD', 2: 'GBP', 3: 'EUR', 4: 'CHF', 5: 'RUB',
        6: 'PLN', 7: 'BRL', 8: 'JPY', 9: 'SEK', 10: 'IDR',
        11: 'MYR', 12: 'PHP', 13: 'SGD', 14: 'THB', 15: 'VND',
        16: 'KRW', 17: 'TRY', 18: 'UAH', 19: 'MXN', 20: 'CAD',
        21: 'AUD', 22: 'NZD', 23: 'CNY', 24: 'INR', 25: 'CLP',
        26: 'PEN', 27: 'COP', 28: 'ZAR', 29: 'HKD', 30: 'TWD',
        31: 'SAR', 32: 'AED', 33: 'ARS', 34: 'ILS', 35: 'KWD',
        36: 'QAR', 37: 'KZT', 38: 'CRC', 39: 'UYU',
    }

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        shared_secret: Optional[str] = None,
        identity_secret: Optional[str] = None,
        steamid: Optional[str] = None,
        config_path: Optional[str] = None,
        exe_path: Optional[str] = None
    ):
        """
        Инициализация

        Args:
            username: Steam логин
            password: Steam пароль
            shared_secret: Shared secret для 2FA
            identity_secret: Identity secret для confirmations
            steamid: Steam ID64
            config_path: Путь к конфигу steam_account.json
            exe_path: Путь к exe файлу SteamWalletChecker
        """
        self.username = username
        self.password = password
        self.shared_secret = shared_secret
        self.identity_secret = identity_secret
        self.steamid = steamid
        self.config_path = config_path

        # Инициализируем checker
        try:
            self.checker = SteamAutoAuthChecker(exe_path=exe_path)
        except FileNotFoundError as e:
            # Если exe не найден, пытаемся скомпилировать
            print(f"SteamWalletChecker exe не найден: {e}")
            print("Попытка компиляции...")
            self._build_checker()
            self.checker = SteamAutoAuthChecker(exe_path=exe_path)

    def _build_checker(self):
        """Компиляция SteamWalletChecker если нужно"""
        import subprocess

        steamwallet_dir = Path(__file__).parent.parent / 'SteamWalletChecker'
        try:
            subprocess.run(
                ['dotnet', 'build', '-c', 'Release'],
                cwd=steamwallet_dir,
                check=True,
                capture_output=True,
                text=True
            )
            print("SteamWalletChecker успешно скомпилирован")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Не удалось скомпилировать SteamWalletChecker: {e.stderr}")
        except FileNotFoundError:
            raise RuntimeError("dotnet не найден. Установите .NET 8.0 SDK")

    def get_balance(self) -> Dict:
        """
        Получить баланс кошелька.
        Возвращает результат в формате, совместимом с SteamMarketScraper.

        Returns:
            Dict с результатом:
            {
                'success': bool,
                'balance': float,  # Баланс в основной валюте
                'currency': str,  # Код валюты (RUB, USD, EUR...)
                'raw_balance': str,  # Форматированная строка
                'method': str,  # Метод получения
                'currency_code': int,  # Код валюты Steam
                'country_code': str,  # Код страны
                'balance_usd': float,  # Баланс в USD
            }
        """
        try:
            # Проверяем баланс через новый метод
            if self.config_path:
                wallet_data = self.checker.check_wallet(config_path=self.config_path)
            elif all([self.username, self.password, self.shared_secret]):
                wallet_data = self.checker.check_wallet(
                    username=self.username,
                    password=self.password,
                    shared_secret=self.shared_secret,
                    identity_secret=self.identity_secret,
                    steamid=self.steamid
                )
            else:
                return {
                    'success': False,
                    'error': 'No credentials provided. Either config_path or (username, password, shared_secret) required'
                }

            # Парсим результат
            body = wallet_data.get('Body', {})

            if not body:
                return {
                    'success': False,
                    'error': 'Empty response from SteamWalletChecker'
                }

            # Извлекаем данные
            balance_cents = body.get('balance', 0)
            currency_code = body.get('currency_code', 5)
            formatted_balance = body.get('formatted_balance', '')
            country_code = body.get('wallet_country_code', '')
            balance_usd_cents = body.get('balance_in_usd', 0)

            # Конвертируем центы в основную валюту
            balance = balance_cents / 100.0
            balance_usd = balance_usd_cents / 100.0

            # Определяем валюту
            currency = self.CURRENCY_CODES.get(currency_code, 'USD')

            return {
                'success': True,
                'balance': balance,
                'currency': currency,
                'raw_balance': formatted_balance or f'{balance:.2f} {currency}',
                'method': 'steamkit2_auto_auth',
                'currency_code': currency_code,
                'country_code': country_code,
                'balance_usd': balance_usd,
            }

        except Exception as e:
            return {
                'success': False,
                'error': f'SteamWalletChecker error: {str(e)}'
            }

    @staticmethod
    def from_account_config(account_config: Dict) -> 'SteamWalletBalance':
        """
        Создать экземпляр из конфига аккаунта

        Args:
            account_config: Словарь с данными аккаунта
                {
                    'username': str,
                    'password': str,
                    'shared_secret': str,
                    ...
                }

        Returns:
            SteamWalletBalance instance
        """
        return SteamWalletBalance(
            username=account_config.get('username'),
            password=account_config.get('password'),
            shared_secret=account_config.get('shared_secret')
        )


def test_wallet_checker():
    """Тест проверки баланса"""
    import json

    print("=" * 70)
    print("ТЕСТ: Новый метод проверки баланса Steam (SteamKit2)")
    print("=" * 70)
    print()

    # Ищем конфиг
    config_files = [
        'SteamWalletChecker/steam_account.json',
        'steam_account.json',
    ]

    config_path = None
    for path in config_files:
        if os.path.exists(path):
            config_path = path
            break

    if not config_path:
        print("[X] Конфиг не найден!")
        print("Создайте steam_account.json с форматом:")
        print('{')
        print('  "username": "your_login",')
        print('  "password": "your_password",')
        print('  "shared_secret": "your_shared_secret_base64"')
        print('}')
        return

    print(f"[OK] Используем конфиг: {config_path}")
    print()

    # Создаем checker
    checker = SteamWalletBalance(config_path=config_path)

    print("-" * 70)
    print("Получение баланса...")
    print("-" * 70)
    print()

    result = checker.get_balance()

    print()
    print("=" * 70)
    print("РЕЗУЛЬТАТ")
    print("=" * 70)
    print()

    if result['success']:
        print("[SUCCESS]")
        print()
        print(f"  Balance:     {result['balance']} {result['currency']}")
        print(f"  Raw:         {result['raw_balance']}")
        print(f"  USD:         ${result.get('balance_usd', 0):.2f}")
        print(f"  Country:     {result.get('country_code', 'N/A')}")
        print(f"  Method:      {result['method']}")
        print()
        print("Новый метод работает!")
    else:
        print("[ERROR]")
        print(f"  {result.get('error')}")

    print()
    print("=" * 70)


if __name__ == '__main__':
    test_wallet_checker()
