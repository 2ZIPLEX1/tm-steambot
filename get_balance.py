"""
Получение баланса Steam
Использует новый метод SteamWalletChecker с 2FA авторизацией
"""

import os
import json
from src.steam_wallet_checker import SteamWalletBalance


def load_account_data(filepath='steam_account.json'):
    """Загружает данные аккаунта из JSON файла"""
    if not os.path.exists(filepath):
        # Пробуем найти в SteamWalletChecker
        alt_path = os.path.join('SteamWalletChecker', 'steam_account.json')
        if os.path.exists(alt_path):
            filepath = alt_path
        else:
            return None, f"Файл {filepath} не найден"

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        username = data.get('username')
        password = data.get('password')
        shared_secret = data.get('shared_secret')

        if not username or not password or not shared_secret:
            return None, "username, password или shared_secret не найдены в конфиге"

        return {
            'username': username,
            'password': password,
            'shared_secret': shared_secret,
            'config_path': filepath
        }, None

    except Exception as e:
        return None, f"Ошибка чтения файла: {e}"


def main():
    """Основная функция"""
    print("=" * 70)
    print("STEAM BALANCE CHECKER (New SteamKit2 Method)")
    print("=" * 70)
    print()

    # Load account config
    print("Loading account configuration...")
    account, error = load_account_data()

    if error or not account:
        print(f"[X] Error: {error or 'No account config found'}")
        print()
        print("=" * 70)
        print("PLEASE CREATE steam_account.json")
        print("=" * 70)
        print()
        print("You need to create a config file with your Steam credentials:")
        print()
        print("Create steam_account.json with format:")
        print('   {')
        print('     "username": "your_steam_login",')
        print('     "password": "your_steam_password",')
        print('     "shared_secret": "your_shared_secret_base64"')
        print('   }')
        print()
        print("To get shared_secret:")
        print("1. Use Steam Desktop Authenticator")
        print("2. Open maFiles/YOUR_STEAMID.maFile")
        print("3. Copy 'shared_secret' value")
        print()
        return

    print("[OK] Account config loaded")
    print(f"  Username: {account.get('username', 'N/A')}")
    print(f"  Shared secret: {'YES' if account.get('shared_secret') else 'NO'}")
    print()

    # Get balance
    print("-" * 70)
    print("GETTING BALANCE...")
    print("-" * 70)
    print()

    # Используем новый метод
    if 'config_path' in account:
        checker = SteamWalletBalance(config_path=account['config_path'])
    else:
        checker = SteamWalletBalance(
            username=account['username'],
            password=account['password'],
            shared_secret=account['shared_secret']
        )

    result = checker.get_balance()

    print()
    print("=" * 70)
    print("RESULT")
    print("=" * 70)
    print()

    if result['success']:
        print("SUCCESS!")
        print()
        print(f"  Balance:      {result['balance']} {result['currency']}")
        print(f"  Raw:          {result['raw_balance']}")
        print(f"  USD:          ${result.get('balance_usd', 0):.2f}")
        print(f"  Country:      {result.get('country_code', 'N/A')}")
        print(f"  Method:       {result['method']}")
        print()
        print("New method with SteamKit2 works perfectly!")
    else:
        print("ERROR!")
        print()
        print(f"  Message: {result.get('error')}")
        print()
        print("If you see authentication errors:")
        print("  - Check your username and password")
        print("  - Verify shared_secret is correct (Base64 format)")
        print("  - Make sure 2FA is enabled on your account")

    print()
    print("=" * 70)


if __name__ == '__main__':
    main()
