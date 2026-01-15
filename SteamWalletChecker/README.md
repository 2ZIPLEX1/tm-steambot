# Steam Wallet Checker

Автоматическая проверка баланса Steam кошелька с поддержкой 2FA.

## 🚀 Быстрый старт

### 1. Создайте конфиг

Создайте файл `steam_account.json`:

```json
{
  "username": "ваш_логин",
  "password": "ваш_пароль",
  "shared_secret": "ваш_shared_secret_base64",
  "identity_secret": "ваш_identity_secret_base64",
  "auto_login": true
}
```

### 2. Запустите

```bash
# Через dotnet
dotnet run

# Или напрямую
dotnet run steam_account.json
```

### 3. Python интеграция

```python
from python_auto_auth import SteamAutoAuthChecker

checker = SteamAutoAuthChecker()
wallet = checker.check_wallet(config_path="steam_account.json")
balance = checker.extract_balance(wallet)
print(f"Balance: ${balance:.2f}")
```

## 🔑 Получение shared_secret

1. Скачайте [Steam Desktop Authenticator](https://github.com/Jessecar96/SteamDesktopAuthenticator)
2. Настройте аутентификатор на аккаунт
3. Откройте файл `maFiles/ВАШЕ_STEAMID.maFile`
4. Скопируйте `shared_secret` и `identity_secret`

## 📁 Структура

```
SteamWalletChecker/
├── SteamWalletChecker.csproj       # Проект
├── ProgramWithAutoAuth.cs          # Основная программа с auto-auth
├── SteamGuardAuthenticator.cs      # Генератор 2FA кодов
├── SteamConfig.cs                  # Конфигурация
├── python_auto_auth.py             # Python интеграция
└── steam_account.example.json      # Пример конфига
```

## 🔒 Безопасность

⚠️ **ВАЖНО:**
- `steam_account.json` уже в `.gitignore`
- Не коммитьте файлы с секретами
- Храните конфиги в безопасном месте

## 📊 Пример результата

```json
{
  "Body": {
    "has_wallet": true,
    "balance": 172176,
    "formatted_balance": "1721,76 руб",
    "wallet_country_code": "RU",
    "currency_code": 5,
    "balance_in_usd": 2195
  }
}
```

## 🛠️ Требования

- .NET 8.0 SDK
- Python 3.8+ (для интеграции)

## 📝 Лицензия

Используйте на свой риск. SteamKit2 под LGPL-2.1.
