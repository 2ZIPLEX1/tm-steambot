"""
GUI для Steam Trading Bot на CustomTkinter.

Функции:
- Dashboard с статистикой и графиками
- Управление аккаунтами
- Просмотр активных ордеров
- Логи в реальном времени
- Start/Stop бота
"""

import customtkinter as ctk
import threading
import time
import subprocess
import os
from pathlib import Path
from datetime import datetime
from typing import Optional
import json
import sys
from urllib.parse import quote

from src.logger import get_logger
from src.account_manager import AccountManager
from src.trading_bot import TradingBot
from src.database import trades_db
from src.currency_converter import currency_converter
from src.auto_scanner import AutoScanner
from src.proxy_manager import ProxyManager
from src.statistics import TradingStatistics

logger = get_logger(__name__)

# Настройки темы
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class TradingBotGUI(ctk.CTk):
    """Главное окно GUI приложения."""

    def __init__(self):
        super().__init__()

        # Настройки окна
        self.title("Steam Trading Bot")
        self.geometry("1200x800")
        self.minsize(1000, 600)

        # Состояние бота
        self.bot_running = False
        self.bot_thread: Optional[threading.Thread] = None
        self.account_manager: Optional[AccountManager] = None

        # Scanner process
        self.scanner_process: Optional[subprocess.Popen] = None
        self.scanner_running = False
        self.scanner_monitor_thread: Optional[threading.Thread] = None

        # Словарь для хранения ссылок на balance labels аккаунтов
        self.account_balance_labels = {}

        # Состояние сортировки для TM Parser
        self.sort_by_profit = False  # False = по умолчанию (по дате), True = по профиту

        # Новые модули
        self.auto_scanner: Optional[AutoScanner] = None
        self.proxy_manager: Optional[ProxyManager] = None
        self.statistics: Optional[TradingStatistics] = None

        # Настройки бота (загружаются из bot_config.json)
        self.bot_config = {
            'min_profit_pct': 5.0,
            'cycle_interval_minutes': 5,
            'bottm_db_path': 'data/main.db',
            'auto_refresh': True,
            'debug_mode': False,
            # Trading настройки (выставление ордеров)
            'trade_min_price': 100,
            'trade_max_price': 5000,
            # TM Parser настройки
            'scanner_min_price': 1000,
            'scanner_max_price': 10000,
            'scanner_min_profit': -5.0,
            'csgo_commission': 7.0,
            'min_sales_7d': 50,
            'proxy_file': 'proxies.txt',
            'requests_per_proxy': 50,  # Increased to avoid rotation during scan
            'scanner_max_items': 10,  # Reduced to avoid rate limits
            'scanner_delay': 7.0,  # Increased delay to avoid 429 errors
            'scanner_workers': 1,  # Reduced workers to avoid overwhelming proxies
            # Auto Scanner настройки
            'auto_scan_enabled': False,
            'auto_scan_interval': 30,  # минуты
            # Proxy Manager настройки
            'proxy_max_requests': 15,
            'proxy_cooldown': 60,  # секунды
            'proxy_blacklist_duration': 30,  # минуты
        }

        # Создаем интерфейс
        self._create_ui()

        # Загружаем аккаунты и настройки
        self._load_accounts()
        self._load_bot_config()

        # Загружаем предметы из БД сразу при запуске
        self._refresh_profitable_items()

        # Обработка закрытия окна
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _create_ui(self):
        """Создать интерфейс."""
        # Верхняя панель с кнопками управления
        self._create_header()

        # Табы
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Создаем вкладки
        self.tab_dashboard = self.tabview.add("📊 Dashboard")
        self.tab_accounts = self.tabview.add("👤 Аккаунты")
        self.tab_orders = self.tabview.add("📋 Ордера")
        self.tab_inventory = self.tabview.add("📦 Инвентарь")
        self.tab_tm_parser = self.tabview.add("🔍 TM Parser")
        self.tab_history = self.tabview.add("📜 История")
        self.tab_settings = self.tabview.add("⚙️ Настройки")
        self.tab_logs = self.tabview.add("📝 Логи")

        # Наполняем вкладки
        self._create_dashboard_tab()
        self._create_accounts_tab()
        self._create_orders_tab()
        self._create_inventory_tab()
        self._create_tm_parser_tab()
        self._create_history_tab()
        self._create_settings_tab()
        self._create_logs_tab()

        # Статус бар внизу
        self._create_statusbar()

    def _create_header(self):
        """Создать верхнюю панель."""
        header_frame = ctk.CTkFrame(self, height=60)
        header_frame.pack(fill="x", padx=10, pady=10)

        # Заголовок
        title_label = ctk.CTkLabel(
            header_frame,
            text="🤖 Steam Trading Bot",
            font=ctk.CTkFont(size=24, weight="bold")
        )
        title_label.pack(side="left", padx=20)

        # Кнопки управления
        btn_frame = ctk.CTkFrame(header_frame, fg_color="transparent")
        btn_frame.pack(side="right", padx=20)

        self.btn_start = ctk.CTkButton(
            btn_frame,
            text="▶️ Старт",
            command=self._start_bot,
            fg_color="green",
            hover_color="darkgreen",
            width=130,
            height=45,
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self.btn_start.pack(side="left", padx=5)

        self.btn_stop = ctk.CTkButton(
            btn_frame,
            text="⏸️ Стоп",
            command=self._stop_bot,
            fg_color="red",
            hover_color="darkred",
            width=130,
            height=45,
            font=ctk.CTkFont(size=14, weight="bold"),
            state="disabled"
        )
        self.btn_stop.pack(side="left", padx=5)

        self.btn_refresh = ctk.CTkButton(
            btn_frame,
            text="🔄 Обновить",
            command=self._refresh_data,
            width=130,
            height=45,
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self.btn_refresh.pack(side="left", padx=5)

    def _create_dashboard_tab(self):
        """Создать вкладку Dashboard."""
        # Верхняя панель со статистикой
        stats_frame = ctk.CTkFrame(self.tab_dashboard)
        stats_frame.pack(fill="x", padx=10, pady=10)

        # Карточки статистики
        self.stats_cards = {}

        stats_data = [
            ("💰 Баланс Steam", "0.00 RUB", "balance_steam"),
            ("💎 Баланс CSGO.TM", "0.00 RUB", "balance_csgotm"),
            ("📋 Активных ордеров", "0", "active_orders"),
            ("📦 На холде", "0", "on_hold"),
            ("💵 Прибыль", "$0.00", "profit"),
            ("✅ Продано", "0", "sold_items"),
        ]

        for i, (title, value, key) in enumerate(stats_data):
            card = self._create_stat_card(stats_frame, title, value)
            card.grid(row=i//3, column=i%3, padx=10, pady=10, sticky="ew")
            self.stats_cards[key] = card

        stats_frame.grid_columnconfigure(0, weight=1)
        stats_frame.grid_columnconfigure(1, weight=1)
        stats_frame.grid_columnconfigure(2, weight=1)

        # Таблица последних сделок
        trades_frame = ctk.CTkFrame(self.tab_dashboard)
        trades_frame.pack(fill="both", expand=True, padx=10, pady=10)

        trades_label = ctk.CTkLabel(
            trades_frame,
            text="📈 Последние сделки",
            font=ctk.CTkFont(size=16, weight="bold")
        )
        trades_label.pack(padx=10, pady=10, anchor="w")

        # Скроллируемый фрейм для таблицы
        self.trades_scrollable = ctk.CTkScrollableFrame(trades_frame, height=200)
        self.trades_scrollable.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _create_stat_card(self, parent, title: str, value: str):
        """Создать карточку статистики."""
        card_frame = ctk.CTkFrame(parent)

        title_label = ctk.CTkLabel(
            card_frame,
            text=title,
            font=ctk.CTkFont(size=14)
        )
        title_label.pack(padx=15, pady=(15, 5))

        value_label = ctk.CTkLabel(
            card_frame,
            text=value,
            font=ctk.CTkFont(size=24, weight="bold")
        )
        value_label.pack(padx=15, pady=(5, 15))

        # Сохраняем ссылку на label для обновления
        card_frame.value_label = value_label

        return card_frame

    def _create_accounts_tab(self):
        """Создать вкладку с аккаунтами."""
        # Верхняя панель
        header = ctk.CTkFrame(self.tab_accounts)
        header.pack(fill="x", padx=10, pady=10)

        label = ctk.CTkLabel(
            header,
            text="👤 Управление аккаунтами",
            font=ctk.CTkFont(size=16, weight="bold")
        )
        label.pack(side="left", padx=10)

        btn_add = ctk.CTkButton(
            header,
            text="➕ Добавить",
            command=self._add_account,
            width=100
        )
        btn_add.pack(side="right", padx=10)

        # Список аккаунтов
        self.accounts_scrollable = ctk.CTkScrollableFrame(self.tab_accounts)
        self.accounts_scrollable.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _create_orders_tab(self):
        """Создать вкладку с ордерами."""
        label = ctk.CTkLabel(
            self.tab_orders,
            text="📋 Активные ордера",
            font=ctk.CTkFont(size=16, weight="bold")
        )
        label.pack(padx=10, pady=10, anchor="w")

        # Таблица ордеров
        self.orders_scrollable = ctk.CTkScrollableFrame(self.tab_orders)
        self.orders_scrollable.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _create_inventory_tab(self):
        """Создать вкладку с инвентарем."""
        label = ctk.CTkLabel(
            self.tab_inventory,
            text="📦 Предметы на холде",
            font=ctk.CTkFont(size=16, weight="bold")
        )
        label.pack(padx=10, pady=10, anchor="w")

        # Таблица предметов
        self.inventory_scrollable = ctk.CTkScrollableFrame(self.tab_inventory)
        self.inventory_scrollable.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _create_tm_parser_tab(self):
        """Создать вкладку TM Parser с выгодными предметами."""
        # Заголовок с информацией
        header = ctk.CTkFrame(self.tab_tm_parser)
        header.pack(fill="x", padx=10, pady=10)

        label = ctk.CTkLabel(
            header,
            text="🔍 Выгодные предметы (TM Parser)",
            font=ctk.CTkFont(size=16, weight="bold")
        )
        label.pack(side="left", padx=10)

        # Кнопка запуска сканирования
        self.scan_btn = ctk.CTkButton(
            header,
            text="🔄 Запустить сканирование",
            command=self._start_scanner,
            width=200,
            height=38,
            font=ctk.CTkFont(size=13)
        )
        self.scan_btn.pack(side="right", padx=5)

        # Кнопка пересканирования всех
        self.rescan_all_btn = ctk.CTkButton(
            header,
            text="🔁 Пересканировать все",
            command=self._rescan_all_items,
            width=180,
            height=38,
            fg_color="#E67E22",
            font=ctk.CTkFont(size=13)
        )
        self.rescan_all_btn.pack(side="right", padx=5)

        # Кнопка поиска новых
        self.scan_new_btn = ctk.CTkButton(
            header,
            text="⭐ Искать новые",
            command=self._scan_new_items,
            width=150,
            height=38,
            fg_color="#27AE60",
            font=ctk.CTkFont(size=13)
        )
        self.scan_new_btn.pack(side="right", padx=5)

        # Статус сканирования
        self.scanner_status_label = ctk.CTkLabel(
            header,
            text="Статус: не запущено",
            text_color="gray",
            font=ctk.CTkFont(size=13)
        )
        self.scanner_status_label.pack(side="right", padx=10)

        # Вторая строка с фильтром
        filter_frame = ctk.CTkFrame(self.tab_tm_parser)
        filter_frame.pack(fill="x", padx=10, pady=(0, 10))

        # Чекбокс для включения/выключения фильтра
        self.profit_filter_enabled_var = ctk.BooleanVar(value=False)
        profit_filter_checkbox = ctk.CTkCheckBox(
            filter_frame,
            text="Фильтр по профиту:",
            variable=self.profit_filter_enabled_var,
            command=self._on_profit_filter_toggle,
            font=ctk.CTkFont(size=13)
        )
        profit_filter_checkbox.pack(side="left", padx=(10, 5))

        self.parser_min_profit_entry = ctk.CTkEntry(
            filter_frame,
            width=100,
            height=35,
            placeholder_text="-5.0",
            font=ctk.CTkFont(size=13),
            state="disabled"  # По умолчанию выключен
        )
        self.parser_min_profit_entry.pack(side="left", padx=5)
        self.parser_min_profit_entry.insert(0, str(self.bot_config.get('scanner_min_profit', -5.0)))

        apply_filter_btn = ctk.CTkButton(
            filter_frame,
            text="Применить фильтр",
            command=self._apply_profit_filter,
            width=150,
            height=35,
            fg_color="#3498DB",
            font=ctk.CTkFont(size=13)
        )
        apply_filter_btn.pack(side="left", padx=5)

        items_count_label = ctk.CTkLabel(
            filter_frame,
            text="",
            font=ctk.CTkFont(size=13),
            text_color="gray"
        )
        items_count_label.pack(side="left", padx=10)
        self.parser_items_count_label = items_count_label

        # Кнопка Auto Scanner
        self.auto_scan_toggle_btn = ctk.CTkButton(
            filter_frame,
            text="⏰ Auto Scan: OFF",
            command=self._toggle_auto_scan,
            width=160,
            height=35,
            fg_color="#95A5A6",
            font=ctk.CTkFont(size=13)
        )
        self.auto_scan_toggle_btn.pack(side="right", padx=5)

        # Кнопка Statistics
        stats_btn = ctk.CTkButton(
            filter_frame,
            text="📊 Статистика",
            command=self._show_statistics_window,
            width=130,
            height=35,
            fg_color="#16A085",
            font=ctk.CTkFont(size=13)
        )
        stats_btn.pack(side="right", padx=5)

        # Кнопка Export
        export_btn = ctk.CTkButton(
            filter_frame,
            text="📤 Экспорт",
            command=self._export_data,
            width=110,
            height=35,
            fg_color="#D35400",
            font=ctk.CTkFont(size=13)
        )
        export_btn.pack(side="right", padx=5)

        # Таблица выгодных предметов
        self.parser_scrollable = ctk.CTkScrollableFrame(self.tab_tm_parser)
        self.parser_scrollable.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _create_history_tab(self):
        """Создать вкладку истории операций."""
        # Заголовок
        header_frame = ctk.CTkFrame(self.tab_history)
        header_frame.pack(fill="x", padx=10, pady=10)

        header = ctk.CTkLabel(
            header_frame,
            text="История операций",
            font=ctk.CTkFont(size=16, weight="bold")
        )
        header.pack(side="left", padx=10)

        # Кнопка обновления
        refresh_btn = ctk.CTkButton(
            header_frame,
            text="Обновить",
            command=self._refresh_history,
            width=120,
            height=32
        )
        refresh_btn.pack(side="right", padx=5)

        # Фильтры
        filter_frame = ctk.CTkFrame(self.tab_history)
        filter_frame.pack(fill="x", padx=10, pady=(0, 10))

        ctk.CTkLabel(filter_frame, text="Тип:", font=ctk.CTkFont(size=13)).pack(side="left", padx=10)

        self.history_type_var = ctk.StringVar(value="all")
        type_options = ctk.CTkOptionMenu(
            filter_frame,
            values=["all", "purchased", "sold", "orders"],
            variable=self.history_type_var,
            width=120,
            command=lambda _: self._refresh_history()
        )
        type_options.pack(side="left", padx=5)

        ctk.CTkLabel(filter_frame, text="Период:", font=ctk.CTkFont(size=13)).pack(side="left", padx=10)

        self.history_days_var = ctk.StringVar(value="7")
        days_options = ctk.CTkOptionMenu(
            filter_frame,
            values=["7", "14", "30", "60", "90", "365"],
            variable=self.history_days_var,
            width=100,
            command=lambda _: self._refresh_history()
        )
        days_options.pack(side="left", padx=5)

        ctk.CTkLabel(filter_frame, text="дней", font=ctk.CTkFont(size=13)).pack(side="left")

        # Таблица истории
        self.history_scrollable = ctk.CTkScrollableFrame(self.tab_history)
        self.history_scrollable.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Загружаем историю
        self._refresh_history()

    def _refresh_history(self):
        """Обновить историю операций."""
        # Очищаем
        for widget in self.history_scrollable.winfo_children():
            widget.destroy()

        history_type = self.history_type_var.get()
        days = int(self.history_days_var.get())

        try:
            from datetime import datetime, timedelta
            cutoff_date = datetime.now() - timedelta(days=days)

            items = []

            if history_type in ["all", "purchased"]:
                # Купленные предметы
                ready = trades_db.get_items_ready_to_sell()
                on_hold = trades_db.get_items_on_hold()
                for item in ready + on_hold:
                    items.append({
                        'type': 'purchased',
                        'date': item.get('purchased_at', ''),
                        'item_name': item.get('item_name', 'N/A'),
                        'price': item.get('price', 0),
                        'status': item.get('status', 'N/A')
                    })

            if history_type in ["all", "sold"]:
                # Проданные предметы
                sold = trades_db.get_recent_sales(limit=1000)
                for item in sold:
                    items.append({
                        'type': 'sold',
                        'date': item.get('sold_at', ''),
                        'item_name': item.get('item_name', 'N/A'),
                        'price': item.get('sale_price', 0),
                        'profit': item.get('profit', 0),
                        'status': 'sold'
                    })

            if history_type in ["all", "orders"]:
                # Активные ордера
                orders = trades_db.get_active_orders()
                for order in orders:
                    items.append({
                        'type': 'order',
                        'date': order.get('created_at', ''),
                        'item_name': order.get('item_name', 'N/A'),
                        'price': order.get('order_price', 0),  # Исправлено: order_price вместо price
                        'status': order.get('status', 'N/A')
                    })

            # Сортируем по дате
            items.sort(key=lambda x: x['date'], reverse=True)

            # Отображаем
            if not items:
                no_data_label = ctk.CTkLabel(
                    self.history_scrollable,
                    text="Нет данных для отображения",
                    font=ctk.CTkFont(size=14),
                    text_color="gray"
                )
                no_data_label.pack(pady=50)
            else:
                # Заголовок таблицы
                header_frame = ctk.CTkFrame(self.history_scrollable)
                header_frame.pack(fill="x", padx=5, pady=5)

                ctk.CTkLabel(header_frame, text="Дата", width=150, font=ctk.CTkFont(weight="bold")).pack(side="left", padx=5)
                ctk.CTkLabel(header_frame, text="Тип", width=100, font=ctk.CTkFont(weight="bold")).pack(side="left", padx=5)
                ctk.CTkLabel(header_frame, text="Предмет", width=300, font=ctk.CTkFont(weight="bold")).pack(side="left", padx=5)
                ctk.CTkLabel(header_frame, text="Цена", width=100, font=ctk.CTkFont(weight="bold")).pack(side="left", padx=5)
                ctk.CTkLabel(header_frame, text="Статус/Профит", width=150, font=ctk.CTkFont(weight="bold")).pack(side="left", padx=5)

                # Строки данных
                for item in items[:100]:  # Первые 100
                    row_frame = ctk.CTkFrame(self.history_scrollable)
                    row_frame.pack(fill="x", padx=5, pady=2)

                    # Цвет и текст по типу
                    if item['type'] == 'purchased':
                        fg_color = "#3498DB"
                        type_text = "Куплено"
                    elif item['type'] == 'sold':
                        fg_color = "#27AE60"
                        type_text = "Продано"
                    else:
                        fg_color = "#95A5A6"
                        type_text = "Ордер"

                    date_str = item['date'][:16] if item['date'] else 'N/A'
                    ctk.CTkLabel(row_frame, text=date_str, width=150).pack(side="left", padx=5)
                    ctk.CTkLabel(row_frame, text=type_text, width=100, fg_color=fg_color).pack(side="left", padx=5)
                    ctk.CTkLabel(row_frame, text=item['item_name'][:40], width=300).pack(side="left", padx=5)
                    ctk.CTkLabel(row_frame, text=f"{item['price']:.2f} ₽", width=100).pack(side="left", padx=5)

                    status_text = item['status']
                    if item['type'] == 'sold' and 'profit' in item:
                        status_text = f"+{item['profit']:.2f} ₽"

                    ctk.CTkLabel(row_frame, text=status_text, width=150).pack(side="left", padx=5)

                total_label = ctk.CTkLabel(
                    self.history_scrollable,
                    text=f"Показано: {min(len(items), 100)}/{len(items)}",
                    font=ctk.CTkFont(size=12),
                    text_color="gray"
                )
                total_label.pack(pady=10)

        except Exception as e:
            logger.error(f"Error refreshing history: {e}", exc_info=True)
            error_label = ctk.CTkLabel(
                self.history_scrollable,
                text=f"Ошибка загрузки истории: {e}",
                font=ctk.CTkFont(size=14),
                text_color="red"
            )
            error_label.pack(pady=50)

    def _create_settings_tab(self):
        """Создать вкладку с настройками."""
        # Заголовок
        header = ctk.CTkLabel(
            self.tab_settings,
            text="⚙️ Настройки бота",
            font=ctk.CTkFont(size=18, weight="bold")
        )
        header.pack(padx=20, pady=20, anchor="w")

        # Контейнер для настроек
        settings_frame = ctk.CTkScrollableFrame(self.tab_settings)
        settings_frame.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        # === Настройки торговли ===
        trade_section = ctk.CTkFrame(settings_frame)
        trade_section.pack(fill="x", pady=10)

        trade_label = ctk.CTkLabel(
            trade_section,
            text="💰 Настройки торговли",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        trade_label.pack(padx=15, pady=10, anchor="w")

        # Минимальный процент профита
        profit_frame = ctk.CTkFrame(trade_section, fg_color="transparent")
        profit_frame.pack(fill="x", padx=15, pady=5)

        profit_label = ctk.CTkLabel(
            profit_frame,
            text="Минимальный профит (%):",
            width=200,
            anchor="w"
        )
        profit_label.pack(side="left", padx=5)

        self.profit_entry = ctk.CTkEntry(
            profit_frame,
            width=100,
            placeholder_text="5.0"
        )
        self.profit_entry.pack(side="left", padx=5)
        self.profit_entry.insert(0, str(self.bot_config.get('min_profit_pct', 5.0)))

        profit_info = ctk.CTkLabel(
            profit_frame,
            text="(Покупать только предметы с профитом выше этого значения. Отрицательные значения = покупка в убыток)",
            text_color="gray"
        )
        profit_info.pack(side="left", padx=10)

        # Интервал между циклами
        interval_frame = ctk.CTkFrame(trade_section, fg_color="transparent")
        interval_frame.pack(fill="x", padx=15, pady=5)

        interval_label = ctk.CTkLabel(
            interval_frame,
            text="Интервал между циклами (мин):",
            width=200,
            anchor="w"
        )
        interval_label.pack(side="left", padx=5)

        self.interval_entry = ctk.CTkEntry(
            interval_frame,
            width=100,
            placeholder_text="5"
        )
        self.interval_entry.pack(side="left", padx=5)
        self.interval_entry.insert(0, str(self.bot_config.get('cycle_interval_minutes', 5)))

        interval_info = ctk.CTkLabel(
            interval_frame,
            text="(Пауза между торговыми циклами в 24/7 режиме)",
            text_color="gray"
        )
        interval_info.pack(side="left", padx=10)

        # === Настройки базы данных ===
        db_section = ctk.CTkFrame(settings_frame)
        db_section.pack(fill="x", pady=10)

        db_label = ctk.CTkLabel(
            db_section,
            text="💾 База данных",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        db_label.pack(padx=15, pady=10, anchor="w")

        # Путь к bottm.db
        db_frame = ctk.CTkFrame(db_section, fg_color="transparent")
        db_frame.pack(fill="x", padx=15, pady=5)

        db_path_label = ctk.CTkLabel(
            db_frame,
            text="Путь к bottm.db:",
            width=200,
            anchor="w"
        )
        db_path_label.pack(side="left", padx=5)

        self.db_path_entry = ctk.CTkEntry(
            db_frame,
            width=300,
            placeholder_text="data/main.db"
        )
        self.db_path_entry.pack(side="left", padx=5)
        self.db_path_entry.insert(0, self.bot_config.get('bottm_db_path', 'data/main.db'))

        db_info = ctk.CTkLabel(
            db_frame,
            text="(База данных с прибыльными предметами)",
            text_color="gray"
        )
        db_info.pack(side="left", padx=10)

        # === Настройки торговли (выставление ордеров) ===
        trading_section = ctk.CTkFrame(settings_frame)
        trading_section.pack(fill="x", pady=10)

        trading_label = ctk.CTkLabel(
            trading_section,
            text="💰 Настройки торговли (выставление ордеров)",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        trading_label.pack(padx=15, pady=10, anchor="w")

        # Минимальная цена предмета для покупки
        trade_min_price_frame = ctk.CTkFrame(trading_section, fg_color="transparent")
        trade_min_price_frame.pack(fill="x", padx=15, pady=5)

        trade_min_price_label = ctk.CTkLabel(
            trade_min_price_frame,
            text="Мин. цена предмета (RUB):",
            width=200,
            anchor="w"
        )
        trade_min_price_label.pack(side="left", padx=5)

        self.trade_min_price_entry = ctk.CTkEntry(
            trade_min_price_frame,
            width=100,
            placeholder_text="100"
        )
        self.trade_min_price_entry.pack(side="left", padx=5)
        self.trade_min_price_entry.insert(0, str(self.bot_config.get('trade_min_price', 100)))

        trade_min_price_info = ctk.CTkLabel(
            trade_min_price_frame,
            text="(Минимальная цена для выставления ордеров)",
            text_color="gray"
        )
        trade_min_price_info.pack(side="left", padx=10)

        # Максимальная цена предмета для покупки
        trade_max_price_frame = ctk.CTkFrame(trading_section, fg_color="transparent")
        trade_max_price_frame.pack(fill="x", padx=15, pady=5)

        trade_max_price_label = ctk.CTkLabel(
            trade_max_price_frame,
            text="Макс. цена предмета (RUB):",
            width=200,
            anchor="w"
        )
        trade_max_price_label.pack(side="left", padx=5)

        self.trade_max_price_entry = ctk.CTkEntry(
            trade_max_price_frame,
            width=100,
            placeholder_text="5000"
        )
        self.trade_max_price_entry.pack(side="left", padx=5)
        self.trade_max_price_entry.insert(0, str(self.bot_config.get('trade_max_price', 5000)))

        trade_max_price_info = ctk.CTkLabel(
            trade_max_price_frame,
            text="(Максимальная цена для выставления ордеров)",
            text_color="gray"
        )
        trade_max_price_info.pack(side="left", padx=10)

        # === Настройки TM Parser (bottm) ===
        parser_section = ctk.CTkFrame(settings_frame)
        parser_section.pack(fill="x", pady=10)

        parser_label = ctk.CTkLabel(
            parser_section,
            text="🔍 Настройки TM Parser (сканер выгодных предметов)",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        parser_label.pack(padx=15, pady=10, anchor="w")

        # Минимальная цена
        min_price_frame = ctk.CTkFrame(parser_section, fg_color="transparent")
        min_price_frame.pack(fill="x", padx=15, pady=5)

        min_price_label = ctk.CTkLabel(
            min_price_frame,
            text="Минимальная цена (RUB):",
            width=200,
            anchor="w"
        )
        min_price_label.pack(side="left", padx=5)

        self.min_price_entry = ctk.CTkEntry(
            min_price_frame,
            width=100,
            placeholder_text="1000"
        )
        self.min_price_entry.pack(side="left", padx=5)
        self.min_price_entry.insert(0, str(self.bot_config.get('scanner_min_price', 1000)))

        min_price_info = ctk.CTkLabel(
            min_price_frame,
            text="(Фильтр дешевых предметов)",
            text_color="gray"
        )
        min_price_info.pack(side="left", padx=10)

        # Максимальная цена
        max_price_frame = ctk.CTkFrame(parser_section, fg_color="transparent")
        max_price_frame.pack(fill="x", padx=15, pady=5)

        max_price_label = ctk.CTkLabel(
            max_price_frame,
            text="Максимальная цена (RUB):",
            width=200,
            anchor="w"
        )
        max_price_label.pack(side="left", padx=5)

        self.max_price_entry = ctk.CTkEntry(
            max_price_frame,
            width=100,
            placeholder_text="10000"
        )
        self.max_price_entry.pack(side="left", padx=5)
        self.max_price_entry.insert(0, str(self.bot_config.get('scanner_max_price', 10000)))

        max_price_info = ctk.CTkLabel(
            max_price_frame,
            text="(Фильтр дорогих предметов)",
            text_color="gray"
        )
        max_price_info.pack(side="left", padx=10)

        # Минимальный профит для сканера
        scanner_profit_frame = ctk.CTkFrame(parser_section, fg_color="transparent")
        scanner_profit_frame.pack(fill="x", padx=15, pady=5)

        scanner_profit_label = ctk.CTkLabel(
            scanner_profit_frame,
            text="Мин. профит сканера (%):",
            width=200,
            anchor="w"
        )
        scanner_profit_label.pack(side="left", padx=5)

        self.scanner_profit_entry = ctk.CTkEntry(
            scanner_profit_frame,
            width=100,
            placeholder_text="-5.0"
        )
        self.scanner_profit_entry.pack(side="left", padx=5)
        self.scanner_profit_entry.insert(0, str(self.bot_config.get('scanner_min_profit', -5.0)))

        scanner_profit_info = ctk.CTkLabel(
            scanner_profit_frame,
            text="(Порог для сохранения предмета в БД, может быть отрицательным)",
            text_color="gray"
        )
        scanner_profit_info.pack(side="left", padx=10)

        # Комиссия CSGO.TM
        commission_frame = ctk.CTkFrame(parser_section, fg_color="transparent")
        commission_frame.pack(fill="x", padx=15, pady=5)

        commission_label = ctk.CTkLabel(
            commission_frame,
            text="Комиссия CSGO.TM (%):",
            width=200,
            anchor="w"
        )
        commission_label.pack(side="left", padx=5)

        self.commission_entry = ctk.CTkEntry(
            commission_frame,
            width=100,
            placeholder_text="7.0"
        )
        self.commission_entry.pack(side="left", padx=5)
        self.commission_entry.insert(0, str(self.bot_config.get('csgo_commission', 7.0)))

        commission_info = ctk.CTkLabel(
            commission_frame,
            text="(Комиссия площадки при продаже)",
            text_color="gray"
        )
        commission_info.pack(side="left", padx=10)

        # Минимальные продажи за 7 дней
        sales_7d_frame = ctk.CTkFrame(parser_section, fg_color="transparent")
        sales_7d_frame.pack(fill="x", padx=15, pady=5)

        sales_7d_label = ctk.CTkLabel(
            sales_7d_frame,
            text="Мин. продаж за 7 дней:",
            width=200,
            anchor="w"
        )
        sales_7d_label.pack(side="left", padx=5)

        self.sales_7d_entry = ctk.CTkEntry(
            sales_7d_frame,
            width=100,
            placeholder_text="50"
        )
        self.sales_7d_entry.pack(side="left", padx=5)
        self.sales_7d_entry.insert(0, str(self.bot_config.get('min_sales_7d', 50)))

        sales_7d_info = ctk.CTkLabel(
            sales_7d_frame,
            text="(Фильтр ликвидности предметов)",
            text_color="gray"
        )
        sales_7d_info.pack(side="left", padx=10)

        # Путь к файлу прокси
        proxy_file_frame = ctk.CTkFrame(parser_section, fg_color="transparent")
        proxy_file_frame.pack(fill="x", padx=15, pady=5)

        proxy_file_label = ctk.CTkLabel(
            proxy_file_frame,
            text="Файл прокси:",
            width=200,
            anchor="w"
        )
        proxy_file_label.pack(side="left", padx=5)

        self.proxy_file_entry = ctk.CTkEntry(
            proxy_file_frame,
            width=300,
            placeholder_text="proxies.txt"
        )
        self.proxy_file_entry.pack(side="left", padx=5)
        self.proxy_file_entry.insert(0, self.bot_config.get('proxy_file', 'proxies.txt'))

        proxy_file_info = ctk.CTkLabel(
            proxy_file_frame,
            text="(Опционально. Файл со списком прокси, по одному на строку)",
            text_color="gray"
        )
        proxy_file_info.pack(side="left", padx=10)

        # Запросов на прокси
        requests_per_proxy_frame = ctk.CTkFrame(parser_section, fg_color="transparent")
        requests_per_proxy_frame.pack(fill="x", padx=15, pady=5)

        requests_per_proxy_label = ctk.CTkLabel(
            requests_per_proxy_frame,
            text="Запросов на прокси:",
            width=200,
            anchor="w"
        )
        requests_per_proxy_label.pack(side="left", padx=5)

        self.requests_per_proxy_entry = ctk.CTkEntry(
            requests_per_proxy_frame,
            width=100,
            placeholder_text="15"
        )
        self.requests_per_proxy_entry.pack(side="left", padx=5)
        self.requests_per_proxy_entry.insert(0, str(self.bot_config.get('requests_per_proxy', 15)))

        requests_per_proxy_info = ctk.CTkLabel(
            requests_per_proxy_frame,
            text="(Менять прокси после N запросов)",
            text_color="gray"
        )
        requests_per_proxy_info.pack(side="left", padx=10)

        # Максимум предметов для сканирования
        max_items_frame = ctk.CTkFrame(parser_section, fg_color="transparent")
        max_items_frame.pack(fill="x", padx=15, pady=5)

        max_items_label = ctk.CTkLabel(
            max_items_frame,
            text="Макс. предметов для скана:",
            width=200,
            anchor="w"
        )
        max_items_label.pack(side="left", padx=5)

        self.max_items_entry = ctk.CTkEntry(
            max_items_frame,
            width=100,
            placeholder_text="10"
        )
        self.max_items_entry.pack(side="left", padx=5)
        self.max_items_entry.insert(0, str(self.bot_config.get('scanner_max_items', 10)))

        max_items_info = ctk.CTkLabel(
            max_items_frame,
            text="(Количество предметов за один проход)",
            text_color="gray"
        )
        max_items_info.pack(side="left", padx=10)

        # Задержка между запросами
        delay_frame = ctk.CTkFrame(parser_section, fg_color="transparent")
        delay_frame.pack(fill="x", padx=15, pady=5)

        delay_label = ctk.CTkLabel(
            delay_frame,
            text="Задержка между запросами (сек):",
            width=200,
            anchor="w"
        )
        delay_label.pack(side="left", padx=5)

        self.delay_entry = ctk.CTkEntry(
            delay_frame,
            width=100,
            placeholder_text="7.0"
        )
        self.delay_entry.pack(side="left", padx=5)
        self.delay_entry.insert(0, str(self.bot_config.get('scanner_delay', 7.0)))

        delay_info = ctk.CTkLabel(
            delay_frame,
            text="(Задержка для избежания блокировки)",
            text_color="gray"
        )
        delay_info.pack(side="left", padx=10)

        # Параллельные воркеры
        workers_frame = ctk.CTkFrame(parser_section, fg_color="transparent")
        workers_frame.pack(fill="x", padx=15, pady=5)

        workers_label = ctk.CTkLabel(
            workers_frame,
            text="Параллельных воркеров:",
            width=200,
            anchor="w"
        )
        workers_label.pack(side="left", padx=5)

        self.workers_entry = ctk.CTkEntry(
            workers_frame,
            width=100,
            placeholder_text="1"
        )
        self.workers_entry.pack(side="left", padx=5)
        self.workers_entry.insert(0, str(self.bot_config.get('scanner_workers', 1)))

        workers_info = ctk.CTkLabel(
            workers_frame,
            text="(Количество одновременных запросов)",
            text_color="gray"
        )
        workers_info.pack(side="left", padx=10)

        # === Настройки Auto Buyer ===
        # === Настройки Auto Scanner ===
        auto_scanner_section = ctk.CTkFrame(settings_frame)
        auto_scanner_section.pack(fill="x", pady=10)

        auto_scanner_label = ctk.CTkLabel(
            auto_scanner_section,
            text="⏰ Настройки Auto Scanner (автоматическое сканирование)",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        auto_scanner_label.pack(padx=15, pady=10, anchor="w")

        # Интервал сканирования
        as_interval_frame = ctk.CTkFrame(auto_scanner_section, fg_color="transparent")
        as_interval_frame.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(as_interval_frame, text="Интервал (минуты):", width=200, anchor="w").pack(side="left", padx=5)
        self.as_interval_entry = ctk.CTkEntry(as_interval_frame, width=100, placeholder_text="30")
        self.as_interval_entry.insert(0, str(self.bot_config.get('auto_scan_interval', 30)))
        self.as_interval_entry.pack(side="left", padx=5)
        ctk.CTkLabel(as_interval_frame, text="(Запускать сканирование каждые N минут)", text_color="gray").pack(side="left", padx=10)

        # === Настройки Proxy Manager ===
        proxy_manager_section = ctk.CTkFrame(settings_frame)
        proxy_manager_section.pack(fill="x", pady=10)

        proxy_manager_label = ctk.CTkLabel(
            proxy_manager_section,
            text="🔄 Настройки Proxy Manager (ротация прокси)",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        proxy_manager_label.pack(padx=15, pady=10, anchor="w")

        # Макс. запросов на прокси
        pm_max_requests_frame = ctk.CTkFrame(proxy_manager_section, fg_color="transparent")
        pm_max_requests_frame.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(pm_max_requests_frame, text="Макс. запросов:", width=200, anchor="w").pack(side="left", padx=5)
        self.pm_max_requests_entry = ctk.CTkEntry(pm_max_requests_frame, width=100, placeholder_text="15")
        self.pm_max_requests_entry.insert(0, str(self.bot_config.get('proxy_max_requests', 15)))
        self.pm_max_requests_entry.pack(side="left", padx=5)
        ctk.CTkLabel(pm_max_requests_frame, text="(Менять прокси после N запросов)", text_color="gray").pack(side="left", padx=10)

        # Cooldown прокси
        pm_cooldown_frame = ctk.CTkFrame(proxy_manager_section, fg_color="transparent")
        pm_cooldown_frame.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(pm_cooldown_frame, text="Cooldown (секунды):", width=200, anchor="w").pack(side="left", padx=5)
        self.pm_cooldown_entry = ctk.CTkEntry(pm_cooldown_frame, width=100, placeholder_text="60")
        self.pm_cooldown_entry.insert(0, str(self.bot_config.get('proxy_cooldown', 60)))
        self.pm_cooldown_entry.pack(side="left", padx=5)
        ctk.CTkLabel(pm_cooldown_frame, text="(Пауза перед повторным использованием)", text_color="gray").pack(side="left", padx=10)

        # Время блокировки прокси
        pm_blacklist_frame = ctk.CTkFrame(proxy_manager_section, fg_color="transparent")
        pm_blacklist_frame.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(pm_blacklist_frame, text="Blacklist (минуты):", width=200, anchor="w").pack(side="left", padx=5)
        self.pm_blacklist_entry = ctk.CTkEntry(pm_blacklist_frame, width=100, placeholder_text="30")
        self.pm_blacklist_entry.insert(0, str(self.bot_config.get('proxy_blacklist_duration', 30)))
        self.pm_blacklist_entry.pack(side="left", padx=5)
        ctk.CTkLabel(pm_blacklist_frame, text="(Время блокировки плохого прокси)", text_color="gray").pack(side="left", padx=10)

        # === Дополнительные настройки ===
        extra_section = ctk.CTkFrame(settings_frame)
        extra_section.pack(fill="x", pady=10)

        extra_label = ctk.CTkLabel(
            extra_section,
            text="🔧 Дополнительные настройки",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        extra_label.pack(padx=15, pady=10, anchor="w")

        # Автообновление статистики
        auto_refresh_frame = ctk.CTkFrame(extra_section, fg_color="transparent")
        auto_refresh_frame.pack(fill="x", padx=15, pady=5)

        auto_refresh_label = ctk.CTkLabel(
            auto_refresh_frame,
            text="Автообновление статистики:",
            width=200,
            anchor="w"
        )
        auto_refresh_label.pack(side="left", padx=5)

        self.auto_refresh_var = ctk.BooleanVar(value=self.bot_config.get('auto_refresh', True))
        auto_refresh_switch = ctk.CTkSwitch(
            auto_refresh_frame,
            text="Включено",
            variable=self.auto_refresh_var
        )
        auto_refresh_switch.pack(side="left", padx=5)

        # Показывать отладочные логи
        debug_frame = ctk.CTkFrame(extra_section, fg_color="transparent")
        debug_frame.pack(fill="x", padx=15, pady=5)

        debug_label = ctk.CTkLabel(
            debug_frame,
            text="Отладочные логи:",
            width=200,
            anchor="w"
        )
        debug_label.pack(side="left", padx=5)

        self.debug_var = ctk.BooleanVar(value=self.bot_config.get('debug_mode', False))
        debug_switch = ctk.CTkSwitch(
            debug_frame,
            text="Включено",
            variable=self.debug_var
        )
        debug_switch.pack(side="left", padx=5)

        # Кнопки управления настройками
        btn_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        btn_frame.pack(fill="x", pady=20)

        save_btn = ctk.CTkButton(
            btn_frame,
            text="💾 Сохранить настройки",
            command=self._save_bot_config,
            fg_color="green",
            hover_color="darkgreen",
            width=200,
            height=40
        )
        save_btn.pack(side="left", padx=5)

        reset_btn = ctk.CTkButton(
            btn_frame,
            text="🔄 Сбросить",
            command=self._reset_bot_config,
            width=150,
            height=40
        )
        reset_btn.pack(side="left", padx=5)

        # Информация о настройках
        info_frame = ctk.CTkFrame(settings_frame)
        info_frame.pack(fill="x", pady=10)

        info_label = ctk.CTkLabel(
            info_frame,
            text="ℹ️ Настройки сохраняются в файл bot_config.json\n"
                 "Изменения применяются сразу после сохранения.\n"
                 "Для настройки аккаунтов используйте файл accounts.json",
            justify="left",
            text_color="gray"
        )
        info_label.pack(padx=15, pady=15)

    def _create_logs_tab(self):
        """Создать вкладку с логами."""
        # Текстовое поле для логов
        self.logs_text = ctk.CTkTextbox(
            self.tab_logs,
            wrap="word",
            font=ctk.CTkFont(size=13)
        )
        self.logs_text.pack(fill="both", expand=True, padx=10, pady=10)

        # Кнопка очистки логов
        btn_clear = ctk.CTkButton(
            self.tab_logs,
            text="🗑️ Очистить логи",
            command=self._clear_logs,
            width=150,
            height=35,
            font=ctk.CTkFont(size=13)
        )
        btn_clear.pack(pady=(0, 10))

    def _create_statusbar(self):
        """Создать статус бар."""
        self.statusbar = ctk.CTkFrame(self, height=30)
        self.statusbar.pack(fill="x", side="bottom")

        self.status_label = ctk.CTkLabel(
            self.statusbar,
            text="⏸️ Бот остановлен",
            font=ctk.CTkFont(size=12)
        )
        self.status_label.pack(side="left", padx=10, pady=5)

        self.time_label = ctk.CTkLabel(
            self.statusbar,
            text=f"🕒 {datetime.now().strftime('%H:%M:%S')}",
            font=ctk.CTkFont(size=12)
        )
        self.time_label.pack(side="right", padx=10, pady=5)

        # Обновление времени
        self._update_time()

    def _update_time(self):
        """Обновить время в статус баре."""
        self.time_label.configure(text=f"🕒 {datetime.now().strftime('%H:%M:%S')}")
        self.after(1000, self._update_time)

    # ========== Логика управления ботом ==========

    def _load_accounts(self):
        """Загрузить аккаунты из accounts.json."""
        try:
            if not Path("accounts.json").exists():
                self._log("⚠️ Файл accounts.json не найден")
                return

            self.account_manager = AccountManager("accounts.json")
            self._log(f"✅ Загружено аккаунтов: {len(self.account_manager.accounts)}")
            self._refresh_accounts_list()

            # Обновляем балансы аккаунтов после загрузки
            self._update_accounts_balances()

        except Exception as e:
            self._log(f"❌ Ошибка загрузки аккаунтов: {e}")

    def _load_bot_config(self):
        """Загрузить настройки бота из bot_config.json."""
        try:
            config_path = Path("bot_config.json")
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    self.bot_config.update(loaded_config)

                # Обновляем GUI поля значениями из конфига
                self._update_gui_from_config()

                self._log(f"✅ Настройки загружены из bot_config.json")
            else:
                self._log("ℹ️ Файл bot_config.json не найден, используются настройки по умолчанию")
        except Exception as e:
            self._log(f"❌ Ошибка загрузки настроек: {e}")

    def _update_gui_from_config(self):
        """Обновить поля GUI значениями из bot_config."""
        try:
            # Основные настройки
            self.profit_entry.delete(0, 'end')
            self.profit_entry.insert(0, str(self.bot_config.get('min_profit_pct', 5.0)))

            self.interval_entry.delete(0, 'end')
            self.interval_entry.insert(0, str(self.bot_config.get('cycle_interval_minutes', 5)))

            self.db_path_entry.delete(0, 'end')
            self.db_path_entry.insert(0, self.bot_config.get('bottm_db_path', 'data/main.db'))

            self.auto_refresh_var.set(self.bot_config.get('auto_refresh', True))
            self.debug_var.set(self.bot_config.get('debug_mode', False))

            # Trading настройки
            self.trade_min_price_entry.delete(0, 'end')
            self.trade_min_price_entry.insert(0, str(self.bot_config.get('trade_min_price', 100)))

            self.trade_max_price_entry.delete(0, 'end')
            self.trade_max_price_entry.insert(0, str(self.bot_config.get('trade_max_price', 5000)))

            # TM Parser настройки
            self.min_price_entry.delete(0, 'end')
            self.min_price_entry.insert(0, str(self.bot_config.get('scanner_min_price', 1000)))

            self.max_price_entry.delete(0, 'end')
            self.max_price_entry.insert(0, str(self.bot_config.get('scanner_max_price', 10000)))

            self.scanner_profit_entry.delete(0, 'end')
            self.scanner_profit_entry.insert(0, str(self.bot_config.get('scanner_min_profit', -5.0)))

            self.commission_entry.delete(0, 'end')
            self.commission_entry.insert(0, str(self.bot_config.get('csgo_commission', 7.0)))

            self.sales_7d_entry.delete(0, 'end')
            self.sales_7d_entry.insert(0, str(self.bot_config.get('min_sales_7d', 50)))

            self.proxy_file_entry.delete(0, 'end')
            self.proxy_file_entry.insert(0, self.bot_config.get('proxy_file', 'proxies.txt'))

            self.requests_per_proxy_entry.delete(0, 'end')
            self.requests_per_proxy_entry.insert(0, str(self.bot_config.get('requests_per_proxy', 15)))

            self.max_items_entry.delete(0, 'end')
            self.max_items_entry.insert(0, str(self.bot_config.get('scanner_max_items', 10)))

            self.delay_entry.delete(0, 'end')
            self.delay_entry.insert(0, str(self.bot_config.get('scanner_delay', 7.0)))

            self.workers_entry.delete(0, 'end')
            self.workers_entry.insert(0, str(self.bot_config.get('scanner_workers', 1)))

        except Exception as e:
            logger.error(f"Error updating GUI from config: {e}")

    def _save_bot_config(self):
        """Сохранить настройки бота в bot_config.json."""
        try:
            # Собираем настройки из полей
            min_profit = float(self.profit_entry.get())
            cycle_interval = int(self.interval_entry.get())

            # Валидация
            if cycle_interval < 1:
                self._log("❌ Ошибка: интервал между циклами должен быть минимум 1 минута")
                return

            # Предупреждение о отрицательном профите
            if min_profit < 0:
                self._log("⚠️ ВНИМАНИЕ: Установлен отрицательный профит!")
                self._log(f"⚠️ Бот будет покупать предметы с УБЫТКОМ до {abs(min_profit):.1f}%")
                self._log("⚠️ Это может привести к финансовым потерям!")
                # Можно добавить диалог подтверждения здесь
            elif min_profit < 3:
                self._log(f"⚠️ Предупреждение: низкий профит {min_profit:.1f}% может не покрывать комиссию CSGO.TM (~10%)")

            self.bot_config['min_profit_pct'] = min_profit
            self.bot_config['cycle_interval_minutes'] = cycle_interval
            self.bot_config['bottm_db_path'] = self.db_path_entry.get()
            self.bot_config['auto_refresh'] = self.auto_refresh_var.get()
            self.bot_config['debug_mode'] = self.debug_var.get()

            # Trading настройки (выставление ордеров)
            self.bot_config['trade_min_price'] = float(self.trade_min_price_entry.get())
            self.bot_config['trade_max_price'] = float(self.trade_max_price_entry.get())

            # TM Parser настройки
            self.bot_config['scanner_min_price'] = float(self.min_price_entry.get())
            self.bot_config['scanner_max_price'] = float(self.max_price_entry.get())
            self.bot_config['scanner_min_profit'] = float(self.scanner_profit_entry.get())
            self.bot_config['csgo_commission'] = float(self.commission_entry.get())
            self.bot_config['min_sales_7d'] = int(self.sales_7d_entry.get())

            # TM Parser - дополнительные настройки
            self.bot_config['proxy_file'] = self.proxy_file_entry.get()
            self.bot_config['requests_per_proxy'] = int(self.requests_per_proxy_entry.get())
            self.bot_config['scanner_max_items'] = int(self.max_items_entry.get())
            self.bot_config['scanner_delay'] = float(self.delay_entry.get())
            self.bot_config['scanner_workers'] = int(self.workers_entry.get())

            # Auto Scanner настройки
            self.bot_config['auto_scan_interval'] = int(self.as_interval_entry.get())

            # Proxy Manager настройки
            self.bot_config['proxy_max_requests'] = int(self.pm_max_requests_entry.get())
            self.bot_config['proxy_cooldown'] = int(self.pm_cooldown_entry.get())
            self.bot_config['proxy_blacklist_duration'] = int(self.pm_blacklist_entry.get())

            # Сохраняем в файл
            with open("bot_config.json", 'w', encoding='utf-8') as f:
                json.dump(self.bot_config, f, indent=2, ensure_ascii=False)

            self._log("✅ Настройки сохранены в bot_config.json")

            if min_profit >= 0:
                self._log(f"✅ Минимальный профит: {min_profit:.1f}%")

            # Обновляем параметры в TradingBot
            from src.trading_bot import TradingBot
            TradingBot.MIN_PROFIT_PCT = self.bot_config['min_profit_pct']
            TradingBot.TRADE_MIN_PRICE = self.bot_config['trade_min_price']
            TradingBot.TRADE_MAX_PRICE = self.bot_config['trade_max_price']

        except ValueError as e:
            self._log(f"❌ Ошибка: неверный формат данных. Проверьте введенные значения.")
        except Exception as e:
            self._log(f"❌ Ошибка сохранения настроек: {e}")

    def _reset_bot_config(self):
        """Сбросить настройки к значениям по умолчанию."""
        # Сбрасываем к дефолтным значениям
        self.bot_config = {
            'min_profit_pct': 5.0,
            'cycle_interval_minutes': 5,
            'bottm_db_path': 'data/main.db',
            'auto_refresh': True,
            'debug_mode': False,
        }

        # Обновляем поля
        self.profit_entry.delete(0, 'end')
        self.profit_entry.insert(0, "5.0")

        self.interval_entry.delete(0, 'end')
        self.interval_entry.insert(0, "5")

        self.db_path_entry.delete(0, 'end')
        self.db_path_entry.insert(0, "data/main.db")

        self.auto_refresh_var.set(True)
        self.debug_var.set(False)

        self._log("🔄 Настройки сброшены к значениям по умолчанию")

    def _refresh_accounts_list(self):
        """Обновить список аккаунтов."""
        # Очищаем текущий список
        for widget in self.accounts_scrollable.winfo_children():
            widget.destroy()

        # Очищаем словарь balance labels
        self.account_balance_labels.clear()

        if not self.account_manager:
            return

        # Добавляем аккаунты
        for account in self.account_manager.accounts:
            self._create_account_card(account)

    def _create_account_card(self, account):
        """Создать карточку аккаунта."""
        card = ctk.CTkFrame(self.accounts_scrollable)
        card.pack(fill="x", padx=5, pady=5)

        # Имя аккаунта
        name_label = ctk.CTkLabel(
            card,
            text=f"👤 {account.name}",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        name_label.pack(side="left", padx=10, pady=10)

        # Статус
        status_text = "✅ Включен" if account.config.enabled else "⏸️ Выключен"
        status_label = ctk.CTkLabel(card, text=status_text)
        status_label.pack(side="left", padx=10)

        # Валюта аккаунта
        currency = getattr(account.config, 'currency', 'RUB')
        currency_label = ctk.CTkLabel(card, text=f"💱 {currency}", text_color="gray")
        currency_label.pack(side="left", padx=10)

        # Баланс (будет обновляться)
        balance_label = ctk.CTkLabel(card, text="💰 Баланс: загрузка...")
        balance_label.pack(side="left", padx=10)

        # Сохраняем ссылку на balance_label для обновления
        self.account_balance_labels[account.name] = balance_label

        # Кнопки управления
        btn_frame = ctk.CTkFrame(card, fg_color="transparent")
        btn_frame.pack(side="right", padx=10)

        # Кнопка определения валюты
        btn_detect_currency = ctk.CTkButton(
            btn_frame,
            text="🔍 Валюта",
            command=lambda a=account: self._detect_account_currency(a),
            width=100,
            fg_color="#3498DB",
            hover_color="#2980B9"
        )
        btn_detect_currency.pack(side="left", padx=5)

        btn_toggle = ctk.CTkButton(
            btn_frame,
            text="Вкл/Выкл",
            command=lambda a=account: self._toggle_account(a),
            width=80
        )
        btn_toggle.pack(side="left", padx=5)

    def _start_bot(self):
        """Запустить бота."""
        if self.bot_running:
            return

        try:
            self.bot_running = True
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.status_label.configure(text="▶️ Бот запущен")

            # Запускаем в отдельном потоке
            self.bot_thread = threading.Thread(target=self._bot_loop, daemon=True)
            self.bot_thread.start()

            self._log("▶️ Бот запущен")

        except Exception as e:
            self._log(f"❌ Ошибка запуска бота: {e}")
            self.bot_running = False
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")

    def _stop_bot(self):
        """Остановить бота."""
        self.bot_running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.status_label.configure(text="⏸️ Бот остановлен")
        self._log("⏸️ Бот остановлен")

    def _bot_loop(self):
        """Главный цикл бота (выполняется в отдельном потоке)."""
        try:
            if not self.account_manager:
                self._log("❌ AccountManager не инициализирован")
                return

            # Применяем настройки к TradingBot
            from src.trading_bot import TradingBot
            TradingBot.MIN_PROFIT_PCT = self.bot_config.get('min_profit_pct', 5.0)
            TradingBot.TRADE_MIN_PRICE = self.bot_config.get('trade_min_price', 100.0)
            TradingBot.TRADE_MAX_PRICE = self.bot_config.get('trade_max_price', 5000.0)
            self._log(f"⚙️ Минимальный профит установлен: {TradingBot.MIN_PROFIT_PCT}%")
            self._log(f"⚙️ Диапазон цен для ордеров: {TradingBot.TRADE_MIN_PRICE:.0f}-{TradingBot.TRADE_MAX_PRICE:.0f} RUB")

            # Логин аккаунтов
            self._log("🔐 Логин в аккаунты...")
            login_results = self.account_manager.login_all()

            logged_in_count = sum(1 for v in login_results.values() if v)
            self._log(f"✅ Залогинено: {logged_in_count}/{len(login_results)}")

            # Автоматически определяем валюту для аккаунтов, если не была определена
            self._log("🔍 Проверка валют аккаунтов...")
            for account in self.account_manager.get_enabled_accounts():
                if not self.bot_running:
                    break

                # Проверяем, есть ли кешированный баланс
                if not hasattr(account, '_cached_steam_balance') or not account._cached_steam_balance:
                    self._log(f"[{account.name}] Определение валюты через SteamKit2...")
                    try:
                        # Используем новый метод через SteamWalletChecker
                        from src.steam_wallet_checker import SteamWalletBalance

                        checker = SteamWalletBalance(
                            username=account.config.steam_username,
                            password=account.config.steam_password,
                            shared_secret=account.config.steam_shared_secret,
                            identity_secret=account.config.steam_identity_secret,
                            steamid=account.config.steamid
                        )

                        result = checker.get_balance()

                        if result['success']:
                            detected_currency = result['currency']
                            balance = result['balance']
                            balance_usd = result.get('balance_usd', 0)

                            self._log(f"[{account.name}] ✅ Валюта: {detected_currency}, Баланс: {balance:.2f} {detected_currency}")

                            # Обновляем валюту
                            account.config.currency = detected_currency

                            # Кешируем баланс
                            account._cached_steam_balance = {
                                'balance': balance,
                                'currency': detected_currency,
                                'balance_usd': balance_usd,
                                'timestamp': time.time()
                            }
                        else:
                            self._log(f"[{account.name}] ⚠️ Не удалось определить валюту: {result.get('error')}")
                            self._log(f"[{account.name}] ℹ️ Используется валюта по умолчанию: {account.config.currency}")

                    except Exception as e:
                        self._log(f"[{account.name}] ❌ Ошибка определения валюты: {e}")
                        self._log(f"[{account.name}] ℹ️ Используется валюта по умолчанию: {account.config.currency}")
                else:
                    cached = account._cached_steam_balance
                    self._log(f"[{account.name}] ✅ Валюта уже определена: {cached['currency']}, Баланс: {cached['balance']:.2f} {cached['currency']}")

            # Главный цикл
            cycle_count = 0
            while self.bot_running:
                cycle_count += 1
                self._log(f"\n{'='*50}")
                self._log(f"🔄 Цикл #{cycle_count}")
                self._log(f"{'='*50}")

                # Запускаем цикл для каждого аккаунта
                for account in self.account_manager.get_enabled_accounts():
                    if not self.bot_running:
                        break

                    try:
                        bot = TradingBot(account)
                        stats = bot.run_cycle()

                        self._log(
                            f"[{account.name}] Цикл завершен: "
                            f"создано={stats['orders_created']}, "
                            f"исполнено={stats['orders_filled']}, "
                            f"выставлено={stats['items_listed']}"
                        )

                    except Exception as e:
                        self._log(f"❌ [{account.name}] Ошибка цикла: {e}")

                # Обновляем статистику в GUI
                self.after(0, self._refresh_data)

                # Пауза между циклами (из настроек)
                interval_minutes = self.bot_config.get('cycle_interval_minutes', 5)
                interval_seconds = interval_minutes * 60
                self._log(f"⏰ Следующий цикл через {interval_minutes} минут...")
                time.sleep(interval_seconds)

        except Exception as e:
            self._log(f"❌ Критическая ошибка в bot_loop: {e}")
        finally:
            # Logout
            if self.account_manager:
                self.account_manager.logout_all()
            self.bot_running = False
            self.after(0, lambda: self.btn_start.configure(state="normal"))
            self.after(0, lambda: self.btn_stop.configure(state="disabled"))

    def _refresh_data(self):
        """Обновить данные на всех вкладках."""
        try:
            # Обновляем статистику
            self._update_dashboard_stats()
            self._update_accounts_balances()
            self._refresh_orders_list()
            self._refresh_inventory_list()
            self._refresh_profitable_items()

        except Exception as e:
            logger.error(f"Error refreshing data: {e}")

    def _update_dashboard_stats(self):
        """Обновить статистику на Dashboard."""
        try:
            # Балансы
            total_steam = 0.0
            total_csgotm = 0.0

            if self.account_manager:
                try:
                    balances = self.account_manager.get_total_balance()
                    total_steam = balances.get('steam_balance', 0.0)
                    total_csgotm = balances.get('csgotm_balance', 0.0)
                except Exception as e:
                    logger.error(f"Error getting balances: {e}")

            self.stats_cards['balance_steam'].value_label.configure(
                text=f"{total_steam:.2f} RUB"
            )
            self.stats_cards['balance_csgotm'].value_label.configure(
                text=f"{total_csgotm:.2f} RUB"
            )

            # Статистика из БД
            active_orders = trades_db.get_active_orders_count()
            on_hold = trades_db.get_purchased_items_count()
            profit_data = trades_db.get_total_profit()
            sold_count = trades_db.get_sold_items_count()

            total_profit = profit_data.get('total_profit', 0.0) if profit_data else 0.0

            self.stats_cards['active_orders'].value_label.configure(text=str(active_orders))
            self.stats_cards['on_hold'].value_label.configure(text=str(on_hold))
            self.stats_cards['profit'].value_label.configure(text=f"${total_profit:.2f}")
            self.stats_cards['sold_items'].value_label.configure(text=str(sold_count))

        except Exception as e:
            logger.error(f"Error updating stats: {e}")

    def _update_accounts_balances(self):
        """Обновить балансы всех аккаунтов."""
        if not self.account_manager:
            return

        try:
            for account in self.account_manager.accounts:
                if account.name in self.account_balance_labels:
                    try:
                        # Проверяем, есть ли кешированный баланс
                        steam_balance = 0.0
                        if hasattr(account, '_cached_steam_balance') and account._cached_steam_balance:
                            # Используем кешированный баланс (чтобы не делать лишние запросы к Steam API)
                            steam_balance = account._cached_steam_balance.get('balance', 0.0)
                            currency = account._cached_steam_balance.get('currency', 'RUB')
                        else:
                            # Если кеша нет, используем валюту из конфига, но НЕ запрашиваем баланс
                            # Баланс будет показан только после нажатия "Определить валюту"
                            currency = getattr(account.config, 'currency', 'RUB')
                            steam_balance = 0.0

                        # Получаем баланс CSGO.TM (это быстро и не вызывает rate limit)
                        try:
                            csgotm_balance = account.get_csgotm_balance()  # Всегда в RUB
                        except Exception as e:
                            logger.warning(f"Failed to get CSGO.TM balance for {account.name}: {e}")
                            csgotm_balance = 0.0

                        # Если валюта не RUB, конвертируем TM баланс
                        if currency != 'RUB':
                            # Steam баланс уже в нужной валюте
                            # TM баланс в RUB -> конвертируем в валюту аккаунта
                            csgotm_converted = currency_converter.convert_from_rub(csgotm_balance, currency)
                            if steam_balance > 0:
                                balance_text = f"💰 Steam: {steam_balance:.2f} {currency} | TM: {csgotm_converted:.2f} {currency} ({csgotm_balance:.2f} RUB)"
                            else:
                                balance_text = f"💰 Steam: нажмите '🔍 Валюта' | TM: {csgotm_converted:.2f} {currency} ({csgotm_balance:.2f} RUB)"
                        else:
                            if steam_balance > 0:
                                balance_text = f"💰 Steam: {steam_balance:.2f} {currency} | TM: {csgotm_balance:.2f} {currency}"
                            else:
                                balance_text = f"💰 Steam: нажмите '🔍 Валюта' | TM: {csgotm_balance:.2f} {currency}"

                        self.account_balance_labels[account.name].configure(text=balance_text)

                    except Exception as e:
                        logger.error(f"Error getting balance for {account.name}: {e}")
                        self.account_balance_labels[account.name].configure(
                            text="💰 Ошибка загрузки баланса"
                        )

        except Exception as e:
            logger.error(f"Error updating account balances: {e}")

    def _refresh_orders_list(self):
        """Обновить список ордеров."""
        # Очищаем
        for widget in self.orders_scrollable.winfo_children():
            widget.destroy()

        try:
            # Получаем активные ордера
            orders = trades_db.get_all_active_orders()

            if not orders:
                label = ctk.CTkLabel(
                    self.orders_scrollable,
                    text="Нет активных ордеров",
                    font=ctk.CTkFont(size=14)
                )
                label.pack(pady=20)
                return

            # Создаем заголовок таблицы
            header = ctk.CTkFrame(self.orders_scrollable)
            header.pack(fill="x", pady=(0, 5))

            # Настраиваем grid для адаптивной ширины
            header.grid_columnconfigure(0, weight=1, minsize=150)  # Аккаунт
            header.grid_columnconfigure(1, weight=4, minsize=350)  # Предмет
            header.grid_columnconfigure(2, weight=1, minsize=120)  # Цена
            header.grid_columnconfigure(3, weight=1, minsize=150)  # Создан

            headers = ["Аккаунт", "Предмет", "Цена", "Создан"]
            for i, text in enumerate(headers):
                label = ctk.CTkLabel(
                    header,
                    text=text,
                    font=ctk.CTkFont(size=14, weight="bold")
                )
                label.grid(row=0, column=i, padx=10, pady=8, sticky="ew")

            # Добавляем ордера
            for order in orders:
                row = ctk.CTkFrame(self.orders_scrollable)
                row.pack(fill="x", pady=3)

                # Настраиваем grid для строки
                row.grid_columnconfigure(0, weight=1, minsize=150)
                row.grid_columnconfigure(1, weight=4, minsize=350)
                row.grid_columnconfigure(2, weight=1, minsize=120)
                row.grid_columnconfigure(3, weight=1, minsize=150)

                values = [
                    order.get('account_name', 'N/A'),
                    order.get('item_name', 'N/A'),
                    f"{order.get('order_price', 0):.2f} ₽",
                    order.get('created_at', 'N/A')[:16]
                ]

                for i, value in enumerate(values):
                    label = ctk.CTkLabel(
                        row,
                        text=value,
                        font=ctk.CTkFont(size=13)
                    )
                    label.grid(row=0, column=i, padx=10, pady=6, sticky="ew")

        except Exception as e:
            logger.error(f"Error refreshing orders: {e}")

    def _refresh_inventory_list(self):
        """Обновить список предметов на холде."""
        # Очищаем
        for widget in self.inventory_scrollable.winfo_children():
            widget.destroy()

        try:
            # Получаем предметы на холде
            items = trades_db.get_items_on_hold()

            if not items:
                label = ctk.CTkLabel(
                    self.inventory_scrollable,
                    text="Нет предметов на холде",
                    font=ctk.CTkFont(size=14)
                )
                label.pack(pady=20)
                return

            # Создаем заголовок
            header = ctk.CTkFrame(self.inventory_scrollable)
            header.pack(fill="x", pady=(0, 5))

            # Настраиваем grid для адаптивной ширины
            header.grid_columnconfigure(0, weight=1, minsize=150)  # Аккаунт
            header.grid_columnconfigure(1, weight=4, minsize=350)  # Предмет
            header.grid_columnconfigure(2, weight=1, minsize=150)  # Куплен
            header.grid_columnconfigure(3, weight=1, minsize=150)  # Разблокировка

            headers = ["Аккаунт", "Предмет", "Куплен", "Разблокировка"]
            for i, text in enumerate(headers):
                label = ctk.CTkLabel(
                    header,
                    text=text,
                    font=ctk.CTkFont(size=14, weight="bold")
                )
                label.grid(row=0, column=i, padx=10, pady=8, sticky="ew")

            # Добавляем предметы
            for item in items:
                row = ctk.CTkFrame(self.inventory_scrollable)
                row.pack(fill="x", pady=3)

                # Настраиваем grid для строки
                row.grid_columnconfigure(0, weight=1, minsize=150)
                row.grid_columnconfigure(1, weight=4, minsize=350)
                row.grid_columnconfigure(2, weight=1, minsize=150)
                row.grid_columnconfigure(3, weight=1, minsize=150)

                values = [
                    item.get('account_name', 'N/A'),
                    item.get('item_name', 'N/A'),
                    item.get('purchase_date', 'N/A')[:16],
                    item.get('unlock_date', 'N/A')[:16]
                ]

                for i, value in enumerate(values):
                    label = ctk.CTkLabel(
                        row,
                        text=value,
                        font=ctk.CTkFont(size=13)
                    )
                    label.grid(row=0, column=i, padx=10, pady=6, sticky="ew")

        except Exception as e:
            logger.error(f"Error refreshing inventory: {e}")

    def _toggle_profit_sort(self):
        """Переключить сортировку по профиту."""
        self.sort_by_profit = not self.sort_by_profit
        self._refresh_profitable_items()

    def _refresh_profitable_items(self):
        """Обновить список выгодных предметов из БД."""
        # Очищаем
        for widget in self.parser_scrollable.winfo_children():
            widget.destroy()

        try:
            # Определяем фильтр по профиту
            if self.profit_filter_enabled_var.get():
                # Фильтр включен - используем значение из поля
                try:
                    min_profit = float(self.parser_min_profit_entry.get())
                except ValueError:
                    min_profit = -5.0  # Fallback значение
            else:
                # Фильтр выключен - показываем ВСЕ предметы
                min_profit = None

            items = trades_db.get_active_profitable_items(min_profit=min_profit, limit=100)

            # Фильтруем фейк-профиты (где buy_order >= lowest_sell)
            fake_profit_items = []
            filtered_items = []
            for item in items:
                steam_buy = item.get('steam_buy_order')
                steam_sell = item.get('steam_lowest_sell')

                # Если есть данные о lowest_sell и buy_order >= lowest_sell - это фейк
                if steam_sell is not None and steam_buy is not None and steam_sell > 0 and steam_buy >= steam_sell:
                    fake_profit_items.append(item.get('market_hash_name'))
                else:
                    filtered_items.append(item)

            # Удаляем фейк-профиты из БД
            if fake_profit_items:
                logger.info(f"Обнаружено {len(fake_profit_items)} фейк-профитов, удаляем...")
                trades_db.mark_items_inactive(fake_profit_items)
                self._log(f"🗑️ Удалено {len(fake_profit_items)} фейк-профитов")

            items = filtered_items

            # Сортировка по профиту если включена
            if self.sort_by_profit and items:
                # Обрабатываем None значения: ставим их в конец списка (-999)
                items.sort(key=lambda x: x.get('profit_pct') if x.get('profit_pct') is not None else -999, reverse=True)

            # Обновляем счётчик предметов
            if hasattr(self, 'parser_items_count_label'):
                self.parser_items_count_label.configure(
                    text=f"Найдено: {len(items)} предметов"
                )

            if not items:
                label = ctk.CTkLabel(
                    self.parser_scrollable,
                    text="Нет выгодных предметов. Запустите сканирование!",
                    font=ctk.CTkFont(size=14)
                )
                label.pack(pady=20)
                return

            # Создаем заголовок таблицы
            header = ctk.CTkFrame(self.parser_scrollable)
            header.pack(fill="x", pady=(0, 5))

            headers = ["Предмет", "Steam Buy", "CSGO Sell", "Профит %", "Рек. цена", "Обновлено", "Действия"]
            # Используем grid weights для адаптивной ширины
            header.grid_columnconfigure(0, weight=4, minsize=350)  # Название предмета - самая широкая
            header.grid_columnconfigure(1, weight=1, minsize=100)  # Steam Buy
            header.grid_columnconfigure(2, weight=1, minsize=100)  # CSGO Sell
            header.grid_columnconfigure(3, weight=1, minsize=90)   # Профит
            header.grid_columnconfigure(4, weight=1, minsize=100)  # Рек. цена
            header.grid_columnconfigure(5, weight=1, minsize=140)  # Обновлено
            header.grid_columnconfigure(6, weight=1, minsize=180)  # Действия

            for i, text in enumerate(headers):
                # Для "Профит %" делаем кликабельную кнопку для сортировки
                if text == "Профит %":
                    sort_indicator = " ▼" if self.sort_by_profit else ""
                    profit_btn = ctk.CTkButton(
                        header,
                        text=f"{text}{sort_indicator}",
                        command=self._toggle_profit_sort,
                        font=ctk.CTkFont(size=14, weight="bold"),
                        fg_color="transparent",
                        hover_color=("gray70", "gray30"),
                        width=90
                    )
                    profit_btn.grid(row=0, column=i, padx=8, pady=8, sticky="ew")
                else:
                    label = ctk.CTkLabel(
                        header,
                        text=text,
                        font=ctk.CTkFont(size=14, weight="bold"),
                    )
                    label.grid(row=0, column=i, padx=8, pady=8, sticky="ew")

            # Добавляем предметы
            for item in items:
                row = ctk.CTkFrame(self.parser_scrollable)
                row.pack(fill="x", pady=3)

                # Настраиваем grid для каждой строки с теми же пропорциями
                row.grid_columnconfigure(0, weight=4, minsize=350)
                row.grid_columnconfigure(1, weight=1, minsize=100)
                row.grid_columnconfigure(2, weight=1, minsize=100)
                row.grid_columnconfigure(3, weight=1, minsize=90)
                row.grid_columnconfigure(4, weight=1, minsize=100)
                row.grid_columnconfigure(5, weight=1, minsize=140)
                row.grid_columnconfigure(6, weight=1, minsize=180)

                market_hash_name = item.get('market_hash_name', 'N/A')

                # Получаем профит (используем recommended если есть, иначе обычный)
                # Обрабатываем None значения корректно
                profit_pct = item.get('profit_pct') if item.get('profit_pct') is not None else 0
                recommended_profit_pct = item.get('recommended_profit_pct')
                best_profit = recommended_profit_pct if recommended_profit_pct is not None else profit_pct

                # Цвет в зависимости от профита (обрабатываем None)
                if best_profit is None:
                    best_profit = 0
                profit_color = "green" if best_profit >= 5 else "orange" if best_profit >= 0 else "red"

                # Название предмета (только отображение)
                item_label = ctk.CTkLabel(
                    row,
                    text=market_hash_name[:50] + "..." if len(market_hash_name) > 50 else market_hash_name,
                    anchor="w",
                    font=ctk.CTkFont(size=13)
                )
                item_label.grid(row=0, column=0, padx=8, pady=6, sticky="ew")

                # Steam Buy Order (кнопка-ссылка на Steam Market)
                def open_steam(name=market_hash_name):
                    import webbrowser
                    encoded_name = name.replace(' ', '%20').replace('|', '%7C')
                    webbrowser.open(f"https://steamcommunity.com/market/listings/730/{encoded_name}")

                steam_btn = ctk.CTkButton(
                    row,
                    text=f"{item.get('steam_buy_order', 0):.2f} ₽",
                    command=open_steam,
                    fg_color="transparent",
                    hover_color=("gray70", "gray30"),
                    font=ctk.CTkFont(size=13)
                )
                steam_btn.grid(row=0, column=1, padx=8, pady=6, sticky="ew")

                # CSGO Market Sell (кнопка-ссылка)
                def open_csgotm(name=market_hash_name):
                    import webbrowser
                    encoded_name = quote(name)
                    webbrowser.open(f"https://market.csgo.com/ru/{encoded_name}")

                csgo_btn = ctk.CTkButton(
                    row,
                    text=f"{item.get('csgo_price', 0):.2f} ₽",
                    command=open_csgotm,
                    fg_color="transparent",
                    hover_color=("gray70", "gray30"),
                    font=ctk.CTkFont(size=13)
                )
                csgo_btn.grid(row=0, column=2, padx=8, pady=6, sticky="ew")

                # Профит
                profit_label = ctk.CTkLabel(
                    row,
                    text=f"{best_profit:.2f}%",
                    text_color=profit_color,
                    font=ctk.CTkFont(size=14, weight="bold")
                )
                profit_label.grid(row=0, column=3, padx=8, pady=6, sticky="ew")

                # Рекомендованная цена
                rec_price = item.get('recommended_buy_order', 0)
                rec_label = ctk.CTkLabel(
                    row,
                    text=f"{rec_price:.2f} ₽",
                    font=ctk.CTkFont(size=13)
                )
                rec_label.grid(row=0, column=4, padx=8, pady=6, sticky="ew")

                # Время обновления
                updated_label = ctk.CTkLabel(
                    row,
                    text=item.get('updated_at', 'N/A')[:16],
                    font=ctk.CTkFont(size=12)
                )
                updated_label.grid(row=0, column=5, padx=8, pady=6, sticky="ew")

                # Кнопки действий
                actions_frame = ctk.CTkFrame(row, fg_color="transparent")
                actions_frame.grid(row=0, column=6, padx=8, pady=6, sticky="ew")

                # Кнопка обновления
                def update_item(name=market_hash_name):
                    self._update_single_item(name)

                update_btn = ctk.CTkButton(
                    actions_frame,
                    text="🔄",
                    command=update_item,
                    width=60,
                    height=32,
                    fg_color=("gray75", "gray25"),
                    hover_color=("gray65", "gray35"),
                    font=ctk.CTkFont(size=16)
                )
                update_btn.pack(side="left", padx=3)

                # Кнопка удаления
                def delete_item(name=market_hash_name):
                    self._delete_profitable_item(name)

                delete_btn = ctk.CTkButton(
                    actions_frame,
                    text="🗑️",
                    command=delete_item,
                    width=60,
                    height=32,
                    fg_color=("red", "darkred"),
                    hover_color=("darkred", "red"),
                    font=ctk.CTkFont(size=16)
                )
                delete_btn.pack(side="left", padx=3)

        except Exception as e:
            logger.error(f"Error refreshing profitable items: {e}")

    def _delete_profitable_item(self, market_hash_name: str):
        """Удалить предмет из списка выгодных."""
        try:
            # Удаляем из БД
            trades_db.remove_profitable_item(market_hash_name)
            self._log(f"🗑️ Удален предмет: {market_hash_name}")

            # Обновляем отображение
            self._refresh_profitable_items()
        except Exception as e:
            logger.error(f"Error deleting profitable item: {e}")
            self._log(f"❌ Ошибка удаления предмета: {e}")

    def _update_single_item(self, market_hash_name: str):
        """Обновить данные одного предмета."""
        try:
            # Проверяем, не обновляем ли мы слишком часто
            if hasattr(self, '_last_update_time'):
                elapsed = time.time() - self._last_update_time
                if elapsed < 60.0:  # Минимум 60 секунд между обновлениями
                    remaining = 60.0 - elapsed
                    self._log(f"Подождите {remaining:.0f} секунд перед следующим обновлением (cooldown)")
                    return

            self._log(f"Обновление предмета: {market_hash_name}")
            self._log(f"Steam может заблокировать при частых запросах. Рекомендуется использовать 'Пересканировать все' вместо ручного обновления.")
            self._update_scanner_status(f"Обновление: {market_hash_name}", "yellow")

            self._last_update_time = time.time()

            # Запускаем обновление в отдельном потоке
            update_thread = threading.Thread(
                target=self._update_item_thread,
                args=(market_hash_name,),
                daemon=True
            )
            update_thread.start()
        except Exception as e:
            logger.error(f"Error updating item: {e}")
            self._log(f"Ошибка обновления предмета: {e}")

    def _update_item_thread(self, market_hash_name: str):
        """Поток для обновления одного предмета."""
        try:
            from src.steam_client import SteamClient
            from src.csgotm_client import CsgoTmClient
            import requests
            import os

            # Получаем минимальный профит из конфига (используем self.bot_config напрямую)
            min_profit = self.bot_config.get('min_profit_percent', 15)

            # Загружаем прокси если есть
            proxy_file = self.bot_config.get('proxy_file', 'proxies.txt')
            proxies_list = self._load_proxies(proxy_file)

            # Попробуем несколько прокси, если первый не работает
            histogram = None
            steam_buy_order = None
            csgo_price = None
            max_proxy_attempts = min(3, len(proxies_list)) if proxies_list else 1

            for attempt in range(max_proxy_attempts if proxies_list else 1):
                session = None
                if proxies_list:
                    # Используем случайный прокси
                    import random
                    proxy = random.choice(proxies_list)
                    self._log(f"Используем прокси для обхода rate limit: {proxy.split('@')[-1] if '@' in proxy else proxy}")

                    # Создаём сессию с прокси
                    session = requests.Session()
                    session.proxies = {'http': proxy, 'https': proxy}
                else:
                    # Создаём обычную сессию если нет прокси
                    self._log(f"ℹ️ Работаем без прокси (возможны ограничения по rate limit)")
                    session = requests.Session()

                # Создаём клиенты напрямую
                steam_client = SteamClient()

                # КРИТИЧЕСКИ ВАЖНО: Устанавливаем сессию ДО любых запросов!
                # Иначе _make_public_request создаст новую сессию без прокси
                steam_client._session = session

                # Уменьшаем задержку и retry для быстрой проверки прокси
                steam_client.REQUEST_DELAY = 0.5 if proxies_list else 1.0
                steam_client.MAX_RETRIES = 2  # Только 2 попытки для быстрой проверки

                csgotm_api_key = os.getenv('CSGOTM_API_KEY', self.bot_config.get('csgo_tm_api_key', ''))
                csgotm_client = CsgoTmClient(api_key=csgotm_api_key, session=session)

                self._log(f"Запрос цены на Steam Market...")

                try:
                    # Получаем текущую цену buy order на Steam
                    histogram = steam_client.get_market_histogram(market_hash_name)
                    if not histogram or not histogram.get('highest_buy_order'):
                        if proxies_list:
                            self._log(f"Не удалось получить данные через этот прокси")
                            if attempt < max_proxy_attempts - 1:
                                self._log(f"Пробуем другой прокси...")
                                continue
                        else:
                            self._log(f"Не удалось получить данные")
                        break

                    steam_buy_order = histogram['highest_buy_order']
                    steam_lowest_sell = histogram.get('lowest_sell_order')
                    self._log(f"Steam buy order: {steam_buy_order:.2f} RUB")

                    # Проверка на фейк-профит
                    if steam_lowest_sell and steam_buy_order >= steam_lowest_sell:
                        self._log(f"❌ ФЕЙК ПРОФИТ! Buy order ({steam_buy_order:.2f}) >= Lowest sell ({steam_lowest_sell:.2f})")
                        self._log(f"Предмет {market_hash_name} удалён из списка")
                        trades_db.mark_items_inactive([market_hash_name])
                        self._update_scanner_status("Готов", "green")
                        # Обновляем отображение
                        self.after(0, self._refresh_profitable_items)
                        return

                    # Получаем цену CSGO.TM (используем ту же сессию, пока она открыта)
                    self._log(f"Запрос цены на CSGO.TM...")
                    tm_price_data = csgotm_client.get_item_price(market_hash_name)
                    if not tm_price_data or 'min_price' not in tm_price_data:
                        self._log(f"Не удалось получить цену CSGO.TM для {market_hash_name}")
                        self._update_scanner_status("Готов", "green")
                        return

                    csgo_price = tm_price_data['min_price']
                    self._log(f"CSGO.TM price: {csgo_price:.2f} RUB")

                    # Успешно получили все данные - прерываем цикл
                    break

                except Exception as e:
                    if proxies_list:
                        self._log(f"Ошибка с прокси: {str(e)[:100]}")
                        if attempt < max_proxy_attempts - 1:
                            self._log(f"Пробуем другой прокси...")
                            continue
                    else:
                        self._log(f"Ошибка запроса: {str(e)[:100]}")
                finally:
                    # Закрываем сессию перед следующей попыткой
                    if session and hasattr(session, 'close'):
                        try:
                            session.close()
                        except:
                            pass

            if not histogram or not histogram.get('highest_buy_order') or not csgo_price:
                self._log(f"Не удалось получить цены для {market_hash_name}")
                if not proxies_list:
                    self._log(f"Совет: Добавьте прокси в файл {proxy_file} для обхода блокировки")
                self._update_scanner_status("Готов", "green")
                return

            # Рассчитываем профит (с учетом комиссии 7% на CSGO.TM)
            net_revenue = csgo_price * 0.93
            if steam_buy_order > 0:
                instant_profit_pct = ((net_revenue - steam_buy_order) / steam_buy_order) * 100
            else:
                instant_profit_pct = 0

            # Рекомендуемая цена для покупки (на 1 копейку выше текущего максимума)
            recommended_buy_order = steam_buy_order + 0.01

            # Проверяем, выгоден ли предмет
            if instant_profit_pct >= min_profit:
                # Обновляем в БД
                trades_db.add_profitable_item(
                    market_hash_name=market_hash_name,
                    item_type='weapon',
                    steam_buy_order=steam_buy_order,
                    recommended_buy_order=recommended_buy_order,
                    csgo_price=csgo_price,
                    csgo_buy_order=0,
                    instant_profit_pct=instant_profit_pct,
                    wait_profit_pct=instant_profit_pct,  # Упрощённо, без учета ожидания
                    recommended_instant_pct=instant_profit_pct,
                    recommended_wait_pct=instant_profit_pct,
                    orders_above=0
                )
                self._log(f"Предмет обновлен: {market_hash_name} (профит: {instant_profit_pct:.2f}%)")
            else:
                self._log(f"Предмет больше не выгоден: {market_hash_name} (профит: {instant_profit_pct:.2f}% < {min_profit}%)")

            # Обновляем отображение
            self.after(0, self._refresh_profitable_items)
            self._update_scanner_status("Готов", "green")

        except Exception as e:
            logger.error(f"Error in update item thread: {e}", exc_info=True)
            self._log(f"Ошибка обновления: {e}")
            self._update_scanner_status("Готов", "green")

    def _on_profit_filter_toggle(self):
        """Обработка переключения чекбокса фильтра по профиту."""
        if self.profit_filter_enabled_var.get():
            # Фильтр включен - активируем поле ввода
            self.parser_min_profit_entry.configure(state="normal")
        else:
            # Фильтр выключен - отключаем поле ввода
            self.parser_min_profit_entry.configure(state="disabled")

        # Автоматически применяем фильтр при переключении
        self._apply_profit_filter()

    def _apply_profit_filter(self):
        """Применить фильтр по минимальному профиту."""
        try:
            if self.profit_filter_enabled_var.get():
                # Фильтр включен - проверяем и сохраняем значение
                min_profit = float(self.parser_min_profit_entry.get())
                self.bot_config['scanner_min_profit'] = min_profit
                self._log(f"✅ Фильтр включен: мин. профит {min_profit}%")
            else:
                # Фильтр выключен - показываем все предметы
                self._log("✅ Фильтр по профиту выключен - показываем ВСЕ предметы")

            self._refresh_profitable_items()
        except ValueError:
            self._log("❌ Неверное значение минимального профита")

    def _load_proxies(self, proxy_file: str) -> list[str]:
        """Загрузить прокси из файла."""
        proxies = []
        proxy_path = Path(proxy_file)

        if not proxy_path.exists():
            self._log(f"ℹ️ Файл прокси не найден: {proxy_file}. Работаем без прокси.")
            return proxies

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
                self._log(f"✅ Загружено {len(proxies)} прокси из {proxy_file}")
            else:
                self._log(f"ℹ️ Файл прокси пуст: {proxy_file}. Работаем без прокси.")
        except Exception as e:
            self._log(f"❌ Ошибка чтения файла прокси: {e}. Работаем без прокси.")

        return proxies

    def _start_scanner(self):
        """Запустить сканер выгодных предметов."""
        # Prevent multiple simultaneous scans
        if hasattr(self, 'scanner_running') and self.scanner_running:
            self._log("⚠️ Сканер уже работает!")
            return

        self.scanner_running = True
        self._log("🔄 Запуск сканера TM Parser...")
        self.scanner_status_label.configure(text="Статус: запуск...", text_color="yellow")

        # Run scanner in separate thread to not block GUI
        scanner_thread = threading.Thread(target=self._run_scanner_subprocess, daemon=True)
        scanner_thread.start()

    def _rescan_all_items(self):
        """Пересканировать все предметы из БД."""
        if hasattr(self, 'scanner_running') and self.scanner_running:
            self._log("⚠️ Сканер уже работает!")
            return

        # Получаем все предметы из БД
        all_items = trades_db.get_all_profitable_items()

        if not all_items:
            self._log("⚠️ В базе нет предметов для пересканирования")
            return

        self._log(f"🔁 Пересканирование {len(all_items)} предметов из базы...")
        self._log("⚠️ Это может занять некоторое время (проверка актуальности цен)...")

        # Run rescan in background thread
        self.scanner_running = True
        self.scanner_status_label.configure(text="Статус: пересканирование...", text_color="yellow")
        rescan_thread = threading.Thread(target=self._rescan_items_thread, args=(all_items,), daemon=True)
        rescan_thread.start()

    def _rescan_items_thread(self, items: list[dict]):
        """Thread function for rescanning items."""
        try:
            from src.steam_client import SteamClient
            from src.csgotm_client import CsgoTmClient
            import requests
            import random
            import os

            # Загружаем прокси
            proxy_file = self.bot_config.get('proxy_file', 'proxies.txt')
            proxies_list = self._load_proxies(proxy_file)

            # Try to use existing logged-in account if available
            steam_client = None
            csgotm_client = None
            account_currency = None

            if self.account_manager and len(self.account_manager.accounts) > 0:
                account = self.account_manager.accounts[0]

                # Try to login if not logged in
                if not account.is_logged_in():
                    self._log(f"⚠️ Аккаунт {account.name} не залогинен, попытка входа...")
                    try:
                        if account.login():
                            self._log(f"✅ Успешный вход в аккаунт {account.name}")
                        else:
                            self._log(f"⚠️ Не удалось войти в аккаунт {account.name}")
                    except Exception as e:
                        self._log(f"⚠️ Ошибка входа: {e}")

                # Use account's client if logged in
                if account.is_logged_in():
                    steam_client = account.steam_client
                    csgotm_client = account.csgotm_client
                    account_currency = account.config.currency
                    self._log(f"✅ Используем аккаунт {account.name}, валюта: {account_currency}")

            # If no logged-in account, create clients without auth (for public price checks)
            # Create with proxy support
            if not steam_client:
                self._log(f"⚠️ Нет залогиненного аккаунта, используем публичный доступ (валюта: RUB по умолчанию)")
                steam_client = SteamClient()
                # Set proxy session if available
                if proxies_list:
                    proxy = random.choice(proxies_list)
                    session = requests.Session()
                    session.proxies = {'http': proxy, 'https': proxy}
                    steam_client._session = session
                    steam_client.REQUEST_DELAY = 0.5
                    self._log(f"ℹ️ Используем прокси для пересканирования")
                else:
                    self._log(f"ℹ️ Пересканирование без прокси (возможны ограничения)")

            if not csgotm_client:
                # Use same session for CSGO.TM
                session = steam_client._session if hasattr(steam_client, '_session') else None
                csgotm_api_key = os.getenv('CSGOTM_API_KEY', self.bot_config.get('csgo_tm_api_key', ''))
                csgotm_client = CsgoTmClient(api_key=csgotm_api_key, session=session)

            still_profitable = 0
            no_longer_profitable = 0
            errors = 0

            for idx, item in enumerate(items, 1):
                try:
                    item_name = item['market_hash_name']
                    self._log(f"[{idx}/{len(items)}] Проверка: {item_name}")

                    # Get current Steam price
                    histogram = steam_client.get_market_histogram(item_name)
                    if not histogram or not histogram.get('highest_buy_order'):
                        self._log(f"  ⚠️ Нет данных Steam")
                        errors += 1
                        continue

                    steam_buy_order = histogram['highest_buy_order']
                    steam_lowest_sell = histogram.get('lowest_sell_order')

                    # Check for fake profit
                    if steam_lowest_sell and steam_buy_order >= steam_lowest_sell:
                        self._log(f"  ❌ Фейк профит: buy {steam_buy_order:.0f} >= sell {steam_lowest_sell:.0f}")
                        trades_db.mark_items_inactive([item_name])
                        no_longer_profitable += 1
                        continue

                    # Get current CSGO.TM price
                    csgotm_price_data = csgotm_client.get_item_price(item_name)
                    if not csgotm_price_data or not csgotm_price_data.get('min_price'):
                        self._log(f"  ⚠️ Нет данных CSGO.TM")
                        errors += 1
                        continue

                    csgotm_price = csgotm_price_data['min_price']

                    # Log prices for debugging
                    steam_sell_str = f"{steam_lowest_sell:.2f}" if steam_lowest_sell else "N/A"
                    self._log(f"  💰 Цены: Steam buy={steam_buy_order:.2f}, sell={steam_sell_str}, CSGO.TM={csgotm_price:.2f}")

                    # Calculate profit
                    net_revenue = csgotm_price * 0.93
                    profit_pct = ((net_revenue - steam_buy_order) / steam_buy_order) * 100

                    # Check if still profitable
                    min_profit = float(self.scanner_profit_entry.get() or -5.0)
                    if profit_pct < min_profit:
                        self._log(f"  ❌ Больше не выгоден: профит {profit_pct:.1f}% < {min_profit}%")
                        trades_db.mark_items_inactive([item_name])
                        no_longer_profitable += 1
                    else:
                        self._log(f"  ✅ Актуален: профит {profit_pct:.1f}%")
                        # Update prices in DB
                        trades_db.add_or_update_profitable_item({
                            'market_hash_name': item_name,
                            'item_type': item.get('item_type', 'unknown'),
                            'steam_buy_order': steam_buy_order,
                            'recommended_buy_order': steam_buy_order,
                            'csgo_price': csgotm_price,
                            'csgo_buy_order': csgotm_price_data.get('buy_order', 0),
                            'instant_profit_pct': profit_pct,
                            'wait_profit_pct': profit_pct,
                            'recommended_instant_pct': profit_pct,
                            'recommended_wait_pct': profit_pct,
                            'orders_above': 0,
                        })
                        still_profitable += 1

                    # Small delay to avoid rate limits
                    time.sleep(2)

                except Exception as e:
                    logger.error(f"Error rescanning {item.get('market_hash_name', 'unknown')}: {e}")
                    self._log(f"  ❌ Ошибка: {e}")
                    errors += 1

            self._log(f"\n✅ Пересканирование завершено!")
            self._log(f"Актуальны: {still_profitable}, Удалены: {no_longer_profitable}, Ошибки: {errors}")
            self._update_scanner_status("Статус: пересканирование завершено", "green")
            self._refresh_profitable_items()

        except Exception as e:
            logger.error(f"Rescan error: {e}", exc_info=True)
            self._log(f"❌ Ошибка пересканирования: {e}")
            self._update_scanner_status("Статус: ошибка", "red")
        finally:
            # Clean up session
            if 'steam_client' in locals() and steam_client and hasattr(steam_client, '_session'):
                try:
                    steam_client._session.close()
                except:
                    pass
            self.scanner_running = False

    def _scan_new_items(self):
        """Искать только новые предметы (не в БД)."""
        if hasattr(self, 'scanner_running') and self.scanner_running:
            self._log("⚠️ Сканер уже работает!")
            return

        self._log("⭐ Поиск новых предметов (пропускаем те что уже в БД)...")

        # Get existing items from DB
        existing_items = trades_db.get_all_profitable_items()
        existing_names = {item['market_hash_name'] for item in existing_items}

        self._log(f"ℹ️ В базе {len(existing_names)} предметов, ищем новые...")
        self._log("ℹ️ Запускаю обычное сканирование с фильтрацией...")

        # Store skip list and run normal scanner
        self.skip_existing_items = existing_names
        self._start_scanner()

    def _run_scanner_subprocess(self):
        """Run scanner as subprocess (called from thread)."""
        import subprocess
        import json
        from pathlib import Path

        try:
            # Read settings from GUI
            config = {
                'min_price': float(self.min_price_entry.get() or 1000),
                'max_price': float(self.max_price_entry.get() or 10000),
                'min_profit': float(self.scanner_profit_entry.get() or -5.0),
                'min_sales_7d': int(self.sales_7d_entry.get() or 50),
                'proxy_file': self.proxy_file_entry.get() or 'proxies.txt',
                'requests_per_proxy': int(self.requests_per_proxy_entry.get() or 15),
                'max_items': int(self.max_items_entry.get() or 100),
                'delay': float(self.delay_entry.get() or 1.5),
                'workers': int(self.workers_entry.get() or 3),
            }

            self._log(f"📊 Настройки: цена {config['min_price']}-{config['max_price']}, мин. профит {config['min_profit']}%")
            self._log(f"📊 Макс. предметов: {config['max_items']}, задержка: {config['delay']}s, воркеров: {config['workers']}")

            # Save config to JSON file
            config_file = Path("scanner_config.json")
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)

            # Start subprocess
            import sys
            process = subprocess.Popen(
                [sys.executable, "-m", "src.bottm.scanner_process"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            # Read output line by line
            for line in process.stdout:
                line = line.strip()
                if line:
                    # Parse log line and display in GUI
                    # Format: "HH:MM:SS [LEVEL] module: message"
                    if '[INFO]' in line or '[WARNING]' in line or '[ERROR]' in line:
                        # Extract just the message part
                        parts = line.split(':', 2)
                        if len(parts) >= 3:
                            message = parts[2].strip()
                        else:
                            message = line
                        self._log(message)
                    else:
                        self._log(line)

            # Wait for process to complete
            return_code = process.wait()

            if return_code == 0:
                self._log("✅ Сканирование завершено успешно!")
                self._update_scanner_status("Статус: завершено", "green")
                self._refresh_profitable_items()
            else:
                self._log(f"❌ Сканер завершился с ошибкой (код: {return_code})")
                self._update_scanner_status("Статус: ошибка", "red")

        except Exception as e:
            logger.error(f"Scanner subprocess error: {e}", exc_info=True)
            self._log(f"❌ Ошибка сканера: {e}")
            self._update_scanner_status("Статус: ошибка", "red")
        finally:
            self.scanner_running = False

    def _log(self, message: str):
        """Добавить сообщение в логи (thread-safe)."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{timestamp}] {message}\n"

        # Schedule GUI update in main thread
        def update_log():
            try:
                self.logs_text.configure(state="normal")
                self.logs_text.insert("end", log_line)
                self.logs_text.see("end")
                self.logs_text.configure(state="disabled")
            except Exception:
                # Widget might be destroyed, ignore
                pass

        try:
            self.after(0, update_log)
        except Exception:
            # If after() fails, just print to console
            pass

        # Также выводим в консоль (с обработкой кодировки для Windows)
        try:
            print(log_line.strip())
        except UnicodeEncodeError:
            # Если консоль не поддерживает UTF-8, выводим без эмодзи
            print(log_line.strip().encode('ascii', 'ignore').decode('ascii'))

    def _update_scanner_status(self, text: str, color: str):
        """Update scanner status label (thread-safe)."""
        def update():
            try:
                self.scanner_status_label.configure(text=text, text_color=color)
            except Exception:
                pass

        try:
            self.after(0, update)
        except Exception:
            pass

    def _clear_logs(self):
        """Очистить логи."""
        self.logs_text.configure(state="normal")
        self.logs_text.delete("1.0", "end")
        self.logs_text.configure(state="disabled")

    def _add_account(self):
        """Добавить новый аккаунт (заглушка)."""
        self._log("ℹ️ Функция добавления аккаунта еще не реализована")

    def _toggle_account(self, account):
        """Переключить статус аккаунта (включить/выключить)."""
        # Переключаем статус
        account.config.enabled = not account.config.enabled

        status = "включен" if account.config.enabled else "выключен"
        self._log(f"ℹ️ Аккаунт {account.name} {status}")

        # Обновляем отображение
        self._refresh_accounts_list()

        # Сохраняем изменения в файл конфигурации
        self._save_accounts_config()

    def _save_accounts_config(self):
        """Сохранить текущую конфигурацию аккаунтов в файл."""
        if not self.account_manager:
            return

        import json
        from pathlib import Path

        config_file = Path("accounts.json")

        try:
            # Собираем данные всех аккаунтов
            accounts_data = []
            for account in self.account_manager.accounts:
                acc_data = {
                    "name": account.config.name,
                    "enabled": account.config.enabled,
                    "currency": account.config.currency,
                    "steam": {
                        "username": account.config.steam_username,
                        "password": account.config.steam_password,
                        "api_key": account.config.steam_api_key,
                        "shared_secret": account.config.steam_shared_secret,
                        "identity_secret": account.config.steam_identity_secret
                    },
                    "csgotm": {
                        "api_key": account.config.csgotm_api_key
                    },
                    "limits": {
                        "max_items": account.config.max_items,
                        "max_price_per_item": account.config.max_price_per_item,
                        "total_budget": account.config.total_budget
                    }
                }

                # Добавляем прокси, если есть
                if account.config.proxy:
                    acc_data["proxy"] = account.config.proxy

                accounts_data.append(acc_data)

            # Сохраняем в файл с красивым форматированием
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(accounts_data, f, indent=2, ensure_ascii=False)

            logger.info(f"Accounts config saved to {config_file}")

        except Exception as e:
            logger.error(f"Failed to save accounts config: {e}")
            self._log(f"❌ Ошибка сохранения конфигурации: {e}")

    def _detect_account_currency(self, account):
        """
        Определить валюту аккаунта автоматически из Steam используя SteamWalletChecker.

        Args:
            account: Account instance
        """
        def detect_thread():
            try:
                from src.steam_wallet_checker import SteamWalletBalance

                self._log(f"Определение валюты для аккаунта {account.name}...")

                # Проверяем наличие необходимых данных
                if not account.config.steam_username or not account.config.steam_password:
                    self._log(f"Отсутствуют username или password для {account.name}")
                    return

                if not account.config.steam_shared_secret:
                    self._log(f"Отсутствует shared_secret для {account.name}")
                    self._log(f"Для автоматического определения валюты нужен shared_secret")
                    return

                self._log(f"Используем SteamWalletChecker с 2FA...")
                self._log(f"  Username: {account.config.steam_username}")
                self._log(f"  Shared secret: {'***' if account.config.steam_shared_secret else 'NO'}")

                # Add delay to avoid rate limiting
                import time
                self._log("Ожидание 10 секунд перед запуском SteamWalletChecker...")
                time.sleep(10)

                # Используем SteamWalletBalance для получения баланса и валюты
                checker = SteamWalletBalance(
                    username=account.config.steam_username,
                    password=account.config.steam_password,
                    shared_secret=account.config.steam_shared_secret,
                    identity_secret=account.config.steam_identity_secret,
                    steamid=account.config.steamid
                )

                self._log(f"Получение баланса через SteamKit2...")
                result = checker.get_balance()

                if result['success']:
                    detected_currency = result['currency']
                    balance = result['balance']
                    balance_usd = result.get('balance_usd', 0)
                    country_code = result.get('country_code', 'N/A')

                    self._log(f"Валюта: {detected_currency}, Баланс: {balance} {detected_currency}")
                    self._log(f"USD эквивалент: ${balance_usd:.2f}")
                    self._log(f"Страна: {country_code}")

                    # Обновляем валюту в конфигурации аккаунта
                    account.config.currency = detected_currency

                    # ВАЖНО: Сохраняем баланс в аккаунт, чтобы не делать повторные запросы
                    if not hasattr(account, '_cached_steam_balance'):
                        account._cached_steam_balance = {}
                    account._cached_steam_balance = {
                        'balance': balance,
                        'currency': detected_currency,
                        'balance_usd': balance_usd,
                        'timestamp': time.time()
                    }

                    # Обновляем отображение
                    self.after(0, self._refresh_accounts_list)

                    # Обновляем баланс в GUI
                    if account.name in self.account_balance_labels:
                        try:
                            csgotm_balance = account.get_csgotm_balance()
                            if detected_currency != 'RUB':
                                csgotm_converted = currency_converter.convert_from_rub(csgotm_balance, detected_currency)
                                balance_text = f"💰 Steam: {balance:.2f} {detected_currency} | TM: {csgotm_converted:.2f} {detected_currency} ({csgotm_balance:.2f} RUB)"
                            else:
                                balance_text = f"💰 Steam: {balance:.2f} {detected_currency} | TM: {csgotm_balance:.2f} {detected_currency}"
                            self.after(0, lambda: self.account_balance_labels[account.name].configure(text=balance_text))
                        except Exception as e:
                            logger.error(f"Failed to update balance display: {e}")

                    # Сохраняем изменения
                    self._save_accounts_config()
                else:
                    error_msg = result.get('error', 'Unknown error')
                    self._log(f"Не удалось получить баланс: {error_msg}")
                    self._log(f"Проверьте правильность username, password и shared_secret")

            except Exception as e:
                logger.error(f"Error detecting currency for {account.name}: {e}")
                import traceback
                traceback.print_exc()
                self._log(f"Ошибка определения валюты: {e}")

        # Запускаем в отдельном потоке
        thread = threading.Thread(target=detect_thread, daemon=True)
        thread.start()

    # ============================================================
    # НОВЫЕ МЕТОДЫ: Auto Buyer, Auto Scanner, Statistics
    # ============================================================

    def _toggle_auto_scan(self):
        """Переключить автосканирование."""
        if self.auto_scanner and self.auto_scanner.is_running():
            # Остановить
            self.auto_scanner.stop()
            self.auto_scan_toggle_btn.configure(
                text="⏰ Auto Scan: OFF",
                fg_color="#95A5A6"
            )
            self._log("⏰ Автосканирование остановлено")
        else:
            # Запустить
            interval = self.bot_config.get('auto_scan_interval', 30)

            # Создаём AutoScanner если его нет
            if not self.auto_scanner:
                self.auto_scanner = AutoScanner(
                    scan_callback=self._start_scanner,
                    interval_minutes=interval,
                    enabled=False
                )

            self.auto_scanner.set_interval(interval)
            self.auto_scanner.start()

            self.auto_scan_toggle_btn.configure(
                text=f"⏰ Auto Scan: ON ({interval}m)",
                fg_color="#27AE60"
            )
            self._log(f"⏰ Автосканирование запущено (интервал: {interval} минут)")

    def _show_statistics_window(self):
        """Показать окно статистики."""
        # Создаём окно статистики
        stats_window = ctk.CTkToplevel(self)
        stats_window.title("Статистика торговли")
        stats_window.geometry("900x700")
        stats_window.transient(self)

        # Создаём TradingStatistics если его нет
        if not self.statistics:
            self.statistics = TradingStatistics(trades_db)

        # Заголовок
        header_frame = ctk.CTkFrame(stats_window)
        header_frame.pack(fill="x", padx=20, pady=20)

        header = ctk.CTkLabel(
            header_frame,
            text="Статистика торговли",
            font=ctk.CTkFont(size=20, weight="bold")
        )
        header.pack(side="left")

        # Выбор периода
        period_frame = ctk.CTkFrame(stats_window)
        period_frame.pack(fill="x", padx=20, pady=(0, 10))

        ctk.CTkLabel(period_frame, text="Период:", font=ctk.CTkFont(size=14)).pack(side="left", padx=10)

        period_var = ctk.StringVar(value="30")
        period_options = ctk.CTkOptionMenu(
            period_frame,
            values=["7", "14", "30", "60", "90", "365"],
            variable=period_var,
            width=100,
            font=ctk.CTkFont(size=13)
        )
        period_options.pack(side="left", padx=5)

        ctk.CTkLabel(period_frame, text="дней", font=ctk.CTkFont(size=14)).pack(side="left", padx=5)

        # Кнопка обновления
        refresh_btn = ctk.CTkButton(
            period_frame,
            text="Обновить",
            command=lambda: update_report(),
            width=120,
            height=32,
            font=ctk.CTkFont(size=13)
        )
        refresh_btn.pack(side="right", padx=10)

        # Scrollable frame для статистики
        stats_scroll = ctk.CTkScrollableFrame(stats_window)
        stats_scroll.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        # Контейнеры для данных
        summary_label = ctk.CTkLabel(
            stats_scroll,
            text="",
            font=ctk.CTkFont(size=13),
            anchor="w",
            justify="left"
        )
        summary_label.pack(fill="both", expand=True, padx=10, pady=10)

        def update_report():
            try:
                days = int(period_var.get())
                summary = self.statistics.get_summary(days=days)

                if not summary:
                    summary_label.configure(text="Нет данных для отображения")
                    return

                # Форматируем статистику
                text = f"""
ОТЧЁТ О ТОРГОВЛЕ (за {summary['period_days']} дней)
{'=' * 60}

ПОКУПКИ И ПРОДАЖИ:
  Куплено:              {summary['items_purchased']} шт
  Продано:              {summary['items_sold']} шт

АКТИВНЫЕ ПОЗИЦИИ:
  Buy ордеров на Steam: {summary.get('active_orders', 0)} шт
  Выгодных предметов:   {summary['active_profitable_items']} шт

ФИНАНСЫ:
  Потрачено:            {summary['total_spent']:.2f} ₽
  Получено:             {summary['total_earned']:.2f} ₽
  Профит:               {summary['total_profit']:.2f} ₽

ЭФФЕКТИВНОСТЬ:
  ROI:                  {summary['roi_percent']:.2f} %
  Ср. профит:           {summary['avg_profit_per_trade']:.2f} ₽/сделка

{'=' * 60}

Примечания:
- "Buy ордеров" - активные заявки на покупку на Steam Market
- "Выгодных предметов" - найденные предметы с профитом в TM Parser
"""
                summary_label.configure(text=text)

            except Exception as e:
                logger.error(f"Error updating report: {e}", exc_info=True)
                summary_label.configure(text=f"Ошибка загрузки статистики: {e}")

        # Загружаем отчёт
        update_report()

    def _export_data(self):
        """Экспортировать данные в CSV."""
        def export_thread():
            try:
                self._log("📤 Экспорт данных...")

                # Создаём TradingStatistics если его нет
                if not self.statistics:
                    self.statistics = TradingStatistics(trades_db)

                # Создаём папку exports
                export_dir = Path("exports")
                export_dir.mkdir(exist_ok=True)

                # Генерируем имя файла с датой
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_file = export_dir / f"trading_data_{timestamp}.csv"

                # Экспортируем все данные
                success = self.statistics.export_to_csv(
                    output_file=str(output_file),
                    data_type='all',
                    days=30
                )

                if success:
                    self._log(f"✅ Данные экспортированы в {output_file.parent}")
                    self._log(f"   - {output_file.stem}_purchased.csv")
                    self._log(f"   - {output_file.stem}_sold.csv")
                    self._log(f"   - {output_file.stem}_profitable.csv")
                else:
                    self._log("❌ Ошибка экспорта данных")

            except Exception as e:
                logger.error(f"Error exporting data: {e}", exc_info=True)
                self._log(f"❌ Ошибка экспорта: {e}")

        thread = threading.Thread(target=export_thread, daemon=True)
        thread.start()

    def _on_closing(self):
        """Обработка закрытия окна."""
        if self.bot_running:
            self._stop_bot()
            time.sleep(1)

        self.destroy()


def main():
    """Запуск GUI приложения."""
    app = TradingBotGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
