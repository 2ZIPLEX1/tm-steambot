"""
Trading Bot - основная логика автоматической торговли.

Workflow:
1. Читает profitable_items из bottm.db
2. Фильтрует предметы (положительный профит, в рамках лимитов)
3. Создает бай-ордера на Steam Market
4. Отслеживает исполненные ордера
5. Добавляет купленные предметы в БД (7-дневный холд)
6. Проверяет предметы после холда
7. Продает на CSGO.TM
"""

import time
from datetime import datetime, timedelta
from typing import Optional

from src.logger import get_logger
from src.account_manager import Account
from src.database import trades_db
from src.bottm_parser import bottm_parser
from config import settings

logger = get_logger(__name__)


class TradingBot:
    """
    Бот для автоматической торговли Steam Market → CSGO.TM.
    """

    # Минимальный профит для покупки (%)
    MIN_PROFIT_PCT = 5.0

    # Минимальная и максимальная цена предмета для выставления ордеров (RUB)
    TRADE_MIN_PRICE = 100.0
    TRADE_MAX_PRICE = 5000.0

    # Задержка между проверками ордеров (секунды)
    ORDER_CHECK_INTERVAL = 60

    def __init__(self, account: Account):
        """
        Initialize trading bot for an account.

        Args:
            account: Account instance with logged in clients
        """
        self.account = account
        self.name = account.name

        logger.info(f"[{self.name}] Trading bot initialized")

    # ============ Проверка актуальности цен ============

    def _verify_item_profitability(self, item: dict) -> dict:
        """
        Проверить актуальность профита перед созданием ордера.

        Получает текущие цены с Steam Market и CSGO.TM,
        пересчитывает профит и рекомендует цену для ордера.

        Args:
            item: Item data from database

        Returns:
            Dict with:
            - is_profitable: bool (стоит ли создавать ордер)
            - current_steam_price: float (текущая цена на Steam)
            - current_csgotm_price: float (текущая цена на CSGO.TM)
            - current_profit_pct: float (текущий профит в %)
            - recommended_price: float (рекомендуемая цена для ордера)
            - reason: str (причина, если not profitable)
        """
        item_name = item['name']

        try:
            logger.info(f"[{self.name}] 🔍 Проверка актуальности цен для {item_name}...")

            # Получаем текущие цены на Steam Market
            histogram = self.account.steam_client.get_market_histogram(item_name)
            if not histogram or not histogram.get('highest_buy_order'):
                return {
                    'is_profitable': False,
                    'reason': 'Не удалось получить данные Steam Market'
                }

            current_steam_price = histogram['highest_buy_order']

            # Получаем текущие цены на CSGO.TM
            if not self.account.csgotm_client:
                return {
                    'is_profitable': False,
                    'reason': 'CSGO.TM клиент не инициализирован'
                }

            csgotm_price_data = self.account.csgotm_client.get_item_price(item_name)
            if not csgotm_price_data or not csgotm_price_data.get('min_price'):
                return {
                    'is_profitable': False,
                    'reason': 'Не удалось получить данные CSGO.TM'
                }

            current_csgotm_price = csgotm_price_data['min_price']

            # Рассчитываем профит с учетом комиссии CSGO.TM (7%)
            # Чистая выручка = цена продажи - комиссия 7%
            net_revenue = current_csgotm_price * 0.93

            # Профит = (выручка - затраты) / затраты * 100%
            if current_steam_price > 0:
                current_profit_pct = ((net_revenue - current_steam_price) / current_steam_price) * 100
            else:
                current_profit_pct = 0

            # Проверка на фейковые предметы
            # Сравниваем текущую цену Steam с рекомендованной из базы
            recommended_price = item.get('recommended_price', current_steam_price)
            if recommended_price > 0:
                price_diff_pct = abs(current_steam_price - recommended_price) / recommended_price * 100
                if price_diff_pct > 20:  # Если разница больше 20%
                    return {
                        'is_profitable': False,
                        'current_steam_price': current_steam_price,
                        'current_csgotm_price': current_csgotm_price,
                        'current_profit_pct': current_profit_pct,
                        'reason': f'Подозрение на фейк: цена отличается на {price_diff_pct:.1f}% (текущая {current_steam_price:.2f} vs рекомендованная {recommended_price:.2f})'
                    }

            # Проверяем минимальный профит
            if current_profit_pct < self.MIN_PROFIT_PCT:
                return {
                    'is_profitable': False,
                    'current_steam_price': current_steam_price,
                    'current_csgotm_price': current_csgotm_price,
                    'current_profit_pct': current_profit_pct,
                    'reason': f'Профит слишком низкий: {current_profit_pct:.1f}% < {self.MIN_PROFIT_PCT}%'
                }

            # Проверяем цену в диапазоне
            if current_steam_price < self.TRADE_MIN_PRICE:
                return {
                    'is_profitable': False,
                    'current_steam_price': current_steam_price,
                    'current_csgotm_price': current_csgotm_price,
                    'current_profit_pct': current_profit_pct,
                    'reason': f'Цена слишком низкая: {current_steam_price:.2f} < {self.TRADE_MIN_PRICE}'
                }

            if current_steam_price > self.TRADE_MAX_PRICE:
                return {
                    'is_profitable': False,
                    'current_steam_price': current_steam_price,
                    'current_csgotm_price': current_csgotm_price,
                    'current_profit_pct': current_profit_pct,
                    'reason': f'Цена слишком высокая: {current_steam_price:.2f} > {self.TRADE_MAX_PRICE}'
                }

            # Все проверки пройдены!
            logger.info(
                f"[{self.name}] ✅ Предмет актуален: Steam {current_steam_price:.2f} → "
                f"CSGO.TM {current_csgotm_price:.2f} (профит: {current_profit_pct:.1f}%)"
            )

            return {
                'is_profitable': True,
                'current_steam_price': current_steam_price,
                'current_csgotm_price': current_csgotm_price,
                'current_profit_pct': current_profit_pct,
                'recommended_price': current_steam_price,
            }

        except Exception as e:
            logger.error(f"[{self.name}] Ошибка проверки актуальности: {e}")
            return {
                'is_profitable': False,
                'reason': f'Ошибка: {e}'
            }

    # ============ Шаг 1: Получение прибыльных предметов ============

    def get_profitable_items(self, limit: Optional[int] = None) -> list[dict]:
        """
        Получить прибыльные предметы из bottm.db.

        Args:
            limit: Максимум предметов (None = без ограничения)

        Returns:
            List of profitable items as dicts
        """
        try:
            # Получаем все предметы из bottm
            items = bottm_parser.get_all_items()

            if not items:
                logger.warning(f"[{self.name}] No items found in bottm.db")
                return []

            # Фильтруем по профиту
            profitable = []
            for item in items:
                # Проверяем валидность цен
                if not item.has_valid_prices:
                    continue

                # Проверяем минимальный профит (используем встроенный метод)
                if not item.is_profitable(min_profit_pct=self.MIN_PROFIT_PCT):
                    continue

                # Проверяем лимит цены
                if item.recommended_buy_order > self.account.config.max_price_per_item:
                    continue

                # Конвертируем ItemData в словарь для удобства
                item_dict = {
                    'name': item.market_hash_name,  # Alias для удобства
                    'market_hash_name': item.market_hash_name,
                    'steam_price': item.recommended_buy_order,
                    'csgo_price': item.csgo_price,
                    'csgotm_price': item.csgo_price,  # Alias для совместимости
                    'profit_pct': item.net_profit_pct,
                    'net_profit': item.net_profit_after_commission,
                    'item_type': item.item_type,
                    'orders_above': item.orders_above,
                }
                profitable.append(item_dict)

            # Сортируем по профиту (убывание)
            profitable.sort(key=lambda x: x['profit_pct'], reverse=True)

            # Применяем лимит
            if limit:
                profitable = profitable[:limit]

            logger.info(
                f"[{self.name}] Found {len(profitable)} profitable items "
                f"(min profit: {self.MIN_PROFIT_PCT}%)"
            )

            return profitable

        except Exception as e:
            logger.error(f"[{self.name}] Failed to get profitable items: {e}")
            return []

    # ============ Шаг 2: Создание бай-ордеров ============

    def create_buy_orders(self, max_items: Optional[int] = None) -> dict:
        """
        Создать бай-ордера на прибыльные предметы.

        Args:
            max_items: Максимум предметов для покупки

        Returns:
            Dict with success/failed counts
        """
        logger.info(f"[{self.name}] Creating buy orders...")

        # Получаем Steam ID и identity_secret один раз в начале
        steamid = self.account.steam_client.get_steamid()
        identity_secret = self.account.config.steam_identity_secret

        if not steamid or not identity_secret:
            logger.warning(f"[{self.name}] Steam ID or identity_secret not available, confirmations may fail")

        # Проверяем баланс (используем кешированный если есть, чтобы не делать лишние запросы)
        if hasattr(self.account, '_cached_steam_balance') and self.account._cached_steam_balance:
            balance = self.account._cached_steam_balance.get('balance', 0.0)
            logger.debug(f"[{self.name}] Using cached balance: {balance:.2f}")
        else:
            # Если кеша нет, запрашиваем баланс (но лучше сначала нажать "Определить валюту")
            balance = self.account.get_wallet_balance()
            logger.debug(f"[{self.name}] Fetched balance from Steam: {balance:.2f}")

        if balance < 1.0:
            logger.warning(f"[{self.name}] Insufficient balance: {balance:.2f}. Tip: Click '🔍 Валюта' button to detect currency and balance.")
            return {'success': 0, 'failed': 0, 'skipped': 0, 'confirmed': 0}

        # Получаем прибыльные предметы
        if max_items is None:
            max_items = self.account.config.max_items

        items = self.get_profitable_items(limit=max_items * 2)  # Берем с запасом

        if not items:
            logger.info(f"[{self.name}] No profitable items to buy")
            return {'success': 0, 'failed': 0, 'skipped': 0, 'confirmed': 0}

        # Создаем ордера
        success_count = 0
        failed_count = 0
        skipped_count = 0
        confirmed_count = 0
        total_spent = 0.0

        for item in items:
            # Проверяем лимиты
            if success_count >= max_items:
                logger.info(f"[{self.name}] Reached max items limit ({max_items})")
                break

            if total_spent >= self.account.config.total_budget:
                logger.info(f"[{self.name}] Reached budget limit ({self.account.config.total_budget})")
                break

            # Проверяем баланс
            steam_price = item.get('steam_price', 0)
            if balance - total_spent < steam_price:
                logger.warning(f"[{self.name}] Insufficient balance for {item['name']}")
                skipped_count += 1
                continue

            # Проверяем цену предмета (фильтр для выставления ордеров)
            if steam_price < self.TRADE_MIN_PRICE:
                logger.debug(f"[{self.name}] Item too cheap: {item['name']} ({steam_price:.2f} < {self.TRADE_MIN_PRICE})")
                skipped_count += 1
                continue

            if steam_price > self.TRADE_MAX_PRICE:
                logger.debug(f"[{self.name}] Item too expensive: {item['name']} ({steam_price:.2f} > {self.TRADE_MAX_PRICE})")
                skipped_count += 1
                continue

            # Проверяем, нет ли уже активного ордера на этот предмет
            existing_order = trades_db.get_order_by_item_name(item['name'])
            if existing_order:
                logger.debug(f"[{self.name}] Order already exists for {item['name']}")
                skipped_count += 1
                continue

            # ВАЖНО: Проверяем актуальность цен перед созданием ордера
            verification = self._verify_item_profitability(item)

            if not verification['is_profitable']:
                logger.warning(
                    f"[{self.name}] ⚠️ Предмет больше не выгоден: {item['name']} - "
                    f"{verification.get('reason', 'unknown')}"
                )
                skipped_count += 1
                continue

            # Обновляем цену на актуальную
            item['steam_price'] = verification['recommended_price']
            item['csgotm_price'] = verification['current_csgotm_price']
            item['profit_pct'] = verification['current_profit_pct']
            steam_price = item['steam_price']  # Обновляем локальную переменную

            # Создаем ордер
            try:
                result = self._create_buy_order_for_item(item)

                if result['success']:
                    success_count += 1
                    total_spent += steam_price
                    logger.info(
                        f"[{self.name}] ✅ Order created: {item['name']} @ {steam_price:.2f} "
                        f"(profit: {item['profit_pct']:.1f}%)"
                    )

                    # ВАЖНО: Подтверждаем ордер сразу после создания
                    # Steam блокирует новые ордера, если предыдущие не подтверждены
                    if steamid and identity_secret:
                        logger.info(f"[{self.name}] Confirming order...")
                        time.sleep(3)  # Даем Steam время зарегистрировать ордер

                        confirm_result = self.account.steam_client.confirm_all_market_transactions(
                            identity_secret=identity_secret,
                            steamid=steamid
                        )

                        if confirm_result > 0:
                            confirmed_count += confirm_result
                            logger.info(f"[{self.name}] ✅ Order confirmed")
                        else:
                            logger.warning(f"[{self.name}] No confirmation found (may not require confirmation)")
                else:
                    failed_count += 1
                    logger.warning(f"[{self.name}] ❌ Order failed: {item['name']}")

                # Задержка между ордерами (rate limiting)
                time.sleep(2)

            except Exception as e:
                logger.error(f"[{self.name}] Error creating order for {item['name']}: {e}")
                failed_count += 1

        logger.info(
            f"[{self.name}] Buy orders complete: "
            f"{success_count} success, {failed_count} failed, {skipped_count} skipped, "
            f"{confirmed_count} confirmed"
        )

        return {
            'success': success_count,
            'failed': failed_count,
            'skipped': skipped_count,
            'total_spent': total_spent,
            'confirmed': confirmed_count,
        }

    def _create_buy_order_for_item(self, item: dict) -> dict:
        """
        Создать бай-ордер для одного предмета.

        Args:
            item: Item data from bottm.db

        Returns:
            Dict with success status and order_id
        """
        market_hash_name = item['name']
        price = item['steam_price']
        expected_sell_price = item.get('csgotm_price', 0)

        try:
            # Создаем ордер через Steam client
            result = self.account.steam_client.create_buy_order(
                market_hash_name=market_hash_name,
                price=price,
                quantity=1
            )

            if result.success and result.order_id:
                # Добавляем в БД
                trades_db.add_order(
                    account_name=self.name,
                    item_name=market_hash_name,
                    market_hash_name=market_hash_name,
                    order_id=result.order_id,
                    order_price=price,
                    quantity=1,
                    expected_sell_price=expected_sell_price,
                )

                return {'success': True, 'order_id': result.order_id}
            else:
                return {'success': False, 'message': result.message}

        except Exception as e:
            logger.error(f"[{self.name}] Failed to create order: {e}")
            return {'success': False, 'message': str(e)}

    # ============ Шаг 3: Проверка исполненных ордеров ============

    def check_filled_orders(self) -> int:
        """
        Проверить исполненные ордера и добавить предметы в purchased_items.

        Returns:
            Count of newly filled orders
        """
        logger.info(f"[{self.name}] Checking filled orders...")

        # Получаем активные ордера из БД
        active_orders = [
            order for order in trades_db.get_active_orders()
            if order['account_name'] == self.name
        ]

        if not active_orders:
            logger.debug(f"[{self.name}] No active orders to check")
            return 0

        filled_count = 0

        for order in active_orders:
            try:
                # Проверяем статус ордера в Steam
                # TODO: Implement order status check in steam_client
                # For now, we'll check inventory for the item
                is_filled = self._check_order_filled(order)

                if is_filled:
                    # Отмечаем ордер как исполненный
                    trades_db.update_order_status(order['order_id'], 'filled')

                    # Добавляем в purchased_items
                    trades_db.add_purchased_item(
                        account_name=self.name,
                        item_name=order['item_name'],
                        market_hash_name=order.get('market_hash_name', order['item_name']),
                        purchase_price=order['order_price'],
                        expected_sell_price=order.get('expected_sell_price'),
                        order_id=order['order_id'],
                    )

                    filled_count += 1
                    logger.info(f"[{self.name}] ✅ Order filled: {order['item_name']}")

                time.sleep(2)  # Rate limiting

            except Exception as e:
                logger.error(f"[{self.name}] Error checking order {order['order_id']}: {e}")

        if filled_count > 0:
            logger.info(f"[{self.name}] {filled_count} orders filled")

        return filled_count

    def _check_order_filled(self, order: dict) -> bool:
        """
        Проверить, исполнен ли ордер (есть ли предмет в инвентаре).

        Args:
            order: Order data with 'market_hash_name' field

        Returns:
            True if order is filled (item found in inventory)
        """
        try:
            # Получаем инвентарь Steam
            inventory = self.account.steam_client.get_inventory()

            if not inventory:
                logger.debug(f"[{self.name}] Inventory is empty or failed to fetch")
                return False

            # Ищем предмет по market_hash_name
            market_hash_name = order.get('market_hash_name', '')
            if not market_hash_name:
                logger.warning(f"[{self.name}] Order #{order.get('id')} has no market_hash_name")
                return False

            # Проверяем есть ли предмет в инвентаре
            for item in inventory:
                if item.market_hash_name == market_hash_name:
                    logger.info(
                        f"[{self.name}] ✅ Order filled! Found '{market_hash_name}' "
                        f"in inventory (asset_id: {item.asset_id})"
                    )
                    return True

            # Предмет не найден
            logger.debug(f"[{self.name}] Item '{market_hash_name}' not yet in inventory")
            return False

        except Exception as e:
            logger.error(f"[{self.name}] Error checking order status: {e}")
            return False

    # ============ Шаг 4: Продажа предметов после холда ============

    def sell_ready_items(self) -> int:
        """
        Продать предметы, прошедшие 7-дневный холд.

        Returns:
            Count of items listed for sale
        """
        logger.info(f"[{self.name}] Checking items ready to sell...")

        # Получаем предметы, готовые к продаже
        ready_items = [
            item for item in trades_db.get_items_ready_to_sell()
            if item['account_name'] == self.name
        ]

        if not ready_items:
            logger.debug(f"[{self.name}] No items ready to sell")
            return 0

        listed_count = 0

        for item in ready_items:
            try:
                # Получаем актуальную цену на CSGO.TM
                current_price = self._get_csgotm_sell_price(item['item_name'])

                if not current_price or current_price < item['purchase_price']:
                    logger.warning(
                        f"[{self.name}] Skipping {item['item_name']}: "
                        f"price too low ({current_price} < {item['purchase_price']})"
                    )
                    continue

                # Продаем на CSGO.TM
                result = self._sell_item_on_csgotm(item, current_price)

                if result['success']:
                    # Обновляем статус в БД
                    trades_db.update_purchased_item_status(item['id'], 'listed')

                    # Добавляем в listed_items
                    trades_db.add_listed_item(
                        account_name=self.name,
                        item_name=item['item_name'],
                        market_hash_name=item.get('market_hash_name', item['item_name']),
                        asset_id=item.get('asset_id'),
                        list_price=current_price,
                        purchase_price=item['purchase_price'],
                    )

                    listed_count += 1
                    logger.info(f"[{self.name}] ✅ Listed for sale: {item['item_name']} @ {current_price:.2f}")

                time.sleep(5)  # Rate limiting

            except Exception as e:
                logger.error(f"[{self.name}] Error selling item {item['item_name']}: {e}")

        if listed_count > 0:
            logger.info(f"[{self.name}] {listed_count} items listed for sale")

        return listed_count

    def _get_csgotm_sell_price(self, item_name: str) -> Optional[float]:
        """
        Получить актуальную цену продажи на CSGO.TM из bottm.db.

        Args:
            item_name: Market hash name

        Returns:
            Price or None
        """
        try:
            item = bottm_parser.get_item_by_name(item_name)
            if item:
                return item.get('csgotm_price')
            return None
        except Exception as e:
            logger.error(f"[{self.name}] Failed to get CSGO.TM price for {item_name}: {e}")
            return None

    def _sell_item_on_csgotm(self, item: dict, price: float) -> dict:
        """
        Выставить предмет на продажу на CSGO.TM.

        Process:
        1. Find item in Steam inventory
        2. Call CSGO.TM set_price API (creates trade offer from bot)
        3. Item will be listed after accepting trade offer

        Args:
            item: Item data from purchased_items with 'market_hash_name'
            price: Sale price in USD

        Returns:
            Dict with success status and optional item_id
        """
        try:
            market_hash_name = item.get('market_hash_name', item.get('item_name'))

            # Шаг 1: Найти предмет в инвентаре Steam
            inventory = self.account.steam_client.get_inventory()

            if not inventory:
                logger.warning(f"[{self.name}] Inventory is empty or failed to fetch")
                return {'success': False, 'message': 'Empty inventory'}

            # Ищем предмет
            inventory_item = None
            for inv_item in inventory:
                if inv_item.market_hash_name == market_hash_name:
                    inventory_item = inv_item
                    break

            if not inventory_item:
                logger.warning(
                    f"[{self.name}] Item '{market_hash_name}' not found in inventory"
                )
                return {'success': False, 'message': 'Item not found in inventory'}

            if not inventory_item.tradable:
                logger.warning(
                    f"[{self.name}] Item '{market_hash_name}' is not tradable yet (7-day hold)"
                )
                return {'success': False, 'message': 'Item not tradable (still on hold)'}

            # Шаг 2: Вызвать CSGO.TM set_price API
            logger.info(
                f"[{self.name}] Listing '{market_hash_name}' on CSGO.TM "
                f"at ${price:.2f} (class_id: {inventory_item.class_id})"
            )

            result = self.account.csgotm_client.set_price(
                class_id=inventory_item.class_id,
                instance_id=inventory_item.instance_id,
                price=price
            )

            if result.success:
                logger.info(
                    f"[{self.name}] ✅ Trade offer requested for '{market_hash_name}'. "
                    f"Accept trade to complete listing (item_id: {result.item_id})"
                )
                return {
                    'success': True,
                    'item_id': result.item_id,
                    'message': 'Trade offer created, waiting for acceptance',
                    'asset_id': inventory_item.asset_id
                }
            else:
                logger.error(
                    f"[{self.name}] Failed to list '{market_hash_name}': {result.message}"
                )
                return {'success': False, 'message': result.message}

        except Exception as e:
            logger.error(f"[{self.name}] Error selling item on CSGO.TM: {e}")
            return {'success': False, 'message': str(e)}

    # ============ Main Loop ============

    def run_cycle(self) -> dict:
        """
        Выполнить один цикл торговли:
        1. Создать бай-ордера
        2. Проверить исполненные ордера
        3. Продать готовые предметы

        Returns:
            Dict with cycle stats
        """
        logger.info(f"[{self.name}] ===== Running trading cycle =====")

        # Проверяем, что клиенты инициализированы
        if not self.account.is_logged_in():
            logger.error(f"[{self.name}] Steam client not logged in")
            return {'orders_created': 0, 'orders_filled': 0, 'items_listed': 0}

        if not self.account.csgotm_client:
            logger.error(f"[{self.name}] CSGO.TM client not initialized")
            return {'orders_created': 0, 'orders_filled': 0, 'items_listed': 0}

        stats = {
            'orders_created': 0,
            'orders_filled': 0,
            'items_listed': 0,
        }

        try:
            # Шаг 1: Создать ордера
            order_results = self.create_buy_orders()
            stats['orders_created'] = order_results['success']

            # Шаг 2: Проверить исполненные ордера
            filled_count = self.check_filled_orders()
            stats['orders_filled'] = filled_count

            # Шаг 3: Продать готовые предметы
            listed_count = self.sell_ready_items()
            stats['items_listed'] = listed_count

        except Exception as e:
            logger.error(f"[{self.name}] Error in trading cycle: {e}")

        logger.info(
            f"[{self.name}] Cycle complete: "
            f"{stats['orders_created']} orders, "
            f"{stats['orders_filled']} filled, "
            f"{stats['items_listed']} listed"
        )

        return stats
