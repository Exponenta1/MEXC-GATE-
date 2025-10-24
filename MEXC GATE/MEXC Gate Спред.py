#!/usr/bin/env python3
"""
MEXC vs Gate.io Монитор спредов с динамическими Telegram сообщениями v3.2
Отслеживает спреды между двумя биржами с автоматическим обновлением сообщений
Добавлены: объёмы бидов MEXC, закреплённое сообщение, строгое использование priceScale
"""

import time
import json
import hmac
import hashlib
import requests
import requests.exceptions
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Set
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import sys

# Отключаем предупреждения SSL
urllib3.disable_warnings()

# Настройка логирования для telegram
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING
)
logging.getLogger('httpx').setLevel(logging.WARNING)

# Импортируем Telegram Bot только при необходимости
try:
    from telegram import Bot
    from telegram.error import TelegramError
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("⚠️ python-telegram-bot не установлен. Telegram функции недоступны.")
    print("   Для установки: pip install python-telegram-bot")

class DailySpreadTracker:
    """Класс для отслеживания дневных спредов в закреплённом сообщении"""       
    def __init__(self):
        self.daily_spreads = []
        self.pinned_message_id = None
        self.last_pin_update = 0
        self.last_reset_date = self.get_current_moscow_date()
        self.spread_counter = 0
        self.pin_initialized = False
        self.current_date_for_pin = self.get_current_moscow_date()
        self.pin_created_today = False
        
    def can_create_pin_today(self) -> bool:
        """Проверяет, можно ли создать новый закреп сегодня (только один раз в день)"""
        current_date = self.get_current_moscow_date()
        
        # Если дата изменилась, сбрасываем флаг
        if current_date != self.current_date_for_pin:
            self.pin_created_today = False
            self.current_date_for_pin = current_date
        
        # Разрешаем создание только если ещё не создавали сегодня
        return not self.pin_created_today
        
    def find_existing_daily_message(self, telegram_notifier) -> Optional[int]:
        """Ищет существующее сообщение 'Спреды за день' для текущей даты в канале"""
        try:
            if not telegram_notifier:
                return None
                
            from telegram.error import TelegramError
            
            current_date = self.get_current_moscow_date()
            date_formatted = datetime.strptime(current_date, '%Y-%m-%d').strftime('%d.%m.%Y')
            
            try:
                # Проверяем закреплённое сообщение
                chat = telegram_notifier.bot.get_chat(telegram_notifier.chat_id)
                
                if hasattr(chat, 'pinned_message') and chat.pinned_message:
                    pinned_msg = chat.pinned_message
                    if pinned_msg.text and "СПРЕДЫ ЗА ДЕНЬ" in pinned_msg.text and date_formatted in pinned_msg.text:
                        print(f"✅ Найден закреп для текущей даты {date_formatted}")
                        # Загружаем существующие спреды из сообщения
                        self.load_spreads_from_message(pinned_msg.text)
                        return pinned_msg.message_id
                
                # Ищем в последних сообщениях
                updates = telegram_notifier.bot.get_updates(limit=100)
                
                for update in reversed(updates):
                    if (update.message and 
                        update.message.chat.id == int(telegram_notifier.chat_id) and
                        update.message.text and 
                        "СПРЕДЫ ЗА ДЕНЬ" in update.message.text and
                        date_formatted in update.message.text):
                        
                        message_id = update.message.message_id
                        
                        # Загружаем существующие спреды из сообщения
                        self.load_spreads_from_message(update.message.text)
                        
                        try:
                            telegram_notifier.bot.pin_chat_message(
                                chat_id=telegram_notifier.chat_id,
                                message_id=message_id,
                                disable_notification=True
                            )
                            print(f"📌 Сообщение {message_id} закреплено для даты {date_formatted}")
                        except Exception as pin_error:
                            print(f"⚠️ Не удалось закрепить {message_id}: {pin_error}")
                        
                        return message_id
                        
            except TelegramError as e:
                print(f"⚠️ Ошибка поиска: {e}")
                
            return None
            
        except Exception as e:
            print(f"❌ Ошибка поиска закрепа: {e}")
            return None
        
    def load_spreads_from_message(self, message_text: str):
        """Загружает существующие спреды из текста сообщения"""
        try:
            lines = message_text.split('\n')
            loaded_count = 0
            
            # Пропускаем заголовок и пустые строки
            for line in lines[2:]:  # Первые 2 строки - заголовок и пустая строка
                line = line.strip()
                if not line:
                    continue
                
                # Формат: PAIR SPREAD% DURATIONм START_TIME - END_TIME
                # Пример: BTC 3.50% 15м 30с 10:25:30 - 10:41:00
                parts = line.split()
                if len(parts) >= 5:
                    try:
                        pair = parts[0]
                        spread_str = parts[1].replace('%', '')
                        max_spread = float(spread_str)
                        
                        # Парсим время окончания (последний элемент)
                        end_time = parts[-1]
                        
                        # Парсим длительность - ищем все части с м, с, ч
                        duration_seconds = 0
                        for part in parts[2:-2]:  # Всё между спредом и временем
                            if 'ч' in part:
                                duration_seconds += int(part.replace('ч', '')) * 3600
                            elif 'м' in part:
                                duration_seconds += int(part.replace('м', '')) * 60
                            elif 'с' in part:
                                duration_seconds += int(part.replace('с', ''))
                        
                        # Создаём запись о спреде
                        moscow_tz = timezone(timedelta(hours=3))
                        current_date_str = self.get_current_moscow_date()
                        
                        # Преобразуем время в timestamp
                        end_datetime_str = f"{current_date_str} {end_time}"
                        end_datetime = datetime.strptime(end_datetime_str, '%Y-%m-%d %H:%M:%S')
                        end_datetime = end_datetime.replace(tzinfo=moscow_tz)
                        
                        start_datetime = end_datetime - timedelta(seconds=duration_seconds)
                        
                        self.spread_counter += 1
                        
                        spread_data = {
                            'id': self.spread_counter,
                            'pair': pair,
                            'max_spread': max_spread,
                            'duration_seconds': duration_seconds,
                            'end_time': end_time,
                            'start_time': start_datetime.strftime('%H:%M:%S'),
                            'timestamp': end_datetime.timestamp()
                        }
                        
                        self.daily_spreads.append(spread_data)
                        loaded_count += 1
                        
                    except (ValueError, IndexError) as e:
                        print(f"⚠️ Не удалось распарсить строку: {line} ({e})")
                        continue
            
            if loaded_count > 0:
                print(f"📥 Загружено {loaded_count} существующих спредов из закрепа")
            
        except Exception as e:
            print(f"❌ Ошибка загрузки спредов из сообщения: {e}")    
        
    def start_tracking_spread(self, pair: str, message_id: int, initial_spread: float, is_new_ticker: bool = False):
        """Начинает отслеживание спреда с фиксированным временем старта и начальным спредом"""
        current_time = time.time()
        self.active_spreads[pair] = {
            'start_time': current_time,
            'message_id': message_id,
            'last_update': current_time,
            'last_message_update': current_time,
            'max_spread': initial_spread,
            'is_new_ticker': is_new_ticker,
            'new_ticker_post_time': current_time if is_new_ticker else 0
        }
        
        if is_new_ticker:
            print(f"🆕 Начато отслеживание НОВОГО тикера {pair} (24-часовой период)")
    
    def is_new_ticker_expired(self, pair: str) -> bool:
        """Проверяет, истёк ли 24-часовой период для нового тикера"""
        if pair not in self.active_spreads or not self.active_spreads[pair].get('is_new_ticker'):
            return False
        
        current_time = time.time()
        post_time = self.active_spreads[pair]['new_ticker_post_time']
        hours_passed = (current_time - post_time) / 3600
        
        return hours_passed >= 24.0
    
    def get_current_moscow_date(self) -> str:
        """Получает текущую дату по московскому времени в формате YYYY-MM-DD"""
        moscow_tz = timezone(timedelta(hours=3))
        current_time = datetime.now(moscow_tz)
        return current_time.strftime('%Y-%m-%d')
    
    def should_reset_daily_spreads(self) -> bool:
        """Проверяет, нужно ли сбросить дневные спреды (каждый день в 00:00 МСК)"""
        current_date = self.get_current_moscow_date()
        return current_date != self.last_reset_date
    
    def reset_daily_spreads(self):
        """Сбрасывает дневные спреды и обновляет дату"""
        self.daily_spreads = []
        self.spread_counter = 0
        self.last_reset_date = self.get_current_moscow_date()
        self.current_date_for_pin = self.last_reset_date
        self.pinned_message_id = None
        self.pin_created_today = False
        print(f"🔄 Дневные спреды сброшены (новый день: {self.last_reset_date})")
        
    def try_find_existing_pin(self, telegram_notifier, max_attempts: int = 5) -> Optional[int]:
        """Пытается найти существующий закреп для текущей даты с повторными попытками"""
        print(f"🔍 Поиск существующего закрепа для текущей даты (до {max_attempts} попыток)...")
        
        for attempt in range(1, max_attempts + 1):
            print(f"   Попытка {attempt}/{max_attempts}...")
            
            message_id = self.find_existing_daily_message(telegram_notifier)
            if message_id:
                print(f"✅ Закреп найден на попытке {attempt}: {message_id}")
                self.pinned_message_id = message_id
                return message_id
            
            if attempt < max_attempts:
                print(f"   Ждём 3 секунды перед следующей попыткой...")
                time.sleep(3)
        
        print(f"❌ Закреп для текущей даты не найден после {max_attempts} попыток")
        return None
    
    def remove_pair_from_daily(self, pair: str):
        """Удаляет все записи указанной пары из дневной статистики"""
        initial_count = len(self.daily_spreads)
        self.daily_spreads = [s for s in self.daily_spreads if s['pair'] != pair]
        removed_count = initial_count - len(self.daily_spreads)
        
        if removed_count > 0:
            print(f"🗑️ Удалено {removed_count} записей {pair} из дневной статистики")
    
    def add_completed_spread(self, pair: str, max_spread: float, duration_seconds: int, position_limit: int = 0):
        """Добавляет завершённый спред в дневной список с лимитом позиции
        и объединяет записи с временным разрывом ≤ 3 минуты"""
        if duration_seconds < 60:
            print(f"⏱️ Спред {pair} НЕ добавлен: длительность {duration_seconds}с < 1 минуты")
            return
            
        moscow_tz = timezone(timedelta(hours=3))
        current_time = datetime.now(moscow_tz)
        
        # Вычисляем время начала текущего спреда
        start_time = current_time - timedelta(seconds=duration_seconds)
        
        # Ищем последний спред с таким же тикером
        similar_spread_index = None
        for i, spread in enumerate(self.daily_spreads):
            if spread['pair'] == pair:
                similar_spread_index = i
                
        # Если нашли похожий спред, проверяем временной разрыв
        if similar_spread_index is not None:
            last_spread = self.daily_spreads[similar_spread_index]
            last_end_time_str = f"{self.current_date_for_pin} {last_spread['end_time']}"
            last_end_time = datetime.strptime(last_end_time_str, '%Y-%m-%d %H:%M:%S')
            last_end_time = last_end_time.replace(tzinfo=moscow_tz)
            
            # Вычисляем временной разрыв в минутах
            time_gap = (start_time - last_end_time).total_seconds() / 60.0
            
            # Если разрыв ≤ 3 минуты, объединяем спреды
            if time_gap <= 3.0:
                # Обновляем время окончания последнего спреда
                last_spread['end_time'] = current_time.strftime('%H:%M:%S')
                
                # Обновляем длительность (старая длительность + разрыв + новая длительность)
                old_duration = last_spread['duration_seconds']
                gap_duration = int(time_gap * 60)
                new_total_duration = old_duration + gap_duration + duration_seconds
                last_spread['duration_seconds'] = new_total_duration
                
                # Обновляем максимальный спред (берем наибольший)
                last_spread['max_spread'] = max(last_spread['max_spread'], max_spread)
                
                # Обновляем timestamp
                last_spread['timestamp'] = current_time.timestamp()
                
                print(f"🔄 Спред {pair} объединен с предыдущим: общая длительность {self.format_duration(new_total_duration)}")
                return
        
        # Если не объединяли, добавляем новый спред
        self.spread_counter += 1
        
        spread_data = {
            'id': self.spread_counter,
            'pair': pair,
            'max_spread': max_spread,
            'duration_seconds': duration_seconds,
            'end_time': current_time.strftime('%H:%M:%S'),
            'start_time': start_time.strftime('%H:%M:%S'),
            'timestamp': current_time.timestamp(),
            'position_limit': position_limit
        }
        
        # ДОБАВЛЯЕМ СПРЕД В СПИСОК
        self.daily_spreads.append(spread_data)
        print(f"📝 Добавлен спред {pair} ({max_spread:.2f}%, {self.format_duration(duration_seconds)}) в дневную статистику")
    
    def format_daily_spreads_message(self) -> str:
        """Форматирует сообщение с дневными спредами, включая лимит позиции"""
        date_str = datetime.strptime(self.current_date_for_pin, '%Y-%m-%d').strftime('%d.%m.%Y')
        
        # Первая строка жирная
        message = f"<b>СПРЕДЫ ЗА ДЕНЬ {date_str}</b>\n\n"
        
        if not self.daily_spreads:
            return message
        
        sorted_spreads = sorted(self.daily_spreads, key=lambda x: x['timestamp'])
        
        for spread in sorted_spreads:
            duration_formatted = self.format_duration(spread['duration_seconds'])
            time_interval = f"{spread['start_time']} - {spread['end_time']}"
            
            # Добавляем лимит позиции, если он есть
            limit_info = ""
            if 'position_limit' in spread and spread['position_limit'] > 0:
                limit_info = f" [{spread['position_limit']:,} USDT]".replace(',', ' ')
            
            message += f"{spread['pair']} {spread['max_spread']:.2f}%{limit_info} {duration_formatted} {time_interval}\n"
        
        return message
    
    def format_duration(self, seconds: int) -> str:
        """Форматирует длительность в читаемый вид"""
        if seconds < 60:
            return f"{seconds}с"
        elif seconds < 3600:
            minutes = seconds // 60
            remaining_seconds = seconds % 60
            return f"{minutes}м {remaining_seconds}с"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            remaining_seconds = seconds % 60
            return f"{hours}ч {minutes}м {remaining_seconds}с"
    

class SpreadTracker:
    """Класс для отслеживания спредов и управления сообщениями"""
    
    def __init__(self):
        self.active_spreads = {}
        self.pending_spreads = {}  # Новое: хранит спреды в ожидании подтверждения
        self.banned_tickers = set()
        self.banned_file = "banned_tickers.txt"
        self.price_failure_count = {}
        self.new_tickers_today = []
        self.max_new_tickers_per_day = 3
        self.load_banned_tickers()
        
    def add_pending_spread(self, pair: str, initial_spread: float, is_new_ticker: bool = False):
        """Добавляет спред в список ожидающих подтверждения (5 секунд)"""
        current_time = time.time()
        self.pending_spreads[pair] = {
            'start_time': current_time,
            'initial_spread': initial_spread,
            'is_new_ticker': is_new_ticker
        }
        print(f"⏳ Спред {pair} ({initial_spread:.2f}%) в ожидании подтверждения (5 секунд)")
        
    def check_pending_spreads(self) -> List[Dict]:
        """Проверяет ожидающие спреды и возвращает те, что прошли 5 секунд"""
        current_time = time.time()
        confirmed_spreads = []
        
        for pair in list(self.pending_spreads.keys()):
            pending_info = self.pending_spreads[pair]
            time_elapsed = current_time - pending_info['start_time']
            
            if time_elapsed >= 5.0:
                confirmed_spreads.append({
                    'pair': pair,
                    'initial_spread': pending_info['initial_spread'],
                    'is_new_ticker': pending_info['is_new_ticker']
                })
                del self.pending_spreads[pair]
                print(f"✅ Спред {pair} подтверждён после 5 секунд ожидания")
        
        return confirmed_spreads
    
    def remove_pending_spread(self, pair: str):
        """Удаляет спред из списка ожидающих (если он пропал)"""
        if pair in self.pending_spreads:
            del self.pending_spreads[pair]
            print(f"🗑️ Спред {pair} удалён из ожидания (пропал до подтверждения)")    
        
    def increment_price_failure(self, pair: str):
        """Увеличивает счетчик неудачных получений цены"""
        self.price_failure_count[pair] = self.price_failure_count.get(pair, 0) + 1
        print(f"⚠️ Неудача получения цены {pair}: {self.price_failure_count[pair]}/3")
        
        # Если 3 неудачи подряд - считаем как новый тикер
        if self.price_failure_count[pair] >= 3:
            print(f"🆕 Тикер {pair} достиг 3 неудач, проверяем лимит новых тикеров")
            
            # Проверяем лимит новых тикеров за 24 часа
            if self.can_add_new_ticker():
                print(f"✅ Тикер {pair} помечен как новый (лимит не превышен)")
                # НЕ сбрасываем счетчик здесь - это будет сделано при первом успешном получении цены
                return True
            else:
                print(f"🚫 Тикер {pair} НЕ помечен как новый (превышен лимит {self.max_new_tickers_per_day}/день)")
                self.price_failure_count[pair] = 0  # Сбрасываем счетчик чтобы не накапливать
                return False
        return False
    
    def can_add_new_ticker(self) -> bool:
        """Проверяет, можно ли добавить новый тикер (лимит 3 за 24 часа)"""
        current_time = time.time()
        
        # Удаляем записи старше 24 часов
        self.new_tickers_today = [
            (pair, timestamp) for pair, timestamp in self.new_tickers_today
            if current_time - timestamp < 24 * 3600
        ]
        
        # Проверяем лимит
        return len(self.new_tickers_today) < self.max_new_tickers_per_day
    
    def add_new_ticker_record(self, pair: str):
        """Добавляет запись о новом тикере"""
        current_time = time.time()
        self.new_tickers_today.append((pair, current_time))
        print(f"📝 Добавлен новый тикер {pair} ({len(self.new_tickers_today)}/{self.max_new_tickers_per_day})")    
        
    def update_spread_time(self, pair: str, current_spread: float):
        """Обновляет время и добавляет точку в историю спреда"""
        if pair in self.active_spreads:
            current_time = time.time()
            start_time = self.active_spreads[pair]['start_time']
            seconds_from_start = int(current_time - start_time)
            
            self.active_spreads[pair]['last_update'] = current_time
            
            # Обновляем максимальный спред
            if current_spread > self.active_spreads[pair]['max_spread']:
                self.active_spreads[pair]['max_spread'] = current_spread
            
            # Добавляем точку в историю (каждые 30 секунд для экономии места)
            history = self.active_spreads[pair]['spread_history']
            if not history or seconds_from_start - history[-1][0] >= 30:
                history.append((seconds_from_start, current_spread))
                
                # Ограничиваем историю до 60 точек (30 минут)
                if len(history) > 60:
                    history.pop(0)                  
            
    def reset_price_failure(self, pair: str):
        """Сбрасывает счетчик неудачных получений цены при успешном получении"""
        if pair in self.price_failure_count:
            del self.price_failure_count[pair]    
        
    def load_banned_tickers(self):
        """Загружает черный список тикеров из файла"""
        try:
            import os
            if os.path.exists(self.banned_file):
                with open(self.banned_file, 'r', encoding='utf-8') as f:
                    self.banned_tickers = set(line.strip() for line in f if line.strip())
                print(f"📄 Загружен черный список: {len(self.banned_tickers)} тикеров")
        except Exception as e:
            print(f"❌ Ошибка загрузки черного списка: {e}")
    
    def save_banned_tickers(self):
        """Сохраняет черный список тикеров в файл"""
        try:
            with open(self.banned_file, 'w', encoding='utf-8') as f:
                for ticker in sorted(self.banned_tickers):
                    f.write(f"{ticker}\n")
        except Exception as e:
            print(f"❌ Ошибка сохранения черного списка: {e}")
    
    def add_banned_ticker(self, ticker: str):
        """Добавляет тикер в черный список"""
        self.banned_tickers.add(ticker)
        self.save_banned_tickers()
        print(f"🚫 Тикер {ticker} добавлен в черный список")
    
    def remove_banned_ticker(self, ticker: str):
        """Удаляет тикер из черного списка"""
        if ticker in self.banned_tickers:
            self.banned_tickers.remove(ticker)
            self.save_banned_tickers()
            print(f"✅ Тикер {ticker} удален из черного списка")
            return True
        return False
    
    def is_ticker_banned(self, ticker: str) -> bool:
        """Проверяет, забанен ли тикер"""
        return ticker in self.banned_tickers
    
    def format_duration(self, seconds: int) -> str:
        """Форматирует длительность в читаемый вид"""
        if seconds < 60:
            return f"{seconds}с"
        elif seconds < 3600:  # меньше часа
            minutes = seconds // 60
            remaining_seconds = seconds % 60
            return f"{minutes}м {remaining_seconds}с"
        else:  # час и больше
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            remaining_seconds = seconds % 60
            return f"{hours}ч {minutes}м {remaining_seconds}с"
    
    def start_tracking_spread(self, pair: str, message_id: int, initial_spread: float, is_new_ticker: bool = False):
        """Начинает отслеживание спреда с историей для графика"""
        current_time = time.time()
        self.active_spreads[pair] = {
            'start_time': current_time,
            'message_id': message_id,
            'last_update': current_time,
            'last_message_update': current_time,
            'max_spread': initial_spread,
            'is_new_ticker': is_new_ticker,
            'new_ticker_post_time': current_time if is_new_ticker else 0,
            'spread_history': [(0, initial_spread)]  # НОВОЕ: история (секунды_от_старта, спред)
        }
        
        if is_new_ticker:
            self.add_new_ticker_record(pair)
            print(f"🆕 Начато отслеживание НОВОГО тикера {pair} (24-часовой период)")
        
    def should_update_message(self, pair: str) -> bool:
        """Проверяет, нужно ли обновить сообщение (каждые 3 секунды)"""
        if pair not in self.active_spreads:
            return False
        
        current_time = time.time()
        last_message_update = self.active_spreads[pair]['last_message_update']
        
        # Обновляем сообщение каждые 3 секунды
        if current_time - last_message_update >= 3:
            self.active_spreads[pair]['last_message_update'] = current_time
            return True
        return False

    def get_spread_duration(self, pair: str) -> int:
        """Возвращает время существования спреда в секундах от момента старта"""
        if pair in self.active_spreads:
            current_time = time.time()
            start_time = self.active_spreads[pair]['start_time']
            return int(current_time - start_time)
        return 0    
    
    def get_max_spread(self, pair: str) -> float:
        """Возвращает максимальный спред для пары"""
        if pair in self.active_spreads:
            return self.active_spreads[pair]['max_spread']
        return 0.0
    
    def should_ban_ticker(self, pair: str) -> bool:
        """Проверяет, нужно ли забанить тикер (более 4 часов)"""
        duration = self.get_spread_duration(pair)
        return duration > 4 * 3600  # 4 часа
    
    def stop_tracking_spread(self, pair: str, reason: str = 'unknown') -> Optional[Dict]:
        """Прекращает отслеживание спреда и возвращает информацию о спреде"""
        if pair in self.active_spreads:
            spread_info = self.active_spreads[pair].copy()
            spread_info['reason'] = reason
            spread_info['duration'] = self.get_spread_duration(pair)
            del self.active_spreads[pair]
            return spread_info
        return None
    
    def get_active_spreads(self) -> Dict:
        """Возвращает активные спреды"""
        return self.active_spreads.copy()

class TelegramNotifier:
    
    def __init__(self, bot_token: str, chat_id: str):
        if not TELEGRAM_AVAILABLE:
            raise ImportError("python-telegram-bot не установлен")
        
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.bot = Bot(token=bot_token)
        self.last_message_time = 0
        self.message_delay = 1
        
    def create_pinned_message_with_retry(self, text: str, max_attempts: int = 3) -> Optional[int]:
        """Создаёт закреплённое сообщение с повторными попытками"""
        print(f"📌 Создание нового закрепа (до {max_attempts} попыток)...")
        
        for attempt in range(1, max_attempts + 1):
            print(f"   Попытка {attempt}/{max_attempts}...")
            
            try:
                message = self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode='HTML'
                )
                
                # Пауза перед закреплением
                time.sleep(2)
                
                # Проверяем что сообщение действительно создалось
                try:
                    check = self.bot.get_chat(self.chat_id)
                    print(f"✅ Сообщение {message.message_id} создано")
                except:
                    print(f"⚠️ Не удалось проверить сообщение")
                
                # Закрепляем
                self.bot.pin_chat_message(
                    chat_id=self.chat_id,
                    message_id=message.message_id,
                    disable_notification=True
                )
                
                print(f"✅ Закреп создан и закреплён: {message.message_id}")
                return message.message_id
                
            except Exception as e:
                print(f"❌ Ошибка на попытке {attempt}: {e}")
                
                if attempt < max_attempts:
                    print(f"   Ждём 20 секунд перед следующей попыткой...")
                    time.sleep(20)
        
        print(f"❌ Не удалось создать закреп после {max_attempts} попыток")
        return None
    
    def send_spread_message_with_buttons(self, text: str, pair: str) -> Optional[int]:
        """Отправляет сообщение о спреде с кнопками-ссылками на биржи"""
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            
            current_time = time.time()
            if current_time - self.last_message_time < self.message_delay:
                time.sleep(self.message_delay)
            
            mexc_url = f"https://www.mexc.com/ru-RU/futures/{pair}_USDT?type=linear_swap"
            gate_url = f"https://www.gate.com/ru/futures/USDT/{pair}_USDT"
            
            keyboard = [
                [
                    InlineKeyboardButton("MEXC", url=mexc_url),
                    InlineKeyboardButton("GATE", url=gate_url)
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message = self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            
            self.last_message_time = current_time
            return message.message_id
            
        except Exception as e:
            print(f"❌ Ошибка отправки сообщения с кнопками: {e}")
            return self.send_spread_message(text)
        
    def update_spread_message_with_buttons(self, message_id: int, text: str, pair: str) -> tuple:
        """Обновляет существующее сообщение с кнопками"""
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            from telegram.error import BadRequest
            
            mexc_url = f"https://www.mexc.com/ru-RU/futures/{pair}_USDT?type=linear_swap"
            gate_url = f"https://www.gate.com/ru/futures/USDT/{pair}_USDT"
            
            keyboard = [
                [
                    InlineKeyboardButton("MEXC", url=mexc_url),
                    InlineKeyboardButton("GATE", url=gate_url)
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=message_id,
                text=text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            return True, False
            
        except BadRequest as e:
            error_msg = str(e).lower()
            if "message to edit not found" in error_msg or "message_id_invalid" in error_msg:
                print(f"🚫 Сообщение {message_id} для {pair} удалено - добавляем в ЧС")
                return False, True
            elif "message is not modified" in error_msg:
                return True, False
            else:
                print(f"❌ Ошибка обновления {message_id}: {e}")
                return False, False
        except Exception as e:
            print(f"❌ Общая ошибка обновления {message_id}: {e}")
            return False, False
        
    def send_spread_message(self, text: str) -> Optional[int]:
        """Отправляет сообщение о спреде"""
        try:
            current_time = time.time()
            if current_time - self.last_message_time < self.message_delay:
                time.sleep(self.message_delay)
            
            message = self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode='HTML'
            )
            
            self.last_message_time = current_time
            return message.message_id
            
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")
            return None
    
    def update_spread_message(self, message_id: int, text: str) -> tuple:
        """Обновляет существующее сообщение"""
        try:
            from telegram.error import BadRequest
            
            self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=message_id,
                text=text,
                parse_mode='HTML'
            )
            return True, False
            
        except BadRequest as e:
            error_msg = str(e).lower()
            if "message to edit not found" in error_msg or "message_id_invalid" in error_msg:
                print(f"⚠️ Сообщение {message_id} удалено")
                return False, False
            elif "message is not modified" in error_msg:
                return True, False
            else:
                print(f"❌ Ошибка обновления {message_id}: {e}")
                return False, False
        except Exception as e:
            print(f"❌ Общая ошибка обновления: {e}")
            return False, False
    
    def delete_spread_message(self, message_id: int) -> bool:
        """Удаляет сообщение"""
        try:
            self.bot.delete_message(
                chat_id=self.chat_id,
                message_id=message_id
            )
            return True
            
        except Exception as e:
            print(f"❌ Ошибка удаления {message_id}: {e}")
            return False
    
    def send_message(self, text: str):
        """Отправляет обычное сообщение"""
        try:
            current_time = time.time()
            if current_time - self.last_message_time < self.message_delay:
                return
            
            if len(text) > 4000:
                text = text[:4000] + "..."
            
            self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode='HTML'
            )
            
            self.last_message_time = current_time
            
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")
    
    def update_spread_message(self, message_id: int, text: str) -> tuple:
        """Обновляет существующее сообщение, возвращает (success, should_ban)"""
        try:
            from telegram.error import BadRequest
            
            self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=message_id,
                text=text,
                parse_mode='HTML'
            )
            return True, False
            
        except BadRequest as e:
            error_msg = str(e).lower()
            if "message to edit not found" in error_msg or "message_id_invalid" in error_msg:
                print(f"⚠️ Закрепленное сообщение {message_id} удалено пользователем")
                return False, False  # Для закрепа не баним
            elif "message is not modified" in error_msg:
                return True, False
            else:
                print(f"❌ Ошибка обновления сообщения {message_id}: {e}")
                return False, False
        except Exception as e:
            print(f"❌ Общая ошибка обновления сообщения: {e}")
            return False, False
    
    def delete_spread_message(self, message_id: int) -> bool:
        """Удаляет сообщение"""
        try:
            self.bot.delete_message(
                chat_id=self.chat_id,
                message_id=message_id
            )
            return True
            
        except TelegramError as e:
            print(f"❌ Ошибка удаления сообщения {message_id}: {e}")
            return False
        except Exception as e:
            print(f"❌ Общая ошибка удаления сообщения: {e}")
            return False
    
    def send_or_update_pinned_message(self, text: str, current_pinned_id: Optional[int]) -> Optional[int]:
        """Отправляет новое или обновляет существующее закреплённое сообщение"""
        try:
            if current_pinned_id:
                # Пытаемся обновить существующее
                success = self.update_spread_message(current_pinned_id, text)
                if success:
                    print(f"📌 Обновлено закрепленное сообщение {current_pinned_id}")
                    return current_pinned_id
                else:
                    print("⚠️ Не удалось обновить закреплённое сообщение, создаём новое")
            
            # Отправляем новое сообщение только если нет существующего или обновление не удалось
            message_id = self.send_spread_message(text)
            if message_id:
                # Закрепляем сообщение
                try:
                    self.bot.pin_chat_message(
                        chat_id=self.chat_id,
                        message_id=message_id,
                        disable_notification=True
                    )
                    print(f"📌 Создано и закреплено новое сообщение {message_id}")
                    return message_id
                except Exception as e:
                    print(f"⚠️ Не удалось закрепить сообщение: {e}")
                    return message_id
            
            return None
            
        except Exception as e:
            print(f"❌ Ошибка работы с закреплённым сообщением: {e}")
            return None
    
    def send_message(self, text: str):
        """Отправляет обычное сообщение"""
        try:
            current_time = time.time()
            if current_time - self.last_message_time < self.message_delay:
                return
            
            if len(text) > 4000:
                text = text[:4000] + "..."
            
            self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode='HTML'
            )
            
            self.last_message_time = current_time
            
        except TelegramError as e:
            print(f"❌ Ошибка отправки в Telegram: {e}")
        except Exception as e:
            print(f"❌ Общая ошибка Telegram: {e}")

class TwoExchangeMonitor:
    def __init__(self, telegram_notifier: Optional[TelegramNotifier] = None):
        # API ключи для Gate.io
        self.gate_api_key = "8e169592b4053b83201a6e7b35427d52"
        self.gate_secret = "7e302c757e323c9c0d138e655571bc3d5917c65144efe76287b024a9127f0f05"
        
        # Прокси для Gate.io
        self.gate_proxy = {
            'http': 'socks5://kdbB2d:NdnT76dFNB@31.12.92.243:5501',
            'https': 'socks5://kdbB2d:NdnT76dFNB@31.12.92.243:5501'
        }
        
        # Файлы для сохранения данных
        self.pairs_file = "two_exchange_pairs.txt"
        self.verified_file = "verified_two_exchange_pairs.txt"
        
        # Пары по биржам
        self.mexc_pairs = set()
        self.gate_pairs = set()
        
        # Текущие цены
        self.mexc_prices = {}
        self.gate_prices = {}
        
        # Telegram уведомления и трекеры спредов
        self.telegram = telegram_notifier
        self.spread_tracker = SpreadTracker()
        self.daily_tracker = DailySpreadTracker()
        
        # Кэш информации о контрактах MEXC (для priceScale)
        self.mexc_contracts_info = {}
        
        # Настройка сессий
        self._setup_sessions()
        
        # Добавляем переменные для отслеживания новых пар
        self.last_pairs_update = 0
        self.pairs_update_interval = 10  # каждые 10 секунд
        self.verified_coverage_cache = {}
        self.verified_coverage_last_update = 0 
        
    def manage_pinned_messages(self, max_pins=30):
        """Управляет количеством закрепленных сообщений, удаляет старые закрепы если их слишком много"""
        if not self.telegram:
            return False
            
        try:
            print(f"📌 Проверка количества закрепленных сообщений...")
            
            # Получаем все сообщения из канала
            from telegram import Bot
            from telegram.error import TelegramError
            
            # Получаем текущую дату в московском формате
            moscow_tz = timezone(timedelta(hours=3))
            current_date = datetime.now(moscow_tz).strftime('%d.%m.%Y')
            today_pin_text = f"СПРЕДЫ ЗА ДЕНЬ {current_date}"
            
            try:
                # Получаем историю сообщений
                chat = self.telegram.bot.get_chat(self.telegram.chat_id)
                
                # Проверяем закрепленные сообщения
                pinned_messages = []
                updates = self.telegram.bot.get_updates(limit=1000)  # Получаем максимальное количество сообщений
                
                # Идентификация всех закрепленных сообщений
                for update in updates:
                    if hasattr(update, 'channel_post') and update.channel_post and update.channel_post.chat.id == int(self.telegram.chat_id):
                        if update.channel_post.pinned_message:
                            # Если сообщение закреплено
                            pinned_msg = update.channel_post.pinned_message
                            
                            # Проверяем, не является ли это сегодняшним закрепом со спредами
                            is_today_pin = False
                            if pinned_msg.text and today_pin_text in pinned_msg.text:
                                is_today_pin = True
                            
                            # Добавляем в список, помечаем как сегодняшний или нет
                            pinned_messages.append({
                                'message_id': pinned_msg.message_id,
                                'date': pinned_msg.date,
                                'is_today_pin': is_today_pin
                            })
                
                # Если обнаружено слишком много закрепов
                if len(pinned_messages) > max_pins:
                    print(f"⚠️ Обнаружено {len(pinned_messages)} закрепленных сообщений, удаляем старые...")
                    
                    # Сортируем по дате (от старых к новым)
                    pinned_messages.sort(key=lambda x: x['date'])
                    
                    # Выделяем сегодняшний закреп
                    today_pins = [msg for msg in pinned_messages if msg['is_today_pin']]
                    other_pins = [msg for msg in pinned_messages if not msg['is_today_pin']]
                    
                    # Определяем, сколько закрепов нужно открепить
                    pins_to_unpin = len(pinned_messages) - max_pins
                    if pins_to_unpin > 0:
                        # Открепляем самые старые, но не трогаем сегодняшний
                        for i in range(min(pins_to_unpin, len(other_pins))):
                            try:
                                self.telegram.bot.unpin_chat_message(
                                    chat_id=self.telegram.chat_id,
                                    message_id=other_pins[i]['message_id']
                                )
                                print(f"📌 Откреплено сообщение {other_pins[i]['message_id']}")
                            except Exception as e:
                                print(f"⚠️ Ошибка при откреплении {other_pins[i]['message_id']}: {e}")
                    
                    return True
                else:
                    print(f"✅ Закрепленных сообщений: {len(pinned_messages)}/{max_pins}")
                    return False
            
            except Exception as e:
                print(f"❌ Ошибка при проверке закрепленных сообщений: {e}")
                return False
                
        except Exception as e:
            print(f"❌ Критическая ошибка управления закрепами: {e}")
            return False        
        
    def calculate_mexc_max_position_limit(self, pair: str) -> int:
        """Рассчитывает максимальный лимит позиции в USDT для пары"""
        try:
            print(f"\n💰 Начинаем расчёт лимита позиции для {pair}")
            
            # 1. Получаем информацию о контракте для данной пары
            if pair not in self.mexc_contracts_info:
                print(f"❌ Нет информации о контракте для {pair}")
                return 0
            
            contract_info = self.mexc_contracts_info[pair]
            contract_size = contract_info.get('contractSize')
            
            print(f"📋 Размер контракта для {pair}: {contract_size} монет/контракт")
            
            # 2. Получаем maxVol из API контракта
            try:
                symbol = f"{pair}_USDT"
                url = 'https://contract.mexc.com/api/v1/contract/detail'
                
                response = self.mexc_session.get(url, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    
                    if data.get('success') and data.get('data'):
                        for contract in data['data']:
                            if contract.get('symbol') == symbol:
                                max_vol = float(contract.get('maxVol', 0))
                                print(f"📊 Максимальный объём контрактов для {pair}: {max_vol}")
                                
                                # Получаем текущую цену
                                mexc_price = self._get_mexc_price(pair)
                                if not mexc_price:
                                    print(f"⚠️ Не удалось получить текущую цену для {pair}")
                                    return 0
                                    
                                # Рассчитываем максимум в USDT
                                max_tokens = max_vol * contract_size
                                max_usdt = max_tokens * mexc_price
                                
                                print(f"✅ Максимальный лимит для {pair}: {int(max_usdt)} USDT")
                                return int(max_usdt)
                
                print(f"❌ Не удалось получить maxVol для {pair}")
                return 0
                    
            except Exception as e:
                print(f"❌ Ошибка расчёта лимита позиции: {str(e)}")
                return 0
                
        except Exception as e:
            print(f"❌ Критическая ошибка расчёта лимита для {pair}: {str(e)}")
            import traceback
            print(f"   Трассировка: {traceback.format_exc()}")
            return 0    
        
    def check_duplicate_spreads(self):
        """Проверяет и удаляет дублирующиеся спреды одного и того же тикера"""
        active_spreads = self.spread_tracker.get_active_spreads()
        
        # Группируем спреды по парам
        pairs_count = {}
        for pair in active_spreads:
            pairs_count[pair] = pairs_count.get(pair, 0) + 1
        
        # Ищем дубликаты (тикеры с количеством > 1)
        duplicates = {pair: count for pair, count in pairs_count.items() if count > 1}
        
        if not duplicates:
            return False
        
        print(f"🔄 Найдено {len(duplicates)} тикеров с дубликатами: {', '.join(duplicates.keys())}")
        
        # Обрабатываем каждый тикер с дубликатами
        for pair, count in duplicates.items():
            # Собираем все сообщения для этой пары
            pair_messages = []
            for active_pair, data in active_spreads.items():
                if active_pair == pair:
                    last_update_time = data.get('last_update', 0)
                    message_id = data.get('message_id')
                    pair_messages.append((message_id, last_update_time))
            
            # Сортируем по времени последнего обновления (от новых к старым)
            pair_messages.sort(key=lambda x: x[1], reverse=True)
            
            # Оставляем самый свежий, удаляем остальные
            newest_message = pair_messages[0][0]
            messages_to_delete = [msg_id for msg_id, _ in pair_messages[1:]]
            
            print(f"✅ Для {pair} оставляем сообщение {newest_message}, удаляем {len(messages_to_delete)} дубликатов")
            
            # Удаляем сообщения и записи о спредах
            for msg_id in messages_to_delete:
                if self.telegram:
                    self.telegram.delete_spread_message(msg_id)
                    print(f"   🗑️ Удалено сообщение {msg_id}")
                
                # Ищем и удаляем соответствующий спред из активных
                for active_pair, data in list(active_spreads.items()):
                    if active_pair == pair and data.get('message_id') == msg_id:
                        self.spread_tracker.stop_tracking_spread(active_pair, 'duplicate_removed')
                        print(f"   🗑️ Удалена запись о спреде {active_pair} (сообщение {msg_id})")
        
        return True
    
    def check_stale_messages(self, max_idle_time=60):
        """Проверяет и удаляет сообщения, которые не обновлялись слишком долго"""
        active_spreads = self.spread_tracker.get_active_spreads()
        current_time = time.time()
        stale_messages = []
        
        for pair, data in list(active_spreads.items()):
            last_update = data.get('last_update', 0)
            idle_time = current_time - last_update
            
            # Если сообщение не обновлялось более X секунд, считаем его зависшим
            if idle_time > max_idle_time:
                message_id = data.get('message_id')
                stale_messages.append((pair, message_id, idle_time))
        
        if not stale_messages:
            return False
        
        print(f"⚠️ Найдено {len(stale_messages)} зависших сообщений:")
        
        for pair, message_id, idle_time in stale_messages:
            print(f"   🕒 {pair}: сообщение {message_id} не обновлялось {idle_time:.1f}с")
            
            # Удаляем сообщение и запись о спреде
            if self.telegram:
                self.telegram.delete_spread_message(message_id)
                print(f"   🗑️ Удалено зависшее сообщение {message_id}")
            
            self.spread_tracker.stop_tracking_spread(pair, 'stale_message')
            print(f"   🗑️ Удалена запись о зависшем спреде {pair}")
        
        return True
        
    def initialize_daily_spreads_message(self):
        """Инициализирует закрепленное сообщение при запуске"""
        if not self.telegram:
            return
        
        print("🚀 Инициализация системы закреплённых сообщений...")
        
        # Сбрасываем дневные спреды если нужно
        if self.daily_tracker.should_reset_daily_spreads():
            self.daily_tracker.reset_daily_spreads()
        
        # Сначала пытаемся найти существующий закреп
        existing_pin = self.daily_tracker.try_find_existing_pin(self.telegram, max_attempts=5)
        
        if existing_pin:
            print(f"✅ Используем существующий закреп {existing_pin}")
            self.daily_tracker.pin_created_today = True
            return
        
        # Если не нашли и можно создать новый
        if self.daily_tracker.can_create_pin_today():
            message_text = self.daily_tracker.format_daily_spreads_message()
            message_id = self.telegram.create_pinned_message_with_retry(message_text, max_attempts=3)
            
            if message_id:
                self.daily_tracker.pinned_message_id = message_id
                self.daily_tracker.pin_created_today = True
                print(f"✅ Создан новый закреп {message_id} для {self.daily_tracker.current_date_for_pin}")
            else:
                print("❌ Не удалось создать закреп после всех попыток")
        else:
            print("⚠️ Закреп уже создавался сегодня, пропускаем создание")
        
        self.daily_tracker.pin_initialized = True    
        
    def initialize_daily_spreads_message(self):
        """Инициализирует закрепленное сообщение при запуске"""
        if not self.telegram:
            return
        
        print("🚀 Инициализация системы закреплённых сообщений...")
        
        # Сбрасываем дневные спреды если нужно
        if self.daily_tracker.should_reset_daily_spreads():
            self.daily_tracker.reset_daily_spreads()
        
        # Сначала пытаемся найти существующий закреп
        existing_pin = self.daily_tracker.try_find_existing_pin(self.telegram, max_attempts=5)
        
        if existing_pin:
            print(f"✅ Используем существующий закреп {existing_pin}")
            self.daily_tracker.pin_created_today = True
            return
        
        # Если не нашли и можно создать новый
        if self.daily_tracker.can_create_pin_today():
            message_text = self.daily_tracker.format_daily_spreads_message()
            message_id = self.telegram.create_pinned_message_with_retry(message_text, max_attempts=3)
            
            if message_id:
                self.daily_tracker.pinned_message_id = message_id
                self.daily_tracker.pin_created_today = True
                print(f"✅ Создан новый закреп {message_id} для {self.daily_tracker.current_date_for_pin}")
            else:
                print("❌ Не удалось создать закреп после всех попыток")
        else:
            print("⚠️ Закреп уже создавался сегодня, пропускаем создание")
        
        self.daily_tracker.pin_initialized = True
        
    def _setup_sessions(self):
        """Настройка HTTP сессий"""
        # Сессия для MEXC (без прокси)
        self.mexc_session = requests.Session()
        self.mexc_session.verify = False
        
        # Сессия для Gate.io (с прокси)
        self.gate_session = requests.Session()
        self.gate_session.proxies = self.gate_proxy
        self.gate_session.verify = False
        
        print("✅ HTTP сессии настроены для двух бирж")
    
    def get_mexc_contracts_info(self) -> Dict[str, Dict]:
        """Получает детальную информацию о контрактах MEXC включая priceScale и contractSize"""
        try:
            print("🔍 Получаем детальную информацию о контрактах MEXC...")
            
            url = 'https://contract.mexc.com/api/v1/contract/detail'
            
            response = self.mexc_session.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get('success') and data.get('data'):
                    contracts_info = {}
                    for contract in data['data']:
                        symbol = contract.get('symbol')
                        if symbol and symbol.endswith('_USDT'):
                            base_symbol = symbol.replace('_USDT', '')
                            contracts_info[base_symbol] = {
                                'priceScale': contract.get('priceScale'),
                                'contractSize': contract.get('contractSize'),  # Размер контракта
                                'symbol': symbol
                            }
                    
                    print(f"✅ MEXC: загружена информация о {len(contracts_info)} контрактах")
                    self.mexc_contracts_info = contracts_info
                    return contracts_info
                else:
                    print(f"❌ MEXC contract detail API вернул неожиданную структуру: {data}")
                    return {}
            else:
                print(f"❌ Ошибка получения детальной информации MEXC: HTTP {response.status_code}")
                return {}
                
        except Exception as e:
            print(f"❌ Ошибка получения детальной информации MEXC: {str(e)}")
            return {}
    
    def get_mexc_order_book(self, pair: str) -> Optional[Dict]:
        """Получает стакан MEXC для расчёта объёма 5 бидов"""
        try:
            symbol = f"{pair}_USDT"
            url = f'https://contract.mexc.com/api/v1/contract/depth/{symbol}'
            
            print(f"🔍 Запрашиваем стакан MEXC для {symbol}: {url}")
            
            response = self.mexc_session.get(url, timeout=10)
            
            print(f"📊 Ответ MEXC стакан {symbol}: HTTP {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Получен JSON ответ для {symbol}")
                print(f"   Ключи верхнего уровня: {list(data.keys())}")
                
                # Проверяем успешность запроса
                if not data.get('success'):
                    print(f"❌ API вернул success=False для {symbol}")
                    print(f"   Код ошибки: {data.get('code')}")
                    print(f"   Сообщение: {data.get('message', 'Нет сообщения')}")
                    return None
                
                # Данные находятся в data.data согласно примеру
                if 'data' not in data:
                    print(f"❌ Отсутствует ключ 'data' в ответе для {symbol}")
                    return None
                
                order_book_data = data['data']
                print(f"   Ключи в data: {list(order_book_data.keys())}")
                
                if 'bids' in order_book_data:
                    bids_count = len(order_book_data['bids'])
                    print(f"   Количество бидов: {bids_count}")
                    
                    if bids_count > 0:
                        print(f"   Первые 3 бида:")
                        for i, bid in enumerate(order_book_data['bids'][:3]):
                            print(f"     Бид {i+1}: {bid} (тип: {type(bid)})")
                    else:
                        print(f"   ⚠️ Список бидов пуст!")
                else:
                    print(f"   ❌ Ключ 'bids' отсутствует в data!")
                    print(f"   Доступные ключи в data: {list(order_book_data.keys())}")
                
                if 'asks' in order_book_data:
                    asks_count = len(order_book_data['asks'])
                    print(f"   Количество асков: {asks_count}")
                
                # Возвращаем данные стакана, а не весь ответ
                return order_book_data
            else:
                print(f"❌ Ошибка получения стакана {symbol}: HTTP {response.status_code}")
                try:
                    error_data = response.json()
                    print(f"   Ответ сервера: {error_data}")
                except:
                    print(f"   Текст ответа: {response.text[:200]}")
                return None
                
        except Exception as e:
            print(f"❌ Исключение при получении стакана MEXC для {pair}: {str(e)}")
            import traceback
            print(f"   Трассировка: {traceback.format_exc()}")
            return None
    
    def calculate_mexc_5_bids_volume(self, pair: str) -> int:
        """Рассчитывает объём 9 бидов MEXC в USDT с учётом размера контракта"""
        try:
            print(f"\n💰 Начинаем расчёт объёма 9 бидов для {pair}")
            
            # 1. Получаем размер контракта для данной пары
            if pair not in self.mexc_contracts_info:
                print(f"❌ Нет информации о контракте для {pair}")
                return 0
            
            contract_info = self.mexc_contracts_info[pair]
            contract_size = contract_info.get('contractSize')
            
            print(f"📋 Размер контракта для {pair}: {contract_size} монет/контракт")
            
            # 2. Получаем стакан ордеров
            order_book = self.get_mexc_order_book(pair)
            if not order_book:
                print(f"❌ Не удалось получить стакан для {pair}")
                return 0
            
            if 'bids' not in order_book:
                print(f"❌ Отсутствует ключ 'bids' в стакане для {pair}")
                return 0
            
            bids = order_book['bids']
            bids_count = len(bids)
            
            print(f"📊 Общее количество бидов: {bids_count}")
            
            if bids_count == 0:
                print(f"❌ Список бидов пуст для {pair}")
                return 0
            
            total_volume_usdt = 0.0
            bids_to_process = min(9, bids_count)
            
            print(f"🔢 Обрабатываем {bids_to_process} бидов:")
            
            # 3. Рассчитываем объём каждого бида
            for i in range(bids_to_process):
                bid = bids[i]
                
                if not isinstance(bid, (list, tuple)) or len(bid) < 2:
                    print(f"   ❌ Неправильный формат бида: {bid}")
                    continue
                
                try:
                    price = float(bid[0])              # Цена за 1 монету в USDT
                    quantity_contracts = float(bid[1]) # Количество КОНТРАКТОВ
                    
                    # Переводим контракты в монеты: количество_контрактов × размер_контракта
                    quantity_coins = quantity_contracts * contract_size
                    
                    # Объём в USDT = цена × количество монет
                    volume_usdt = price * quantity_coins
                    total_volume_usdt += volume_usdt
                    
                    print(f"   Бид {i+1}: {quantity_contracts} контрактов × {contract_size} = {quantity_coins} монет")
                    print(f"   Объём: {price} × {quantity_coins} = {volume_usdt:.2f} USDT")
                    print(f"   Накопленный объём: {total_volume_usdt:.2f} USDT")
                    
                except (ValueError, TypeError, IndexError) as e:
                    print(f"   ❌ Ошибка парсинга бида {i+1}: {e}")
                    continue
            
            final_volume = int(total_volume_usdt)
            print(f"✅ Итоговый объём 9 бидов для {pair}: {total_volume_usdt:.2f} USDT → {final_volume} USDT")
            
            return final_volume
            
        except Exception as e:
            print(f"❌ Критическая ошибка расчёта объёма для {pair}: {str(e)}")
            import traceback
            print(f"   Трассировка: {traceback.format_exc()}")
            return 0
    
    def format_price_by_scale(self, price: float, pair: str) -> str:
        """Форматирует цену согласно priceScale из API MEXC"""
        if pair in self.mexc_contracts_info:
            price_scale = self.mexc_contracts_info[pair]['priceScale']
            return f"{price:.{price_scale}f}"
        else:
            # Если нет информации о priceScale, используем как есть без форматирования
            return str(price)
    
    def generate_gate_signature(self, method: str, url: str, query_string: str = "", body: str = "") -> dict:
        """Генерирует подпись для Gate.io API"""
        timestamp = str(int(time.time()))
        sign_str = f"{method}\n{url}\n{query_string}\n{hashlib.sha512(body.encode()).hexdigest()}\n{timestamp}"
        signature = hmac.new(
            self.gate_secret.encode(),
            sign_str.encode(),
            hashlib.sha512
        ).hexdigest()
        
        return {
            'KEY': self.gate_api_key,
            'Timestamp': timestamp,
            'SIGN': signature
        }
    
    def get_mexc_contracts(self) -> Set[str]:
        """Получает список фьючерсных контрактов MEXC"""
        try:
            print("🔍 Получаем список контрактов MEXC...")
            
            url = 'https://futures.mexc.com/api/v1/contract/ticker'
            
            response = self.mexc_session.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get('success') and data.get('data'):
                    contracts = set()
                    for contract in data['data']:
                        symbol = contract.get('symbol')
                        if symbol and symbol.endswith('_USDT'):
                            # Убираем только суффикс _USDT для унификации
                            base_symbol = symbol.replace('_USDT', '')
                            contracts.add(base_symbol)
                    
                    print(f"✅ MEXC: найдено {len(contracts)} USDT контрактов")
                    return contracts
                else:
                    print(f"❌ MEXC API вернул неожиданную структуру: {data}")
                    return set()
            else:
                print(f"❌ Ошибка получения контрактов MEXC: HTTP {response.status_code}")
                return set()
                
        except Exception as e:
            print(f"❌ Ошибка получения контрактов MEXC: {str(e)}")
            return set()
    
    def get_gate_contracts(self) -> Set[str]:
        """Получает список фьючерсных контрактов Gate.io"""
        try:
            print("🔍 Получаем список контрактов Gate.io...")
            
            url = '/api/v4/futures/usdt/contracts'
            
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }
            
            response = self.gate_session.get(
                f"https://api.gateio.ws{url}",
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                
                if isinstance(data, list):
                    contracts = set()
                    for contract in data:
                        name = contract.get('name')
                        if name and name.endswith('_USDT'):
                            # Убираем суффикс _USDT для унификации
                            base_symbol = name.replace('_USDT', '')
                            contracts.add(base_symbol)
                    
                    print(f"✅ Gate.io: найдено {len(contracts)} USDT контрактов")
                    return contracts
                else:
                    print(f"❌ Gate.io API вернул неожиданную структуру: {type(data)}")
                    return set()
            else:
                print(f"❌ Ошибка получения контрактов Gate.io: HTTP {response.status_code}")
                return set()
                
        except Exception as e:
            print(f"❌ Ошибка получения контрактов Gate.io: {str(e)}")
            return set()
    
    def find_pairs_coverage(self, force_refresh: bool = False) -> Dict[str, List[str]]:
        """Находит покрытие пар по биржам"""
        print("🔍 DEBUG: Начало find_pairs_coverage")
        
        if not force_refresh:
            print("🔍 DEBUG: Проверяем возраст файла")
            file_age = self.get_file_age_hours()
            print(f"🔍 DEBUG: Возраст файла: {file_age}")
            
            if file_age is not None and file_age < 12:
                print("🔍 DEBUG: Загружаем из файла")
                coverage = self.load_coverage_from_file()
                print(f"🔍 DEBUG: Загружено {len(coverage) if coverage else 0} пар")
                if coverage:
                    # Загружаем информацию о контрактах для priceScale
                    self.get_mexc_contracts_info()
                    print("🔍 DEBUG: Проверяем цены...")
                    verified_coverage = self.verify_pairs_prices(coverage, False)
                    print(f"🔍 DEBUG: Проверено {len(verified_coverage)} пар")
                    return verified_coverage
        
        print("🔍 DEBUG: Получаем новые данные с бирж")
        
        # Загружаем информацию о контрактах MEXC для priceScale
        self.get_mexc_contracts_info()
        
        self.mexc_pairs = self.get_mexc_contracts()
        self.gate_pairs = self.get_gate_contracts()
        
        if not self.mexc_pairs and not self.gate_pairs:
            print("❌ Не удалось получить списки контрактов ни с одной биржи")
            return {}
        
        # Находим пары, которые есть на обеих биржах
        common_pairs = self.mexc_pairs & self.gate_pairs
        
        if not common_pairs:
            print("❌ Нет общих пар между MEXC и Gate.io")
            return {}
        
        # Формируем покрытие
        potential_coverage = {}
        for pair in common_pairs:
            potential_coverage[pair] = ['MEXC', 'Gate.io']
        
        # Статистика ДО проверки цен
        print(f"\n📊 Анализ покрытия пар (по спискам контрактов):")
        print(f"   MEXC: {len(self.mexc_pairs)} пар")
        print(f"   Gate.io: {len(self.gate_pairs)} пар")
        print(f"   Общих пар: {len(common_pairs)}")
        
        # Проверяем реальную доступность цен для всех общих пар
        print(f"\n🔍 Проверяем реальную доступность цен для {len(potential_coverage)} общих пар...")
        verified_coverage = self.verify_pairs_prices(potential_coverage, True)
        
        if not verified_coverage:
            print("❌ После проверки цен не осталось рабочих пар")
            return {}
        
        # Финальная статистика ПОСЛЕ проверки цен
        print(f"\n✅ Финальное покрытие (после проверки цен):")
        print(f"   Рабочих пар: {len(verified_coverage)}")
        print(f"   Отсеяно нерабочих: {len(potential_coverage) - len(verified_coverage)} пар")
        
        # Сохраняем ТОЛЬКО проверенное покрытие
        self.save_coverage_to_file(verified_coverage)
        self.save_verified_coverage(verified_coverage)
        
        return verified_coverage
    
    def save_coverage_to_file(self, coverage: Dict[str, List[str]]):
        """Сохраняет покрытие пар в файл"""
        try:
            with open(self.pairs_file, 'w', encoding='utf-8') as f:
                f.write(f"# Покрытие пар MEXC и Gate.io\n")
                f.write(f"# Создано: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# Всего пар для арбитража: {len(coverage)}\n")
                f.write(f"# Формат: символ биржа1,биржа2\n\n")
                
                for pair in sorted(coverage.keys()):
                    exchanges = ','.join(coverage[pair])
                    f.write(f"{pair} {exchanges}\n")
            
            print(f"💾 Покрытие пар сохранено в файл: {self.pairs_file}")
            
        except Exception as e:
            print(f"❌ Ошибка сохранения покрытия: {str(e)}")
    
    def load_coverage_from_file(self) -> Optional[Dict[str, List[str]]]:
        """Загружает покрытие пар из файла"""
        try:
            import os
            
            if not os.path.exists(self.pairs_file):
                return None
            
            with open(self.pairs_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            coverage = {}
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split(' ', 1)
                    if len(parts) == 2:
                        pair = parts[0]
                        exchanges = parts[1].split(',')
                        coverage[pair] = exchanges
            
            print(f"📄 Загружено покрытие для {len(coverage)} пар из файла")
            return coverage
            
        except Exception as e:
            print(f"❌ Ошибка загрузки покрытия: {str(e)}")
            return None
    
    def get_file_age_hours(self) -> Optional[float]:
        """Возвращает возраст файла в часах"""
        try:
            import os
            
            if not os.path.exists(self.pairs_file):
                return None
            
            file_time = os.path.getmtime(self.pairs_file)
            current_time = time.time()
            age_hours = (current_time - file_time) / 3600
            
            return age_hours
            
        except Exception:
            return None
    
    def verify_pairs_prices(self, coverage: Dict[str, List[str]], force_verify: bool = False) -> Dict[str, List[str]]:
        """Проверяет доступность цен для пар и фильтрует рабочие"""
        
        if not force_verify:
            verified_coverage = self.load_verified_coverage()
            if verified_coverage:
                if set(coverage.keys()).issubset(set(verified_coverage.keys())):
                    print("✅ Используем проверенные пары из кэша")
                    return verified_coverage
        
        print(f"\n🔍 Проверяем доступность цен для {len(coverage)} пар...")
        
        verified_coverage = {}
        
        # Разбиваем на батчи для лучшего контроля
        pairs_list = list(coverage.keys())
        batch_size = 10
        batches = [pairs_list[i:i + batch_size] for i in range(0, len(pairs_list), batch_size)]
        
        for batch_num, batch in enumerate(batches, 1):
            print(f"📦 Проверяем батч {batch_num}/{len(batches)} ({len(batch)} пар)...")
            batch_start_time = time.time()
            
            batch_results = {}
            
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_pair = {
                    executor.submit(self._verify_single_pair_with_timeout, pair, coverage[pair]): pair 
                    for pair in batch
                }
                
                completed = 0
                for future in as_completed(future_to_pair, timeout=60):
                    pair = future_to_pair[future]
                    completed += 1
                    
                    try:
                        working_exchanges = future.result(timeout=15)
                        if len(working_exchanges) >= 2:
                            batch_results[pair] = working_exchanges
                            print(f"✅ {pair}: {','.join(working_exchanges)}")
                        else:
                            print(f"❌ {pair}: недостаточно бирж ({','.join(working_exchanges) if working_exchanges else 'нет'})")
                    except Exception as e:
                        print(f"❌ {pair}: ошибка - {str(e)[:100]}")
            
            verified_coverage.update(batch_results)
            
            batch_time = time.time() - batch_start_time
            print(f"📊 Батч {batch_num}: ✅{len(batch_results)} за {batch_time:.1f}с")
            
            # Пауза между батчами
            if batch_num < len(batches):
                print("⏳ Пауза 3 секунды...")
                time.sleep(3)
        
        print(f"\n📊 Финальный результат проверки:")
        print(f"   ✅ Рабочих пар: {len(verified_coverage)}")
        print(f"   ❌ Нерабочих пар: {len(coverage) - len(verified_coverage)}")
        
        # Сохраняем результат
        self.save_verified_coverage(verified_coverage)
        
        return verified_coverage
    
    def _verify_single_pair_with_timeout(self, pair: str, exchanges: List[str]) -> List[str]:
        """Проверяет одну пару на указанных биржах с таймаутами"""
        working_exchanges = []
        
        for exchange in exchanges:
            try:
                if exchange == 'MEXC':
                    price = self._get_mexc_price_quick(pair)
                elif exchange == 'Gate.io':
                    price = self._get_gate_price_quick(pair)
                else:
                    continue
                
                if price is not None and price > 0:
                    working_exchanges.append(exchange)
                    
            except Exception:
                continue
        
        return working_exchanges
    
    def _get_mexc_price_quick(self, pair: str) -> Optional[float]:
        """Быстрое получение цены с MEXC (с коротким таймаутом)"""
        try:
            if not hasattr(self, '_mexc_all_tickers') or time.time() - getattr(self, '_mexc_last_update', 0) > 60:
                self._update_mexc_tickers_quick()
            
            symbol = f"{pair}_USDT"
            return self._mexc_all_tickers.get(symbol)
            
        except Exception:
            return None
    
    def _update_mexc_tickers_quick(self):
        """Быстрое обновление MEXC тикеров"""
        try:
            url = 'https://futures.mexc.com/api/v1/contract/ticker'
            response = self.mexc_session.get(url, timeout=8)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('success') and data.get('data'):
                    self._mexc_all_tickers = {}
                    for ticker in data['data']:
                        symbol = ticker.get('symbol')
                        last_price = ticker.get('lastPrice')
                        if symbol and last_price:
                            self._mexc_all_tickers[symbol] = float(last_price)
                    
                    self._mexc_last_update = time.time()
        except Exception:
            if not hasattr(self, '_mexc_all_tickers'):
                self._mexc_all_tickers = {}
    
    def _get_gate_price_quick(self, pair: str) -> Optional[float]:
        """Быстрое получение цены с Gate.io"""
        try:
            contract = f"{pair}_USDT"
            url = f'https://api.gateio.ws/api/v4/futures/usdt/contracts/{contract}'
            
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }
            
            response = self.gate_session.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                return float(data.get('last_price', 0))
            return None
            
        except Exception:
            return None
    
    def save_verified_coverage(self, coverage: Dict[str, List[str]]):
        """Сохраняет проверенное покрытие в файл"""
        try:
            with open(self.verified_file, 'w', encoding='utf-8') as f:
                f.write(f"# Проверенное покрытие пар\n")
                f.write(f"# Проверено: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# Рабочих пар: {len(coverage)}\n\n")
                
                for pair in sorted(coverage.keys()):
                    exchanges = ','.join(coverage[pair])
                    f.write(f"{pair} {exchanges}\n")
            
            print(f"💾 Проверенное покрытие сохранено в файл: {self.verified_file}")
            
        except Exception as e:
            print(f"❌ Ошибка сохранения проверенного покрытия: {str(e)}")
    
    def load_verified_coverage(self) -> Optional[Dict[str, List[str]]]:
        """Загружает проверенное покрытие из файла"""
        try:
            import os
            
            if not os.path.exists(self.verified_file):
                return None
            
            # Проверяем возраст файла
            file_age = (time.time() - os.path.getmtime(self.verified_file)) / 3600
            if file_age > 6:  # Если старше 6 часов
                return None
            
            with open(self.verified_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            coverage = {}
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split(' ', 1)
                    if len(parts) == 2:
                        pair = parts[0]
                        exchanges = parts[1].split(',')
                        coverage[pair] = exchanges
            
            print(f"📄 Загружено проверенное покрытие для {len(coverage)} пар (возраст: {file_age:.1f}ч)")
            return coverage
            
        except Exception:
            return None
    
    def _get_mexc_price(self, pair: str) -> Optional[float]:
        """Получает цену с MEXC"""
        try:
            if not hasattr(self, '_mexc_all_tickers') or time.time() - getattr(self, '_mexc_last_update', 0) > 10:
                self._update_mexc_tickers()
            
            symbol = f"{pair}_USDT"
            return self._mexc_all_tickers.get(symbol)
            
        except Exception:
            return None
    
    def _update_mexc_tickers(self):
        """Обновляет все тикеры MEXC"""
        try:
            url = 'https://futures.mexc.com/api/v1/contract/ticker'
            response = self.mexc_session.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('success') and data.get('data'):
                    self._mexc_all_tickers = {}
                    for ticker in data['data']:
                        symbol = ticker.get('symbol')
                        last_price = ticker.get('lastPrice')
                        if symbol and last_price:
                            self._mexc_all_tickers[symbol] = float(last_price)
                    
                    self._mexc_last_update = time.time()
                    print(f"✅ MEXC: загружено {len(self._mexc_all_tickers)} тикеров")
                else:
                    print(f"❌ MEXC API вернул неожиданную структуру")
            else:
                print(f"❌ MEXC API ошибка: HTTP {response.status_code}")
        except Exception as e:
            print(f"❌ Ошибка обновления MEXC тикеров: {str(e)}")
            if not hasattr(self, '_mexc_all_tickers'):
                self._mexc_all_tickers = {}
    
    def _get_gate_price(self, pair: str) -> Optional[float]:
        """Получает цену с Gate.io"""
        try:
            contract = f"{pair}_USDT"
            url = f'https://api.gateio.ws/api/v4/futures/usdt/contracts/{contract}'
            
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }
            
            response = self.gate_session.get(url, headers=headers, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                return float(data.get('last_price', 0))
            return None
            
        except Exception:
            return None
    
    def get_current_prices(self, coverage: Dict[str, List[str]]) -> Dict[str, Dict[str, float]]:
        """Получает текущие цены для всех пар на всех доступных биржах + проверяет новые пары"""
        # Сначала проверяем новые пары
        self.check_and_update_pairs_coverage()
        
        # Если есть обновленный кэш, используем его
        if hasattr(self, 'verified_coverage_cache') and self.verified_coverage_cache:
            coverage = self.verified_coverage_cache
        
        print(f"📄 Получаем цены для {len(coverage)} пар...")
        prices = {'MEXC': {}, 'Gate.io': {}}
        
        # Обновляем тикеры MEXC
        print("📊 Обновляем тикеры MEXC...")
        start_time = time.time()
        self._update_mexc_tickers()
        mexc_time = time.time() - start_time
        print(f"✅ MEXC тикеры обновлены за {mexc_time:.2f}с")
        
        # Собираем цены для MEXC из кэша
        for pair, exchanges in coverage.items():
            if 'MEXC' in exchanges and not self.spread_tracker.is_ticker_banned(pair):
                symbol = f"{pair}_USDT"
                if symbol in getattr(self, '_mexc_all_tickers', {}):
                    prices['MEXC'][pair] = self._mexc_all_tickers[symbol]
                    self.spread_tracker.reset_price_failure(pair)  # Сбрасываем счетчик неудач
                else:
                    # Увеличиваем счетчик неудач ТОЛЬКО если тикер не в активных спредах или не новый тикер
                    if (pair not in self.spread_tracker.get_active_spreads() or 
                        not self.spread_tracker.get_active_spreads().get(pair, {}).get('is_new_ticker')):
                        self.spread_tracker.increment_price_failure(pair)
        
        print(f"✅ MEXC: получено {len(prices['MEXC'])} цен")
        
        # Для Gate.io делаем параллельные запросы
        gate_pairs = [pair for pair, exchanges in coverage.items() 
                     if 'Gate.io' in exchanges and not self.spread_tracker.is_ticker_banned(pair)]
        
        if gate_pairs:
            print(f"📊 Получаем цены Gate.io для {len(gate_pairs)} пар...")
            start_time = time.time()
            
            with ThreadPoolExecutor(max_workers=20) as executor:
                gate_futures = {
                    executor.submit(self._get_gate_price, pair): pair 
                    for pair in gate_pairs
                }
                
                completed = 0
                for future in as_completed(gate_futures, timeout=25):
                    pair = gate_futures[future]
                    completed += 1
                    try:
                        price = future.result()
                        if price:
                            prices['Gate.io'][pair] = price
                            self.spread_tracker.reset_price_failure(pair)  # Сбрасываем счетчик неудач
                        else:
                            # Увеличиваем счетчик неудач ТОЛЬКО если тикер не в активных спредах или не новый тикер
                            if (pair not in self.spread_tracker.get_active_spreads() or 
                                not self.spread_tracker.get_active_spreads().get(pair, {}).get('is_new_ticker')):
                                self.spread_tracker.increment_price_failure(pair)
                    except Exception as e:
                        # Увеличиваем счетчик неудач при ошибке ТОЛЬКО если тикер не в активных спредах или не новый тикер
                        if (pair not in self.spread_tracker.get_active_spreads() or 
                            not self.spread_tracker.get_active_spreads().get(pair, {}).get('is_new_ticker')):
                            self.spread_tracker.increment_price_failure(pair)
                        print(f"⚠️ Ошибка получения цены {pair} с Gate.io: {str(e)[:100]}")
                    
                    # Показываем прогресс каждые 50 пар
                    if completed % 100 == 0:
                        print(f"   Обработано Gate.io: {completed}/{len(gate_pairs)}")
            
            gate_time = time.time() - start_time
            print(f"✅ Gate.io: получено {len(prices['Gate.io'])} цен за {gate_time:.2f}с")
        
        # Статистика
        total_time = mexc_time + (gate_time if gate_pairs else 0)
        print(f"📊 ИТОГО получено цен: MEXC: {len(prices['MEXC'])}, Gate.io: {len(prices['Gate.io'])}")
        print(f"⏱️ Общее время получения цен: {total_time:.2f}с")
        
        return prices
    
    def check_active_spreads_prices(self) -> Dict[str, Dict]:
        """Быстро проверяет цены только для активных спредов"""
        active_spreads = self.spread_tracker.get_active_spreads()
        if not active_spreads:
            return {}
        
        active_pairs = list(active_spreads.keys())
        print(f"⚡ Быстрая проверка цен для {len(active_pairs)} активных спредов")
        
        prices = {'MEXC': {}, 'Gate.io': {}}
        mexc_failures = {}
        gate_failures = {}
        
        # Получаем цены MEXC для активных пар
        for pair in active_pairs:
            mexc_price = self._get_mexc_price(pair)
            if mexc_price and mexc_price > 0:
                prices['MEXC'][pair] = mexc_price
            else:
                mexc_failures[pair] = mexc_failures.get(pair, 0) + 1
        
        # Получаем цены Gate.io для активных пар параллельно
        with ThreadPoolExecutor(max_workers=10) as executor:
            gate_futures = {
                executor.submit(self._get_gate_price, pair): pair 
                for pair in active_pairs
            }
            
            for future in as_completed(gate_futures, timeout=15):
                pair = gate_futures[future]
                try:
                    gate_price = future.result()
                    if gate_price and gate_price > 0:
                        prices['Gate.io'][pair] = gate_price
                    else:
                        gate_failures[pair] = gate_failures.get(pair, 0) + 1
                except Exception:
                    gate_failures[pair] = gate_failures.get(pair, 0) + 1
        
        # Проверяем на неудачи получения цен
        pairs_to_remove = []
        for pair in active_pairs:
            mexc_failed = pair not in prices['MEXC']
            gate_failed = pair not in prices['Gate.io']
            
            if mexc_failed or gate_failed:
                # Увеличиваем счетчик неудач для активных спредов
                if not hasattr(self.spread_tracker, 'active_spread_failures'):
                    self.spread_tracker.active_spread_failures = {}
                
                self.spread_tracker.active_spread_failures[pair] = self.spread_tracker.active_spread_failures.get(pair, 0) + 1
                
                print(f"⚠️ Не удалось получить цену для активного спреда {pair}: MEXC={'✗' if mexc_failed else '✓'}, Gate={'✗' if gate_failed else '✓'} (неудача #{self.spread_tracker.active_spread_failures[pair]})")
                
                # Если 3 неудачи подряд - удаляем спред
                if self.spread_tracker.active_spread_failures[pair] >= 3:
                    print(f"🗑️ Удаляем активный спред {pair} после 3 неудач получения цены")
                    pairs_to_remove.append(pair)
            else:
                # Сбрасываем счетчик неудач при успешном получении
                if hasattr(self.spread_tracker, 'active_spread_failures') and pair in self.spread_tracker.active_spread_failures:
                    del self.spread_tracker.active_spread_failures[pair]
        
        # Удаляем проблемные спреды
        for pair in pairs_to_remove:
            spread_info = self.spread_tracker.stop_tracking_spread(pair, 'price_unavailable')
            if spread_info and spread_info['message_id'] and self.telegram:
                self.telegram.delete_spread_message(spread_info['message_id'])
        
        print(f"✅ Быстрая проверка: MEXC={len(prices['MEXC'])}, Gate.io={len(prices['Gate.io'])}, удалено={len(pairs_to_remove)}")
        
        return prices 
    
    def update_active_spreads(self):
        """Обновляет только активные спреды каждые 3 секунды"""
        # Получаем цены только для активных спредов
        prices = self.check_active_spreads_prices()
        
        if not prices['MEXC'] and not prices['Gate.io']:
            return
        
        # Рассчитываем спреды только для активных пар
        active_spreads_data = []
        mexc_pairs = set(prices['MEXC'].keys())
        gate_pairs = set(prices['Gate.io'].keys())
        common_pairs = mexc_pairs & gate_pairs
        
        for pair in common_pairs:
            mexc_price = prices['MEXC'][pair]
            gate_price = prices['Gate.io'][pair]
            spread_percent = ((mexc_price - gate_price) / gate_price) * 100
            
            active_spreads_data.append({
                'pair': pair,
                'mexc_price': mexc_price,
                'gate_price': gate_price,
                'spread_percent': spread_percent,
                'spread_abs': abs(spread_percent)
            })
        
        # Обновляем сообщения для активных спредов
        active_spreads = self.spread_tracker.get_active_spreads()
        threshold = 3.0
        
        for spread_data in active_spreads_data:
            pair = spread_data['pair']
            
            if pair in active_spreads:
                # Обновляем время и максимальный спред
                self.spread_tracker.update_spread_time(pair, spread_data['spread_abs'])
                
                # Проверяем автобан по времени
                if self.spread_tracker.should_ban_ticker(pair):
                    print(f"🚫 Активный спред {pair} забанен за превышение 4 часов")
                    self.spread_tracker.add_banned_ticker(pair)
                    spread_info = self.spread_tracker.stop_tracking_spread(pair, 'timeout')
                    if spread_info and spread_info['message_id']:
                        self.telegram.delete_spread_message(spread_info['message_id'])
                    # НОВОЕ: Удаляем из дневной статистики
                    self.daily_tracker.remove_pair_from_daily(pair)
                    self.update_daily_spreads_message()
                    continue
                
                # Для новых тикеров проверяем 24-часовой период
                is_new_ticker = active_spreads[pair].get('is_new_ticker', False)
                if is_new_ticker:
                    if self.spread_tracker.is_new_ticker_expired(pair):
                        # 24 часа истекли, проверяем спред
                        if spread_data['spread_abs'] < threshold:
                            print(f"🗑️ Новый тикер {pair} удален: истёк 24-часовой период и спред < {threshold}%")
                            spread_info = self.spread_tracker.stop_tracking_spread(pair, 'natural_end')
                            if spread_info and spread_info['message_id']:
                                self.telegram.delete_spread_message(spread_info['message_id'])
                            continue
                        else:
                            # Убираем статус нового тикера, теперь это обычный спред
                            active_spreads[pair]['is_new_ticker'] = False
                            print(f"🔄 Тикер {pair} больше не новый, спред {spread_data['spread_abs']:.2f}% > {threshold}%")
                else:
                    # Для обычных спредов проверяем порог
                    if spread_data['spread_abs'] < threshold:
                        print(f"🗑️ Обычный спред {pair} удален: упал ниже {threshold}%")
                        spread_info = self.spread_tracker.stop_tracking_spread(pair, 'natural_end')
                        if spread_info and spread_info['message_id']:
                            self.telegram.delete_spread_message(spread_info['message_id'])
                            
                            # Добавляем в статистику
                            if spread_info['duration'] >= 60:
                                self.daily_tracker.add_completed_spread(pair, spread_info['max_spread'], spread_info['duration'])
                                self.update_daily_spreads_message()
                        continue
                
                # Обновляем сообщение каждые 3 секунды
                if self.spread_tracker.should_update_message(pair):
                    message = self.format_spread_message(spread_data)
                    if is_new_ticker:
                        message = f"🆕 НОВЫЙ ТИКЕР\n{message}"
                    
                    message_id = active_spreads[pair]['message_id']
                    success, should_ban = self.telegram.update_spread_message_with_buttons(message_id, message, pair)
                    
                    if should_ban:
                        print(f"🚫 Активный спред {pair} забанен - пользователь удалил сообщение")
                        self.spread_tracker.add_banned_ticker(pair)
                        self.spread_tracker.stop_tracking_spread(pair, 'user_deleted')
                        # НОВОЕ: Удаляем из дневной статистики
                        self.daily_tracker.remove_pair_from_daily(pair)
                        self.update_daily_spreads_message()
                    elif success:
                        duration = self.spread_tracker.get_spread_duration(pair)
                        duration_formatted = self.spread_tracker.format_duration(duration)
                        print(f"⚡ Обновлен активный спред: {pair} ({spread_data['spread_abs']:.2f}%) - {duration_formatted}")    

    
    def calculate_spreads(self, prices: Dict[str, Dict[str, float]]) -> List[Dict]:
        """Рассчитывает спреды между MEXC и Gate.io"""
        spreads = []
        
        # Получаем пары, для которых есть цены на обеих биржах
        mexc_pairs = set(prices['MEXC'].keys())
        gate_pairs = set(prices['Gate.io'].keys())
        common_pairs = mexc_pairs & gate_pairs
        
        for pair in common_pairs:
            mexc_price = prices['MEXC'][pair]
            gate_price = prices['Gate.io'][pair]
            
            # Спред как процентная разница (MEXC - Gate) / Gate * 100
            spread_percent = ((mexc_price - gate_price) / gate_price) * 100
            
            spreads.append({
                'pair': pair,
                'mexc_price': mexc_price,
                'gate_price': gate_price,
                'spread_percent': spread_percent,
                'spread_abs': abs(spread_percent)
            })
        
        return spreads
    
    def format_spread_message(self, spread: Dict) -> str:
        """Форматирует сообщение о спреде с объёмом бидов и лимитом позиции"""
        pair = spread['pair']
        spread_percent = spread['spread_percent']
        mexc_price = spread['mexc_price']
        gate_price = spread['gate_price']
        
        # Определяем направление
        if spread_percent > 0:
            mexc_direction = "SHORT"
            gate_direction = "LONG"
            reverse_icon = ""
        else:
            mexc_direction = "LONG" 
            gate_direction = "SHORT"
            reverse_icon = " 🔄"
        
        # Получаем актуальные данные
        duration_seconds = self.spread_tracker.get_spread_duration(pair)
        duration_formatted = self.spread_tracker.format_duration(duration_seconds)
        max_spread = self.spread_tracker.get_max_spread(pair)
        
        # Форматируем цены согласно priceScale
        mexc_price_formatted = self.format_price_by_scale(mexc_price, pair)
        gate_price_formatted = self.format_price_by_scale(gate_price, pair)
        
        # Получаем объём 9 бидов MEXC
        mexc_volume = self.calculate_mexc_5_bids_volume(pair)
        
        # Получаем максимальный лимит позиции
        position_limit = self.calculate_mexc_max_position_limit(pair)
        
        # Собираем сообщение
        message = f"<b>СПРЕД {pair} {abs(spread_percent):.2f}%{reverse_icon}</b>\n\n"
        message += f"MEXC: {mexc_price_formatted} ({mexc_direction})\n"
        message += f"GATE: {gate_price_formatted} ({gate_direction})\n\n"
        message += f"Время существования: {duration_formatted}\n"
        message += f"Максимальный спред: {max_spread:.2f}%\n"
        if position_limit > 0:
            message += f"Лимит позиции: {position_limit:,} USDT".replace(',', ' ')
        
        return message
    
    def manage_spread_messages(self, spreads: List[Dict], threshold: float):
        """Управляет сообщениями о спредах в Telegram с 5-секундным ожиданием"""
        if not self.telegram:
            return
        
        # Проверяем подтверждённые спреды (прошло 5 секунд)
        confirmed_spreads = self.spread_tracker.check_pending_spreads()
        for confirmed in confirmed_spreads:
            pair = confirmed['pair']
            
            # Проверяем, что спред всё ещё актуален
            current_spread_data = next((s for s in spreads if s['pair'] == pair and s['spread_abs'] >= threshold), None)
            
            if current_spread_data:
                # Публикуем пост
                message = self.format_spread_message(current_spread_data)
                if confirmed['is_new_ticker']:
                    message = f"🆕 НОВЫЙ ТИКЕР\n{message}"
                
                message_id = self.telegram.send_spread_message_with_buttons(message, pair)
                if message_id:
                    self.spread_tracker.start_tracking_spread(
                        pair, 
                        message_id, 
                        current_spread_data['spread_abs'],
                        is_new_ticker=confirmed['is_new_ticker']
                    )
                    print(f"📤 Опубликован подтверждённый спред: {pair} ({current_spread_data['spread_abs']:.2f}%)")
        
        # Получаем текущие активные спреды
        active_spreads = self.spread_tracker.get_active_spreads()
        pending_spreads = self.spread_tracker.pending_spreads
        current_pairs = {spread['pair']: spread for spread in spreads if spread['spread_abs'] >= threshold}
        
        # Обрабатываем новые спреды - добавляем в ожидание
        for pair, spread_data in current_pairs.items():
            if self.spread_tracker.is_ticker_banned(pair):
                continue
            
            # ИСПРАВЛЕНИЕ: Проверяем не только активные, но и недавно удалённые спреды
            if pair not in active_spreads and pair not in pending_spreads:
                # Новый спред - добавляем в ожидание на 5 секунд
                self.spread_tracker.add_pending_spread(pair, spread_data['spread_abs'])
        
        # Проверяем, не пропали ли ожидающие спреды
        for pair in list(pending_spreads.keys()):
            if pair not in current_pairs:
                self.spread_tracker.remove_pending_spread(pair)
        
        # Обновляем активные спреды
        for pair, spread_data in current_pairs.items():
            if pair in active_spreads:
                self.spread_tracker.update_spread_time(pair, spread_data['spread_abs'])
                
                if self.spread_tracker.should_ban_ticker(pair):
                    print(f"🚫 Тикер {pair} забанен за превышение 4 часов")
                    self.spread_tracker.add_banned_ticker(pair)
                    spread_info = self.spread_tracker.stop_tracking_spread(pair, 'timeout')
                    if spread_info and spread_info['message_id']:
                        self.telegram.delete_spread_message(spread_info['message_id'])
                    # НОВОЕ: Удаляем из дневной статистики
                    self.daily_tracker.remove_pair_from_daily(pair)
                    self.update_daily_spreads_message()
                    continue
                
                is_new_ticker = active_spreads[pair].get('is_new_ticker', False)
                if is_new_ticker:
                    if self.spread_tracker.is_new_ticker_expired(pair):
                        if spread_data['spread_abs'] < threshold:
                            print(f"🗑️ Новый тикер {pair} удален: истёк 24-часовой период и спред < {threshold}%")
                            spread_info = self.spread_tracker.stop_tracking_spread(pair, 'natural_end')
                            if spread_info and spread_info['message_id']:
                                self.telegram.delete_spread_message(spread_info['message_id'])
                            continue
                        else:
                            active_spreads[pair]['is_new_ticker'] = False
                            print(f"🔄 Тикер {pair} больше не новый")
                else:
                    if spread_data['spread_abs'] < threshold:
                        print(f"🗑️ Спред {pair} удален: упал ниже {threshold}%")
                        spread_info = self.spread_tracker.stop_tracking_spread(pair, 'natural_end')
                        if spread_info and spread_info['message_id']:
                            self.telegram.delete_spread_message(spread_info['message_id'])
                            
                            if spread_info['duration'] >= 60:
                                # Получаем лимит позиции
                                position_limit = self.calculate_mexc_max_position_limit(pair)
                                self.daily_tracker.add_completed_spread(pair, spread_info['max_spread'], spread_info['duration'], position_limit)
                                self.update_daily_spreads_message()
                        continue
                
                if self.spread_tracker.should_update_message(pair):
                    message = self.format_spread_message(spread_data)
                    if is_new_ticker:
                        message = f"🆕 НОВЫЙ ТИКЕР\n{message}"
                    
                    message_id = active_spreads[pair]['message_id']
                    success, should_ban = self.telegram.update_spread_message_with_buttons(message_id, message, pair)
                    
                    if should_ban:
                        print(f"🚫 Спред {pair} забанен - пользователь удалил сообщение")
                        self.spread_tracker.add_banned_ticker(pair)
                        self.spread_tracker.stop_tracking_spread(pair, 'user_deleted')
                        # НОВОЕ: Удаляем из дневной статистики
                        self.daily_tracker.remove_pair_from_daily(pair)
                        self.update_daily_spreads_message()
                        
    def remove_pair_from_daily(self, pair: str):
        """Удаляет все записи указанной пары из дневной статистики"""
        self.daily_tracker.remove_pair_from_daily(pair)    
    
    def check_and_update_pairs_coverage(self):
        """Проверяет и обновляет список пар каждые 10 секунд"""
        current_time = time.time()
        
        # Проверяем, нужно ли обновить список пар
        if current_time - self.last_pairs_update >= self.pairs_update_interval:
            try:
                print("🔄 Проверяем новые фьючерсные пары...")
                
                # Получаем актуальные списки пар с бирж
                new_mexc_pairs = self.get_mexc_contracts()
                new_gate_pairs = self.get_gate_contracts()
                
                if new_mexc_pairs and new_gate_pairs:
                    # Находим новые общие пары
                    new_common_pairs = new_mexc_pairs & new_gate_pairs
                    
                    # Проверяем есть ли новые пары
                    if hasattr(self, 'verified_coverage_cache') and self.verified_coverage_cache:
                        existing_pairs = set(self.verified_coverage_cache.keys())
                        truly_new_pairs = new_common_pairs - existing_pairs
                        
                        if truly_new_pairs:
                            print(f"🆕 Найдено {len(truly_new_pairs)} новых пар: {', '.join(list(truly_new_pairs)[:5])}")
                            
                            # Быстро проверяем только новые пары
                            new_coverage = {pair: ['MEXC', 'Gate.io'] for pair in truly_new_pairs}
                            verified_new = self.verify_pairs_prices(new_coverage, True)
                            
                            if verified_new:
                                # Добавляем проверенные новые пары в кэш
                                self.verified_coverage_cache.update(verified_new)
                                print(f"✅ Добавлено {len(verified_new)} новых рабочих пар в мониторинг")
                        else:
                            print("📊 Новых пар не найдено")
                    
                    self.last_pairs_update = current_time
                else:
                    print("⚠️ Не удалось получить обновленные списки пар")
                    
            except Exception as e:
                print(f"❌ Ошибка обновления списка пар: {str(e)}")
    
    def update_daily_spreads_message(self):
        """Обновляет закрепленное сообщение или создаёт новое при смене дня"""
        if not self.telegram:
            return
        
        # Проверяем смену дня
        if self.daily_tracker.should_reset_daily_spreads():
            self.daily_tracker.reset_daily_spreads()

            # Создаём новое закреплённое сообщение для нового дня
            message_text = self.daily_tracker.format_daily_spreads_message()
            message_id = self.telegram.create_pinned_message_with_retry(message_text, max_attempts=3)

            if message_id:
                self.daily_tracker.pinned_message_id = message_id
                self.daily_tracker.pin_created_today = True
                print(f"✅ Создан новый закреп {message_id} для {self.daily_tracker.current_date_for_pin}")
            else:
                print("❌ Не удалось создать новый закреп")
            return
        
        # Обновляем существующее сообщение
        if self.daily_tracker.pinned_message_id:
            message_text = self.daily_tracker.format_daily_spreads_message()
            success, _ = self.telegram.update_spread_message(
                self.daily_tracker.pinned_message_id,
                message_text
            )

            if not success:
                old_message_id = self.daily_tracker.pinned_message_id
                print(f"⚠️ Закрепленное сообщение {old_message_id} недоступно для обновления")
                print("🔄 Пытаемся найти существующий пост или создать новый...")

                # Сначала пытаемся найти существующий пост
                existing_pin = self.daily_tracker.try_find_existing_pin(self.telegram, max_attempts=3)

                if existing_pin:
                    print(f"✅ Найден существующий пост {existing_pin}, используем его")
                    self.daily_tracker.pinned_message_id = existing_pin
                else:
                    # Если не нашли, создаём новый
                    print("📌 Создаём новый закреплённый пост...")
                    new_message_id = self.telegram.create_pinned_message_with_retry(message_text, max_attempts=3)

                    if new_message_id:
                        self.daily_tracker.pinned_message_id = new_message_id
                        print(f"✅ Создан новый закреп {new_message_id}")
                    else:
                        print("❌ Не удалось создать новый закреп")
                        self.daily_tracker.pinned_message_id = None
    
    def print_spreads(self, spreads: List[Dict], threshold: float):
        """Выводит спреды в красивом формате с ценами"""
        filtered_spreads = [s for s in spreads if s['spread_abs'] >= threshold 
                          and not self.spread_tracker.is_ticker_banned(s['pair'])]
        
        if not filtered_spreads:
            print(f"📊 Нет спредов выше {threshold}% (забанено: {len(self.spread_tracker.banned_tickers)})")
            return
        
        print(f"\n🎯 Спреды выше {threshold}% (найдено {len(filtered_spreads)}):")
        print("=" * 110)
        print(f"{'Пара':<15} {'MEXC':<12} {'Gate.io':<12} {'Спред':<10} {'Направление':<35} {'Время':<15}")
        print("-" * 110)
        
        for spread in filtered_spreads:
            pair = spread['pair']
            mexc_price = spread['mexc_price']
            gate_price = spread['gate_price']
            spread_percent = spread['spread_percent']
            
            # Определяем направление арбитража
            if spread_percent > 0:
                direction = "SHORT MEXC → LONG Gate.io"
                color = "🔴"
            else:
                direction = "LONG MEXC → SHORT Gate.io"
                color = "🟢"
            
            # Получаем время существования спреда если он отслеживается
            duration = ""
            if pair in self.spread_tracker.get_active_spreads():
                duration_seconds = self.spread_tracker.get_spread_duration(pair)
                duration = self.spread_tracker.format_duration(duration_seconds)
            
            # Форматируем цены согласно priceScale
            mexc_formatted = self.format_price_by_scale(mexc_price, pair)
            gate_formatted = self.format_price_by_scale(gate_price, pair)
            
            print(f"{pair}_USDT{'':<5} {mexc_formatted:<12} {gate_formatted:<12} {spread_percent:>+7.2f}% {color} {direction:<33} {duration}")
    
    def monitor_spreads(self, threshold: float, update_interval: int = 3):
        """Основной цикл мониторинга с разделением на быстрые и медленные обновления"""
        print(f"\n🚀 Запуск мониторинга спредов для 2 бирж (порог: {threshold}%, интервал: {update_interval}с)")
        print(f"⚡ Активные спреды: каждые 3 секунды")
        print(f"🔍 Поиск новых спредов: каждые 15 секунд")
        print(f"🧹 Проверка дубликатов: каждые 30 секунд")
        print(f"⏱️ Проверка зависших: каждые 5 минут")
        print(f"📌 Контроль закрепов: каждые 15 минут")
        
        # Получаем начальное покрытие пар
        force_refresh = False
        file_age = self.get_file_age_hours()
        self.initialize_daily_spreads_message()
        
        if file_age is not None and file_age < 12:
            print(f"✅ Используем кэшированное покрытие пар (возраст: {file_age:.1f}ч)")
        else:
            print("📄 Обновляем покрытие пар...")
            force_refresh = True
        
        verified_coverage = self.find_pairs_coverage(force_refresh)
        if not verified_coverage:
            print("❌ Не найдено пар для мониторинга")
            return
        
        self.verified_coverage_cache = verified_coverage
        print(f"\n✅ Мониторинг настроен для {len(verified_coverage)} проверенных пар")
        
        # Создаём начальное закреплённое сообщение
        self.update_daily_spreads_message()
        
        try:
            cycle_count = 0
            last_full_scan = 0
            last_duplicate_check = 0
            last_stale_check = 0
            last_pins_check = 0
            full_scan_interval = 15       # каждые 15 секунд
            duplicate_check_interval = 30 # каждые 30 секунд
            stale_check_interval = 300    # каждые 5 минут (было 60 секунд)
            pins_check_interval = 900     # каждые 15 минут
            
            while True:
                cycle_count += 1
                current_time = time.time()
                
                print(f"\n{'='*60}")
                print(f"🔄 Цикл #{cycle_count} - {datetime.now().strftime('%H:%M:%S')}")
                
                # Быстрое обновление активных спредов каждые 3 секунды
                if self.spread_tracker.get_active_spreads():
                    print("⚡ Быстрое обновление активных спредов...")
                    self.update_active_spreads()
                
                # Проверка на дубликаты спредов
                if current_time - last_duplicate_check >= duplicate_check_interval:
                    print("🔍 Проверка на дубликаты спредов...")
                    self.check_duplicate_spreads()
                    last_duplicate_check = current_time
                
                # Проверка на зависшие сообщения каждые 5 минут
                if current_time - last_stale_check >= stale_check_interval:
                    print("⏱️ Проверка на зависшие сообщения...")
                    self.check_stale_messages(max_idle_time=120)  # 120 секунд без обновления = зависло
                    last_stale_check = current_time
                
                # Полное сканирование каждые 15 секунд
                if current_time - last_full_scan >= full_scan_interval:
                    print("🔍 Полное сканирование всех пар...")
                    
                    # Получаем цены для всех пар
                    prices = self.get_current_prices(verified_coverage)
                    all_spreads = self.calculate_spreads(prices)
                    
                    # Ищем новые спреды выше порога
                    self.manage_spread_messages(all_spreads, threshold)
                    self.print_spreads(all_spreads, threshold)
                    
                    last_full_scan = current_time
                    
                    # Обновляем закреплённое сообщение каждые 20 полных сканирований
                    if cycle_count % (20 * (full_scan_interval // update_interval)) == 0:
                        self.update_daily_spreads_message()
                
                # Проверка количества закрепов
                if current_time - last_pins_check >= pins_check_interval:
                    print("📌 Проверка количества закрепленных сообщений...")
                    self.manage_pinned_messages(max_pins=30)
                    last_pins_check = current_time
                
                time.sleep(update_interval)
                    
        except KeyboardInterrupt:
            print("\n\n🛑 Мониторинг остановлен пользователем")
            if self.telegram:
                moscow_tz = timezone(timedelta(hours=3))
                current_time = datetime.now(moscow_tz)
                stop_message = f"🛑 Мониторинг остановлен\n⏰ {current_time.strftime('%H:%M:%S')}"
                self.telegram.send_message(stop_message)
        except Exception as e:
            print(f"\n❌ Ошибка мониторинга: {str(e)}")
    
class TelegramBot:
    def __init__(self, token: str):
        if not TELEGRAM_AVAILABLE:
            raise ImportError("python-telegram-bot не установлен")
            
        self.token = token
        self.bot = Bot(token=token)
        self.monitor = None
        self.monitor_thread = None
        self.is_running = False
        self.monitoring_enabled = False
        
    def start_monitoring(self, chat_id: str):
        """Запускает мониторинг в отдельном потоке"""
        if self.is_running:
            return "⚠️ Мониторинг уже запущен!"
        
        try:
            # Создаем уведомитель для Telegram
            telegram_notifier = TelegramNotifier(self.token, chat_id)
            
            # Создаем монитор
            self.monitor = TwoExchangeMonitor(telegram_notifier)
            
            # Запускаем в отдельном потоке
            self.monitor_thread = threading.Thread(
                target=self._run_monitor, 
                daemon=True
            )
            self.monitor_thread.start()
            self.is_running = True
            
            return "✅ Система мониторинга инициализирована. Используйте /on для начала отслеживания спредов."
            
        except Exception as e:
            return f"❌ Ошибка запуска: {str(e)}"
    
    def enable_monitoring(self):
        """Включает отправку спредов"""
        if not self.is_running or not self.monitor:
            return "❌ Система мониторинга не запущена. Используйте /start"
        
        self.monitoring_enabled = True
        return "✅ Мониторинг спредов ВКЛЮЧЕН. Сообщения будут отправляться в канал."
    
    def disable_monitoring(self):
        """Выключает отправку спредов"""
        if not self.is_running or not self.monitor:
            return "❌ Система мониторинга не запущена"
        
        self.monitoring_enabled = False
        
        # Удаляем все активные сообщения о спредах
        if self.monitor:
            active_spreads = self.monitor.spread_tracker.get_active_spreads()
            for pair in list(active_spreads.keys()):
                spread_info = self.monitor.spread_tracker.stop_tracking_spread(pair, 'manual_stop')
                if spread_info and spread_info['message_id'] and self.monitor.telegram:
                    self.monitor.telegram.delete_spread_message(spread_info['message_id'])
        
        return "🛑 Мониторинг спредов ВЫКЛЮЧЕН. Все активные сообщения удалены."
    
    def _run_monitor(self):
        """Запускает мониторинг спредов с проверкой состояния"""
        try:
            threshold = 3.0
            interval = 3
            
            # Получаем покрытие пар один раз при запуске
            verified_coverage = self.monitor.find_pairs_coverage(False)
            if not verified_coverage:
                print("❌ Не найдено пар для мониторинга")
                return
            
            print(f"✅ Мониторинг настроен для {len(verified_coverage)} проверенных пар")
            
            cycle_count = 0
            while True:
                cycle_count += 1
                
                moscow_tz = timezone(timedelta(hours=3))
                current_time = datetime.now(moscow_tz)
                
                print(f"\n{'='*60}")
                print(f"🔄 Цикл мониторинга #{cycle_count} - {current_time.strftime('%H:%M:%S')}")
                print(f"📊 Состояние: {'АКТИВЕН' if self.monitoring_enabled else 'ПАУЗА'}")
                print(f"{'='*60}")
                
                if self.monitoring_enabled:
                    # Получаем текущие цены
                    prices = self.monitor.get_current_prices(verified_coverage)
                    
                    # Рассчитываем спреды
                    all_spreads = self.monitor.calculate_spreads(prices)
                    print(f"📊 Рассчитано спредов: {len(all_spreads)}")
                    
                    # Управляем сообщениями в Telegram
                    self.monitor.manage_spread_messages(all_spreads, threshold)
                    
                    # Обновляем закреплённое сообщение каждые 20 циклов (примерно раз в минуту)
                    if cycle_count % 20 == 0:
                        self.monitor.update_daily_spreads_message()
                    
                    # Выводим результаты в консоль
                    self.monitor.print_spreads(all_spreads, threshold)
                else:
                    print("⏸️ Мониторинг приостановлен. Используйте /on для возобновления.")
                
                # Ждём до следующего обновления
                time.sleep(interval)
                
        except Exception as e:
            print(f"❌ Ошибка в потоке мониторинга: {str(e)}")
            import traceback
            traceback.print_exc()
        finally:
            self.is_running = False
    
    def get_status(self):
        """Возвращает статус мониторинга"""
        if self.is_running and self.monitor:
            active_count = len(self.monitor.spread_tracker.get_active_spreads())
            banned_count = len(self.monitor.spread_tracker.banned_tickers)
            daily_count = len(self.monitor.daily_tracker.daily_spreads)  # Теперь это список
            status_text = "АКТИВЕН" if self.monitoring_enabled else "ПРИОСТАНОВЛЕН"
            
            return f"✅ Система: Запущена\n📊 Мониторинг: {status_text}\n🎯 Активных спредов: {active_count}\n🚫 Забанено: {banned_count}\n📋 Дневных спредов: {daily_count}"
        else:
            return "🛑 Система мониторинга не запущена"
    
    def get_banned_list(self):
        """Возвращает список забаненных тикеров"""
        if not self.monitor:
            return "❌ Система мониторинга не запущена"
        
        banned_tickers = self.monitor.spread_tracker.banned_tickers
        if not banned_tickers:
            return "✅ Черный список пуст"
        
        return f"🚫 Забанено тикеров: {len(banned_tickers)}\n" + "\n".join(sorted(banned_tickers))
    
    def ban_tickers(self, tickers: List[str]):
        """Банит указанные тикеры"""
        if not self.monitor:
            return "❌ Система мониторинга не запущена"
        
        banned_count = 0
        for ticker in tickers:
            ticker = ticker.upper()
            if not self.monitor.spread_tracker.is_ticker_banned(ticker):
                self.monitor.spread_tracker.add_banned_ticker(ticker)
                banned_count += 1
                
                # Удаляем активный спред если есть
                if ticker in self.monitor.spread_tracker.get_active_spreads():
                    spread_info = self.monitor.spread_tracker.stop_tracking_spread(ticker, 'manual_ban')
                    if spread_info and spread_info['message_id']:
                        self.monitor.telegram.delete_spread_message(spread_info['message_id'])
        
        return f"🚫 Забанено тикеров: {banned_count} из {len(tickers)}"
    
    def unban_tickers(self, tickers: List[str]):
        """Разбанивает указанные тикеры"""
        if not self.monitor:
            return "❌ Система мониторинга не запущена"
        
        unbanned_count = 0
        result_messages = []
        
        for ticker in tickers:
            ticker = ticker.upper().strip()
            if self.monitor.spread_tracker.remove_banned_ticker(ticker):
                unbanned_count += 1
                result_messages.append(f"✅ Тикер {ticker} разбанен")
            else:
                result_messages.append(f"ℹ️ Тикер {ticker} не был в черном списке")
        
        summary = f"📊 Разбанено тикеров: {unbanned_count} из {len(tickers)}"
        return summary + "\n" + "\n".join(result_messages)
    
def run_telegram_bot():
    """Запускает Telegram бота с командами управления"""
    try:
        from telegram.ext import Application, CommandHandler
    except ImportError:
        print("❌ Не установлена библиотека python-telegram-bot")
        print("   Установите: pip install python-telegram-bot")
        return
    
    BOT_TOKEN = "8213114507:AAFr0ut26G_o-iCtHkny7FQFh242sK7pO0I"
    CHANNEL_ID = "-1002913842685"
    
    try:
        print("🔧 Инициализация Telegram бота...")
        telegram_bot = TelegramBot(BOT_TOKEN)
        print("✅ Telegram бот создан успешно")
        
        async def start_command(update, context):
            """Инициализация системы мониторинга"""
            result = telegram_bot.start_monitoring(CHANNEL_ID)
            await update.message.reply_text(result)
        
        async def on_command(update, context):
            """Включение мониторинга спредов"""
            result = telegram_bot.enable_monitoring()
            await update.message.reply_text(result)
        
        async def off_command(update, context):
            """Выключение мониторинга спредов"""
            result = telegram_bot.disable_monitoring()
            await update.message.reply_text(result)
        
        async def status_command(update, context):
            """Статус мониторинга"""
            status = telegram_bot.get_status()
            await update.message.reply_text(status)
        
        async def list_command(update, context):
            """Список забаненных тикеров"""
            banned_list = telegram_bot.get_banned_list()
            await update.message.reply_text(banned_list)
        
        async def ban_command(update, context):
            """Бан тикеров"""
            if not context.args:
                await update.message.reply_text("❌ Укажите тикеры для бана\nПример: /ban BTC ETH ADA")
                return
            
            result = telegram_bot.ban_tickers(context.args)
            await update.message.reply_text(result)
        
        async def unban_command(update, context):
            """Разбан тикеров"""
            if not context.args:
                await update.message.reply_text("❌ Укажите тикеры для разбана\nПример: /unban BTC ETH ADA")
                return
            
            result = telegram_bot.unban_tickers(context.args)
            await update.message.reply_text(result)
        
        # Создаем Application
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Добавляем обработчики
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("on", on_command))
        application.add_handler(CommandHandler("off", off_command))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(CommandHandler("list", list_command))
        application.add_handler(CommandHandler("ban", ban_command))
        application.add_handler(CommandHandler("unban", unban_command))
        
        print("=" * 60)
        print("🤖 TELEGRAM БОТ v3.2 ЗАПУЩЕН!")
        print("=" * 60)
        print("📱 Команды:")
        print("   /start - инициализация системы")
        print("   /on - ВКЛЮЧИТЬ мониторинг спредов")
        print("   /off - ВЫКЛЮЧИТЬ мониторинг спредов")
        print("   /status - статус системы")
        print("   /list - черный список")
        print("   /ban BTC ETH - забанить тикеры")
        print("   /unban BTC ETH - разбанить тикеры")
        print("=" * 60)
        
        # Запускаем бота
        application.run_polling(allowed_updates=['message'])
        
    except Exception as e:
        print(f"❌ Критическая ошибка: {str(e)}")
    
def main():
    print("=" * 80)
    print("🎯 MEXC vs Gate.io с динамическими уведомлениями v3.2")  
    print("=" * 80)
    
    # Настройки - используем канал вместо личного чата
    BOT_TOKEN = "8012334983:AAEbzjXBx3XXS_nJZPptCz9dzXRuPQsz6a4"
    CHANNEL_ID = "-1003153711998"  # ID канала для отправки спредов
    
    telegram_notifier = None
    
    if BOT_TOKEN != "YOUR_BOT_TOKEN_HERE" and CHANNEL_ID != "YOUR_CHAT_ID_HERE":
        try:
            telegram_notifier = TelegramNotifier(BOT_TOKEN, CHANNEL_ID)
            print("🤖 Telegram уведомления включены")
            print(f"📢 Канал для спредов: {CHANNEL_ID}")
            
        except Exception as e:
            print(f"⚠️ Ошибка Telegram: {e}")
            telegram_notifier = None
    else:
        print("💻 Работаем без Telegram")
    
    # Создаем монитор с уведомителем
    monitor = TwoExchangeMonitor(telegram_notifier)
    
    # Настройки мониторинга
    threshold = 3.0
    interval = 3
    
    print(f"\n🚀 Настройки v3.2:")
    print(f"   • Пороговый спред: {threshold}%")
    print(f"   • Интервал: {interval}с")
    print(f"   • Динамические сообщения: ✅")
    print(f"   • Отслеживание максимального спреда: ✅")
    print(f"   • Автобан после 4ч: ✅")
    print(f"   • Объём 9 бидов MEXC: ✅")
    print(f"   • Закреплённое сообщение: ✅")
    print(f"   • Строгое использование priceScale: ✅")
    print(f"   • Московское время: ✅")
    print(f"   • Канал для уведомлений: {CHANNEL_ID}")
    print()
    
    # Запускаем мониторинг
    monitor.monitor_spreads(threshold, interval)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--bot":
        run_telegram_bot()
    else:
        main()
