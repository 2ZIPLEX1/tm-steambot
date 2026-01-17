"""
Python интеграция с автоматической 2FA аутентификацией
Использует shared_secret и identity_secret для автоматического входа
"""
import subprocess
import json
import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SteamAutoAuthChecker:
    """
    Класс для проверки Steam кошелька с автоматической 2FA аутентификацией
    """

    def __init__(self, exe_path: Optional[str] = None):
        """
        Инициализация

        Args:
            exe_path: Путь к exe файлу с auto-auth поддержкой
        """
        if exe_path is None:
            self.exe_path = self._find_exe()
        else:
            self.exe_path = Path(exe_path)

        if not self.exe_path.exists():
            raise FileNotFoundError(
                f"Executable not found: {self.exe_path}\n"
                "Build with: dotnet publish -c Release"
            )

        logger.info(f"Using executable: {self.exe_path}")

    def _find_exe(self) -> Path:
        """Поиск exe файла с auto-auth"""
        base = Path(__file__).parent
        paths = [
            base / "bin" / "Release" / "net8.0" / "SteamWalletChecker.exe",
            base / "bin" / "Debug" / "net8.0" / "SteamWalletChecker.exe",
        ]

        for path in paths:
            if path.exists():
                return path

        raise FileNotFoundError("SteamWalletChecker.exe not found")

    def create_config(
        self,
        username: str,
        password: str,
        shared_secret: str,
        identity_secret: Optional[str] = None,
        steamid: Optional[str] = None,
        config_path: str = "steam_account.json"
    ) -> str:
        """
        Создать конфигурационный файл

        Args:
            username: Steam логин
            password: Steam пароль
            shared_secret: Shared secret (Base64)
            identity_secret: Identity secret (Base64) - опционально
            steamid: Steam ID 64 - опционально
            config_path: Путь для сохранения конфига

        Returns:
            str: Путь к созданному конфигу
        """
        config = {
            "username": username,
            "password": password,
            "shared_secret": shared_secret,
            "identity_secret": identity_secret if identity_secret else None,
            "steamid": steamid if steamid else None,
            "auto_login": True
        }

        config_file = Path(config_path)
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)

        logger.info(f"Config created: {config_file}")
        return str(config_file.absolute())

    def check_wallet(
        self,
        config_path: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        shared_secret: Optional[str] = None,
        identity_secret: Optional[str] = None,
        steamid: Optional[str] = None,
        timeout: int = 120
    ) -> Dict[str, Any]:
        """
        Проверить баланс кошелька

        Args:
            config_path: Путь к JSON конфигу (приоритет)
            username: Логин (если config_path не указан)
            password: Пароль (если config_path не указан)
            shared_secret: Shared secret (если config_path не указан)
            timeout: Таймаут в секундах

        Returns:
            dict: Информация о кошельке
        """
        # Если config_path не указан, создаем временный конфиг
        temp_config = None
        if config_path is None:
            if not all([username, password, shared_secret]):
                raise ValueError(
                    "Either config_path or (username, password, shared_secret) required"
                )

            temp_config = f"temp_config_{datetime.now():%Y%m%d_%H%M%S}.json"
            config_path = self.create_config(
                username=username,
                password=password,
                shared_secret=shared_secret,
                identity_secret=identity_secret,
                steamid=steamid,
                config_path=temp_config
            )

        try:
            logger.info(f"Checking wallet with config: {config_path}")

            # Copy config to exe directory if needed
            exe_dir = self.exe_path.parent
            config_name = Path(config_path).name
            exe_config_path = exe_dir / config_name
            if exe_config_path != Path(config_path):
                import shutil
                shutil.copy2(config_path, exe_config_path)
                config_to_use = str(exe_config_path)
            else:
                config_to_use = config_path

            process = subprocess.Popen(
                [str(self.exe_path), config_to_use],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.exe_path.parent),  # Set cwd to exe directory
                shell=True
            )

            # Ожидание завершения (увеличен timeout до 120 сек)
            stdout, stderr = process.communicate(timeout=120)

            # Decode output
            try:
                stdout = stdout.decode('cp866', errors='replace')
                stderr = stderr.decode('cp866', errors='replace')
            except UnicodeDecodeError:
                stdout = stdout.decode('utf-8', errors='replace')
                stderr = stderr.decode('utf-8', errors='replace')

            if process.returncode != 0:
                logger.error(f"Process failed: {stderr}")
                raise RuntimeError(f"Process failed: {stderr}")

            logger.info(f"Process stdout length: {len(stdout)}")
            logger.debug(f"Process stdout: {stdout}")
            logger.debug(f"Process stderr: {stderr}")

            # Парсинг вывода
            wallet_data = self._parse_output(stdout)
            logger.info("Successfully retrieved wallet data")

            return wallet_data

        except subprocess.TimeoutExpired:
            process.kill()
            logger.error("Process timeout")
            raise RuntimeError("Process timeout - check credentials and 2FA secrets")

        except Exception as e:
            logger.error(f"Error: {e}")
            raise

        finally:
            # Удаление временного конфига
            if temp_config and os.path.exists(temp_config):
                try:
                    os.remove(temp_config)
                    logger.debug(f"Removed temp config: {temp_config}")
                except:
                    pass
            # Remove copied config
            if exe_config_path.exists() and exe_config_path != Path(config_path):
                try:
                    exe_config_path.unlink()
                except:
                    pass

    def _parse_output(self, output: str) -> Dict[str, Any]:
        """Парсинг вывода программы"""
        lines = output.split('\n')
        json_lines = []
        in_json = False

        for line in lines:
            stripped = line.strip()

            # Look for JSON between markers
            if '=== WALLET_JSON_START ===' in stripped:
                in_json = True
                continue
            elif '=== WALLET_JSON_END ===' in stripped:
                break

            if in_json:
                json_lines.append(line)

        if json_lines:
            try:
                json_str = '\n'.join(json_lines)
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error: {e}")
                logger.error(f"JSON string: {json_str}")
                raise RuntimeError(f"Failed to parse JSON: {e}")

        logger.error(f"No JSON markers found in output. Full output: {output}")
        raise RuntimeError("No JSON data in output")

    def check_multiple_accounts(
        self,
        configs: list[str],
        parallel: bool = False
    ) -> Dict[str, Any]:
        """
        Проверить несколько аккаунтов

        Args:
            configs: Список путей к конфигам
            parallel: Запускать параллельно (не рекомендуется для Steam)

        Returns:
            dict: Результаты по каждому аккаунту
        """
        results = {}

        if parallel:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                futures = {
                    executor.submit(self.check_wallet, cfg): cfg
                    for cfg in configs
                }

                for future in concurrent.futures.as_completed(futures):
                    config = futures[future]
                    try:
                        results[config] = {
                            'success': True,
                            'data': future.result()
                        }
                    except Exception as e:
                        results[config] = {
                            'success': False,
                            'error': str(e)
                        }
        else:
            # Последовательная проверка (рекомендуется)
            for config in configs:
                try:
                    logger.info(f"Checking: {config}")
                    data = self.check_wallet(config)
                    results[config] = {
                        'success': True,
                        'data': data
                    }
                except Exception as e:
                    logger.error(f"Failed {config}: {e}")
                    results[config] = {
                        'success': False,
                        'error': str(e)
                    }

                # Пауза между проверками
                import time
                time.sleep(3)

        return results

    @staticmethod
    def extract_balance(wallet_data: Dict[str, Any]) -> float:
        """Извлечь баланс в USD"""
        try:
            return wallet_data['Body']['balance_in_usd'] / 100.0
        except (KeyError, TypeError):
            return 0.0

    @staticmethod
    def format_summary(wallet_data: Dict[str, Any], username: str = "") -> str:
        """Форматированный вывод информации"""
        try:
            body = wallet_data.get('Body', {})
            lines = [
                f"\n=== {username or 'Account'} ===",
                f"Balance: {body.get('formatted_balance', 'N/A')}",
                f"Country: {body.get('wallet_country_code', 'N/A')}",
                f"USD Balance: ${body.get('balance_in_usd', 0) / 100:.2f}",
                "=" * 40
            ]
            return "\n".join(lines)
        except Exception:
            return "Error formatting data"


# Утилиты для получения shared_secret
class SteamGuardHelper:
    """
    Вспомогательный класс для работы с Steam Guard
    """

    @staticmethod
    def extract_from_mafile(mafile_path: str) -> Dict[str, str]:
        """
        Извлечь секреты из maFile (Steam Desktop Authenticator)

        Args:
            mafile_path: Путь к .maFile

        Returns:
            dict: {'shared_secret': '...', 'identity_secret': '...', 'steamid': '...'}
        """
        try:
            with open(mafile_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            return {
                'shared_secret': data.get('shared_secret', ''),
                'identity_secret': data.get('identity_secret', ''),
                'steamid': data.get('Session', {}).get('SteamID', ''),
                'username': data.get('account_name', '')
            }
        except Exception as e:
            raise RuntimeError(f"Failed to parse maFile: {e}")

    @staticmethod
    def create_config_from_mafile(
        mafile_path: str,
        password: str,
        output_path: str = "steam_account.json"
    ) -> str:
        """
        Создать конфиг из maFile

        Args:
            mafile_path: Путь к .maFile
            password: Пароль Steam
            output_path: Путь для сохранения конфига

        Returns:
            str: Путь к созданному конфигу
        """
        secrets = SteamGuardHelper.extract_from_mafile(mafile_path)

        config = {
            'username': secrets['username'],
            'password': password,
            'shared_secret': secrets['shared_secret'],
            'identity_secret': secrets['identity_secret'],
            'steamid': secrets['steamid'],
            'auto_login': True
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)

        logger.info(f"Config created from maFile: {output_path}")
        return output_path


# Примеры использования
if __name__ == "__main__":
    print("=== Steam Auto-Auth Checker ===\n")

    # Способ 1: Использование готового конфига
    try:
        checker = SteamAutoAuthChecker()

        config_file = "steam_account.json"
        if os.path.exists(config_file):
            print(f"Using config: {config_file}\n")
            wallet = checker.check_wallet(config_path=config_file)
            print(SteamAutoAuthChecker.format_summary(wallet))

        else:
            print(f"Config not found: {config_file}")
            print("\nCreate config manually or use:")
            print("  python python_auto_auth.py --create\n")

    except FileNotFoundError as e:
        print(f"Error: {e}\n")
        print("Build the project first:")
        print("  dotnet publish -c Release")

    except Exception as e:
        print(f"Error: {e}")
        logger.exception("Fatal error")

    # Способ 2: Создание конфига программно (закомментировано)
    """
    checker = SteamAutoAuthChecker()

    # Создание конфига
    config_path = checker.create_config(
        username="your_username",
        password="your_password",
        shared_secret="your_shared_secret_base64",
        identity_secret="your_identity_secret_base64"
    )

    # Проверка баланса
    wallet = checker.check_wallet(config_path=config_path)
    balance = checker.extract_balance(wallet)
    print(f"Balance: ${balance:.2f}")
    """

    # Способ 3: Из maFile (закомментировано)
    """
    # Если у вас есть .maFile от Steam Desktop Authenticator
    config_path = SteamGuardHelper.create_config_from_mafile(
        mafile_path="path/to/steamid.maFile",
        password="your_password"
    )

    wallet = checker.check_wallet(config_path=config_path)
    print(checker.format_summary(wallet))
    """

    # Способ 4: Пакетная проверка (закомментировано)
    """
    configs = [
        "account1.json",
        "account2.json",
        "account3.json"
    ]

    results = checker.check_multiple_accounts(configs)

    total = 0.0
    for config, result in results.items():
        if result['success']:
            balance = checker.extract_balance(result['data'])
            total += balance
            print(f"{config}: ${balance:.2f}")
        else:
            print(f"{config}: ERROR - {result['error']}")

    print(f"\nTotal balance: ${total:.2f}")
    """
