"""
Менеджер прокси с ротацией и отслеживанием блокировок.

Функции:
- Ротация прокси при блокировке
- Отслеживание статистики использования
- Blacklist для нерабочих прокси
"""

import time
from typing import List, Optional, Dict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ProxyStats:
    """Статистика использования прокси."""
    proxy_url: str
    requests_count: int = 0
    success_count: int = 0
    error_count: int = 0
    rate_limit_count: int = 0
    last_used: Optional[datetime] = None
    last_error: Optional[datetime] = None
    is_blacklisted: bool = False
    blacklist_until: Optional[datetime] = None

    @property
    def success_rate(self) -> float:
        """Процент успешных запросов."""
        if self.requests_count == 0:
            return 0.0
        return (self.success_count / self.requests_count) * 100

    @property
    def is_healthy(self) -> bool:
        """Проверка здоровья прокси."""
        # Если в blacklist - нездоров
        if self.is_blacklisted and self.blacklist_until:
            if datetime.now() < self.blacklist_until:
                return False
            else:
                # Время blacklist истекло - снимаем блокировку
                self.is_blacklisted = False
                self.blacklist_until = None

        # Если было меньше 5 запросов - считаем здоровым
        if self.requests_count < 5:
            return True

        # Если success rate < 50% - нездоров
        return self.success_rate >= 50.0


class ProxyManager:
    """Менеджер прокси с ротацией."""

    def __init__(
        self,
        proxies: List[str],
        max_requests_per_proxy: int = 15,
        blacklist_duration_minutes: int = 30,
        cooldown_seconds: int = 60
    ):
        """
        Initialize proxy manager.

        Args:
            proxies: Список прокси URL
            max_requests_per_proxy: Макс. запросов на 1 прокси до ротации
            blacklist_duration_minutes: Время блокировки плохого прокси (минуты)
            cooldown_seconds: Cooldown после использования прокси (секунды)
        """
        self.proxies = proxies
        self.max_requests_per_proxy = max_requests_per_proxy
        self.blacklist_duration_minutes = blacklist_duration_minutes
        self.cooldown_seconds = cooldown_seconds

        # Статистика по каждому прокси
        self.stats: Dict[str, ProxyStats] = {
            proxy: ProxyStats(proxy_url=proxy)
            for proxy in proxies
        }

        self.current_proxy_index = 0
        self.current_proxy_requests = 0

        logger.info(f"Proxy manager initialized with {len(proxies)} proxies")

    def get_next_proxy(self, skip_unhealthy: bool = True) -> Optional[str]:
        """
        Получить следующий прокси для использования.

        Args:
            skip_unhealthy: Пропускать нездоровые прокси

        Returns:
            Proxy URL или None если нет доступных
        """
        # Проверяем, нужно ли ротировать
        if self.current_proxy_requests >= self.max_requests_per_proxy:
            self._rotate_proxy()

        # Ищем здоровый прокси
        attempts = 0
        max_attempts = len(self.proxies)

        while attempts < max_attempts:
            proxy = self.proxies[self.current_proxy_index]
            stats = self.stats[proxy]

            # Проверяем cooldown
            if stats.last_used:
                time_since_used = (datetime.now() - stats.last_used).total_seconds()
                if time_since_used < self.cooldown_seconds:
                    logger.debug(f"Proxy {self._mask_proxy(proxy)} in cooldown, rotating...")
                    self._rotate_proxy()
                    attempts += 1
                    continue

            # Проверяем здоровье
            if skip_unhealthy and not stats.is_healthy:
                logger.debug(f"Proxy {self._mask_proxy(proxy)} unhealthy, rotating...")
                self._rotate_proxy()
                attempts += 1
                continue

            # Нашли подходящий прокси
            self.current_proxy_requests += 1
            stats.last_used = datetime.now()
            return proxy

        # Не нашли ни одного подходящего прокси
        logger.warning("No healthy proxies available!")
        return None

    def _rotate_proxy(self):
        """Переключиться на следующий прокси."""
        old_index = self.current_proxy_index
        self.current_proxy_index = (self.current_proxy_index + 1) % len(self.proxies)
        self.current_proxy_requests = 0

        logger.debug(f"Rotated proxy: {old_index} → {self.current_proxy_index}")

    def report_success(self, proxy: str):
        """
        Сообщить об успешном запросе.

        Args:
            proxy: Proxy URL
        """
        if proxy in self.stats:
            stats = self.stats[proxy]
            stats.requests_count += 1
            stats.success_count += 1

    def report_error(self, proxy: str, is_rate_limit: bool = False):
        """
        Сообщить об ошибке.

        Args:
            proxy: Proxy URL
            is_rate_limit: Это rate limit ошибка
        """
        if proxy in self.stats:
            stats = self.stats[proxy]
            stats.requests_count += 1
            stats.error_count += 1
            stats.last_error = datetime.now()

            if is_rate_limit:
                stats.rate_limit_count += 1

                # Если слишком много rate limit - блокируем прокси
                if stats.rate_limit_count >= 3:
                    self.blacklist_proxy(proxy)

    def blacklist_proxy(self, proxy: str, duration_minutes: Optional[int] = None):
        """
        Добавить прокси в blacklist.

        Args:
            proxy: Proxy URL
            duration_minutes: Длительность блокировки (по умолчанию из конфига)
        """
        if proxy in self.stats:
            stats = self.stats[proxy]
            stats.is_blacklisted = True

            duration = duration_minutes or self.blacklist_duration_minutes
            stats.blacklist_until = datetime.now() + timedelta(minutes=duration)

            logger.warning(f"Proxy {self._mask_proxy(proxy)} blacklisted for {duration} minutes")

            # Если это текущий прокси - ротируем
            if self.proxies[self.current_proxy_index] == proxy:
                self._rotate_proxy()

    # Aliases for compatibility with SteamClient
    def record_success(self, proxy: str):
        """Alias for report_success."""
        self.report_success(proxy)

    def record_error(self, proxy: str):
        """Alias for report_error."""
        self.report_error(proxy, is_rate_limit=False)

    def record_rate_limit(self, proxy: str):
        """Alias for report_error with rate limit flag."""
        self.report_error(proxy, is_rate_limit=True)

    def get_stats_summary(self) -> dict:
        """
        Получить сводную статистику.

        Returns:
            Dict со статистикой
        """
        total_requests = sum(s.requests_count for s in self.stats.values())
        total_success = sum(s.success_count for s in self.stats.values())
        total_errors = sum(s.error_count for s in self.stats.values())
        healthy_count = sum(1 for s in self.stats.values() if s.is_healthy)
        blacklisted_count = sum(1 for s in self.stats.values() if s.is_blacklisted)

        return {
            'total_proxies': len(self.proxies),
            'healthy_proxies': healthy_count,
            'blacklisted_proxies': blacklisted_count,
            'total_requests': total_requests,
            'total_success': total_success,
            'total_errors': total_errors,
            'overall_success_rate': (total_success / total_requests * 100) if total_requests > 0 else 0,
            'current_proxy_index': self.current_proxy_index,
            'current_proxy_requests': self.current_proxy_requests
        }

    def get_detailed_stats(self) -> List[dict]:
        """
        Получить детальную статистику по каждому прокси.

        Returns:
            List of dicts с информацией о прокси
        """
        result = []

        for proxy, stats in self.stats.items():
            result.append({
                'proxy': self._mask_proxy(proxy),
                'requests': stats.requests_count,
                'success': stats.success_count,
                'errors': stats.error_count,
                'rate_limits': stats.rate_limit_count,
                'success_rate': f"{stats.success_rate:.1f}%",
                'is_healthy': stats.is_healthy,
                'is_blacklisted': stats.is_blacklisted,
                'last_used': stats.last_used.strftime('%H:%M:%S') if stats.last_used else 'Never',
                'last_error': stats.last_error.strftime('%H:%M:%S') if stats.last_error else 'Never'
            })

        return result

    def _mask_proxy(self, proxy: str) -> str:
        """Скрыть учётные данные в прокси URL."""
        if '@' in proxy:
            parts = proxy.split('@')
            return f"***@{parts[-1]}"
        return proxy

    def reset_stats(self):
        """Сбросить всю статистику."""
        for stats in self.stats.values():
            stats.requests_count = 0
            stats.success_count = 0
            stats.error_count = 0
            stats.rate_limit_count = 0
            stats.is_blacklisted = False
            stats.blacklist_until = None

        self.current_proxy_requests = 0
        logger.info("Proxy stats reset")

    def get_best_proxy(self) -> Optional[str]:
        """
        Получить лучший прокси по статистике.

        Returns:
            Proxy URL с лучшим success rate
        """
        healthy_proxies = [
            (proxy, stats)
            for proxy, stats in self.stats.items()
            if stats.is_healthy and stats.requests_count >= 5
        ]

        if not healthy_proxies:
            return None

        # Сортируем по success rate
        best_proxy, best_stats = max(healthy_proxies, key=lambda x: x[1].success_rate)

        return best_proxy
